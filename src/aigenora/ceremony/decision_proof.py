from __future__ import annotations

from typing import Any

from .canonical import canonical_json_bytes, domain_hash_hex, require_hex, sha256_hex
from .errors import CONTEXT_MISMATCH, NON_CANONICAL, TALLY_MISMATCH, VsdpError
from .manifest import PROFILE_ID, decision_id, validate_final_manifest, validate_setup_manifest


PROVISIONAL_PROOF_SCHEMA = "vsdp-provisional-decision-proof/1"


def _tally_entries(
    tally_result: dict[str, Any],
    final_manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    options = tally_result.get("options")
    if not isinstance(options, list):
        raise VsdpError(NON_CANONICAL, "tally result options must be an array")
    frozen_ids = [
        choice["option_id"] for choice in final_manifest["decision"]["choices"]
    ]
    if len(options) != len(frozen_ids):
        raise VsdpError(TALLY_MISMATCH, "tally option count mismatch")
    entries: list[dict[str, Any]] = []
    for option_id, value in zip(frozen_ids, options, strict=True):
        if not isinstance(value, dict):
            raise VsdpError(NON_CANONICAL, "tally option must be an object")
        if value.get("option_id") != option_id:
            raise VsdpError(TALLY_MISMATCH, "tally option order mismatch")
        count = value.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise VsdpError(NON_CANONICAL, "tally count must be a non-negative integer")
        entries.append({"count": count, "option_id": option_id})
    accepted_count = tally_result.get("accepted_count")
    if sum(entry["count"] for entry in entries) != accepted_count:
        raise VsdpError(TALLY_MISMATCH, "single-choice tally total mismatch")
    return entries


def _outcome(
    tally: list[dict[str, Any]],
    final_manifest: dict[str, Any],
) -> dict[str, str]:
    decision = final_manifest["decision"]
    if decision.get("pass_rule") != {"kind": "plurality"}:
        raise VsdpError(CONTEXT_MISMATCH, "unsupported decision pass rule")
    if decision.get("tie_rule") != "no_outcome":
        raise VsdpError(CONTEXT_MISMATCH, "unsupported decision tie rule")
    maximum = max(entry["count"] for entry in tally)
    winners = [entry["option_id"] for entry in tally if entry["count"] == maximum]
    if len(winners) == 1:
        return {"kind": "selected", "option_id": winners[0]}
    return {"kind": "no_outcome", "reason": "tie"}


def build_provisional_decision_proof(
    *,
    setup_manifest: dict[str, Any],
    final_manifest: dict[str, Any],
    checkpoint_hash: str,
    authorization: dict[str, Any],
    tally_result: dict[str, Any],
) -> dict[str, Any]:
    validate_setup_manifest(setup_manifest)
    validate_final_manifest(final_manifest, setup_manifest=setup_manifest)
    require_hex(checkpoint_hash, 64, "checkpoint_hash")
    authorization_hash = require_hex(
        authorization.get("authorization_hash"),
        64,
        "authorization_hash",
    )
    tally = _tally_entries(tally_result, final_manifest)
    body = {
        "accepted_count": tally_result["accepted_count"],
        "aggregate_authorization_hash": authorization_hash,
        "board_checkpoint_hash": checkpoint_hash,
        "decision_id": decision_id(final_manifest),
        "final_manifest_hash": sha256_hex(canonical_json_bytes(final_manifest)),
        "outcome": _outcome(tally, final_manifest),
        "profile_id": PROFILE_ID,
        "schema": PROVISIONAL_PROOF_SCHEMA,
        "setup_manifest_hash": sha256_hex(canonical_json_bytes(setup_manifest)),
        "status": "provisional",
        "tally": tally,
        "tally_result_hash": sha256_hex(canonical_json_bytes(tally_result)),
    }
    return {
        **body,
        "proof_hash": domain_hash_hex("vsdp/provisional-decision-proof/v1", body),
    }


def verify_provisional_decision_proof(
    value: dict[str, Any],
    *,
    setup_manifest: dict[str, Any],
    final_manifest: dict[str, Any],
    checkpoint_hash: str,
    authorization: dict[str, Any],
    tally_result: dict[str, Any],
) -> str:
    expected = build_provisional_decision_proof(
        setup_manifest=setup_manifest,
        final_manifest=final_manifest,
        checkpoint_hash=checkpoint_hash,
        authorization=authorization,
        tally_result=tally_result,
    )
    if value != expected:
        raise VsdpError(TALLY_MISMATCH, "ProvisionalDecisionProof does not match public artifacts")
    return value["proof_hash"]
