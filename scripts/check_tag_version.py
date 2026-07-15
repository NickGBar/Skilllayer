#!/usr/bin/env python3
"""Verify a release tag matches the package version in pyproject.toml."""
from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args(argv)
    match = re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", args.tag)
    if not match:
        print(f"invalid release tag: {args.tag}")
        return 1
    try:
        metadata = tomllib.loads(args.project.read_text(encoding="utf-8"))["project"]
        version = metadata["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        print(f"missing or invalid package version: {exc}")
        return 1
    expected = ".".join(match.groups())
    if version != expected:
        print(f"tag {args.tag} does not match package version {version}")
        return 1
    print(f"tag {args.tag} matches package version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
