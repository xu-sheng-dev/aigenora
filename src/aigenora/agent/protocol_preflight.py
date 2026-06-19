from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aigenora.engine.config import data_protocols_root
from aigenora.engine.crypto import protocol_hash_from_obj


def classify_diff(draft: dict[str, Any], candidate: dict[str, Any]) -> str:
    """Classify how a draft spec differs from a candidate spec.

    Returns one of: same_hash, metadata_only, options_only, compatible_extension, contract_change, unknown
    """
    draft_hash = protocol_hash_from_obj(draft)
    cand_hash = protocol_hash_from_obj(candidate)
    if draft_hash == cand_hash:
        return "same_hash"

    # Compare messages
    draft_msgs = {(m if isinstance(m, dict) else {}).get("name"): m for m in draft.get("messages", []) if isinstance(m, dict)}
    cand_msgs = {(m if isinstance(m, dict) else {}).get("name"): m for m in candidate.get("messages", []) if isinstance(m, dict)}

    # New messages added
    new_msg_names = set(draft_msgs) - set(cand_msgs)
    # Removed messages
    removed_msg_names = set(cand_msgs) - set(draft_msgs)

    if removed_msg_names:
        return "contract_change"

    # Check for field changes in existing messages
    for name in set(draft_msgs) & set(cand_msgs):
        dm = draft_msgs[name] or {}
        cm = cand_msgs[name] or {}
        df = dm.get("fields", {})
        cf = cm.get("fields", {})
        if set(df.keys()) != set(cf.keys()):
            # new optional fields = compatible_extension, removed/changed = contract_change
            new_fields = set(df.keys()) - set(cf.keys())
            removed_fields = set(cf.keys()) - set(df.keys())
            if removed_fields:
                return "contract_change"
            # check if all new fields are optional
            for f in new_fields:
                if df[f].get("required", False) if isinstance(df[f], dict) else False:
                    return "contract_change"
            return "compatible_extension"
        # same field names, check types/constraints changed
        for key in df:
            ds = df[key] if isinstance(df[key], dict) else {}
            cs = cf[key] if isinstance(cf[key], dict) else {}
            if ds.get("type") != cs.get("type"):
                return "contract_change"
            if ds.get("values") != cs.get("values"):
                return "contract_change"

    # Check flow changes
    draft_flow = draft.get("flow", {})
    cand_flow = candidate.get("flow", {})
    if draft_flow.get("mode") != cand_flow.get("mode"):
        return "contract_change"
    if draft_flow.get("end_when") != cand_flow.get("end_when"):
        return "contract_change"

    # Check rules changes
    if draft.get("rules") != candidate.get("rules"):
        return "contract_change"

    # Check decision changes
    if draft.get("decision") != candidate.get("decision"):
        return "contract_change"

    # Check timing changes (v004)
    if draft.get("timing") != candidate.get("timing"):
        return "contract_change"

    # Check if only new messages were added (compatible_extension)
    if new_msg_names:
        return "compatible_extension"

    # Check parameters (options_only)
    if draft.get("parameters") != candidate.get("parameters"):
        return "options_only"

    # Only metadata changed (name, description, tags, type)
    return "metadata_only"


def preflight(
    draft_spec: dict[str, Any],
    family: str | None = None,
    include_remote: bool = False,
    allow_new: bool = False,
    reason: str = "",
    data_dir: str | None = None,
) -> dict[str, Any]:
    draft_hash = protocol_hash_from_obj(draft_spec)

    # Load local candidates from the user library
    index_file = data_protocols_root(data_dir) / "index.json"
    candidates: list[dict[str, Any]] = []
    if index_file.exists():
        data = json.loads(index_file.read_text(encoding="utf-8"))
        for p in data.get("protocols", []):
            if family and p.get("family") != family:
                continue
            spec_file = data_protocols_root(data_dir) / p.get("path", "") / "spec.json" if p.get("path") else None
            if spec_file and spec_file.exists():
                cand_spec = json.loads(spec_file.read_text(encoding="utf-8"))
                classification = classify_diff(draft_spec, cand_spec)
                candidates.append({
                    "protocol_id": p.get("protocol_id", ""),
                    "alias": p.get("alias", ""),
                    "source": "local",
                    "classification": classification,
                    "family": p.get("family", ""),
                })

    # Check for same hash
    for c in candidates:
        if c["classification"] == "same_hash":
            return {
                "status": "blocked",
                "draft_protocol_id": draft_hash,
                "classification": "same_hash",
                "recommendation": "reuse_existing_protocol",
                "reason": "same contract already exists",
                "existing_protocol_id": c["protocol_id"],
            }

    # Check metadata_only and options_only
    for c in candidates:
        if c["classification"] == "metadata_only":
            return {
                "status": "blocked",
                "draft_protocol_id": draft_hash,
                "classification": "metadata_only",
                "recommendation": "update_metadata",
                "reason": "only metadata differs (name/description/tags/type)",
                "existing_protocol_id": c["protocol_id"],
            }
        if c["classification"] == "options_only":
            return {
                "status": "blocked",
                "draft_protocol_id": draft_hash,
                "classification": "options_only",
                "recommendation": "use_options_or_profile",
                "reason": "only parameters/options differ",
                "existing_protocol_id": c["protocol_id"],
            }

    # contract_change or compatible_extension or unknown
    nearest = [c for c in candidates if c["classification"] in ("contract_change", "compatible_extension", "unknown")]
    draft_family = family or draft_spec.get("family", "")

    if nearest:
        classification = "contract_change" if any(c["classification"] == "contract_change" for c in nearest) else "compatible_extension"
        if classification == "compatible_extension" and not allow_new and not reason:
            return {
                "status": "blocked",
                "draft_protocol_id": draft_hash,
                "classification": "compatible_extension",
                "recommendation": "review_before_creating",
                "reason": "compatible extension detected, use --allow-new or provide --reason",
                "nearest": nearest,
            }

    return {
        "status": "allowed",
        "draft_protocol_id": draft_hash,
        "family": draft_family,
        "classification": nearest[0]["classification"] if nearest else "new_family",
        "recommendation": "create_new_protocol",
        "nearest": nearest,
        "required_metadata": {
            "family": draft_family,
            "parent_protocol_id": nearest[0]["protocol_id"] if nearest else None,
            "created_reason": reason or "new protocol",
        },
    }
