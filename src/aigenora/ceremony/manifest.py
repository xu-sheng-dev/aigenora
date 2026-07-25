from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from .canonical import domain_hash_hex, require_hex, sha256_hex
from .errors import CONTEXT_MISMATCH, NON_CANONICAL, UNKNOWN_PROFILE, VsdpError


PROFILE_ID = "vsdp-low-coercion-single-choice-v1"
SETUP_SCHEMA = "vsdp-setup/1"
FINAL_SCHEMA = "vsdp-final/1"
NETWORK_PRIVACY = "linkable_by_first_hop"
WITNESS_COUNT = 4
WITNESS_QUORUM = 3
GUARDIAN_COUNT = 5
GUARDIAN_THRESHOLD = 3
TREE_DEPTH = 20

_MACHINE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

PROTOCOL_DESCRIPTOR: dict[str, Any] = {
    "execution": {
        "ceremony": "verifiable_secret_decision",
        "kind": "native_ceremony",
        "profile": PROFILE_ID,
    },
    "runtime": "aigenora-vsdp/1",
    "schema": "vsdp-protocol/1",
}


def protocol_id() -> str:
    return domain_hash_hex("vsdp/protocol/v1", PROTOCOL_DESCRIPTOR)


def _exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    if not isinstance(value, dict):
        raise VsdpError(NON_CANONICAL, f"{field} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise VsdpError(
            NON_CANONICAL,
            f"{field} keys mismatch; missing={missing}, unknown={unknown}",
        )


def _machine_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _MACHINE_ID.fullmatch(value):
        raise VsdpError(NON_CANONICAL, f"{field} is not a valid machine id")
    return value


def _positive_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise VsdpError(NON_CANONICAL, f"{field} must be an integer >= {minimum}")
    return value


def _public_keys(
    values: Iterable[str],
    expected: int,
    field: str,
    *,
    require_sorted: bool = False,
) -> list[str]:
    result = list(values)
    if len(result) != expected or len(set(result)) != expected:
        raise VsdpError(NON_CANONICAL, f"{field} must contain {expected} unique public keys")
    for index, value in enumerate(result):
        require_hex(value, 64, f"{field}[{index}]")
    ordered = sorted(result)
    if require_sorted and result != ordered:
        raise VsdpError(NON_CANONICAL, f"{field} must use canonical key order")
    return ordered


def _digest_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VsdpError(NON_CANONICAL, f"{field} must be a non-empty string")
    return sha256_hex(value.strip().encode("utf-8"))


