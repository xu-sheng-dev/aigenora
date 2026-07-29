#!/usr/bin/env python3
"""Verify that a wheel contains exactly the protocols declared by its index."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath


PACKAGE_ROOT = PurePosixPath("aigenora/protocols")


def _fail(message: str) -> None:
    raise ValueError(message)


def check_wheel(wheel_path: Path) -> int:
    if not wheel_path.is_file():
        _fail(f"wheel does not exist: {wheel_path}")
    if wheel_path.suffix != ".whl":
        _fail(f"expected a .whl file: {wheel_path}")

    with zipfile.ZipFile(wheel_path) as archive:
        names = set(archive.namelist())
        index_name = (PACKAGE_ROOT / "index.json").as_posix()
        if index_name not in names:
            _fail(f"wheel is missing {index_name}")
        index = json.loads(archive.read(index_name).decode("utf-8"))

    entries = index.get("protocols") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        _fail("protocol index must contain a protocols array")

    expected_roots: set[str] = set()
    aliases: set[str] = set()
    protocol_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            _fail("protocol index entry must be an object")
        alias = entry.get("alias")
        protocol_id = entry.get("protocol_id")
        relative_path = entry.get("path")
        if not all(isinstance(value, str) and value for value in (alias, protocol_id, relative_path)):
            _fail("protocol index entry is missing alias, protocol_id, or path")
        if alias in aliases:
            _fail(f"duplicate protocol alias: {alias}")
        if protocol_id in protocol_ids:
            _fail(f"duplicate protocol_id: {protocol_id}")
        aliases.add(alias)
        protocol_ids.add(protocol_id)

        path = PurePosixPath(relative_path)
        if path.is_absolute() or ".." in path.parts or len(path.parts) != 2:
            _fail(f"invalid protocol path: {relative_path}")
        if "".join(path.parts) != protocol_id:
            _fail(f"protocol path does not match protocol_id: {relative_path}")
        expected_roots.add((PACKAGE_ROOT / path).as_posix())

    actual_roots = {
        str(PurePosixPath(name).parent)
        for name in names
        if name.startswith(PACKAGE_ROOT.as_posix() + "/") and name.endswith("/spec.json")
    }
    missing = sorted(expected_roots - actual_roots)
    unexpected = sorted(actual_roots - expected_roots)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing indexed protocols: " + ", ".join(missing))
        if unexpected:
            details.append("unindexed protocols: " + ", ".join(unexpected))
        _fail("; ".join(details))

    for root in sorted(expected_roots):
        required = {
            f"{root}/spec.json",
            f"{root}/hooks.py",
            f"{root}/ui/index.html",
        }
        absent = sorted(required - names)
        if absent:
            _fail("protocol bundle is incomplete: " + ", ".join(absent))

    print(
        f"Wheel protocol inventory OK: {wheel_path.name} "
        f"contains {len(expected_roots)} indexed protocol bundles."
    )
    return len(expected_roots)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    try:
        check_wheel(args.wheel.resolve())
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        print(f"Wheel protocol inventory check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
