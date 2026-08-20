"""Discover and run every chapter demo (new src/ layout)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAPTERS = ROOT / "chapters"
EXPECTED_CHAPTERS = 17
# 只跑本地机制的章节，学习者无需 API Key 即可运行。
LOCAL_ONLY_NUMBERS = frozenset({3, 4, 8, 10, 11, 12, 13, 16})


def discover() -> tuple[Path, ...]:
    demos = tuple(sorted(CHAPTERS.glob("[0-9][0-9]-*/src/demo.py")))
    if len(demos) != EXPECTED_CHAPTERS:
        raise RuntimeError(f"expected {EXPECTED_CHAPTERS} chapter demos, discovered {len(demos)}")
    numbers = tuple(path.parent.parent.name[:2] for path in demos)
    expected = tuple(f"{number:02d}" for number in range(1, EXPECTED_CHAPTERS + 1))
    if numbers != expected:
        raise RuntimeError(f"chapter demo sequence is incomplete: {numbers!r}")
    return demos


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 mini-harness 教学章节。")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="只运行不访问模型或 Web Search 的 8 章",
    )
    options = parser.parse_args(argv)
    demos = discover()
    if options.local_only:
        demos = tuple(
            demo for demo in demos if int(demo.parent.parent.name[:2]) in LOCAL_ONLY_NUMBERS
        )
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    total = len(demos)
    for index, demo in enumerate(demos, start=1):
        relative = demo.relative_to(ROOT)
        print(f"[{index:02d}/{total:02d}] {relative}", flush=True)
        completed = subprocess.run(
            [sys.executable, str(demo)],
            cwd=ROOT,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            print(f"FAILED: {relative} (exit {completed.returncode})", file=sys.stderr)
            return completed.returncode or 1
    label = "local " if options.local_only else ""
    print(f"All {total} {label}chapter demos passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