def build_setup_manifest(
    *,
    question: str,
    choices: list[tuple[str, str]],
    guardian_public_keys: Iterable[str],
    witness_public_keys: Iterable[str],
    roster_snapshot_commitment: str,
    minimum_anonymity_set: int = 20,
    enrollment_duration_seconds: int = 604800,
    voting_duration_seconds: int = 172800,
    dispute_duration_seconds: int = 86400,
    challenge_source: str = "physical_coin_after_receipt",
) -> dict[str, Any]:
    if len(choices) < 2 or len(choices) > 16:
        raise VsdpError(NON_CANONICAL, "choices must contain between 2 and 16 options")
    normalized_choices: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, (option_id, summary) in enumerate(choices):
        option_id = _machine_id(option_id, f"choices[{index}].option_id")
        if option_id in seen_ids:
            raise VsdpError(NON_CANONICAL, f"duplicate option id: {option_id}")
        seen_ids.add(option_id)
        normalized_choices.append(
            {
                "option_id": option_id,
                "position": index,
                "summary_digest": _digest_text(summary, f"choices[{index}].summary"),
            }
        )

    if challenge_source not in {
        "physical_coin_after_receipt",
        "second_device_after_receipt",
    }:
        raise VsdpError(NON_CANONICAL, "unsupported challenge source")

    setup = {
        "bulletin_board": {
            "checkpoint_quorum": WITNESS_QUORUM,
            "profile": "static-witness-board-v1",
            "receipt_quorum": WITNESS_QUORUM,
            "witness_count": WITNESS_COUNT,
            "witness_public_keys": _public_keys(
                witness_public_keys, WITNESS_COUNT, "witness_public_keys"
            ),
        },
        "decision": {
            "choices": normalized_choices,
            "pass_rule": {"kind": "plurality"},
            "question_digest": _digest_text(question, "question"),
            "selection_limit": 1,
            "tie_rule": "no_outcome",
            "type": "single_choice",
        },
        "eligibility": {
            "max_enrollments_per_member": 1,
            "profile": "semaphore-v4-e1",
            "roster_mode": "auditable",
            "roster_snapshot_commitment": require_hex(
                roster_snapshot_commitment, 64, "roster_snapshot_commitment"
            ),
            "tree_depth": TREE_DEPTH,
        },
        "guardian": {
            "count": GUARDIAN_COUNT,
            "public_keys": _public_keys(
                guardian_public_keys, GUARDIAN_COUNT, "guardian_public_keys"
            ),
            "threshold": GUARDIAN_THRESHOLD,
        },
        "kind": "verifiable_secret_decision_setup",
        "privacy": {
            "challenge_source": challenge_source,
            "minimum_anonymity_set": _positive_int(
                minimum_anonymity_set, "minimum_anonymity_set", minimum=3
            ),
            "network_privacy": NETWORK_PRIVACY,
            "receipt_policy": "inclusion-only-no-receipt-free-claim",
            "submission_profile": "batched-relay-v1",
        },
        "profile_id": PROFILE_ID,
        "protocol_id": protocol_id(),
        "schedule_policy": {
            "dispute_duration_seconds": _positive_int(
                dispute_duration_seconds, "dispute_duration_seconds"
            ),
            "enrollment_duration_seconds": _positive_int(
                enrollment_duration_seconds, "enrollment_duration_seconds"
            ),
            "voting_duration_seconds": _positive_int(
                voting_duration_seconds, "voting_duration_seconds"
            ),
        },
        "schema_version": SETUP_SCHEMA,
        "software": {
            "crypto_profile": "semaphore-4.14.3+artifacts-4.13.0+elastic-elgamal-0.3.1",
            "runtime_profile": "aigenora-vsdp/1",
            "verifier_profile": "aigenora-vsdp-verifier/1",
        },
        "tally": {
            "profile": "elastic-elgamal-ristretto-single-choice-v1",
        },
    }
    validate_setup_manifest(setup)
    return setup


