from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aigenora.engine.config import data_protocols_root
from aigenora.proto.prefs import is_blocked


def _load_index(data_dir_value: str | None = None) -> list[dict[str, Any]]:
    index_file = data_protocols_root(data_dir_value) / "index.json"
    if not index_file.exists():
        return []
    data = json.loads(index_file.read_text(encoding="utf-8"))
    return data.get("protocols", data if isinstance(data, list) else [])


def search_protocols(
    family: str | None = None,
    tags: list[str] | None = None,
    capabilities: list[str] | None = None,
    status: str | None = None,
    all_status: bool = False,
    data_dir: str | None = None,
) -> list[dict[str, Any]]:
    protocols = _load_index(data_dir)
    results = []
    for p in protocols:
        if status and p.get("status") != status:
            continue
        if not all_status and p.get("status") == "deprecated":
            continue
        if family and p.get("family") != family:
            continue
        if tags:
            p_tags = set(p.get("tags", []))
            if not all(t in p_tags for t in tags):
                continue
        if capabilities:
            p_caps = set(p.get("capabilities", []))
            if not all(c in p_caps for c in capabilities):
                continue
        entry = dict(p)
        if data_dir and is_blocked(data_dir, p.get("protocol_id", "")):
            entry["_blocked"] = True
        results.append(entry)
    return results


def select_protocol(
    protocol_id: str | None = None,
    alias: str | None = None,
    family: str | None = None,
    profile: str | None = None,
    options: dict[str, Any] | None = None,
    non_interactive: bool = True,
    save_preference: bool = False,
    data_dir: str | None = None,
) -> dict[str, Any]:
    from aigenora.proto.validate import validate_options
    from aigenora.proto.prefs import get_preference, set_preference as save_pref

    protocols = _load_index(data_dir)
    proto_map = {p.get("protocol_id"): p for p in protocols}
    alias_map = {p.get("alias"): p for p in protocols}
    family_map: dict[str, list[dict[str, Any]]] = {}
    for p in protocols:
        fam = p.get("family")
        if fam:
            family_map.setdefault(fam, []).append(p)

    # 1. explicit protocol_id
    if protocol_id:
        if protocol_id not in proto_map:
            raise ValueError(f"protocol not found: {protocol_id}")
        if is_blocked(data_dir, protocol_id):
            raise ValueError(f"protocol {protocol_id} is blocked")
        return _build_result(proto_map[protocol_id], source="explicit_protocol_id", profile=profile, options=options, data_dir=data_dir)

    # 2. explicit alias
    if alias:
        if alias not in alias_map:
            raise ValueError(f"alias not found: {alias}")
        p = alias_map[alias]
        if is_blocked(data_dir, p.get("protocol_id", "")):
            raise ValueError(f"protocol {p['protocol_id']} is blocked")
        return _build_result(p, source="explicit_alias", profile=profile, options=options, data_dir=data_dir)

    # 3-6. family-based selection
    if not family:
        raise ValueError("specify --protocol-id, --alias, or --family")

    candidates = family_map.get(family, [])
    if not candidates:
        raise ValueError(f"no protocols found for family: {family}")

    # filter out blocked
    candidates = [c for c in candidates if not is_blocked(data_dir, c.get("protocol_id", ""))]
    if not candidates:
        raise ValueError(f"all protocols for family {family} are blocked")

    # 3. family + user preference
    pref = get_preference(data_dir, family)
    if pref:
        preferred_id = pref.get("preferred_protocol_id")
        if preferred_id in proto_map and not is_blocked(data_dir, preferred_id):
            pref_profile = pref.get("preferred_profile") or profile
            result = _build_result(proto_map[preferred_id], source="user_preference", profile=pref_profile, options=options, data_dir=data_dir)
            if save_preference:
                save_pref(data_dir, family, preferred_id, pref_profile)
            return result

    # 4. family + unique active
    active = [c for c in candidates if c.get("status") == "active"]
    if len(active) == 1:
        result = _build_result(active[0], source="unique_active", profile=profile, options=options, data_dir=data_dir)
        if save_preference:
            save_pref(data_dir, family, active[0]["protocol_id"], profile)
        return result

    # 5. ambiguous
    if len(candidates) > 1:
        return {
            "status": "ambiguous",
            "reason": "multiple candidates match family",
            "family": family,
            "candidates": [_candidate_summary(c) for c in candidates],
            "next_actions": [
                "rerun with --protocol-id",
                "rerun with --alias",
                "set a preference: protocol preferences set --family {} --protocol-id ID".format(family),
            ],
        }

    # single remaining candidate
    result = _build_result(candidates[0], source="single_candidate", profile=profile, options=options, data_dir=data_dir)
    if save_preference:
        save_pref(data_dir, family, candidates[0]["protocol_id"], profile)
    return result


def _build_result(
    p: dict[str, Any], source: str, profile: str | None = None, options: dict[str, Any] | None = None, data_dir: str | None = None
) -> dict[str, Any]:
    from aigenora.proto.validate import validate_options, load_spec
    from aigenora.proto.prefs import list_profiles

    proto_id = p.get("protocol_id", "")
    alias = p.get("alias", "")
    family = p.get("family", "")
    default_profile = p.get("default_profile", "")
    shared_profiles = p.get("profiles", {})

    # Resolve options merge chain
    resolved_options: dict[str, Any] = {}

    # 1. shared default_profile options
    if default_profile and default_profile in shared_profiles:
        resolved_options.update(shared_profiles[default_profile].get("options", {}))

    # 2. explicit --profile shared options
    active_profile = profile or default_profile
    if profile and profile in shared_profiles:
        resolved_options.update(shared_profiles[profile].get("options", {}))

    # 3. user preference profile
    from aigenora.proto.prefs import get_preference
    pref = get_preference(data_dir, family)
    if pref and pref.get("preferred_profile"):
        pp = pref["preferred_profile"]
        if pp in shared_profiles:
            resolved_options.update(shared_profiles[pp].get("options", {}))

    # 4. user custom profile options
    user_profiles = list_profiles(data_dir, family)
    fam_profiles = user_profiles.get(family, {}) if isinstance(user_profiles.get(family), dict) else {}
    if profile and profile in fam_profiles:
        resolved_options.update(fam_profiles[profile].get("options", {}))

    # 5. explicit --options
    if options:
        resolved_options.update(options)

    # Validate options against spec if parameters exist
    warnings: list[str] = []
    proto_dir = data_protocols_root(data_dir) / p.get("path", "") if p.get("path") else None
    if proto_dir and (proto_dir / "spec.json").exists() and resolved_options:
        try:
            spec = load_spec(proto_dir / "spec.json")
            if spec.get("parameters"):
                validate_options(spec, resolved_options)
        except Exception as e:
            raise ValueError(f"options validation failed: {e}")

    return {
        "status": "selected",
        "source": source,
        "protocol_id": proto_id,
        "alias": alias,
        "family": family,
        "profile": active_profile,
        "options": resolved_options,
        "path": str(proto_dir) if proto_dir else "",
        "warnings": warnings,
    }


def _candidate_summary(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_id": p.get("protocol_id", ""),
        "alias": p.get("alias", ""),
        "status": p.get("status", ""),
        "capabilities": p.get("capabilities", []),
        "default_profile": p.get("default_profile", ""),
        "summary": p.get("description", "")[:80],
    }
