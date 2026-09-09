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

VERSION = "1.7.1"
CACHE_SCHEMAS = frozenset({"dex.provider_usage_cache.v1", "dex.provider_usage_cache.v2", "dex.provider_usage_cache.v3"})
RESULT_SCHEMA = "dex.external_worker_result.v1"
SELECTION_SCHEMA = "dex.worker_selection.v1"
PROVIDERS = ("codex", "agy")
ROLES = ("review", "audit", "implementation")
FALLBACK = "CLAUDE_FALLBACK"
CLAUDE_NATIVE = "CLAUDE_NATIVE"
MAX_OUTPUT = 1_048_576
MAX_STATE = 16_384
MAX_RESULT_FILE = 4 * MAX_OUTPUT
WINDOWS = os.name == "nt"
REVIEW_SCOPE = ("Review the staged, unstaged, and untracked changes in this working tree "
                "and report findings only. ")
STILL_ACTIVE = 259

# Reasoning effort. Claude Code offers `max` and `ultra` on top of the levels the
# external CLIs have, so a requested level is clamped to what the provider knows.
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
AGY_EFFORTS = ("low", "medium", "high")
ROLE_EFFORT: dict[str, str | None] = {"review": "low", "audit": "high", "implementation": None}
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:\[\]-]{0,80}$")
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
RUN_ID_PATTERN = re.compile(r"[0-9]{1,20}-[0-9]{1,20}")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FINDINGS_SCHEMA = PLUGIN_ROOT / "schemas/findings.schema.json"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_CONTEXT_FILE = 32 * 1024
MAX_CONTEXT_TOTAL = 128 * 1024
MAX_ACTIVE_DEFAULT = 5

# When set, emit() writes the final JSON here atomically instead of stdout. Used
# by background runs so `wait`/`result` can pick the outcome up later.
RESULT_FILE: Path | None = None


