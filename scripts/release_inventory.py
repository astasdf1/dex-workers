"""Strict release inventory shared by folder and archive installers."""
from __future__ import annotations

import os
import stat
from pathlib import Path

RELEASE_FILES = (
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    "NOTICE.md",
    "README.md",
    "bin/dex-workers",
    "scripts/dex_workers.py",
    "skills/cancel/SKILL.md",
    "skills/delegate/SKILL.md",
    "skills/doctor/SKILL.md",
    "skills/review/SKILL.md",
    "skills/run/SKILL.md",
    "skills/status/SKILL.md",
)


def validate_source_tree(root: Path) -> list[tuple[Path, Path]]:
    """Reject every source link or special node, then return the exact payload."""
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"source contains link or special file: {path.relative_to(root)}")
    inventory = []
    for relative in RELEASE_FILES:
        path = root / relative
        try:
            mode = os.lstat(path).st_mode
        except OSError as exc:
            raise ValueError(f"missing release file: {relative}") from exc
        if not stat.S_ISREG(mode):
            raise ValueError(f"release file is not a regular file: {relative}")
        inventory.append((path, Path(relative)))
    return inventory


def open_regular_nofollow(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"release file changed type while opening: {path}")
    return fd
