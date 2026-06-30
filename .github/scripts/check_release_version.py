#!/usr/bin/env python3
"""Validate release version consistency for CI and PyPI publishing.

The client version has three sources that must move together:
- pyproject.toml project.version
- src/aigenora/__init__.py __version__
- src/aigenora/skill/SKILL.md frontmatter version

During normal CI, pre-release versions such as 0.6.0rc1 are allowed as long as
all three sources match. During PyPI publishing, pass --final-tag and --tag
vX.Y.Z to require a final release tag and block rc/dev builds from PyPI.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FINAL_TAG_RE = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$")
PYPROJECT_VERSION_RE = re.compile(r"(?m)^version\s*=\s*[\"']([^\"']+)[\"']\s*$")
INIT_VERSION_RE = re.compile(r"__version__\s*=\s*[\"']([^\"']+)[\"']")
SKILL_VERSION_RE = re.compile(r"(?m)^version:\s*([^\s]+)\s*$")


def _read(path: Path, encoding: str = "utf-8") -> str:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path.read_text(encoding=encoding)


def _match(pattern: re.Pattern[str], text: str, label: str) -> str:
    match = pattern.search(text)
    if not match:
        raise ValueError(f"{label} version not found")
    return match.group(1).strip().strip("\"'")


def read_versions() -> dict[str, str]:
    return {
        "pyproject.toml": _match(
            PYPROJECT_VERSION_RE,
            _read(ROOT / "pyproject.toml"),
            "pyproject.toml project.version",
        ),
        "src/aigenora/__init__.py": _match(
            INIT_VERSION_RE,
            _read(ROOT / "src" / "aigenora" / "__init__.py"),
            "src/aigenora/__init__.py __version__",
        ),
        "src/aigenora/skill/SKILL.md": _match(
            SKILL_VERSION_RE,
            _read(ROOT / "src" / "aigenora" / "skill" / "SKILL.md", "utf-8-sig"),
            "src/aigenora/skill/SKILL.md frontmatter",
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="Git tag to compare, normally GITHUB_REF_NAME")
    parser.add_argument(
        "--final-tag",
        action="store_true",
        help="Require --tag to be a final vX.Y.Z release tag for PyPI publishing",
    )
    args = parser.parse_args(argv)

    try:
        versions = read_versions()
    except (FileNotFoundError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    expected = next(iter(versions.values()))
    mismatches = {name: value for name, value in versions.items() if value != expected}
    if mismatches:
        print("[FAIL] client version sources disagree:", file=sys.stderr)
        for name, value in versions.items():
            print(f"  {name}: {value}", file=sys.stderr)
        return 1

    if args.tag:
        tag = args.tag.strip()
        match = FINAL_TAG_RE.match(tag)
        if args.final_tag and not match:
            print(
                "[FAIL] only final release tags vX.Y.Z may publish to PyPI; "
                f"got {tag!r}",
                file=sys.stderr,
            )
            return 1
        if match and match.group("version") != expected:
            print(
                "[FAIL] tag version does not match client version sources:",
                file=sys.stderr,
            )
            print(f"  tag: {match.group('version')}", file=sys.stderr)
            for name, value in versions.items():
                print(f"  {name}: {value}", file=sys.stderr)
            return 1
        if args.final_tag:
            print(f"[OK] final release version: {expected}")
            return 0

    print(f"[OK] client versions aligned: {expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
