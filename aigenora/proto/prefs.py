from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prefs_path(data_dir: str | None) -> Path:
    from aigenora.engine.config import data_dir as _data_dir
    base = Path(data_dir) if data_dir else _data_dir()
    return base / "preferences" / "protocols.json"


def _profiles_path(data_dir: str | None) -> Path:
    from aigenora.engine.config import data_dir as _data_dir
    base = Path(data_dir) if data_dir else _data_dir()
    return base / "profiles" / "protocols.json"


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))  # validate
    tmp.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "families": {}, "blocked_protocols": []}
    return json.loads(path.read_text(encoding="utf-8"))


# --- Preferences ---


def list_preferences(data_dir: str | None) -> dict[str, Any]:
    prefs = _load_json(_prefs_path(data_dir))
    return prefs


def get_preference(data_dir: str | None, family: str) -> dict[str, Any] | None:
    prefs = _load_json(_prefs_path(data_dir))
    return prefs.get("families", {}).get(family)


def set_preference(data_dir: str | None, family: str, protocol_id: str, profile: str | None = None, reason: str = "") -> dict[str, Any]:
    prefs = _load_json(_prefs_path(data_dir))
    blocked_ids = {b["protocol_id"] for b in prefs.get("blocked_protocols", [])}
    if protocol_id in blocked_ids:
        raise ValueError(f"protocol {protocol_id} is blocked")
    entry: dict[str, Any] = {
        "preferred_protocol_id": protocol_id,
        "reason": reason,
        "source": "user_confirmed",
        "updated_at": _now_iso(),
    }
    if profile:
        entry["preferred_profile"] = profile
    prefs.setdefault("families", {})[family] = entry
    _atomic_write(_prefs_path(data_dir), prefs)
    return entry


def clear_preference(data_dir: str | None, family: str) -> bool:
    prefs = _load_json(_prefs_path(data_dir))
    families = prefs.get("families", {})
    if family in families:
        del families[family]
        _atomic_write(_prefs_path(data_dir), prefs)
        return True
    return False


def block_protocol(data_dir: str | None, protocol_id: str, reason: str = "") -> None:
    prefs = _load_json(_prefs_path(data_dir))
    blocked = prefs.get("blocked_protocols", [])
    ids = {b["protocol_id"] for b in blocked}
    if protocol_id not in ids:
        blocked.append({"protocol_id": protocol_id, "reason": reason, "updated_at": _now_iso()})
    prefs["blocked_protocols"] = blocked
    # remove from families if present
    families = prefs.get("families", {})
    for fam, entry in list(families.items()):
        if entry.get("preferred_protocol_id") == protocol_id:
            del families[fam]
    _atomic_write(_prefs_path(data_dir), prefs)


def unblock_protocol(data_dir: str | None, protocol_id: str) -> bool:
    prefs = _load_json(_prefs_path(data_dir))
    blocked = prefs.get("blocked_protocols", [])
    new_blocked = [b for b in blocked if b["protocol_id"] != protocol_id]
    if len(new_blocked) == len(blocked):
        return False
    prefs["blocked_protocols"] = new_blocked
    _atomic_write(_prefs_path(data_dir), prefs)
    return True


def is_blocked(data_dir: str | None, protocol_id: str) -> bool:
    prefs = _load_json(_prefs_path(data_dir))
    return any(b["protocol_id"] == protocol_id for b in prefs.get("blocked_protocols", []))


# --- Profiles ---


def list_profiles(data_dir: str | None, family: str | None = None) -> dict[str, Any]:
    data = _load_json(_profiles_path(data_dir))
    if family:
        return {family: data.get("families", {}).get(family, {})}
    return data


def set_profile(data_dir: str | None, family: str, name: str, protocol_id: str, options: dict[str, Any], description: str = "") -> dict[str, Any]:
    profiles = _load_json(_profiles_path(data_dir))
    entry = {
        "protocol_id": protocol_id,
        "description": description,
        "options": options,
        "created_at": _now_iso(),
    }
    profiles.setdefault("families", {}).setdefault(family, {})[name] = entry
    _atomic_write(_profiles_path(data_dir), profiles)
    return entry


def delete_profile(data_dir: str | None, family: str, name: str) -> bool:
    profiles = _load_json(_profiles_path(data_dir))
    fam = profiles.get("families", {}).get(family, {})
    if name in fam:
        del fam[name]
        _atomic_write(_profiles_path(data_dir), profiles)
        return True
    return False
