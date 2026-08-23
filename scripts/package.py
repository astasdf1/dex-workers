#!/usr/bin/env python3
"""Create a deterministic dex-workers release archive from the strict inventory."""
from __future__ import annotations

import argparse
import gzip
import os
import tarfile
import tempfile
from pathlib import Path

from release_inventory import open_regular_nofollow, validate_source_tree

EXECUTABLES = {Path("bin/dex-workers"), Path("scripts/dex_workers.py")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("dist/dex-workers-1.0.0.tar.gz"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = args.out.expanduser().absolute()
    try:
        out.relative_to(root)
    except ValueError:
        pass
    else:
        parser.error("--out must be outside the plugin source directory")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=out.parent, delete=False) as tmp:
        raw = Path(tmp.name)
    try:
        with raw.open("wb") as output, gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path, relative in validate_source_tree(root):
                    fd = open_regular_nofollow(path)
                    with os.fdopen(fd, "rb") as stream:
                        info = archive.gettarinfo(str(path), arcname=str(Path("dex-workers") / relative))
                        info.mode = 0o755 if relative in EXECUTABLES else 0o644
                        info.uid = info.gid = info.mtime = 0
                        info.uname = info.gname = ""
                        archive.addfile(info, stream)
        raw.replace(out)
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        raw.unlink(missing_ok=True)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
