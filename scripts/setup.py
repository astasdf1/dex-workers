#!/usr/bin/env python3
"""Install dex-workers' managed Claude defaults or a minimal project harness."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import stat
import sys
import re
import json
import time
from pathlib import Path

BEGIN = "<!-- dex-workers:default-delegation BEGIN -->"
END = "<!-- dex-workers:default-delegation END -->"
PROTOCOL = f"""{BEGIN}
## Default Delegation Protocol (managed by dex-workers)

- Delegate most bounded implementation, research, review, verification, testing, and documentation work as subtasks.
- Keep the main Claude session responsible for user interaction, decomposition, synthesis, conflict resolution, and final decisions.
- Run at most 5 delegated subtasks concurrently.
- Allow only one writer per worktree; independent read-only subtasks may run in parallel.
- Workers must not recursively delegate. Preserve direct handling for trivial checks, tightly coupled conversational work, and work that cannot be isolated safely.
- Route through the dex-workers `delegate` skill. Prefer Claude/Codex for implementation and deep or high-risk audits, Antigravity for ordinary reviews, and all eligible providers for multi-perspective review.
- Explicit user instructions such as `directly handle`, `no delegation`, `Claude only`, `Codex`, or `Antigravity` override these defaults.
{END}
"""

STATE_DIR = "dex-workers"
DISABLED_STATE = "auto-policy.disabled"
LOCK_DIR = "auto-policy.lock"

HARNESS_FILES = {
    ".harness/README.md": """# Portable project harness

Tool-neutral durable workspace for plans, run evidence, decisions, and verification.
Initialization is additive: existing project instructions and harness files are never overwritten.

Run `./.harness/verify` before claiming an implementation is complete. Set the project's real
verification command in `.harness/config` as `VERIFY_COMMAND='...'`.
""",
    ".harness/config": """# Portable harness configuration
HARNESS_VERSION='1'
# Set this to the project's canonical test/lint/build command.
VERIFY_COMMAND=''
""",
    ".harness/JOURNAL.md": """# Project Journal

Record durable decisions, progress, failures, and verification evidence here.
""",
    ".harness/templates/run.md": """# Run

ID:
Goal:
Scope:
Owner:
Status:

## Changes

## Commands run

## Verification

## Risks

## Remaining work

## Completion Status

STATUS: COMPLETE | PARTIAL | BLOCKED
""",
    ".harness/verify": """#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
config="$root/.harness/config"
[ -f "$config" ] || { echo "missing .harness/config" >&2; exit 2; }
# The config is project-owned and intentionally supports only simple shell assignments.
. "$config"
if [ -z "${VERIFY_COMMAND:-}" ]; then
  echo "harness structure OK; set VERIFY_COMMAND in .harness/config for project verification"
  exit 0
