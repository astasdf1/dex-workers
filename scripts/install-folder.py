#!/usr/bin/env python3
"""Copy the strict dex-workers release payload without following links."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from release_inventory import open_regular_nofollow, validate_source_tree


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    dest = args.destination.expanduser().absolute()
    if dest.exists() or dest.is_symlink():
        print(f"refusing existing destination: {dest}", file=sys.stderr)
        return 2
    try:
        dest.relative_to(source)
    except ValueError:
        pass
    else:
        print("refusing destination inside plugin source", file=sys.stderr)
        return 2
    try:
        inventory = validate_source_tree(source)
    except ValueError as exc:
        print(f"refusing unsafe source: {exc}", file=sys.stderr)
        return 2
    try:
        dest.mkdir(parents=True)
        for src, relative in inventory:
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = open_regular_nofollow(src)
            with os.fdopen(fd, "rb") as input_stream, target.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)
            shutil.copymode(src, target, follow_symlinks=False)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    print(dest)
    print(f"claude --plugin-dir {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
