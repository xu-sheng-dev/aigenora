from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aigenora.ceremony.board import (
    BallotProofVerifier,
    BoardVerification,
    board_record_id,
    verify_board_bundle,
)
from aigenora.ceremony.canonical import parse_canonical_json
from aigenora.ceremony.decision_proof import verify_provisional_decision_proof
from aigenora.ceremony.errors import NON_CANONICAL, VsdpError
from aigenora.ceremony.manifest import (
    PROFILE_ID,
    validate_final_manifest,
    validate_setup_manifest,
)
from aigenora.ceremony.tally import (
    TallyProofVerifier,
    verify_aggregate_authorization,
)


BOARD_BUNDLE_SCHEMA = "vsdp-board-bundle/1"
DECISION_BUNDLE_SCHEMA = "vsdp-decision-bundle/1"


@dataclass(frozen=True)
class DecisionVerification:
    valid: bool
    finalized: bool
    decision_id: str
    accepted_count: int
    checkpoint_hash: str
    proof_hash: str
    tally: tuple[tuple[str, int], ...]
    outcome: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted_count": self.accepted_count,
            "checkpoint_hash": self.checkpoint_hash,
            "decision_id": self.decision_id,
            "finalized": self.finalized,
            "network_privacy": "linkable_by_first_hop",
            "outcome": self.outcome,
            "profile_id": PROFILE_ID,
            "proof_hash": self.proof_hash,
            "schema": "vsdp-decision-verification/1",
            "status": "provisional",
            "tally": [
                {"count": count, "option_id": option_id}
                for option_id, count in self.tally
            ],
            "valid": self.valid,
        }


