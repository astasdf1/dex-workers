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
    # macOS exposes /var as a system symlink to /private/var. Canonicalize the
    # platform prefix, but never accept the requested target itself as a link.
    if path.is_symlink():
        raise ValueError(f"refusing symlink target: {path}")
    path = path.resolve(strict=False)
    if path == Path(path.anchor):
        raise ValueError("refusing filesystem root")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() and stat.S_ISLNK(os.lstat(current).st_mode):
            raise ValueError(f"refusing symlink path component: {current}")
    return path

def backup(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = path.parent / "backups" / f"{path.name}.before-dex-workers.{stamp}"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target

def update_defaults(home: Path, mode: str) -> int:
    path = safe_root(home / ".claude" / "CLAUDE.md")
    old = path.read_text() if path.exists() else ""
    if (BEGIN in old) != (END in old) or old.count(BEGIN) > 1 or old.count(END) > 1:
        print("malformed dex-workers managed markers; refusing update", file=sys.stderr); return 2
    if BEGIN in old:
        start, tail = old.split(BEGIN, 1); _, finish = tail.split(END, 1)
        new = start.rstrip() + "\n\n" + PROTOCOL.rstrip() + finish
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
        print(f"would update {path}"); return 0
    if old == new:
        print(f"already current: {path}"); return 0
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists(): print(f"backup: {backup(path)}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new); os.replace(tmp, path)
    print(f"updated: {path}"); return 0

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
    p=argparse.ArgumentParser(); p.add_argument("command", choices=("setup-user","setup-project"))
    p.add_argument("--home", type=Path, default=Path.home()); p.add_argument("--target", type=Path, default=Path.cwd())
    group=p.add_mutually_exclusive_group(); group.add_argument("--dry-run", action="store_true"); group.add_argument("--check", action="store_true")
    a=p.parse_args(); mode="check" if a.check else "dry-run" if a.dry_run else "apply"
    try:
        return update_defaults(a.home, mode) if a.command == "setup-user" else setup_project(a.target, mode)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

if __name__ == "__main__": raise SystemExit(main())
