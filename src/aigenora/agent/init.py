from __future__ import annotations

import json
import shutil
from pathlib import Path

from aigenora.engine.config import builtin_protocols_root, data_protocols_root
from aigenora.engine.keys import key_path, keygen

# Never seed bytecode caches into the user library.
_SKIP = {"__pycache__"}


def _copy_tree_idempotent(src: Path, dst: Path, *, overwrite: bool) -> None:
    """Copy the src tree into dst; skip existing files unless overwrite=True."""
    for item in src.rglob("*"):
        if any(part in _SKIP for part in item.parts):
            continue
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def _merge_index(dst_index: Path, src_data: dict) -> int:
    """Merge built-in index entries into the user index; existing user entries win.

    Returns the number of newly added entries.
    """
    user_data: dict = {"version": 2, "protocols": []}
    if dst_index.exists():
        try:
            loaded = json.loads(dst_index.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                user_data = loaded
        except Exception:
            user_data = {"version": 2, "protocols": []}
    entries = user_data.setdefault("protocols", [])
    if not isinstance(entries, list):
        entries = []
        user_data["protocols"] = entries
    seen_ids = {p.get("protocol_id") for p in entries if isinstance(p, dict)}
    seen_aliases = {p.get("alias") for p in entries if isinstance(p, dict)}
    added = 0
    for p in src_data.get("protocols", []):
        if not isinstance(p, dict):
            continue
        pid, alias = p.get("protocol_id"), p.get("alias")
        if pid in seen_ids or alias in seen_aliases:
            continue
        entries.append(p)
        seen_ids.add(pid)
        seen_aliases.add(alias)
        added += 1
    # UTF-8 without BOM: callers read with encoding="utf-8" and choke on a BOM.
    dst_index.write_text(json.dumps(user_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return added


def seed_protocol_library(data_dir_value: str | None = None, *, force: bool = False) -> dict:
    """Seed built-in sample protocols into the user library (data_dir/protocols).

    Idempotent by default: never overwrites existing files, so user edits to hooks.py
    / spec.json / ui/ survive a re-init or upgrade. Only missing sample dirs are
    added and new index.json entries are merged. With force=True every sample is
    re-copied over local changes and the index is rewritten from the built-in source.
    """
    src = builtin_protocols_root()
    dst = data_protocols_root(data_dir_value)
    stats = {"samples": 0, "index_added": 0, "forced": force, "seeded": False}
    if not src.exists() or not (src / "index.json").exists():
        return stats
    dst.mkdir(parents=True, exist_ok=True)
    src_index = json.loads((src / "index.json").read_text(encoding="utf-8"))
    stats["samples"] = sum(1 for p in src_index.get("protocols", []) if isinstance(p, dict))
    # 1. protocol dirs + templates (idempotent unless force)
    for child in src.iterdir():
        if child.name in ("index.json", *_SKIP):
            continue
        _copy_tree_idempotent(child, dst / child.name, overwrite=force)
    # 2. index.json: overwrite on force, merge otherwise
    dst_index = dst / "index.json"
    if force:
        dst_index.write_text(json.dumps(src_index, ensure_ascii=False, indent=2), encoding="utf-8")
        stats["index_added"] = stats["samples"]
    else:
        stats["index_added"] = _merge_index(dst_index, src_index)
    stats["seeded"] = True
    return stats


def run(args) -> int:
    kp = keygen(args.data_dir, force=args.force)
    print(f"[OK] initialized: {key_path(args.data_dir)}")
    print(f"public_key: {kp.public_key}")
    force_samples = getattr(args, "force_samples", False)
    stats = seed_protocol_library(args.data_dir, force=force_samples)
    if stats["seeded"] and stats["samples"]:
        action = "re-seeded (force)" if force_samples else "seeded"
        note = "" if force_samples else "; existing files preserved"
        print(f"[protocols] {action} {stats['samples']} built-in samples, "
              f"{stats['index_added']} new index entries{note}")
    return 0
