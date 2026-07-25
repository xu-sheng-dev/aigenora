from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol

from .board import BoardVerification
from .canonical import (
    canonical_json_bytes,
    domain_hash_hex,
    parse_canonical_json,
    require_hex,
)
from .errors import (
    ANONYMITY_SET_TOO_SMALL,
    CONTEXT_MISMATCH,
    DECRYPTION_REPLAY,
    DECRYPTION_UNAUTHORIZED,
    NON_CANONICAL,
    TALLY_MISMATCH,
    VsdpError,
)
from .manifest import PROFILE_ID


AUTHORIZATION_SCHEMA = "vsdp-aggregate-authorization/1"


class TallyProofVerifier(Protocol):
    def verify(
        self,
        *,
        final_manifest: dict[str, Any],
        records: list[dict[str, Any]],
        authorization: dict[str, Any],
        tally_result: dict[str, Any],
        option_ids: list[str],
    ) -> None:
        """Raise VsdpError unless the public threshold tally is valid."""


class CommandTallyVerifier:
    """Invoke a verify-only tally worker through canonical JSON."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 60.0,
        max_output_bytes: int = 4_000_000,
    ):
        if not command:
            raise ValueError("tally verifier command cannot be empty")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def verify(
        self,
        *,
        final_manifest: dict[str, Any],
        records: list[dict[str, Any]],
        authorization: dict[str, Any],
        tally_result: dict[str, Any],
        option_ids: list[str],
    ) -> None:
        request = {
            "authorization": authorization,
            "final_manifest": final_manifest,
            "operation": "verify_tally",
            "option_ids": option_ids,
            "records": records,
            "schema": "vsdp-crypto-request/1",
            "tally_result": tally_result,
        }
        try:
            completed = subprocess.run(
                self.command,
                input=canonical_json_bytes(request),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VsdpError(TALLY_MISMATCH, f"tally worker failed: {exc}") from exc
        if len(completed.stdout) > self.max_output_bytes:
            raise VsdpError(TALLY_MISMATCH, "tally worker output exceeded the limit")
        if completed.returncode != 0:
            raise VsdpError(
                TALLY_MISMATCH,
                f"tally worker rejected the result with exit code {completed.returncode}",
            )
        try:
            response = parse_canonical_json(completed.stdout)
        except VsdpError as exc:
            raise VsdpError(TALLY_MISMATCH, f"invalid tally worker response: {exc}") from exc
        if (
            not isinstance(response, dict)
            or set(response) != {"schema", "valid"}
            or response.get("schema") != "vsdp-tally-verdict/1"
            or response.get("valid") is not True
        ):
            raise VsdpError(TALLY_MISMATCH, "tally worker returned an invalid verdict")


def build_aggregate_authorization(
    *,
    board_verification: BoardVerification,
    final_board_root: str,
    minimum_anonymity_set: int,
    option_aggregate_hashes: Mapping[str, str],
    option_order: Sequence[str],
    dispute_checkpoint_hash: str,
) -> dict[str, Any]:
    if board_verification.accepted_count < minimum_anonymity_set:
        raise VsdpError(
            ANONYMITY_SET_TOO_SMALL,
            "accepted ballot count is below the frozen anonymity minimum",
        )
    if not board_verification.decryption_ready:
        raise VsdpError(
            DECRYPTION_UNAUTHORIZED,
            "Board verification did not authorize decryption",
        )
    require_hex(final_board_root, 64, "final_board_root")
    require_hex(dispute_checkpoint_hash, 64, "dispute_checkpoint_hash")
    if not option_aggregate_hashes:
        raise VsdpError(NON_CANONICAL, "at least one option aggregate is required")
    if (
        len(option_order) != len(option_aggregate_hashes)
        or len(set(option_order)) != len(option_order)
        or set(option_order) != set(option_aggregate_hashes)
    ):
        raise VsdpError(
            NON_CANONICAL,
            "option_order must contain every aggregate option exactly once",
        )
    normalized: list[dict[str, str]] = []
    for option_id in option_order:
        if not isinstance(option_id, str) or not option_id:
            raise VsdpError(NON_CANONICAL, "option id must be a non-empty string")
        aggregate_hash = require_hex(
            option_aggregate_hashes[option_id],
            64,
            f"option aggregate {option_id}",
        )
        normalized.append({"aggregate_hash": aggregate_hash, "option_id": option_id})
    body = {
        "accepted_count": board_verification.accepted_count,
        "accepted_set_root": final_board_root,
        "decision_id": board_verification.decision_id,
        "dispute_checkpoint_hash": dispute_checkpoint_hash,
        "final_board_root": final_board_root,
        "minimum_anonymity_set": minimum_anonymity_set,
        "option_aggregates": normalized,
        "profile_id": PROFILE_ID,
        "schema": AUTHORIZATION_SCHEMA,
    }
    return {
        **body,
        "authorization_hash": domain_hash_hex(
            "vsdp/aggregate-authorization/v1",
            body,
        ),
    }


def verify_aggregate_authorization(
    value: dict[str, Any],
    *,
    board_verification: BoardVerification,
    minimum_anonymity_set: int,
    option_order: Sequence[str],
) -> str:
    expected = {
        "accepted_count",
        "accepted_set_root",
        "authorization_hash",
        "decision_id",
        "dispute_checkpoint_hash",
        "final_board_root",
        "minimum_anonymity_set",
        "option_aggregates",
        "profile_id",
        "schema",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise VsdpError(NON_CANONICAL, "invalid AggregateAuthorization fields")
    if value["schema"] != AUTHORIZATION_SCHEMA or value["profile_id"] != PROFILE_ID:
        raise VsdpError(CONTEXT_MISMATCH, "unsupported AggregateAuthorization profile")
    if value["decision_id"] != board_verification.decision_id:
        raise VsdpError(CONTEXT_MISMATCH, "authorization decision mismatch")
    if value["accepted_count"] != board_verification.accepted_count:
        raise VsdpError(CONTEXT_MISMATCH, "authorization accepted count mismatch")
    if value["minimum_anonymity_set"] != minimum_anonymity_set:
        raise VsdpError(CONTEXT_MISMATCH, "authorization anonymity minimum mismatch")
    if value["accepted_count"] < minimum_anonymity_set:
        raise VsdpError(ANONYMITY_SET_TOO_SMALL, "authorization is below anonymity minimum")
    if not isinstance(value["option_aggregates"], list) or not value["option_aggregates"]:
        raise VsdpError(NON_CANONICAL, "authorization option aggregates are missing")
    option_ids: list[str] = []
    for item in value["option_aggregates"]:
        if not isinstance(item, dict) or set(item) != {"aggregate_hash", "option_id"}:
            raise VsdpError(NON_CANONICAL, "invalid option aggregate entry")
        require_hex(item["aggregate_hash"], 64, "aggregate_hash")
        option_ids.append(item["option_id"])
    if option_ids != list(option_order) or len(set(option_ids)) != len(option_ids):
        raise VsdpError(
            NON_CANONICAL,
            "option aggregates do not follow the frozen Final Manifest order",
        )
    unsigned = dict(value)
    authorization_hash = unsigned.pop("authorization_hash")
    expected_hash = domain_hash_hex("vsdp/aggregate-authorization/v1", unsigned)
    if authorization_hash != expected_hash:
        raise VsdpError(CONTEXT_MISMATCH, "authorization hash mismatch")
    return authorization_hash


class GuardianAuthorizationLedger:
    """Durable one-authorization-per-decision lock.

    The cryptographic worker must acquire this lock before producing any
    decryption share. A conflicting authorization is a permanent dispute, not
    a retry.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS authorization_locks (
                    decision_id TEXT PRIMARY KEY,
                    authorization_hash TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def acquire(self, decision_id: str, authorization_hash: str) -> bool:
        require_hex(decision_id, 64, "decision_id")
        require_hex(authorization_hash, 64, "authorization_hash")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT authorization_hash FROM authorization_locks WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is not None:
                connection.execute("COMMIT")
                if row[0] != authorization_hash:
                    raise VsdpError(
                        DECRYPTION_REPLAY,
                        "a different aggregate was already authorized for this decision",
                    )
                return False
            connection.execute(
                "INSERT INTO authorization_locks(decision_id,authorization_hash) VALUES(?,?)",
                (decision_id, authorization_hash),
            )
            connection.execute("COMMIT")
            return True
