#!/usr/bin/env python3
"""Fail if any CJK text remains in tracked files.

    python scripts/check_english.py

Exits 1 and lists offending files when Chinese/Japanese/Korean characters are found.
Useful as a pre-commit check for this repository, which is English-only.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
TEXT_SUFFIXES = {".md", ".sql", ".py", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".html", ".css", ".js", ".gitignore", ""}


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
        return [root / p for p in out.decode("utf-8").split("\0") if p]
    except Exception:
        return [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    bad: list[tuple[Path, int, str]] = []
    for p in tracked_files(root):
        if p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits = CJK.findall(text)
        if hits:
            line = next(i for i, ln in enumerate(text.splitlines(), 1) if CJK.search(ln))
            bad.append((p.relative_to(root), len(hits), line))

    if not bad:
        print("OK: no CJK text found")
        return 0

    print(f"FAIL: {len(bad)} file(s) still contain CJK text")
    for path, n, line in sorted(bad):
        print(f"  {n:5d} chars  first at line {line:4d}  {path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
