#!/usr/bin/env python3
"""Standalone, stdlib-only external-worker launcher for Claude Code."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import re
import math
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "1.6.1"
CACHE_SCHEMAS = frozenset({"dex.provider_usage_cache.v1", "dex.provider_usage_cache.v2", "dex.provider_usage_cache.v3"})
RESULT_SCHEMA = "dex.external_worker_result.v1"
SELECTION_SCHEMA = "dex.worker_selection.v1"
PROVIDERS = ("codex", "agy")
FALLBACK = "CLAUDE_FALLBACK"
CLAUDE_NATIVE = "CLAUDE_NATIVE"
MAX_OUTPUT = 1_048_576
MAX_STATE = 16_384
WINDOWS = os.name == "nt"
REVIEW_SCOPE = ("Review the staged, unstaged, and untracked changes in this working tree "
                "and report findings only. ")
STILL_ACTIVE = 259


def cache_root(home: Path) -> Path:
    override = os.environ.get("DEX_USAGE_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg).expanduser() if xdg else home / ".cache") / "dex-usage"


def state_root(home: Path) -> Path:
    override = os.environ.get("DEX_WORKERS_STATE_DIR")
    return Path(override).expanduser() if override else home / ".cache/dex-workers"


def load_usage(home: Path) -> dict[str, Any] | None:
    try:
        path = cache_root(home) / "usage.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("schema_version") in CACHE_SCHEMAS else None
    except (OSError, ValueError, TypeError):
        return None


def worker_env() -> dict[str, str]:
    # Inherit credentials for the provider itself, but never serialize or print the environment.
    return dict(os.environ)


def windows_process_identity(pid: int) -> str | None:
    """Creation time of a live process, read straight from the Windows kernel.

    Win32 has no process groups to check, so the creation timestamp carries the
    whole anti-PID-reuse guarantee that `lstart` provides on POSIX.  Only the
    timestamp is read; no command line or environment data is touched.
    """
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return None
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                         ctypes.POINTER(wintypes.FILETIME),
                                         ctypes.POINTER(wintypes.FILETIME),
                                         ctypes.POINTER(wintypes.FILETIME))
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return f"{creation.dwHighDateTime:08x}{creation.dwLowDateTime:08x}"
    finally:
        kernel32.CloseHandle(handle)


def process_identity(pid: int) -> str | None:
    """Return a stable process-start token, or fail closed when unavailable.

    PID and process-group checks alone are vulnerable to PID reuse after a
    wrapper crash.  `lstart` is supplied by the local OS process table and is
    recorded only for the short-lived child process; no command line or
    environment data is read.
    """
    if WINDOWS:
        try:
            return windows_process_identity(pid)
        except (OSError, ValueError, AttributeError):
            return None
    try:
        ps = "/bin/ps" if Path("/bin/ps").is_file() else shutil.which("ps")
        if not ps:
            return None
        check = subprocess.run([ps, "-o", "lstart=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                               timeout=2, check=False, env={"PATH": "/usr/bin:/bin"})
        value = check.stdout.strip()
        return value if check.returncode == 0 and value and len(value) <= 128 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def owns_process(pid: int, identity: object) -> bool:
    """Confirm the recorded pid is still the process this wrapper launched."""
    if not isinstance(identity, str) or not identity or identity != process_identity(pid):
        return False
    if WINDOWS:
        # The creation timestamp already rules out PID reuse; Win32 has no
        # process group to corroborate it with.
        return True
    try:
        return os.getpgid(pid) == pid
    except OSError:
        return False


def signal_process_group(pid: int, force: bool = False) -> None:
    """Terminate a managed worker and its children on either platform."""
    if WINDOWS:
        # A detached console child cannot be asked to exit gracefully from here,
        # so the tree is always torn down forcefully.
        taskkill = shutil.which("taskkill")
        if not taskkill:
            raise OSError("taskkill unavailable")
        subprocess.run([taskkill, "/T", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
        return
    os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)


def stop_process_group(process: subprocess.Popen[str]) -> None:
    """Best-effort cleanup used only for a process launched by this wrapper."""
    if process.poll() is not None:
        return
    try:
        signal_process_group(process.pid)
        process.communicate(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            signal_process_group(process.pid, force=True)
            process.communicate(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def redact(text: str) -> str:
    """Best-effort defense in depth; provider output must not disclose common credentials."""
    patterns = (
        (r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+", r"\1[REDACTED]"),
        (r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[:=]\s*)[^\s,\"']+", r"\1[REDACTED]"),
        (r"\b(sk-[A-Za-z0-9_-]{12,})\b", "[REDACTED]"),
        (r"\b(AIza[A-Za-z0-9_-]{20,})\b", "[REDACTED]"),
        (r"\b(gh[pousr]_[A-Za-z0-9]{20,})\b", "[REDACTED]"),
        (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def probe(provider: str, timeout: float = 5.0) -> dict[str, Any]:
    executable = shutil.which("codex" if provider == "codex" else "agy")
    row: dict[str, Any] = {
        "provider": provider,
        "available": bool(executable),
        "authenticated": False,
        "enabled": False,
    }
    if not executable:
        row["reason"] = "executable_missing"
        return row
    row["executable"] = executable
    command = [executable, "login", "status"] if provider == "codex" else [executable, "--help"]
    try:
        # `text=True` alone decodes with the locale codec, which fails outright on
        # any non-ASCII byte under a non-UTF-8 console codepage such as cp949.
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                                timeout=timeout, env=worker_env(), check=False)
    except (OSError, subprocess.TimeoutExpired):
        row["reason"] = "probe_failed"
        return row
    text = (result.stdout + "\n" + result.stderr).lower()
    if provider == "codex":
        # Current Codex returns non-zero when logged out. Text is checked as a
        # second guard so a misleading successful wrapper is not enabled.
        logged_out = any(token in text for token in ("not logged in", "login required", "unauthenticated"))
        row["authenticated"] = result.returncode == 0 and not logged_out
    else:
        required = ("--print", "--print-timeout", "--sandbox")
        flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", text))
        if not all(flag in flags for flag in required):
            row["reason"] = "unsupported_cli"
            return row
        try:
            auth = subprocess.run([executable, "models"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout, env=worker_env(), check=False)
            text = (auth.stdout + "\n" + auth.stderr).lower()
            result = auth
        except (OSError, subprocess.TimeoutExpired):
            row["reason"] = "probe_failed"
            return row
        logged_out = any(token in text for token in (
            "not logged in", "unauthenticated", "login required", "please log in", "authentication required"
        ))
        row["authenticated"] = result.returncode == 0 and not logged_out
    row["enabled"] = row["authenticated"]
    row["reason"] = "ready" if row["enabled"] else "not_authenticated"
    return row


def remaining(provider: str, usage: dict[str, Any] | None) -> float | None:
    # Antigravity has no reliable quota contract. Never score it using another
    # product's quota; readiness is handled by probe().
    key = {"codex": "openai", "agy": "antigravity", "claude": "claude"}[provider]
    row = usage.get(key) if usage else None
    value = row.get("remaining_percent") if isinstance(row, dict) else None
    if provider == "agy" and isinstance(row, dict):
        windows = row.get("windows")
        values = []
        if isinstance(windows, dict):
            for name in ("five_hour", "one_week"):
                item = windows.get(name)
                percent = item.get("remaining_percent") if isinstance(item, dict) else None
                if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent) or not 0 <= percent <= 100:
                    values=[]
                    break
                values.append(float(percent))
        # v3 collectors provide both named windows. Keep accepting their
        # conservative top-level summary during an in-place dex-usage upgrade.
        if values:value=min(values)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return min(100.0, max(0.0, number)) if math.isfinite(number) else None


def deterministic_pick(names: list[str], routing_key: str) -> str:
    """Pick stably without pretending an unknown provider has numeric quota."""
    ordered = sorted(names)
    digest = hashlib.sha256(routing_key.encode("utf-8", errors="replace")).digest()
    return ordered[int.from_bytes(digest[:8], "big") % len(ordered)]


def choose(requested: str, probes: dict[str, dict[str, Any]], usage: dict[str, Any] | None,
           routing_key: str = "") -> tuple[str | None, str]:
    ready = [name for name in PROVIDERS if probes[name]["enabled"]]
    if requested != "auto":
        return (requested, "explicit") if requested in ready else (None, f"{requested}_unavailable")
    if not ready:
        return None, "no_supported_authenticated_provider"
    scored = [(remaining(name, usage), name) for name in ready]
    known = [(score, name) for score, name in scored if score is not None and score > 0]
    unknown = [name for score, name in scored if score is None]
    if known and unknown:
        score, best_known = max(known)
        name = deterministic_pick([best_known, *unknown], routing_key)
        reason = ("unknown_quota_rotation" if name in unknown
                  else f"dex_usage_advisory:{score:g}%:unknown_quota_rotation")
        return name, reason
    if known:
        score, name = max(known)
        return name, f"dex_usage_advisory:{score:g}%"
    if unknown:
        return unknown[0], "ready_provider_without_quota_data"
    return None, "all_ready_providers_quota_exhausted"


def choose_delegation(probes: dict[str, dict[str, Any]], usage: dict[str, Any] | None,
                      routing_key: str = "") -> tuple[str, str]:
    """Select one subagent route; native Claude is always eligible."""
    candidates = ["claude", *(name for name in PROVIDERS if probes[name]["enabled"])]
    scored = [(remaining(name, usage), name) for name in candidates]
    known = [(score, name) for score, name in scored if score is not None and score > 0]
    unknown_external = [name for score, name in scored if name != "claude" and score is None]
    if known and unknown_external:
        score, best_known = max(known)
        name = deterministic_pick([best_known, *unknown_external], routing_key)
        selection = CLAUDE_NATIVE if name == "claude" else name
        reason = ("unknown_quota_rotation" if name in unknown_external
                  else f"dex_usage_advisory:{score:g}%:unknown_quota_rotation")
        return selection, reason
    if known:
        score, name = max(known)
        return (CLAUDE_NATIVE if name == "claude" else name), f"dex_usage_advisory:{score:g}%"
    if unknown_external:
        return unknown_external[0], "ready_provider_without_quota_data"
    ready_external = [name for name in PROVIDERS if probes[name]["enabled"]]
    if ready_external:
        return CLAUDE_NATIVE, "all_ready_providers_quota_exhausted"
    return CLAUDE_NATIVE, "no_eligible_external_provider_or_quota"


def quota_eligible(provider: str, usage: dict[str, Any] | None) -> bool:
    """Unknown quota remains eligible; only a reliable known value below 5% is excluded."""
    value = remaining(provider, usage)
    return value is None or value >= 5.0


def choose_for_role(role: str, mode: str, requested: str,
                    probes: dict[str, dict[str, Any]], usage: dict[str, Any] | None,
                    routing_key: str = "") -> tuple[list[str], str]:
    ready = {
        CLAUDE_NATIVE: quota_eligible("claude", usage),
        "codex": probes["codex"]["enabled"] and quota_eligible("codex", usage),
        "agy": probes["agy"]["enabled"] and quota_eligible("agy", usage),
    }
    if requested != "auto":
        name = CLAUDE_NATIVE if requested == "claude" else requested
        return ([name], "explicit") if ready.get(name, False) else ([CLAUDE_NATIVE], f"{requested}_unavailable")
    if mode == "multi":
        selected = [name for name in (CLAUDE_NATIVE, "codex", "agy") if ready[name]]
        reason = "multi_perspective_all_eligible" if selected else "multi_perspective_no_eligible_provider"
        return selected, reason
    if role == "review":
        if ready["agy"]:
            return ["agy"], "review_prefers_antigravity"
        if ready["codex"]:
            return ["codex"], "review_antigravity_unavailable"
        return [CLAUDE_NATIVE], "review_external_unavailable"
    if role == "audit":
        eligible = {name: data for name, data in probes.items()}
        eligible["agy"] = {**eligible["agy"], "enabled": False}
        selection, reason = choose_delegation(eligible, usage, routing_key)
        return [selection], f"audit_primary:{reason}"
    # Implementation/build/fix tasks avoid Antigravity in normal auto routing.
    eligible = {name: data for name, data in probes.items()}
    eligible["agy"] = {**eligible["agy"], "enabled": False}
    selection, reason = choose_delegation(eligible, usage, routing_key)
    return [selection], reason


def result(status: str, **values: Any) -> dict[str, Any]:
    return {"schema_version": RESULT_SCHEMA, "status": status, **values}


def emit(value: dict[str, Any]) -> int:
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value["status"] in {"completed", FALLBACK, "cancelled"} else 2


def command_for(provider: str, action: str, cwd: Path, prompt: str, write_enabled: bool,
                executable: str | None = None) -> tuple[list[str], str | None]:
    """Return argv plus the text to feed the worker on stdin, if any.

    argv[0] is the absolute launcher path because Windows `CreateProcess` does
    not apply PATHEXT, so a bare name never resolves the `.cmd` shim that npm
    installs.  Because that shim is re-parsed by cmd.exe, the prompt is handed
    to Codex on stdin there instead: it keeps shell metacharacters out of the
    re-parse and sidesteps the 8191-character command-line limit.
    """
    if provider == "codex":
        launcher = executable or "codex"
        def carry(text: str) -> tuple[str, str | None]:
            """Windows reads the prompt from stdin via `-`; POSIX takes it on argv."""
            return ("-", text) if WINDOWS else (text, None)
        if action == "review":
            # `codex review` has no workspace-write mode and is deliberately
            # invoked from the requested directory rather than with `-C`.
            # codex >= 0.153.1 rejects `--uncommitted` alongside a PROMPT, and the
            # prompt is what carries the caller's bounded subtask, so the scope is
            # stated in the instructions instead.  A prompt-only review still reads
            # staged, unstaged, and untracked changes.
            argument, stdin_prompt = carry(REVIEW_SCOPE + prompt)
            return [launcher, "review", argument], stdin_prompt
        argument, stdin_prompt = carry(prompt)
        sandbox = "workspace-write" if write_enabled else "read-only"
        return ([launcher, "exec", "--ephemeral", "--color", "never", "-C", str(cwd),
                 "--sandbox", sandbox, argument], stdin_prompt)
    # agy's sandbox is a boolean, so plan mode is the additional hard guard
    # for the default read-only route.  Workspace edits require a separate,
    # user-directed opt-in to accept-edits mode.
    mode = "accept-edits" if write_enabled else "plan"
    guard = "You are in read-only mode: do not create, edit, delete, or move files. "
    if write_enabled:
        guard = "You may modify only files needed for this explicitly authorized task. "
    if action == "review":
        guard += "Review the current uncommitted changes and report findings only. "
    # agy takes its prompt on argv; it exposes no documented stdin form to move
    # the text out of the Windows cmd.exe re-parse the way Codex's `-` does.
    return [executable or "agy", "--mode", mode, "--print-timeout", "24h",
            "--sandbox", "--print", guard + prompt], None


def run_worker(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        return emit(result("error", error="invalid_working_directory"))
    probes = {name: probe(name, args.probe_timeout) for name in PROVIDERS}
    usage = load_usage(args.home)
    provider, route_reason = choose(args.provider, probes, usage, args.prompt)
    if provider is None:
        return emit(result(FALLBACK, action=args.action, reason=route_reason, next_action="continue_in_claude",
                           message="No eligible external worker; Claude should continue locally."))
    write_enabled = bool(getattr(args, "write", False))
    argv, stdin_prompt = command_for(provider, args.action, cwd, args.prompt, write_enabled,
                                     probes[provider].get("executable"))
    state = state_root(args.home)
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        return emit(result("error", error="unsafe_state_directory"))
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    try: state.chmod(0o700)
    except OSError: pass
    run_id = f"{int(time.time())}-{os.getpid()}"
    record = state / f"{run_id}.json"
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    process: subprocess.Popen[str] | None = None
    try:
        # Win32 has no sessions; a new process group is the closest isolation
        # primitive and is what `taskkill /T` tears down later.
        detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
                  else {"start_new_session": True})
        process = subprocess.Popen(argv, cwd=cwd,
                                   stdin=subprocess.PIPE if stdin_prompt is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   encoding="utf-8", errors="replace",
                                   env=worker_env(), **detach)
        identity = process_identity(process.pid)
        if identity is None:
            raise OSError("unable to identify managed worker process")
        fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"run_id": run_id, "pid": process.pid, "provider": provider,
                       "action": args.action, "started_at": started, "process_identity": identity}, stream)
        try:
            stdout, stderr = process.communicate(stdin_prompt, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            signal_process_group(process.pid)
            try: stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                signal_process_group(process.pid, force=True); stdout, stderr = process.communicate()
            return emit(result(FALLBACK, provider=provider, run_id=run_id, reason="timeout", next_action="continue_in_claude",
                               message="External worker timed out; Claude should continue locally."))
    except OSError as exc:
        if process is not None:
            stop_process_group(process)
        return emit(result(FALLBACK, provider=provider, reason="launch_failed", next_action="continue_in_claude",
                           message="External worker could not start; Claude should continue locally.", error=type(exc).__name__))
    finally:
        try: record.unlink()
        except OSError: pass
    if process.returncode != 0:
        return emit(result(FALLBACK, provider=provider, run_id=run_id,
                           reason="worker_failed", exit_code=process.returncode,
                           next_action="continue_in_claude", stderr=redact(stderr[-4000:]), message="External worker failed; Claude should continue locally."))
    if len(stdout.encode(errors="replace")) > MAX_OUTPUT:
        return emit(result(FALLBACK, provider=provider, run_id=run_id, reason="output_too_large",
                           next_action="continue_in_claude", message="External worker output exceeded the safe capture limit; Claude should continue locally."))
    return emit(result("completed", provider=provider, run_id=run_id, route_reason=route_reason,
                       read_only=not write_enabled, write_enabled=write_enabled,
                       output=redact(stdout), stderr=redact(stderr[-4000:]) or None))


def status(args: argparse.Namespace) -> int:
    usage = load_usage(args.home)
    probes = {name: probe(name, args.probe_timeout) for name in PROVIDERS}
    ready, reason = choose_delegation(probes, usage, "status")
    active = []
    root = state_root(args.home)
    state_status = "ready"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        state_status = "unsafe"
    for path in root.glob("*.json") if root.exists() and state_status == "ready" else []:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_STATE: continue
            row = json.loads(path.read_text(encoding="utf-8")); pid = int(row["pid"])
            if owns_process(pid, row.get("process_identity")):
                active.append(row)
        except (OSError, ValueError): pass
    return emit(result("completed", version=VERSION, providers=probes,
                       dex_usage_cache="available" if usage else "missing_or_invalid",
                       advisory_route=ready, route_reason=reason,
                       state_directory=state_status, active=active))


def select_worker(args: argparse.Namespace) -> int:
    """Select a delegation target without launching it or intercepting a prompt."""
    usage = load_usage(args.home)
    probes = {name: probe(name, args.probe_timeout) for name in PROVIDERS}
    selections, reason = choose_for_role(args.role, args.mode, args.provider, probes, usage, args.task)
    print(json.dumps({
        "schema_version": SELECTION_SCHEMA,
        "selection": selections[0] if selections else None,
        "selections": selections,
        "role": args.role,
        "mode": args.mode,
        "route_reason": reason,
    }, ensure_ascii=False, indent=2))
    return 0


def cancel(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[0-9]{1,20}-[0-9]{1,20}", args.run_id):
        return emit(result(FALLBACK, reason="invalid_run_id", run_id=args.run_id, next_action="continue_in_claude"))
    root = state_root(args.home)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        return emit(result("error", error="unsafe_state_directory", run_id=args.run_id))
    path = root / f"{args.run_id}.json"
    try:
        if path.is_symlink() or path.stat().st_size > MAX_STATE: raise ValueError("unsafe state record")
        data = json.loads(path.read_text(encoding="utf-8")); pid = int(data["pid"])
        owned = data.get("run_id") == args.run_id and owns_process(pid, data.get("process_identity"))
        if not owned:
            path.unlink(missing_ok=True)
            return emit(result(FALLBACK, reason="stale_or_unowned_run", run_id=args.run_id,
                               next_action="continue_in_claude"))
        signal_process_group(pid); path.unlink(missing_ok=True)
        return emit(result("cancelled", run_id=args.run_id))
    except FileNotFoundError:
        return emit(result(FALLBACK, reason="run_not_active", run_id=args.run_id, next_action="continue_in_claude",
                           message="Managed run is not active; Claude should continue locally."))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return emit(result("error", error=type(exc).__name__, run_id=args.run_id))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="DEX external worker launcher")
    p.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    p.add_argument("--probe-timeout", type=float, default=5.0, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "review"):
        q = sub.add_parser(name); q.set_defaults(func=run_worker, action=name)
        q.add_argument("prompt"); q.add_argument("--provider", choices=("auto", *PROVIDERS), default="auto")
        q.add_argument("--cwd", default="."); q.add_argument("--timeout", type=float, default=900)
        if name == "run":
            q.add_argument("--write", action="store_true",
                           help="allow workspace edits; default execution is read-only")
    for name in ("status", "doctor"):
        q = sub.add_parser(name); q.set_defaults(func=status)
    q = sub.add_parser("select", help="select a delegation target without launching it")
    q.set_defaults(func=select_worker)
    q.add_argument("--task", default="", help="bounded subtask used only as a deterministic routing key")
    q.add_argument("--role", choices=("review", "audit", "implementation"), default="implementation")
    q.add_argument("--mode", choices=("single", "multi"), default="single")
    q.add_argument("--provider", choices=("auto", "claude", *PROVIDERS), default="auto")
    q = sub.add_parser("cancel"); q.add_argument("run_id"); q.set_defaults(func=cancel)
    return p


def main() -> int:
    # Results are JSON on stdout and may carry any character the worker emitted;
    # the console codepage (cp949, cp1252, ...) must not decide what can be printed.
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError): pass
    args = parser().parse_args()
    if getattr(args, "timeout", 1) <= 0 or args.probe_timeout <= 0:
        return emit(result("error", error="timeout_must_be_positive"))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