def verify_board_bundle_value(
    value: dict[str, Any],
    *,
    proof_verifier: BallotProofVerifier,
) -> BoardVerification:
    expected = {
        "checkpoint",
        "final_manifest",
        "minimum_anonymity_set",
        "receipts",
        "records",
        "schema",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise VsdpError(NON_CANONICAL, "invalid Board bundle fields")
    if value["schema"] != BOARD_BUNDLE_SCHEMA:
        raise VsdpError(NON_CANONICAL, "unknown Board bundle schema")
    final_manifest = value["final_manifest"]
    if not isinstance(final_manifest, dict):
        raise VsdpError(NON_CANONICAL, "Final Manifest must be an object")
    validate_final_manifest(final_manifest)
    records = value["records"]
    receipts = value["receipts"]
    checkpoint = value["checkpoint"]
    minimum = value["minimum_anonymity_set"]
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise VsdpError(NON_CANONICAL, "records must be an array of objects")
    if not isinstance(receipts, list) or not all(isinstance(item, dict) for item in receipts):
        raise VsdpError(NON_CANONICAL, "receipts must be an array of objects")
    if not isinstance(checkpoint, dict):
        raise VsdpError(NON_CANONICAL, "checkpoint must be an object")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 3:
        raise VsdpError(NON_CANONICAL, "minimum anonymity set must be an integer >= 3")
    frozen_minimum = final_manifest["privacy"]["minimum_anonymity_set"]
    if minimum != frozen_minimum:
        raise VsdpError(
            NON_CANONICAL,
            "bundle anonymity minimum differs from the frozen Final Manifest",
        )
    return verify_board_bundle(
        final_manifest=final_manifest,
        records=records,
        receipts=receipts,
        checkpoint=checkpoint,
        proof_verifier=proof_verifier,
        minimum_anonymity_set=minimum,
    )


def verify_board_bundle_file(
    path: str | Path,
    *,
    proof_verifier: BallotProofVerifier,
) -> BoardVerification:
    value = parse_canonical_json(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise VsdpError(NON_CANONICAL, "Board bundle must be an object")
    return verify_board_bundle_value(value, proof_verifier=proof_verifier)


def verify_decision_bundle_value(
    value: dict[str, Any],
    *,
    proof_verifier: BallotProofVerifier,
    tally_verifier: TallyProofVerifier,
) -> DecisionVerification:
    expected = {
        "aggregate_authorization",
        "board_bundle",
        "provisional_decision_proof",
        "schema",
        "setup_manifest",
        "tally_result",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise VsdpError(NON_CANONICAL, "invalid Decision bundle fields")
    if value["schema"] != DECISION_BUNDLE_SCHEMA:
        raise VsdpError(NON_CANONICAL, "unknown Decision bundle schema")
    setup_manifest = value["setup_manifest"]
    board_bundle = value["board_bundle"]
    authorization = value["aggregate_authorization"]
    tally_result = value["tally_result"]
    provisional = value["provisional_decision_proof"]
    for field, item in (
        ("setup_manifest", setup_manifest),
        ("board_bundle", board_bundle),
        ("aggregate_authorization", authorization),
        ("tally_result", tally_result),
        ("provisional_decision_proof", provisional),
    ):
        if not isinstance(item, dict):
            raise VsdpError(NON_CANONICAL, f"{field} must be an object")

    validate_setup_manifest(setup_manifest)
    final_manifest = board_bundle.get("final_manifest")
    if not isinstance(final_manifest, dict):
        raise VsdpError(NON_CANONICAL, "Board bundle Final Manifest is missing")
    validate_final_manifest(final_manifest, setup_manifest=setup_manifest)
    board = verify_board_bundle_value(
        board_bundle,
        proof_verifier=proof_verifier,
    )
    option_ids = [
        choice["option_id"] for choice in final_manifest["decision"]["choices"]
    ]
    minimum = final_manifest["privacy"]["minimum_anonymity_set"]
    verify_aggregate_authorization(
        authorization,
        board_verification=board,
        minimum_anonymity_set=minimum,
        option_order=option_ids,
    )
    checkpoint = board_bundle["checkpoint"]
    checkpoint_body = checkpoint["checkpoint"]
    final_root = checkpoint_body["accepted_ballot_root"]
    if (
        authorization["accepted_set_root"] != final_root
        or authorization["final_board_root"] != final_root
        or authorization["dispute_checkpoint_hash"] != board.checkpoint_hash
    ):
        raise VsdpError(
            NON_CANONICAL,
            "AggregateAuthorization is not bound to the verified final checkpoint",
        )

    records_by_id = {
        board_record_id(record): record for record in board_bundle["records"]
    }
    try:
        accepted_records = [
            records_by_id[record_id] for record_id in board.accepted_record_ids
        ]
    except KeyError as exc:
        raise VsdpError(
            NON_CANONICAL,
            "verified accepted record is absent from the Decision bundle",
        ) from exc
    tally_verifier.verify(
        final_manifest=final_manifest,
        records=accepted_records,
        authorization=authorization,
        tally_result=tally_result,
        option_ids=option_ids,
    )
    proof_hash = verify_provisional_decision_proof(
        provisional,
        setup_manifest=setup_manifest,
        final_manifest=final_manifest,
        checkpoint_hash=board.checkpoint_hash,
        authorization=authorization,
        tally_result=tally_result,
    )
    tally = tuple(
        (entry["option_id"], entry["count"]) for entry in provisional["tally"]
    )
    return DecisionVerification(
        valid=True,
        finalized=False,
        decision_id=board.decision_id,
        accepted_count=board.accepted_count,
        checkpoint_hash=board.checkpoint_hash,
        proof_hash=proof_hash,
        tally=tally,
        outcome=dict(provisional["outcome"]),
    )


def verify_decision_bundle_file(
    path: str | Path,
    *,
    proof_verifier: BallotProofVerifier,
    tally_verifier: TallyProofVerifier,
) -> DecisionVerification:
    value = parse_canonical_json(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise VsdpError(NON_CANONICAL, "Decision bundle must be an object")
    return verify_decision_bundle_value(
        value,
        proof_verifier=proof_verifier,
        tally_verifier=tally_verifier,
    )
