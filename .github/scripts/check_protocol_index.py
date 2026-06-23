#!/usr/bin/env python3
"""Validate the built-in protocol library index (guard for release).

Every entry's ``protocol_id`` must equal ``protocol_hash(spec.json)`` of the
spec at its recorded ``path``. A stale ``protocol_id`` (e.g. spec.json changed
but the index was not regenerated) makes a built-in sample unresolvable by
``path_for`` on the guest side, so ``join`` falls back to fetching a hooks
skeleton and fails. This check blocks CI and release builds before that ships.

Self-contained: locates the repo by ``__file__`` and prepends ``src/`` to
``sys.path`` so it can import ``aigenora.engine.crypto.protocol_hash`` (single
source of truth for the hash algorithm) without installing the package.

Exit codes: 0 = all match, 1 = mismatch, 2 = index missing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# .github/scripts/check_protocol_index.py -> repo root
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from aigenora.engine.crypto import protocol_hash  # noqa: E402

BASE = ROOT / "src" / "aigenora" / "protocols"


def main() -> int:
    idx = BASE / "index.json"
    if not idx.exists():
        print(f"FATAL: index.json not found at {idx}", file=sys.stderr)
        return 2
    data = json.loads(idx.read_text(encoding="utf-8"))
    protocols = data.get("protocols", data if isinstance(data, list) else [])
    errors: list[str] = []
    for item in protocols:
        fam = item.get("family", item.get("alias", "?"))
        pid = item.get("protocol_id", "")
        rel = item.get("path", "")
        spec = BASE / rel / "spec.json"
        if not spec.exists():
            errors.append(f"[{fam}] spec.json missing at path='{rel}'")
            continue
        actual = protocol_hash(str(spec))
        if actual != pid:
            errors.append(
                f"[{fam}] protocol_id mismatch: index={pid[:16]}... "
                f"actual={actual[:16]}... "
                f"(fix: re-run `python -m aigenora protocol hash "
                f"src/aigenora/protocols/{rel}/spec.json` and update index.json)"
            )
    if errors:
        print("FAIL: protocol index hash check")
        for e in errors:
            print("  " + e)
        return 1
    print(f"OK: {len(protocols)} built-in protocols, every protocol_id == hash(spec)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