def validate_setup_manifest(value: dict[str, Any]) -> None:
    expected = {
        "bulletin_board",
        "decision",
        "eligibility",
        "guardian",
        "kind",
        "privacy",
        "profile_id",
        "protocol_id",
        "schedule_policy",
        "schema_version",
        "software",
        "tally",
    }
    _exact_keys(value, expected, "setup manifest")
    if value["kind"] != "verifiable_secret_decision_setup" or value["schema_version"] != SETUP_SCHEMA:
        raise VsdpError(CONTEXT_MISMATCH, "invalid setup kind or schema")
    if value["profile_id"] != PROFILE_ID:
        raise VsdpError(UNKNOWN_PROFILE, f"unsupported profile: {value['profile_id']}")
    if value["protocol_id"] != protocol_id():
        raise VsdpError(CONTEXT_MISMATCH, "protocol_id does not match the profile descriptor")
    board = value["bulletin_board"]
    _exact_keys(
        board,
        {
            "checkpoint_quorum",
            "profile",
            "receipt_quorum",
            "witness_count",
            "witness_public_keys",
        },
        "bulletin_board",
    )
    if (
        board.get("witness_count") != WITNESS_COUNT
        or board.get("receipt_quorum") != WITNESS_QUORUM
        or board.get("checkpoint_quorum") != WITNESS_QUORUM
        or board.get("profile") != "static-witness-board-v1"
    ):
        raise VsdpError(CONTEXT_MISMATCH, "unsupported Bulletin Board parameters")
    _public_keys(
        board.get("witness_public_keys", []),
        WITNESS_COUNT,
        "witness_public_keys",
        require_sorted=True,
    )
    guardian = value["guardian"]
    _exact_keys(guardian, {"count", "public_keys", "threshold"}, "guardian")
    if guardian.get("count") != GUARDIAN_COUNT or guardian.get("threshold") != GUARDIAN_THRESHOLD:
        raise VsdpError(CONTEXT_MISMATCH, "unsupported Guardian parameters")
    _public_keys(
        guardian.get("public_keys", []),
        GUARDIAN_COUNT,
        "guardian_public_keys",
        require_sorted=True,
    )
    privacy = value["privacy"]
    _exact_keys(
        privacy,
        {
            "challenge_source",
            "minimum_anonymity_set",
            "network_privacy",
            "receipt_policy",
            "submission_profile",
        },
        "privacy",
    )
    if privacy.get("network_privacy") != NETWORK_PRIVACY:
        raise VsdpError(CONTEXT_MISMATCH, "L0/L1 network privacy declaration cannot be upgraded")
    if privacy.get("challenge_source") not in {
        "physical_coin_after_receipt",
        "second_device_after_receipt",
    }:
        raise VsdpError(CONTEXT_MISMATCH, "unsupported challenge source")
    if privacy.get("receipt_policy") != "inclusion-only-no-receipt-free-claim":
        raise VsdpError(CONTEXT_MISMATCH, "unsupported receipt policy")
    if privacy.get("submission_profile") != "batched-relay-v1":
        raise VsdpError(CONTEXT_MISMATCH, "unsupported submission profile")
    _positive_int(privacy.get("minimum_anonymity_set"), "minimum_anonymity_set", minimum=3)
    decision = value["decision"]
    _exact_keys(
        decision,
        {
            "choices",
            "pass_rule",
            "question_digest",
            "selection_limit",
            "tie_rule",
            "type",
        },
        "decision",
    )
    if decision.get("type") != "single_choice" or decision.get("selection_limit") != 1:
        raise VsdpError(CONTEXT_MISMATCH, "only strict single-choice decisions are supported")
    if decision.get("tie_rule") != "no_outcome":
        raise VsdpError(CONTEXT_MISMATCH, "unsupported tie rule")
    pass_rule = decision.get("pass_rule")
    _exact_keys(pass_rule, {"kind"}, "decision.pass_rule")
    if pass_rule.get("kind") != "plurality":
        raise VsdpError(CONTEXT_MISMATCH, "unsupported pass rule")
    require_hex(decision.get("question_digest"), 64, "question_digest")
    choices = decision.get("choices")
    if not isinstance(choices, list) or not 2 <= len(choices) <= 16:
        raise VsdpError(NON_CANONICAL, "invalid choices")
    seen_ids: set[str] = set()
    for index, choice in enumerate(choices):
        _exact_keys(choice, {"option_id", "position", "summary_digest"}, f"choice[{index}]")
        option_id = _machine_id(choice["option_id"], f"choice[{index}].option_id")
        if option_id in seen_ids:
            raise VsdpError(NON_CANONICAL, f"duplicate option id: {option_id}")
        seen_ids.add(option_id)
        if choice["position"] != index:
            raise VsdpError(NON_CANONICAL, "choice position must match array order")
        require_hex(choice["summary_digest"], 64, f"choice[{index}].summary_digest")
    eligibility = value["eligibility"]
    _exact_keys(
        eligibility,
        {
            "max_enrollments_per_member",
            "profile",
            "roster_mode",
            "roster_snapshot_commitment",
            "tree_depth",
        },
        "eligibility",
    )
    if (
        eligibility.get("max_enrollments_per_member") != 1
        or eligibility.get("profile") != "semaphore-v4-e1"
        or eligibility.get("roster_mode") != "auditable"
        or eligibility.get("tree_depth") != TREE_DEPTH
    ):
        raise VsdpError(CONTEXT_MISMATCH, "unsupported eligibility parameters")
    require_hex(
        eligibility.get("roster_snapshot_commitment"),
        64,
        "roster_snapshot_commitment",
    )
    schedule = value["schedule_policy"]
    _exact_keys(
        schedule,
        {
            "dispute_duration_seconds",
            "enrollment_duration_seconds",
            "voting_duration_seconds",
        },
        "schedule_policy",
    )
    for field in (
        "dispute_duration_seconds",
        "enrollment_duration_seconds",
        "voting_duration_seconds",
    ):
        _positive_int(schedule.get(field), field)
    software = value["software"]
    _exact_keys(
        software,
        {"crypto_profile", "runtime_profile", "verifier_profile"},
        "software",
    )
    if software != {
        "crypto_profile": "semaphore-4.14.3+artifacts-4.13.0+elastic-elgamal-0.3.1",
        "runtime_profile": "aigenora-vsdp/1",
        "verifier_profile": "aigenora-vsdp-verifier/1",
    }:
        raise VsdpError(CONTEXT_MISMATCH, "unsupported software profile")
    tally = value["tally"]
    _exact_keys(tally, {"profile"}, "tally")
    if tally.get("profile") != "elastic-elgamal-ristretto-single-choice-v1":
        raise VsdpError(CONTEXT_MISMATCH, "unsupported tally profile")


