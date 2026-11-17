#!/usr/bin/env python3
"""Rebuild skills/hyra/scripts/hyra.pyz: the whole `hyra` package as one self-contained, stdlib-only zipapp.

Why: a skills.sh / `npx skills add` install copies only the skill folders, so the Python package in this repo's top-level
`hyra/` wouldn't exist there and `scripts/hyra` couldn't run. The launcher falls back to this bundle, which makes any copy of
the skills work with nothing but Python 3.9+. Source of truth stays `hyra/`; a test fails if the bundle is stale.

    python3 scripts/build_bundle.py        # run after changing anything under hyra/
"""
import hashlib
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "skills" / "hyra" / "scripts" / "hyra.pyz"


def source_hash() -> str:
    h = hashlib.sha256()
    for f in sorted((ROOT / "hyra").glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def build() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(ROOT / "hyra", Path(tmp) / "hyra", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        zipapp.create_archive(tmp, OUT, interpreter="/usr/bin/env python3", main="hyra.cli:main", compressed=True)
    digest = source_hash()
    OUT.with_name("hyra.pyz.sha256").write_text(digest + "\n")
    return digest


if __name__ == "__main__":
    d = build()
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB), source hash {d[:12]}")
