from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable

from aigenora.engine.crypto import protocol_hash_from_obj
from aigenora.engine.keys import KeyPair, sign_raw, verify_raw
from aigenora.proto.group import canonical_json, json_hash
from aigenora.proto.spec_version import check_spec_version
from aigenora.proto.validate import validate_flow, validate_timing


RULE_ARTIFACT_VERSION = 1
MAX_RULE_TEXT_BYTES = 64 * 1024
MAX_ENDORSEMENT_REASON_BYTES = 4096


def create_rule_proposal(
    spec: dict[str, Any],
    *,
    rules_text: str,
    keypair: KeyPair,
) -> dict[str, Any]:
    _validate_spec(spec)
    _validate_text(rules_text, MAX_RULE_TEXT_BYTES, "rules text")
    body = {
        "artifact_version": RULE_ARTIFACT_VERSION,
        "artifact_kind": "aigenora-rule-proposal",
        "created_at": _now(),
        "proposer_public_key": keypair.public_key,
        "protocol_id": protocol_hash_from_obj(spec),
        "spec": copy.deepcopy(spec),
        "rules_text": rules_text,
        "rules_sha256": hashlib.sha256(rules_text.encode("utf-8")).hexdigest(),
        "execution_policy": "trusted_local_bundle_required",
    }
    signed = {**body, "proposal_id": json_hash(body)}
    return {
        **signed,
        "signature": sign_raw(keypair.private_key, canonical_json(signed)),
    }


def verify_rule_proposal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("rule proposal must be an object")
    signature = value.get("signature")
    signed = {key: item for key, item in value.items() if key != "signature"}
    body = {key: item for key, item in signed.items() if key != "proposal_id"}
    if (
        body.get("artifact_version") != RULE_ARTIFACT_VERSION
        or body.get("artifact_kind") != "aigenora-rule-proposal"
        or body.get("execution_policy") != "trusted_local_bundle_required"
        or signed.get("proposal_id") != json_hash(body)
    ):
        raise ValueError("rule proposal identity is invalid")
    public_key = body.get("proposer_public_key")
    if not _is_public_key(public_key):
        raise ValueError("rule proposal signer is invalid")
    spec = body.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("rule proposal spec must be an object")
    _validate_spec(spec)
    if body.get("protocol_id") != protocol_hash_from_obj(spec):
        raise ValueError("rule proposal protocol_id is invalid")
    rules_text = body.get("rules_text")
    _validate_text(rules_text, MAX_RULE_TEXT_BYTES, "rules text")
    if body.get("rules_sha256") != hashlib.sha256(
        rules_text.encode("utf-8")
    ).hexdigest():
        raise ValueError("rule proposal text hash is invalid")
    _verify_signature(public_key, signed, signature, "rule proposal")
    return copy.deepcopy(value)


def create_rule_endorsement(
    proposal: dict[str, Any],
    *,
    decision: str,
    reason: str,
    keypair: KeyPair,
) -> dict[str, Any]:
    verified = verify_rule_proposal(proposal)
    if decision not in {"accept", "reject"}:
        raise ValueError("rule endorsement decision must be accept or reject")
    _validate_text(reason, MAX_ENDORSEMENT_REASON_BYTES, "endorsement reason")
    body = {
        "artifact_version": RULE_ARTIFACT_VERSION,
        "artifact_kind": "aigenora-rule-endorsement",
        "created_at": _now(),
        "proposal_id": verified["proposal_id"],
        "protocol_id": verified["protocol_id"],
        "signer_public_key": keypair.public_key,
        "decision": decision,
        "reason": reason,
    }
    signed = {**body, "endorsement_id": json_hash(body)}
    return {
        **signed,
        "signature": sign_raw(keypair.private_key, canonical_json(signed)),
    }