def ceremony_id(setup_manifest: dict[str, Any]) -> str:
    validate_setup_manifest(setup_manifest)
    return domain_hash_hex("vsdp/setup/v1", setup_manifest)


def _utc_seconds(value: datetime | str, field: str) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise VsdpError(NON_CANONICAL, f"{field} must be UTC RFC3339 seconds") from exc
    elif isinstance(value, datetime):
        if value.tzinfo is None:
            raise VsdpError(NON_CANONICAL, f"{field} must be timezone-aware")
        parsed = value.astimezone(timezone.utc)
        if parsed.microsecond:
            raise VsdpError(NON_CANONICAL, f"{field} cannot contain fractional seconds")
    else:
        raise VsdpError(NON_CANONICAL, f"{field} must be a datetime or string")
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_final_manifest(
    *,
    setup_manifest: dict[str, Any],
    eligibility_root: str,
    enrollment_count: int,
    tally_public_key: str,
    dkg_transcript_root: str,
    board_epoch: str,
    board_genesis_hash: str,
    setup_artifact_root: str,
    vote_opens_at: datetime | str,
    vote_closes_at: datetime | str,
    dispute_closes_at: datetime | str,
) -> dict[str, Any]:
    validate_setup_manifest(setup_manifest)
    minimum = setup_manifest["privacy"]["minimum_anonymity_set"]
    if _positive_int(enrollment_count, "enrollment_count") < minimum:
        raise VsdpError(
            "VSDP_ANONYMITY_SET_TOO_SMALL",
            f"enrollment_count {enrollment_count} is below minimum {minimum}",
        )
    if not isinstance(eligibility_root, str) or not eligibility_root.isdigit():
        raise VsdpError(NON_CANONICAL, "eligibility_root must be a decimal field element string")
    if str(int(eligibility_root)) != eligibility_root:
        raise VsdpError(NON_CANONICAL, "eligibility_root must use minimal decimal encoding")
    if not isinstance(tally_public_key, str) or not tally_public_key:
        raise VsdpError(NON_CANONICAL, "tally_public_key must be a non-empty encoded string")

    opens = _utc_seconds(vote_opens_at, "vote_opens_at")
    closes = _utc_seconds(vote_closes_at, "vote_closes_at")
    disputes = _utc_seconds(dispute_closes_at, "dispute_closes_at")
    if not opens < closes < disputes:
        raise VsdpError(NON_CANONICAL, "schedule must satisfy opens < closes < dispute close")

    final = {
        "board_epoch": _machine_id(board_epoch, "board_epoch"),
        "board_genesis_hash": require_hex(board_genesis_hash, 64, "board_genesis_hash"),
        "ceremony_id": ceremony_id(setup_manifest),
        "dkg_transcript_root": require_hex(
            dkg_transcript_root, 64, "dkg_transcript_root"
        ),
        "decision": dict(setup_manifest["decision"]),
        "eligibility_root": eligibility_root,
        "enrollment_count": enrollment_count,
        "kind": "verifiable_secret_decision_final",
        "profile_id": PROFILE_ID,
        "privacy": dict(setup_manifest["privacy"]),
        "role_public_keys": {
            "guardians": list(setup_manifest["guardian"]["public_keys"]),
            "witnesses": list(setup_manifest["bulletin_board"]["witness_public_keys"]),
        },
        "schedule": {
            "dispute_closes_at": disputes,
            "vote_closes_at": closes,
            "vote_opens_at": opens,
        },
        "schema_version": FINAL_SCHEMA,
        "setup_artifact_root": require_hex(
            setup_artifact_root, 64, "setup_artifact_root"
        ),
        "setup_manifest_hash": sha256_hex(_setup_bytes(setup_manifest)),
        "software": dict(setup_manifest["software"]),
        "tally_public_key": tally_public_key,
        "tally_public_key_id": domain_hash_hex(
            "vsdp/tally-public-key/v1", {"encoded_key": tally_public_key}
        ),
        "tree_depth": TREE_DEPTH,
    }
    validate_final_manifest(final, setup_manifest=setup_manifest)
    return final


def _setup_bytes(setup_manifest: dict[str, Any]) -> bytes:
    from .canonical import canonical_json_bytes

    return canonical_json_bytes(setup_manifest)


