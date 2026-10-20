# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Generate the Northbridge demo documents (development).

    uv run python -m app.demo.generate            # write demo_data/northbridge/courses
    uv run python -m app.demo.generate --check    # verify the committed files, byte for byte

Writes exactly the files in ``northbridge_content.DOCUMENTS``. ``--check``
fails if any committed file differs from a fresh render, or if an unlisted
file sits under ``courses/``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from app.demo.northbridge_content import DOCUMENTS
from app.demo.render import render

DATA_ROOT = Path(__file__).resolve().parents[3] / "demo_data" / "northbridge"


def build_all() -> dict[str, bytes]:
    return {path: render(doc) for path, doc in sorted(DOCUMENTS.items())}


def differences(root: Path, built: dict[str, bytes]) -> list[str]:
    problems = []
    for path, data in built.items():
        file = root / path
        if not file.is_file():
            problems.append(f"{path}: missing")
        elif file.read_bytes() != data:
            problems.append(f"{path}: differs from a fresh render")
    courses = root / "courses"
    present = (
        {p.relative_to(root).as_posix() for p in courses.rglob("*") if p.is_file()}
        if courses.is_dir()
        else set()
    )
    problems.extend(f"{extra}: not generated" for extra in sorted(present - built.keys()))
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.demo.generate")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=DATA_ROOT)
    args = parser.parse_args(argv)
    built = build_all()
    if args.check:
        problems = differences(args.root, built)
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    for path, data in built.items():
        target = args.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(f"wrote {len(built)} files under {args.root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