def verify_rule_endorsement(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("rule endorsement must be an object")
    signature = value.get("signature")
    signed = {key: item for key, item in value.items() if key != "signature"}
    body = {key: item for key, item in signed.items() if key != "endorsement_id"}
    if (
        body.get("artifact_version") != RULE_ARTIFACT_VERSION
        or body.get("artifact_kind") != "aigenora-rule-endorsement"
        or body.get("decision") not in {"accept", "reject"}
        or signed.get("endorsement_id") != json_hash(body)
        or not _is_hash(body.get("proposal_id"))
        or not _is_hash(body.get("protocol_id"))
    ):
        raise ValueError("rule endorsement identity is invalid")
    public_key = body.get("signer_public_key")
    if not _is_public_key(public_key):
        raise ValueError("rule endorsement signer is invalid")
    _validate_text(
        body.get("reason"), MAX_ENDORSEMENT_REASON_BYTES, "endorsement reason"
    )
    _verify_signature(public_key, signed, signature, "rule endorsement")
    return copy.deepcopy(value)


def freeze_rule_set(
    proposal: dict[str, Any],
    endorsements: Iterable[dict[str, Any]],
    *,
    quorum: int,
    coordinator_keypair: KeyPair,
) -> dict[str, Any]:
    verified_proposal = verify_rule_proposal(proposal)
    if not isinstance(quorum, int) or isinstance(quorum, bool) or not 1 <= quorum <= 32:
        raise ValueError("rule quorum must be between 1 and 32")
    verified_endorsements = [
        verify_rule_endorsement(value) for value in endorsements
    ]
    if not verified_endorsements:
        raise ValueError("at least one rule endorsement is required")
    accepted = _validate_endorsements(
        verified_proposal, verified_endorsements, quorum
    )
    ordered_endorsements = sorted(
        verified_endorsements, key=lambda item: item["endorsement_id"]
    )
    body = {
        "artifact_version": RULE_ARTIFACT_VERSION,
        "artifact_kind": "aigenora-frozen-ruleset",
        "frozen_at": _now(),
        "coordinator_public_key": coordinator_keypair.public_key,
        "proposal": verified_proposal,
        "endorsements": ordered_endorsements,
        "quorum": quorum,
        "accepted_signers": accepted,
        "protocol_id": verified_proposal["protocol_id"],
        "execution_policy": "trusted_local_bundle_required",
    }
    signed = {**body, "ruleset_id": json_hash(body)}
    return {
        **signed,
        "signature": sign_raw(
            coordinator_keypair.private_key, canonical_json(signed)
        ),
    }


def verify_frozen_rule_set(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("frozen ruleset must be an object")
    signature = value.get("signature")
    signed = {key: item for key, item in value.items() if key != "signature"}
    body = {key: item for key, item in signed.items() if key != "ruleset_id"}
    if (
        body.get("artifact_version") != RULE_ARTIFACT_VERSION
        or body.get("artifact_kind") != "aigenora-frozen-ruleset"
        or body.get("execution_policy") != "trusted_local_bundle_required"
        or signed.get("ruleset_id") != json_hash(body)
    ):
        raise ValueError("frozen ruleset identity is invalid")
    coordinator = body.get("coordinator_public_key")
    if not _is_public_key(coordinator):
        raise ValueError("frozen ruleset coordinator is invalid")
    proposal = verify_rule_proposal(body.get("proposal"))
    endorsements = body.get("endorsements")
    if not isinstance(endorsements, list):
        raise ValueError("frozen ruleset endorsements must be an array")
    quorum = body.get("quorum")
    if not isinstance(quorum, int) or isinstance(quorum, bool) or not 1 <= quorum <= 32:
        raise ValueError("frozen ruleset quorum is invalid")
    verified_endorsements = [verify_rule_endorsement(item) for item in endorsements]
    accepted = _validate_endorsements(proposal, verified_endorsements, quorum)
    if (
        body.get("accepted_signers") != accepted
        or body.get("protocol_id") != proposal["protocol_id"]
        or endorsements
        != sorted(verified_endorsements, key=lambda item: item["endorsement_id"])
    ):
        raise ValueError("frozen ruleset endorsement summary is invalid")
    _verify_signature(coordinator, signed, signature, "frozen ruleset")
    return copy.deepcopy(value)


def verify_rule_artifact(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("rule artifact must be an object")
    kind = value.get("artifact_kind")
    if kind == "aigenora-rule-proposal":
        return verify_rule_proposal(value)
    if kind == "aigenora-rule-endorsement":
        return verify_rule_endorsement(value)
    if kind == "aigenora-frozen-ruleset":
        return verify_frozen_rule_set(value)
    raise ValueError("unknown rule artifact kind")


def _validate_endorsements(
    proposal: dict[str, Any],
    endorsements: list[dict[str, Any]],
    quorum: int,
) -> list[str]:
    signers: set[str] = set()
    for endorsement in endorsements:
        if (
            endorsement["proposal_id"] != proposal["proposal_id"]
            or endorsement["protocol_id"] != proposal["protocol_id"]
        ):
            raise ValueError("rule endorsement targets a different proposal")
        signer = endorsement["signer_public_key"]
        if signer in signers:
            raise ValueError("rule endorsements must have unique signers")
        signers.add(signer)
    accepted = sorted(
        endorsement["signer_public_key"]
        for endorsement in endorsements
        if endorsement["decision"] == "accept"
    )
    if len(accepted) < quorum:
        raise ValueError("accepted rule endorsements do not meet quorum")
    return accepted


def _validate_spec(spec: dict[str, Any]) -> None:
    check_spec_version(spec, reject_unknown=True)
    validate_flow(spec)
    validate_timing(spec)


def _validate_text(value: Any, maximum: int, label: str) -> None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} is invalid or exceeds {maximum} bytes")


def _verify_signature(
    public_key: str, body: dict[str, Any], signature: Any, label: str
) -> None:
    if not isinstance(signature, str):
        raise ValueError(f"{label} signature is missing")
    try:
        verify_raw(public_key, canonical_json(body), signature)
    except Exception as exc:
        raise ValueError(f"{label} signature is invalid") from exc


def _is_public_key(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_hash(value: Any) -> bool:
    return _is_public_key(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "RULE_ARTIFACT_VERSION",
    "create_rule_endorsement",
    "create_rule_proposal",
    "freeze_rule_set",
    "verify_frozen_rule_set",
    "verify_rule_artifact",
    "verify_rule_endorsement",
    "verify_rule_proposal",
]