def cache_root(home: Path) -> Path:
    override = os.environ.get("DEX_USAGE_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg).expanduser() if xdg else home / ".cache") / "dex-usage"


def state_root(home: Path) -> Path:
    override = os.environ.get("DEX_WORKERS_STATE_DIR")
    return Path(override).expanduser() if override else home / ".cache/dex-workers"


def max_active() -> int:
    try:
        value = int(os.environ.get("DEX_WORKERS_MAX_ACTIVE", MAX_ACTIVE_DEFAULT))
    except ValueError:
        return MAX_ACTIVE_DEFAULT
    return max(0, min(value, 32))


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


def redact_value(value: Any) -> Any:
    """Apply redact() to every string inside a provider-derived JSON document."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_value(item) for key, item in value.items()}
    return value


AGY_SETTINGS = Path(".gemini/antigravity-cli/settings.json")
AGY_HARNESS_COMMANDS = ("git", "diff", "grep", "cat", "ls", "find", "sed", "awk")


def agy_missing_permissions(home: Path) -> list[str] | None:
    """Harness commands agy would auto-deny headlessly, or None when unreadable.

    `setup.py setup-agy` adds them; doctor reports them so an empty review is
    explained before it happens.
    """
    path = home / AGY_SETTINGS
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    allow = data.get("permissions", {}).get("allow") if isinstance(data, dict) else None
    if not isinstance(allow, list):
        return list(AGY_HARNESS_COMMANDS)
    present = {rule for rule in allow if isinstance(rule, str)}
    return [name for name in AGY_HARNESS_COMMANDS if f"command({name})" not in present]


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
        # Optional flags are recorded so the launcher only uses what this agy
        # build actually understands.
        row["capabilities"] = {
            "json_output": "--output-format" in flags,
            "json_schema": "--json-schema" in flags,
            "conversation": "--conversation" in flags,
            "model": "--model" in flags,
            "effort": "--effort" in flags,
        }
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
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if RESULT_FILE is not None:
        # Written whole and renamed into place so a concurrent `wait` never
        # observes a partial document.
        tmp = RESULT_FILE.with_name(RESULT_FILE.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, RESULT_FILE)
    else:
        print(text)
    return 0 if value["status"] in {"completed", "started", FALLBACK, "cancelled"} else 2


# --- model, effort and prompt shaping -------------------------------------------

def parse_model(raw: str | None) -> tuple[str | None, str | None]:
    """Split a model spelling into (model, pinned effort).

    Accepts the spellings Claude Code users already type: `gpt-5.5`,
    `gpt-5.5-high`, `gpt-5.5:high`, `codex/gpt-5.5-high`, `gpt-5.5[1m]`.
    An effort suffix pins the effort and wins over `--effort`.
    """
    if raw is None or not raw.strip():
        return None, None
    name = raw.strip()
    if not MODEL_PATTERN.match(name):
        raise ValueError("invalid_model_name")
    name = re.sub(r"\[[0-9]+[kKmM]?\]$", "", name)
    if "/" in name:
        name = name.rsplit("/", 1)[1]
    pinned = None
    for separator in (":", "-"):
        head, found, tail = name.rpartition(separator)
        if found and head and tail in EFFORTS:
            name, pinned = head, tail
            break
    if not name or not MODEL_PATTERN.match(name):
        raise ValueError("invalid_model_name")
    return name, pinned


def clamp_effort(provider: str, effort: str | None) -> str | None:
    """Map a requested effort onto the levels the provider actually has."""
    if effort is None:
        return None
    levels = CODEX_EFFORTS if provider == "codex" else AGY_EFFORTS
    if effort in levels:
        return effort
    if effort in ("max", "ultra", "xhigh"):
        return levels[-1]
    if effort == "minimal":
        return levels[0]
    return None


def resolve_model_effort(provider: str, model: str | None, effort: str | None,
                         role: str | None) -> tuple[str | None, str | None]:
    """Combine the explicit flags, the model suffix and the role default."""
    name, pinned = parse_model(model)
    requested = pinned or effort or (ROLE_EFFORT.get(role) if role else None)
    return name, clamp_effort(provider, requested)


def validate_images(paths: list[str]) -> list[Path]:
    images = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ValueError(f"image_not_found:{raw}")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError(f"image_too_large:{raw}")
        images.append(path.resolve())
    return images


def git_snapshot(cwd: Path) -> str:
    """A bounded view of the working tree so the worker knows what changed."""
    git = shutil.which("git")
    if not git:
        return ""
    def run(*argv: str, limit: int) -> str:
        try:
            done = subprocess.run([git, "-C", str(cwd), *argv], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  encoding="utf-8", errors="replace", timeout=5, check=False,
                                  env=worker_env())
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if done.returncode != 0:
            return ""
        lines = done.stdout.rstrip().splitlines()
        if len(lines) > limit:
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    if run("rev-parse", "--is-inside-work-tree", limit=1) != "true":
        return ""
    parts = []
    branch = run("rev-parse", "--abbrev-ref", "HEAD", limit=1)
    if branch:
        parts.append(f"Branch: {branch}")
    status = run("status", "--short", limit=60)
    parts.append("Uncommitted changes (git status --short):\n" + (status or "(clean)"))
    stat = run("diff", "--stat", "HEAD", limit=40)
    if stat:
        parts.append("Diff summary against HEAD:\n" + stat)
    return "\n\n".join(parts)


def read_context_files(paths: list[str]) -> list[tuple[str, str]]:
    total = 0
    blocks = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ValueError(f"context_not_found:{raw}")
        size = path.stat().st_size
        if size > MAX_CONTEXT_FILE or total + size > MAX_CONTEXT_TOTAL:
            raise ValueError(f"context_too_large:{raw}")
        total += size
        blocks.append((str(path), path.read_text(encoding="utf-8", errors="replace")))
    return blocks


DEFAULT_DELIVERABLE = {
    "review": ("A findings list. Each finding names the file and line, a severity (high, medium, low), "
               "a one-sentence defect statement and a concrete failure scenario. Say explicitly when "
               "nothing was found."),
    "audit": ("A verdict (pass, fail or blocked) with evidence: the commands you ran, the files you read, "
              "and every defect with file:line and a reproduction."),
    "implementation": ("The change applied in the workspace (only when write access is granted), a list of "
                       "files touched, and the verification you ran with its result."),
}
DEFAULT_DONE_WHEN = {
    "review": "Every changed file has been read and each finding cites file:line.",
    "audit": "Each claim in the verdict is backed by something you ran or read, not inferred.",
    "implementation": "The change builds or runs, the relevant tests pass, and nothing outside the task scope was touched.",
}


def build_brief(prompt: str, cwd: Path, role: str | None, write_enabled: bool,
                deliverable: str | None, done_when: str | None,
                context: list[tuple[str, str]], images: list[Path]) -> str:
    """A fixed handoff template so every provider gets the same framing."""
    role_name = role or "implementation"
    lines = [
        "# Delegated subtask brief (dex-workers)",
        "",
        f"- Workspace: {cwd}",
        f"- Access: {'workspace-write (only the files this task needs)' if write_enabled else 'read-only'}",
        f"- Role: {role_name}",
        f"- Deliverable: {deliverable or DEFAULT_DELIVERABLE[role_name]}",
        f"- Done when: {done_when or DEFAULT_DONE_WHEN[role_name]}",
        "- Rules: do not delegate further; do not touch files outside the workspace; state blockers "
        "explicitly instead of guessing; cite file:line for every claim about code.",
    ]
    if images:
        lines.append("- Attached images: " + ", ".join(str(path) for path in images))
    lines += ["", "## Task", "", prompt.strip()]
    snapshot = git_snapshot(cwd)
    if snapshot:
        lines += ["", "## Workspace state", "", snapshot]
    if context:
        lines += ["", "## Context files"]
        for path, text in context:
            lines += ["", f"--- {path} ---", text.rstrip()]
    return "\n".join(lines) + "\n"


# --- command construction ---------------------------------------------------------

class RunOptions:
    """Everything beyond the prompt that shapes one worker invocation."""

    def __init__(self, model: str | None = None, effort: str | None = None, resume: str | None = None,
                 ephemeral: bool = False, schema: Path | None = None, images: list[Path] | None = None,
                 last_message: Path | None = None, capabilities: dict[str, bool] | None = None) -> None:
        self.model = model
        self.effort = effort
        self.resume = resume
        self.ephemeral = ephemeral
        self.schema = schema
        self.images = images or []
        self.last_message = last_message
        self.capabilities = capabilities or {}


def command_for(provider: str, action: str, cwd: Path, prompt: str, write_enabled: bool,
                executable: str | None = None, options: RunOptions | None = None) -> tuple[list[str], str | None]:
    """Return argv plus the text to feed the worker on stdin, if any.

    argv[0] is the absolute launcher path because Windows `CreateProcess` does
    not apply PATHEXT, so a bare name never resolves the `.cmd` shim that npm
    installs.  Because that shim is re-parsed by cmd.exe, the prompt is handed
    to Codex on stdin there instead: it keeps shell metacharacters out of the
    re-parse and sidesteps the 8191-character command-line limit.
    """
    options = options or RunOptions()
    if provider == "codex":
        launcher = executable or "codex"
        def carry(text: str) -> tuple[str, str | None]:
            """Windows reads the prompt from stdin via `-`; POSIX takes it on argv."""
            return ("-", text) if WINDOWS else (text, None)
        settings: list[str] = []
        if options.effort:
            settings += ["-c", f"model_reasoning_effort={options.effort}"]
        if action == "review":
            # `codex review` has no workspace-write mode and is deliberately
            # invoked from the requested directory rather than with `-C`.
            # codex >= 0.153.1 rejects `--uncommitted` alongside a PROMPT, and the
            # prompt is what carries the caller's bounded subtask, so the scope is
            # stated in the instructions instead.  A prompt-only review still reads
            # staged, unstaged, and untracked changes.  `review` has no `-m`, so the
            # model goes through `-c`, and no `--output-schema`, so a schema is
            # requested in the instructions and parsed from the text afterwards.
            if options.model:
                settings += ["-c", "model=" + json.dumps(options.model)]
            text = REVIEW_SCOPE + prompt
            if options.schema is not None:
                text += ("\n\nReply with exactly one JSON document matching this schema and nothing else:\n"
                         + options.schema.read_text(encoding="utf-8"))
            argument, stdin_prompt = carry(text)
            return [launcher, "review", *settings, argument], stdin_prompt
        sandbox = "workspace-write" if write_enabled else "read-only"
        if options.resume:
            # `exec resume` takes neither `-C` nor `--sandbox`; the working
            # directory comes from the process and the sandbox from config.
            argv = [launcher, "exec", "resume", options.resume, "--json",
                    "-c", "sandbox_mode=" + json.dumps(sandbox), *settings]
        else:
            argv = [launcher, "exec", "--json", "--color", "never", "-C", str(cwd),
                    "--sandbox", sandbox, *settings]
            if options.ephemeral:
                argv.append("--ephemeral")
        if options.model:
            argv += ["-m", options.model]
        for image in options.images:
            argv += ["-i", str(image)]
        if options.schema is not None:
            argv += ["--output-schema", str(options.schema)]
        if options.last_message is not None:
            argv += ["-o", str(options.last_message)]
        argument, stdin_prompt = carry(prompt)
        return [*argv, argument], stdin_prompt
    # agy's sandbox is a boolean, so plan mode is the additional hard guard
    # for the default read-only route.  Workspace edits require a separate,
    # user-directed opt-in to accept-edits mode.
    mode = "accept-edits" if write_enabled else "plan"
    guard = "You are in read-only mode: do not create, edit, delete, or move files. "
    if write_enabled:
        guard = "You may modify only files needed for this explicitly authorized task. "
    if action == "review":
        guard += "Review the current uncommitted changes and report findings only. "
    capabilities = options.capabilities
    argv = [executable or "agy", "--mode", mode, "--print-timeout", "24h", "--sandbox"]
    if capabilities.get("json_output", True):
        argv += ["--output-format", "json"]
    if options.model and capabilities.get("model", True):
        argv += ["--model", options.model]
    if options.effort and capabilities.get("effort", True):
        argv += ["--effort", options.effort]
    if options.resume and capabilities.get("conversation", True):
        argv += ["--conversation", options.resume]
    if options.schema is not None and capabilities.get("json_schema", True):
        argv += ["--json-schema", str(options.schema)]
    text = guard + prompt
    if options.images:
        # agy has no image flag; the paths are named so the worker can open them.
        text += "\n\nImage files to inspect: " + ", ".join(str(image) for image in options.images)
    # agy takes its prompt on argv; it exposes no documented stdin form to move
    # the text out of the Windows cmd.exe re-parse the way Codex's `-` does.
    return [*argv, "--print", text], None


# --- output parsing ---------------------------------------------------------------

def parse_codex_events(stdout: str) -> dict[str, Any]:
    """Fold the `codex exec --json` event stream into the fields Claude needs."""
    parsed: dict[str, Any] = {"parsed": False, "messages": [], "errors": []}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        parsed["parsed"] = True
        kind = event.get("type")
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            parsed["session_id"] = event["thread_id"]
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                parsed["messages"].append(item["text"])
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            parsed["usage"] = event["usage"]
        elif kind == "error":
            message = event.get("message")
            if isinstance(message, str):
                parsed["errors"].append(message)
    return parsed


def parse_agy_output(stdout: str) -> dict[str, Any]:
    """Read agy's `--output-format json` document, tolerating plain text."""
    text = stdout.strip()
    if not text.startswith("{"):
        return {"parsed": False}
    try:
        document = json.loads(text)
    except ValueError:
        return {"parsed": False}
    if not isinstance(document, dict):
        return {"parsed": False}
    parsed: dict[str, Any] = {"parsed": True}
    if isinstance(document.get("conversation_id"), str):
        parsed["session_id"] = document["conversation_id"]
    if isinstance(document.get("response"), str):
        parsed["output"] = document["response"]
    if "structured_output" in document:
        parsed["structured"] = document["structured_output"]
    usage = {key: document[key] for key in ("duration_seconds", "num_turns") if key in document}
    if usage:
        parsed["usage"] = usage
    if isinstance(document.get("status"), str):
        parsed["provider_status"] = document["status"]
    denied = document.get("denied_actions")
    if isinstance(denied, list) and denied:
        # Headless agy auto-denies tools that are not allow-listed; naming them
        # tells the caller which `setup.py setup-agy` option is missing.
        parsed["denied_actions"] = [
            {key: item[key] for key in ("action", "display_name") if isinstance(item.get(key), str)}
            for item in denied if isinstance(item, dict)]
    return parsed


def extract_json_document(text: str) -> Any:
    """Return the last JSON object in free text, or None."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.rfind("\n{")
    while start != -1:
        try:
            return json.loads(text[start + 1:])
        except ValueError:
            start = text.rfind("\n{", 0, start)
    return None


# --- state directory --------------------------------------------------------------

def ensure_state(home: Path) -> Path | None:
    state = state_root(home)
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        return None
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    try: state.chmod(0o700)
    except OSError: pass
    return state


def active_runs(root: Path) -> list[dict[str, Any]]:
    active = []
    if not root.is_dir() or root.is_symlink():
        return active
    for path in root.glob("*.json"):
        if path.name.endswith(".result.json"):
            continue
        try:
            if path.is_symlink() or path.stat().st_size > MAX_STATE: continue
            row = json.loads(path.read_text(encoding="utf-8")); pid = int(row["pid"])
            if owns_process(pid, row.get("process_identity")):
                active.append(row)
        except (OSError, ValueError, KeyError, TypeError): pass
    return active


def finished_runs(root: Path) -> list[str]:
    if not root.is_dir() or root.is_symlink():
        return []
    return sorted(path.name[:-len(".result.json")] for path in root.glob("*.result.json")
                  if not path.is_symlink())


def read_result(root: Path, run_id: str, keep: bool) -> dict[str, Any] | None:
    """Return a finished run's result, or None while nothing has been written yet.

    A result file that exists but cannot be used is reported as an error result
    rather than hidden, so `wait` and `result` never spin on it forever.
    """
    path = root / f"{run_id}.result.json"
    if not path.exists() and not path.is_symlink():
        return None
    data: Any = None
    error = None
    try:
        if path.is_symlink() or not path.is_file():
            error = "unsafe_result_file"
        elif path.stat().st_size > MAX_RESULT_FILE:
            error = "result_too_large"
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                error = "malformed_result"
    except (OSError, ValueError):
        error = "unreadable_result"
    if error is not None:
        data = result("error", error=error, run_id=run_id)
        keep = False
    if not keep:
        for stale in (path, root / f"{run_id}.log"):
            try: stale.unlink()
            except OSError: pass
    return data


def launching_runs(root: Path) -> set[str]:
    """Run ids whose detached launcher exists but has not recorded a worker yet.

    The launcher writes its log with O_EXCL before anything else and the log is
    removed only when the result is collected, so a log without a result marks a
    run that is starting, running, or finished-but-uncollected.
    """
    if not root.is_dir() or root.is_symlink():
        return set()
    return {path.name[:-len(".log")] for path in root.glob("*.log")
            if not path.is_symlink() and not (root / (path.name[:-len(".log")] + ".result.json")).exists()}


# --- commands ---------------------------------------------------------------------

def run_worker(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        return emit(result("error", error="invalid_working_directory"))
    if getattr(args, "background", False):
        return start_background(args, cwd)
    run_id = getattr(args, "run_id", None) or f"{int(time.time())}-{os.getpid()}"
    if not RUN_ID_PATTERN.fullmatch(run_id):
        return emit(result("error", error="invalid_run_id"))
    resume = getattr(args, "resume", None)
    if resume and not SESSION_PATTERN.match(resume):
        return emit(result("error", error="invalid_session_id"))
    role = getattr(args, "role", None) or ("review" if args.action == "review" else None)
    try:
        images = validate_images(getattr(args, "image", None) or [])
        context = read_context_files(getattr(args, "context", None) or [])
        parse_model(getattr(args, "model", None))
    except ValueError as exc:
        return emit(result("error", error=str(exc)))
    schema: Path | None = None
    if getattr(args, "findings", False):
        schema = FINDINGS_SCHEMA
    elif getattr(args, "schema", None):
        # The provider runs in --cwd, so a relative path must be fixed here.
        schema = Path(args.schema).expanduser().resolve()
    if schema is not None and not schema.is_file():
        return emit(result("error", error="schema_not_found"))
    probes = {name: probe(name, args.probe_timeout) for name in PROVIDERS}
    usage = load_usage(args.home)
    provider, route_reason = choose(args.provider, probes, usage, args.prompt)
    if provider is None:
        return emit(result(FALLBACK, action=args.action, reason=route_reason, next_action="continue_in_claude",
                           message="No eligible external worker; Claude should continue locally."))
    if resume and provider == "codex" and args.action == "review":
        return emit(result("error", error="resume_unsupported_for_codex_review", provider=provider))
    write_enabled = bool(getattr(args, "write", False))
    model, effort = resolve_model_effort(provider, getattr(args, "model", None),
                                         getattr(args, "effort", None), role)
    prompt = args.prompt
    brief = bool(getattr(args, "brief", False)) or bool(context) or bool(getattr(args, "deliverable", None)) \
        or bool(getattr(args, "done_when", None))
    if brief:
        prompt = build_brief(prompt, cwd, role, write_enabled, getattr(args, "deliverable", None),
                             getattr(args, "done_when", None), context, images)
    state = ensure_state(args.home)
    if state is None:
        return emit(result("error", error="unsafe_state_directory"))
    last_message = state / f"{run_id}.last.txt" if provider == "codex" and args.action != "review" else None
    options = RunOptions(model=model, effort=effort, resume=resume, ephemeral=bool(getattr(args, "ephemeral", False)),
                         schema=schema, images=images, last_message=last_message,
                         capabilities=probes[provider].get("capabilities") or {})
    argv, stdin_prompt = command_for(provider, args.action, cwd, prompt, write_enabled,
                                     probes[provider].get("executable"), options)
    record = state / f"{run_id}.json"
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    common = {"provider": provider, "run_id": run_id, "model": model, "effort": effort}
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
                       "action": args.action, "started_at": started, "process_identity": identity,
                       "model": model, "effort": effort, "cwd": str(cwd),
                       "background": bool(getattr(args, "result_file", None))}, stream)
        try:
            stdout, stderr = process.communicate(stdin_prompt, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            signal_process_group(process.pid)
            try: stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                signal_process_group(process.pid, force=True); stdout, stderr = process.communicate()
            return emit(result(FALLBACK, **common, reason="timeout", next_action="continue_in_claude",
                               message="External worker timed out; Claude should continue locally."))
    except OSError as exc:
        if process is not None:
            stop_process_group(process)
        return emit(result(FALLBACK, provider=provider, reason="launch_failed", next_action="continue_in_claude",
                           message="External worker could not start; Claude should continue locally.", error=type(exc).__name__))
    finally:
        try: record.unlink()
        except OSError: pass
    last_text = ""
    if last_message is not None:
        try:
            if last_message.is_file() and not last_message.is_symlink() and last_message.stat().st_size <= MAX_OUTPUT:
                last_text = last_message.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        try: last_message.unlink()
        except OSError: pass
    if process.returncode != 0:
        failure: dict[str, Any] = {}
        if provider == "codex" and args.action != "review":
            events = parse_codex_events(stdout)
            if events["errors"]:
                # Codex reports API failures (unknown model, auth, rate limit)
                # as JSON events on stdout, not on stderr.
                unique = list(dict.fromkeys(redact(message) for message in events["errors"]))
                failure["provider_errors"] = unique[-5:]
            if "session_id" in events:
                failure["session_id"] = events["session_id"]
        elif provider == "agy":
            events = parse_agy_output(stdout)
            for key in ("provider_status", "session_id"):
                if key in events:
                    failure[key] = events[key]
        return emit(result(FALLBACK, **common,
                           reason="worker_failed", exit_code=process.returncode,
                           next_action="continue_in_claude", stderr=redact(stderr[-4000:]), **failure,
                           message="External worker failed; Claude should continue locally."))
    if len(stdout.encode(errors="replace")) > MAX_OUTPUT:
        return emit(result(FALLBACK, **common, reason="output_too_large",
                           next_action="continue_in_claude", message="External worker output exceeded the safe capture limit; Claude should continue locally."))
    output = stdout
    extra: dict[str, Any] = {}
    if provider == "codex" and args.action != "review":
        parsed = parse_codex_events(stdout)
        if parsed["parsed"]:
            output = last_text or "\n\n".join(parsed["messages"]) or stdout
            for key in ("session_id", "usage"):
                if key in parsed:
                    extra[key] = parsed[key]
            if parsed["errors"]:
                extra["provider_errors"] = [redact(message) for message in parsed["errors"]]
        elif last_text:
            output = last_text
    elif provider == "agy":
        parsed = parse_agy_output(stdout)
        if parsed["parsed"]:
            output = parsed.get("output", stdout)
            for key in ("session_id", "usage", "structured", "provider_status", "denied_actions"):
                if key in parsed:
                    extra[key] = parsed[key]
    if schema is not None and extra.get("structured") is None:
        extra["structured"] = extract_json_document(output)
    if not output.strip() and extra.get("structured") is None:
        # agy in headless mode reports SUCCESS with an empty response when a
        # tool permission was auto-denied; an empty answer is not a result.
        hint = ("permissions were auto-denied; run setup.py setup-agy (see denied_actions)"
                if extra.get("denied_actions") else None)
        return emit(result(FALLBACK, **common, reason="empty_output", next_action="continue_in_claude",
                           denied_actions=extra.get("denied_actions"), hint=hint,
                           stderr=redact(stderr[-4000:]) or None,
                           message="External worker returned no output; Claude should continue locally."))
    if extra.get("structured") is not None:
        extra["structured"] = redact_value(extra["structured"])
    if "session_id" in extra and not (provider == "codex" and getattr(args, "ephemeral", False)):
        extra["resume_hint"] = f"--resume {extra['session_id']} --provider {provider}"
    return emit(result("completed", **common, route_reason=route_reason,
                       read_only=not write_enabled, write_enabled=write_enabled,
                       resumed_from=resume, brief=brief, images=[str(image) for image in images],
                       schema=str(schema) if schema is not None else None, **extra,
                       output=redact(output), stderr=redact(stderr[-4000:]) or None))


def start_background(args: argparse.Namespace, cwd: Path) -> int:
    """Re-launch this command detached, writing its result to the state directory."""
    state = ensure_state(args.home)
    if state is None:
        return emit(result("error", error="unsafe_state_directory"))
    occupied = {row.get("run_id") for row in active_runs(state)} | launching_runs(state)
    limit = max_active()
    if len(occupied) >= limit:
        return emit(result("error", error="too_many_active_runs", active=len(occupied), max_active=limit,
                           message="Wait for or cancel a managed run before starting another."))
    run_id = f"{int(time.time())}-{os.getpid()}"
    result_file = state / f"{run_id}.result.json"
    log_file = state / f"{run_id}.log"
    if result_file.exists() or log_file.exists():
        return emit(result("error", error="run_id_collision", run_id=run_id))
    argv = [sys.executable, str(Path(__file__).resolve()),
            *(argument for argument in sys.argv[1:] if argument != "--background"),
            "--run-id", run_id, "--result-file", str(result_file)]
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)}
              if WINDOWS else {"start_new_session": True})
    try:
        fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as log:
            # The child re-parses the same arguments, so it must start where the
            # caller did: a relative --cwd, --schema, --image or --context would
            # otherwise be resolved against the workspace instead of the caller.
            child = subprocess.Popen(argv, cwd=os.getcwd(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     env=worker_env(), **detach)
    except OSError as exc:
        return emit(result("error", error="background_launch_failed", detail=type(exc).__name__))
    return emit(result("started", run_id=run_id, launcher_pid=child.pid, action=args.action,
                       provider_requested=args.provider, result_file=str(result_file),
                       next_action=f"dex-workers wait {run_id}",
                       message="Worker started in the background; collect the outcome with `wait` or `result`."))


def wait_runs(args: argparse.Namespace) -> int:
    root = state_root(args.home)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        return emit(result("error", error="unsafe_state_directory"))
    pending = list(dict.fromkeys(args.run_id))
    if any(not RUN_ID_PATTERN.fullmatch(run_id) for run_id in pending):
        return emit(result("error", error="invalid_run_id"))
    results: dict[str, Any] = {}
    lost: list[str] = []
    deadline = time.monotonic() + args.timeout
    while pending:
        active = {row.get("run_id") for row in active_runs(root)} if pending else set()
        for run_id in list(pending):
            data = read_result(root, run_id, args.keep)
            if data is not None:
                results[run_id] = data
                pending.remove(run_id)
            elif run_id not in active and not (root / f"{run_id}.log").exists():
                # Never started, or already collected: nothing will ever arrive.
                lost.append(run_id)
                pending.remove(run_id)
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    status = "completed" if not pending else "timeout"
    return emit(result(status, results=results, pending=pending, not_found=lost))


def show_result(args: argparse.Namespace) -> int:
    root = state_root(args.home)
    if not RUN_ID_PATTERN.fullmatch(args.run_id):
        return emit(result("error", error="invalid_run_id"))
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        return emit(result("error", error="unsafe_state_directory"))
    data = read_result(root, args.run_id, args.keep)
    if data is not None:
        return emit(result("completed", run_id=args.run_id, result=data))
    if any(row.get("run_id") == args.run_id for row in active_runs(root)) or (root / f"{args.run_id}.log").exists():
        return emit(result("running", run_id=args.run_id, message="Worker is still running; use `wait`."))
    return emit(result(FALLBACK, reason="run_not_found", run_id=args.run_id, next_action="continue_in_claude"))


def status(args: argparse.Namespace) -> int:
    usage = load_usage(args.home)
    probes = {name: probe(name, args.probe_timeout) for name in PROVIDERS}
    ready, reason = choose_delegation(probes, usage, "status")
    if probes["agy"].get("available"):
        missing = agy_missing_permissions(args.home)
        probes["agy"]["harness_permissions"] = (
            "unknown" if missing is None else "ready" if not missing
            else "missing: " + ", ".join(missing) + " (run setup.py setup-agy)")
    root = state_root(args.home)
    state_status = "ready"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        state_status = "unsafe"
    active = active_runs(root) if state_status == "ready" else []
    finished = finished_runs(root) if state_status == "ready" else []
    launching = sorted(launching_runs(root) - {row.get("run_id") for row in active}) if state_status == "ready" else []
    return emit(result("completed", version=VERSION, providers=probes,
                       dex_usage_cache="available" if usage else "missing_or_invalid",
                       advisory_route=ready, route_reason=reason,
                       state_directory=state_status, active=active, launching=launching, finished=finished,
                       max_active=max_active(), findings_schema=str(FINDINGS_SCHEMA)))


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
        "suggested_effort": ROLE_EFFORT.get(args.role),
    }, ensure_ascii=False, indent=2))
    return 0


def cancel(args: argparse.Namespace) -> int:
    if not RUN_ID_PATTERN.fullmatch(args.run_id):
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
        q.add_argument("--model", help="provider model; an effort suffix such as gpt-5.5-high pins the effort")
        q.add_argument("--effort", choices=EFFORTS, help="reasoning effort, clamped to what the provider supports")
        q.add_argument("--resume", metavar="SESSION_ID",
                       help="continue a previous worker session (Codex thread or agy conversation)")
        q.add_argument("--schema", metavar="FILE", help="JSON schema the final answer must match")
        q.add_argument("--findings", action="store_true",
                       help="use the bundled findings schema for structured review output")
        q.add_argument("--image", action="append", metavar="FILE", help="attach an image (repeatable)")
        q.add_argument("--brief", action="store_true",
                       help="wrap the prompt in the standard handoff template with workspace state")
        q.add_argument("--deliverable", help="what the worker must hand back (implies --brief)")
        q.add_argument("--done-when", dest="done_when", help="completion criteria (implies --brief)")
        q.add_argument("--context", action="append", metavar="FILE",
                       help="inline a file into the brief (repeatable, implies --brief)")
        q.add_argument("--background", action="store_true",
                       help="return immediately; collect the outcome with `wait` or `result`")
        q.add_argument("--run-id", dest="run_id", help=argparse.SUPPRESS)
        q.add_argument("--result-file", dest="result_file", help=argparse.SUPPRESS)
        if name == "run":
            q.add_argument("--write", action="store_true",
                           help="allow workspace edits; default execution is read-only")
            q.add_argument("--role", choices=ROLES, help="sets the default effort: review=low, audit=high")
            q.add_argument("--ephemeral", action="store_true",
                           help="Codex only: do not persist the session, so it cannot be resumed")
    for name in ("status", "doctor"):
        q = sub.add_parser(name); q.set_defaults(func=status)
    q = sub.add_parser("select", help="select a delegation target without launching it")
    q.set_defaults(func=select_worker)
    q.add_argument("--task", default="", help="bounded subtask used only as a deterministic routing key")
    q.add_argument("--role", choices=ROLES, default="implementation")
    q.add_argument("--mode", choices=("single", "multi"), default="single")
    q.add_argument("--provider", choices=("auto", "claude", *PROVIDERS), default="auto")
    q = sub.add_parser("wait", help="wait for background runs and return their results")
    q.set_defaults(func=wait_runs)
    q.add_argument("run_id", nargs="+"); q.add_argument("--timeout", type=float, default=900)
    q.add_argument("--keep", action="store_true", help="leave the result files in place after reading")
    q = sub.add_parser("result", help="read one background run's result")
    q.set_defaults(func=show_result)
    q.add_argument("run_id"); q.add_argument("--keep", action="store_true")
    q = sub.add_parser("cancel"); q.add_argument("run_id"); q.set_defaults(func=cancel)
    return p


def main() -> int:
    global RESULT_FILE
    # Results are JSON on stdout and may carry any character the worker emitted;
    # the console codepage (cp949, cp1252, ...) must not decide what can be printed.
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError): pass
    args = parser().parse_args()
    if getattr(args, "timeout", 1) <= 0 or args.probe_timeout <= 0:
        return emit(result("error", error="timeout_must_be_positive"))
    result_file = getattr(args, "result_file", None)
    if result_file:
        path = Path(result_file)
        if path.parent != state_root(args.home) or not path.name.endswith(".result.json"):
            return emit(result("error", error="invalid_result_file"))
        RESULT_FILE = path
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