def validate_final_manifest(
    value: dict[str, Any],
    *,
    setup_manifest: dict[str, Any] | None = None,
) -> None:
    expected = {
        "board_epoch",
        "board_genesis_hash",
        "ceremony_id",
        "dkg_transcript_root",
        "decision",
        "eligibility_root",
        "enrollment_count",
        "kind",
        "profile_id",
        "privacy",
        "role_public_keys",
        "schedule",
        "schema_version",
        "setup_artifact_root",
        "setup_manifest_hash",
        "software",
        "tally_public_key",
        "tally_public_key_id",
        "tree_depth",
    }
    _exact_keys(value, expected, "final manifest")
    if value["kind"] != "verifiable_secret_decision_final" or value["schema_version"] != FINAL_SCHEMA:
        raise VsdpError(CONTEXT_MISMATCH, "invalid final kind or schema")
    if value["profile_id"] != PROFILE_ID:
        raise VsdpError(UNKNOWN_PROFILE, f"unsupported profile: {value['profile_id']}")
    for field in (
        "board_genesis_hash",
        "ceremony_id",
        "dkg_transcript_root",
        "setup_artifact_root",
        "setup_manifest_hash",
        "tally_public_key_id",
    ):
        require_hex(value[field], 64, field)
    if value["tree_depth"] != TREE_DEPTH:
        raise VsdpError(CONTEXT_MISMATCH, "unexpected Semaphore tree depth")
    decision = value["decision"]
    if not isinstance(decision, dict):
        raise VsdpError(NON_CANONICAL, "Final Manifest decision must be an object")
    _exact_keys(
        decision,
        {
            "choices",
            "pass_rule",
            "question_digest",
            "selection_limit",
            "tie_rule",
            "type",
        },
        "Final Manifest decision",
    )
    if (
        decision.get("type") != "single_choice"
        or decision.get("selection_limit") != 1
        or decision.get("tie_rule") != "no_outcome"
    ):
        raise VsdpError(CONTEXT_MISMATCH, "unsupported Final decision rule")
    _exact_keys(decision.get("pass_rule"), {"kind"}, "Final Manifest pass_rule")
    if decision["pass_rule"].get("kind") != "plurality":
        raise VsdpError(CONTEXT_MISMATCH, "unsupported Final pass rule")
    require_hex(decision.get("question_digest"), 64, "question_digest")
    choices = decision.get("choices")
    if not isinstance(choices, list) or not 2 <= len(choices) <= 16:
        raise VsdpError(NON_CANONICAL, "Final Manifest choices are invalid")
    seen_ids: set[str] = set()
    for index, choice in enumerate(choices):
        if not isinstance(choice, dict):
            raise VsdpError(NON_CANONICAL, f"Final Manifest choice[{index}] must be an object")
        _exact_keys(
            choice,
            {"option_id", "position", "summary_digest"},
            f"Final Manifest choice[{index}]",
        )
        option_id = _machine_id(
            choice["option_id"], f"Final Manifest choice[{index}].option_id"
        )
        if option_id in seen_ids:
            raise VsdpError(NON_CANONICAL, f"duplicate option id: {option_id}")
        seen_ids.add(option_id)
        if choice["position"] != index:
            raise VsdpError(NON_CANONICAL, "Final Manifest choice order is invalid")
        require_hex(
            choice["summary_digest"],
            64,
            f"Final Manifest choice[{index}].summary_digest",
        )
    privacy = value["privacy"]
    if not isinstance(privacy, dict):
        raise VsdpError(NON_CANONICAL, "Final Manifest privacy must be an object")
    _exact_keys(
        privacy,
        {
            "challenge_source",
            "minimum_anonymity_set",
            "network_privacy",
            "receipt_policy",
            "submission_profile",
        },
        "Final Manifest privacy",
    )
    if privacy.get("network_privacy") != NETWORK_PRIVACY:
        raise VsdpError(CONTEXT_MISMATCH, "Final Manifest overstates network privacy")
    if privacy.get("challenge_source") not in {
        "physical_coin_after_receipt",
        "second_device_after_receipt",
    }:
        raise VsdpError(CONTEXT_MISMATCH, "unsupported Final challenge source")
    if (
        privacy.get("receipt_policy") != "inclusion-only-no-receipt-free-claim"
        or privacy.get("submission_profile") != "batched-relay-v1"
    ):
        raise VsdpError(CONTEXT_MISMATCH, "unsupported Final privacy policy")
    _positive_int(privacy.get("minimum_anonymity_set"), "minimum_anonymity_set", minimum=3)
    if not isinstance(value["eligibility_root"], str) or not value["eligibility_root"].isdigit():
        raise VsdpError(NON_CANONICAL, "invalid eligibility root")
    if str(int(value["eligibility_root"])) != value["eligibility_root"]:
        raise VsdpError(NON_CANONICAL, "eligibility root is not minimally encoded")
    _positive_int(value["enrollment_count"], "enrollment_count")
    if value["enrollment_count"] < privacy["minimum_anonymity_set"]:
        raise VsdpError(
            "VSDP_ANONYMITY_SET_TOO_SMALL",
            "enrollment count is below the frozen minimum",
        )
    _machine_id(value["board_epoch"], "board_epoch")
    role_public_keys = value["role_public_keys"]
    _exact_keys(
        role_public_keys,
        {"guardians", "witnesses"},
        "Final Manifest role_public_keys",
    )
    _public_keys(
        role_public_keys.get("guardians", []),
        GUARDIAN_COUNT,
        "Final Manifest guardian public keys",
        require_sorted=True,
    )
    _public_keys(
        role_public_keys.get("witnesses", []),
        WITNESS_COUNT,
        "Final Manifest Witness public keys",
        require_sorted=True,
    )
    schedule = value["schedule"]
    _exact_keys(
        schedule,
        {"dispute_closes_at", "vote_closes_at", "vote_opens_at"},
        "Final Manifest schedule",
    )
    opens = _utc_seconds(schedule.get("vote_opens_at"), "vote_opens_at")
    closes = _utc_seconds(schedule.get("vote_closes_at"), "vote_closes_at")
    disputes = _utc_seconds(
        schedule.get("dispute_closes_at"), "dispute_closes_at"
    )
    if not opens < closes < disputes:
        raise VsdpError(NON_CANONICAL, "Final schedule order is invalid")
    software = value["software"]
    _exact_keys(
        software,
        {"crypto_profile", "runtime_profile", "verifier_profile"},
        "Final Manifest software",
    )
    if software != {
        "crypto_profile": "semaphore-4.14.3+artifacts-4.13.0+elastic-elgamal-0.3.1",
        "runtime_profile": "aigenora-vsdp/1",
        "verifier_profile": "aigenora-vsdp-verifier/1",
    }:
        raise VsdpError(CONTEXT_MISMATCH, "unsupported Final software profile")
    if not isinstance(value["tally_public_key"], str) or not value["tally_public_key"]:
        raise VsdpError(NON_CANONICAL, "invalid tally public key")
    expected_key_id = domain_hash_hex(
        "vsdp/tally-public-key/v1", {"encoded_key": value["tally_public_key"]}
    )
    if value["tally_public_key_id"] != expected_key_id:
        raise VsdpError(CONTEXT_MISMATCH, "tally_public_key_id mismatch")
    if setup_manifest is not None:
        validate_setup_manifest(setup_manifest)
        if value["ceremony_id"] != ceremony_id(setup_manifest):
            raise VsdpError(CONTEXT_MISMATCH, "final manifest references the wrong ceremony")
        if value["setup_manifest_hash"] != sha256_hex(_setup_bytes(setup_manifest)):
            raise VsdpError(CONTEXT_MISMATCH, "setup manifest hash mismatch")
        if value["decision"] != setup_manifest["decision"]:
            raise VsdpError(CONTEXT_MISMATCH, "decision rules changed after setup")
        if value["privacy"] != setup_manifest["privacy"]:
            raise VsdpError(CONTEXT_MISMATCH, "privacy policy changed after setup")
        if value["role_public_keys"]["guardians"] != setup_manifest["guardian"]["public_keys"]:
            raise VsdpError(CONTEXT_MISMATCH, "Guardian set changed after setup")
        if (
            value["role_public_keys"]["witnesses"]
            != setup_manifest["bulletin_board"]["witness_public_keys"]
        ):
            raise VsdpError(CONTEXT_MISMATCH, "Witness set changed after setup")
        minimum = setup_manifest["privacy"]["minimum_anonymity_set"]
        if value["enrollment_count"] < minimum:
            raise VsdpError(
                "VSDP_ANONYMITY_SET_TOO_SMALL",
                "enrollment count is below the frozen minimum",
            )


def decision_id(final_manifest: dict[str, Any]) -> str:
    validate_final_manifest(final_manifest)
    return domain_hash_hex("vsdp/final/v1", final_manifest)