fi
cd "$root"
exec sh -c "$VERIFY_COMMAND"
""",
}

def safe_root(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path == Path(path.anchor):
        raise ValueError("refusing filesystem root")
    # Inspect the spelling supplied by the caller before resolving it. Resolving
    # first would erase an intermediate link such as base/link/project and let
    # writes escape into link's target. Non-existing suffixes are valid.
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"refusing symlink path component: {current}")
    return path

def backup(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = path.parent / "backups" / f"{path.name}.before-dex-workers.{stamp}"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target

def state_path(home: Path, name: str) -> Path:
    return safe_root(home / ".claude" / STATE_DIR / name)

def update_defaults(home: Path, mode: str, quiet: bool = False) -> int:
    path = safe_root(home / ".claude" / "CLAUDE.md")
    old = path.read_text() if path.exists() else ""
    if (BEGIN in old) != (END in old) or old.count(BEGIN) > 1 or old.count(END) > 1:
        print("dex-workers: malformed or duplicate managed markers; no changes made", file=sys.stderr); return 2
    if BEGIN in old:
        start, tail = old.split(BEGIN, 1); _, finish = tail.split(END, 1)
        prefix = start.rstrip() + ("\n\n" if start.strip() else "")
        new = prefix + PROTOCOL.rstrip() + finish
    elif "## Default Delegation Protocol" in old:
        # Migrate the pre-managed section shipped by dex-workers <=1.3.x. Keep
        # surrounding user instructions, including USER:PERSISTENT markers.
        match = re.search(r"(?ms)^## Default Delegation Protocol\s*$.*?(?=^<!-- USER:PERSISTENT:END -->\s*$|\Z)", old)
        if not match:
            print("ambiguous legacy Default Delegation Protocol; refusing update", file=sys.stderr); return 2
        new = old[:match.start()].rstrip() + "\n\n" + PROTOCOL.rstrip() + "\n\n" + old[match.end():].lstrip()
    else:
        new = old.rstrip() + ("\n\n" if old.strip() else "") + PROTOCOL
    if mode == "check":
        return 0 if old == new else 1
    if mode == "dry-run":
        if not quiet: print(f"would update {path}")
        return 0
    if old == new:
        if not quiet: print(f"already current: {path}")
        return 0
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        saved = backup(path)
        if not quiet: print(f"backup: {saved}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new); os.replace(tmp, path)
    if not quiet: print(f"updated: {path}")
    return 0

def acquire_policy_lock(home: Path, wait_seconds: float = 0) -> Path | None:
    lock = state_path(home, LOCK_DIR)
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            lock.mkdir(mode=0o700)
            return lock
        except FileExistsError:
            try:
                if (dt.datetime.now().timestamp() - lock.stat().st_mtime) > 60:
                    lock.rmdir()
                    continue
            except (FileNotFoundError, OSError):
                continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)

def release_policy_lock(lock: Path | None) -> None:
    if lock is not None:
        try: lock.rmdir()
        except OSError: pass

def write_disabled_state(home: Path) -> Path:
    path = state_path(home, DISABLED_STATE)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"disabled": True, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path

def disable_auto_policy(home: Path) -> int:
    lock = acquire_policy_lock(home, wait_seconds=2)
    try:
        path = write_disabled_state(home)
    finally:
        release_policy_lock(lock)
    print(f"auto-policy disabled: {path}")
    return 0

def enable_auto_policy(home: Path) -> int:
    path = state_path(home, DISABLED_STATE)
    if path.exists(): path.unlink()
    print("auto-policy enabled; it will apply on the next SessionStart")
    return 0

def restore_defaults(home: Path) -> int:
    """Remove only a valid managed block and persist the opt-out."""
    lock = acquire_policy_lock(home, wait_seconds=2)
    try:
        path = safe_root(home / ".claude" / "CLAUDE.md")
        old = path.read_text() if path.exists() else ""
        if (BEGIN in old) != (END in old) or old.count(BEGIN) > 1 or old.count(END) > 1:
            print("dex-workers: malformed or duplicate managed markers; no changes made", file=sys.stderr)
            return 2
        if BEGIN in old:
            start, tail = old.split(BEGIN, 1); _, finish = tail.split(END, 1)
            new = (start.rstrip() + "\n\n" + finish.lstrip()).rstrip() + ("\n" if old.endswith("\n") else "")
            saved = backup(path)
            tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
            tmp.write_text(new); os.replace(tmp, path)
            print(f"restored unmanaged instructions; backup: {saved}")
        else:
            print("managed policy not present")
        disabled = write_disabled_state(home)
        print(f"auto-policy disabled: {disabled}")
        return 0
    finally:
        release_policy_lock(lock)

def session_start(home: Path) -> int:
    """Fail-open, non-waiting first-session installer used by the async plugin hook."""
    try:
        disabled = state_path(home, DISABLED_STATE)
        if disabled.exists(): return 0
        lock = acquire_policy_lock(home)
        if lock is None: return 0
        try:
            if disabled.exists(): return 0
            result = update_defaults(home, "apply", quiet=True)
            if result:
                print("dex-workers: auto-policy skipped; run /dex-workers:setup for details", file=sys.stderr)
        finally:
            release_policy_lock(lock)
    except Exception as exc:
        print(f"dex-workers: auto-policy skipped ({type(exc).__name__})", file=sys.stderr)
    return 0

def setup_project(target: Path, mode: str) -> int:
    target = safe_root(target)
    conflicts = []
    for rel, content in HARNESS_FILES.items():
        path = target / rel
        try: safe_root(path)
        except ValueError as exc: print(exc, file=sys.stderr); return 2
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.read_text() != content:
                conflicts.append(rel)
    if conflicts:
        print("conflicts (nothing changed): " + ", ".join(conflicts), file=sys.stderr); return 2
    missing = [rel for rel in HARNESS_FILES if not (target / rel).exists()]
    for rel in (".harness/plans", ".harness/runs"):
        path = target / rel
        if path.exists() and (path.is_symlink() or not path.is_dir()): conflicts.append(rel)
        elif not path.exists(): missing.append(rel + "/")
    if conflicts:
        print("conflicts (nothing changed): " + ", ".join(conflicts), file=sys.stderr); return 2
    if mode == "check": return 0 if not missing else 1
    if mode == "dry-run":
        for rel in missing: print("would create " + rel)
        return 0
    target.mkdir(parents=True, exist_ok=True)
    for rel in (".harness/plans", ".harness/runs"): (target / rel).mkdir(parents=True, exist_ok=True)
    for rel, content in HARNESS_FILES.items():
        path = target / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
            if rel.endswith("/verify"): path.chmod(0o755)
    print(f"project harness ready: {target / '.harness'}"); return 0

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("command", choices=("setup-user","setup-project","session-start","disable-auto-policy","enable-auto-policy","restore-user"))
    p.add_argument("--home", type=Path, default=Path.home()); p.add_argument("--target", type=Path, default=Path.cwd())
    group=p.add_mutually_exclusive_group(); group.add_argument("--dry-run", action="store_true"); group.add_argument("--check", action="store_true")
    a=p.parse_args(); mode="check" if a.check else "dry-run" if a.dry_run else "apply"
    try:
        if a.command == "setup-user": return update_defaults(a.home, mode)
        if a.command == "setup-project": return setup_project(a.target, mode)
        if a.command == "session-start": return session_start(a.home)
        if a.command == "disable-auto-policy": return disable_auto_policy(a.home)
        if a.command == "enable-auto-policy": return enable_auto_policy(a.home)
        return restore_defaults(a.home)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

if __name__ == "__main__": raise SystemExit(main())
