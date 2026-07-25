from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import (
    canonical_json_bytes,
    domain_hash,
    domain_hash_hex,
    parse_canonical_json,
    require_hex,
    sha256_hex,
)
from .errors import (
    BALLOT_PROOF_INVALID,
    BOARD_FORK,
    CANDIDATE_DIGEST_MISMATCH,
    CONTEXT_MISMATCH,
    NON_CANONICAL,
    NULLIFIER_CONFLICT,
    RECEIPT_QUORUM_MISSING,
    RECEIPTED_RECORD_MISSING,
    VOTING_CLOSED,
    VOTING_NOT_OPEN,
    VsdpError,
)
from .manifest import (
    PROFILE_ID,
    WITNESS_QUORUM,
    decision_id,
    validate_final_manifest,
)


BALLOT_SCHEMA = "vsdp-ballot/1"
ACCEPTANCE_SCHEMA = "vsdp-board-acceptance/1"
RECEIPT_SCHEMA = "vsdp-quorum-receipt/1"
CHECKPOINT_SCHEMA = "vsdp-checkpoint/1"
CHECKPOINT_SIGNATURE_SCHEMA = "vsdp-checkpoint-signature/1"


class BallotProofVerifier(Protocol):
    def verify(self, record: dict[str, Any], final_manifest: dict[str, Any]) -> None:
        """Raise VsdpError if any cryptographic proof is invalid."""


class RejectingProofVerifier:
    """Fail-closed default used when no audited worker is configured."""

    def verify(self, record: dict[str, Any], final_manifest: dict[str, Any]) -> None:
        raise VsdpError(
            BALLOT_PROOF_INVALID,
            "no external cryptographic verifier is configured",
        )


class CommandProofVerifier:
    """Invoke a verify-only worker without shell parsing.

    The command receives one canonical JSON request on stdin and must return one
    canonical JSON object. A successful response is exactly
    ``{"schema":"vsdp-proof-verdict/1","valid":true}`` with optional
    ``details``. The process cannot make a record valid through warnings.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
        max_output_bytes: int = 1_000_000,
    ):
        if not command:
            raise ValueError("proof verifier command cannot be empty")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def verify(self, record: dict[str, Any], final_manifest: dict[str, Any]) -> None:
        request = {
            "final_manifest": final_manifest,
            "operation": "verify_ballot",
            "record": record,
            "schema": "vsdp-proof-request/1",
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
            raise VsdpError(BALLOT_PROOF_INVALID, f"proof worker failed: {exc}") from exc
        if len(completed.stdout) > self.max_output_bytes:
            raise VsdpError(BALLOT_PROOF_INVALID, "proof worker output exceeded the limit")
        if completed.returncode != 0:
            raise VsdpError(
                BALLOT_PROOF_INVALID,
                f"proof worker rejected the ballot with exit code {completed.returncode}",
            )
        try:
            response = parse_canonical_json(completed.stdout)
        except VsdpError as exc:
            raise VsdpError(BALLOT_PROOF_INVALID, f"invalid proof worker response: {exc}") from exc
        if not isinstance(response, dict):
            raise VsdpError(BALLOT_PROOF_INVALID, "proof worker response must be an object")
        allowed = {"schema", "valid", "details"}
        if set(response) - allowed:
            raise VsdpError(BALLOT_PROOF_INVALID, "proof worker response has unknown fields")
        if response.get("schema") != "vsdp-proof-verdict/1" or response.get("valid") is not True:
            raise VsdpError(BALLOT_PROOF_INVALID, "proof worker returned an invalid verdict")


@dataclass(frozen=True)
class WitnessKey:
    public_key: str
    private_key: str

    @property
    def witness_id(self) -> str:
        return domain_hash_hex(
            "vsdp/witness-id/v1",
            {"public_key": self.public_key},
        )

    def sign(self, payload: bytes) -> str:
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.private_key))
        return key.sign(payload).hex()


def generate_witness_key(path: str | Path, *, force: bool = False) -> WitnessKey:
    target = Path(path)
    if target.exists() and not force:
        return load_witness_key(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key = WitnessKey(public_key=public_raw.hex(), private_key=private_raw.hex())
    raw = canonical_json_bytes(
        {
            "private_key": key.private_key,
            "public_key": key.public_key,
            "schema": "vsdp-witness-key/1",
        }
    )
    fd, tmp_name = tempfile.mkstemp(prefix=".witness-key.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return key


def load_witness_key(path: str | Path) -> WitnessKey:
    value = parse_canonical_json(Path(path).read_bytes())
    if not isinstance(value, dict) or set(value) != {"private_key", "public_key", "schema"}:
        raise VsdpError(NON_CANONICAL, "invalid Witness key file")
    if value["schema"] != "vsdp-witness-key/1":
        raise VsdpError(NON_CANONICAL, "unknown Witness key schema")
    public = require_hex(value["public_key"], 64, "public_key")
    private = require_hex(value["private_key"], 64, "private_key")
    derived = (
        Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private))
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    if derived != public:
        raise VsdpError(CONTEXT_MISMATCH, "Witness public/private key mismatch")
    return WitnessKey(public_key=public, private_key=private)


def witness_id_for_public_key(public_key: str) -> str:
    require_hex(public_key, 64, "Witness public key")
    return domain_hash_hex("vsdp/witness-id/v1", {"public_key": public_key})


def witness_map(final_manifest: dict[str, Any]) -> dict[str, str]:
    validate_final_manifest(final_manifest)
    keys = final_manifest["role_public_keys"]["witnesses"]
    return {witness_id_for_public_key(key): key for key in keys}


def candidate_digest(record: dict[str, Any], final_manifest: dict[str, Any]) -> str:
    unsigned_candidate = record.get("candidate_record")
    if not isinstance(unsigned_candidate, dict):
        raise VsdpError(NON_CANONICAL, "candidate_record must be an object")
    return domain_hash_hex(
        "vsdp/ballot-binding/v1",
        {
            "candidate_record": unsigned_candidate,
            "decision_id": decision_id(final_manifest),
            "final_manifest_hash": sha256_hex(canonical_json_bytes(final_manifest)),
            "profile_id": PROFILE_ID,
            "tally_public_key_id": final_manifest["tally_public_key_id"],
        },
    )


def validate_ballot_structure(
    record: dict[str, Any],
    final_manifest: dict[str, Any],
) -> None:
    expected_keys = {
        "candidate_digest",
        "candidate_record",
        "decision_id",
        "eligibility_proof",
        "nullifier",
        "profile_id",
        "schema",
    }
    if not isinstance(record, dict) or set(record) != expected_keys:
        raise VsdpError(NON_CANONICAL, "BallotRecord fields do not match the schema")
    if record["schema"] != BALLOT_SCHEMA or record["profile_id"] != PROFILE_ID:
        raise VsdpError(CONTEXT_MISMATCH, "unsupported ballot schema or profile")
    expected_decision = decision_id(final_manifest)
    if record["decision_id"] != expected_decision:
        raise VsdpError(CONTEXT_MISMATCH, "ballot decision_id mismatch")
    require_hex(record["candidate_digest"], 64, "candidate_digest")
    expected_digest = candidate_digest(record, final_manifest)
    if record["candidate_digest"] != expected_digest:
        raise VsdpError(CANDIDATE_DIGEST_MISMATCH, "candidate digest mismatch")
    nullifier = record["nullifier"]
    if not isinstance(nullifier, str) or not nullifier.isdigit() or str(int(nullifier)) != nullifier:
        raise VsdpError(NON_CANONICAL, "nullifier must be a minimal decimal string")
    if not isinstance(record["eligibility_proof"], dict):
        raise VsdpError(NON_CANONICAL, "eligibility_proof must be an object")


def board_record_id(record: dict[str, Any]) -> str:
    return domain_hash_hex("vsdp/board-record/v1", record)


def expected_cutoff_statement_hash(final_manifest: dict[str, Any]) -> str:
    validate_final_manifest(final_manifest)
    return domain_hash_hex(
        "vsdp/cutoff-statement/v1",
        {
            "decision_id": decision_id(final_manifest),
            "epoch": final_manifest["board_epoch"],
            "schema": "vsdp-cutoff-statement/1",
            "vote_closes_at": final_manifest["schedule"]["vote_closes_at"],
        },
    )


def acceptance_statement(
    record: dict[str, Any],
    final_manifest: dict[str, Any],
) -> dict[str, Any]:
    return {
        "candidate_digest": record["candidate_digest"],
        "decision_id": record["decision_id"],
        "epoch": final_manifest["board_epoch"],
        "nullifier": record["nullifier"],
        "profile_id": PROFILE_ID,
        "record_id": board_record_id(record),
        "schema": ACCEPTANCE_SCHEMA,
    }


def _acceptance_signing_bytes(statement: dict[str, Any]) -> bytes:
    return domain_hash("vsdp/board-accept/v1", statement)


def _validate_acceptance_statement(
    statement: dict[str, Any],
    final_manifest: dict[str, Any],
) -> None:
    if not isinstance(statement, dict) or set(statement) != {
        "candidate_digest",
        "decision_id",
        "epoch",
        "nullifier",
        "profile_id",
        "record_id",
        "schema",
    }:
        raise VsdpError(NON_CANONICAL, "receipt statement fields do not match the schema")
    if statement.get("schema") != ACCEPTANCE_SCHEMA or statement.get("profile_id") != PROFILE_ID:
        raise VsdpError(CONTEXT_MISMATCH, "receipt statement profile mismatch")
    require_hex(statement.get("candidate_digest"), 64, "candidate_digest")
    require_hex(statement.get("record_id"), 64, "record_id")
    nullifier = statement.get("nullifier")
    if (
        not isinstance(nullifier, str)
        or not nullifier.isdigit()
        or str(int(nullifier)) != nullifier
    ):
        raise VsdpError(NON_CANONICAL, "receipt nullifier is not canonical")
    if statement.get("decision_id") != decision_id(final_manifest):
        raise VsdpError(CONTEXT_MISMATCH, "receipt decision mismatch")
    if statement.get("epoch") != final_manifest["board_epoch"]:
        raise VsdpError(CONTEXT_MISMATCH, "receipt epoch mismatch")


def verify_signature_entry(
    statement: dict[str, Any],
    entry: dict[str, Any],
    witnesses: Mapping[str, str],
) -> None:
    if not isinstance(entry, dict) or set(entry) != {
        "public_key",
        "signature",
        "witness_id",
    }:
        raise VsdpError(NON_CANONICAL, "invalid Witness signature entry")
    public_key = require_hex(entry["public_key"], 64, "Witness public key")
    signature = require_hex(entry["signature"], 128, "Witness signature")
    witness_id = require_hex(entry["witness_id"], 64, "witness_id")
    if witnesses.get(witness_id) != public_key:
        raise VsdpError(CONTEXT_MISMATCH, "Witness is not in the Final Manifest")
    if witness_id_for_public_key(public_key) != witness_id:
        raise VsdpError(CONTEXT_MISMATCH, "Witness id does not match its public key")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(signature),
            _acceptance_signing_bytes(statement),
        )
    except Exception as exc:
        raise VsdpError(CONTEXT_MISMATCH, "invalid Witness signature") from exc


def combine_acceptances(
    acceptances: Iterable[dict[str, Any]],
    *,
    final_manifest: dict[str, Any],
    quorum: int = 3,
) -> dict[str, Any]:
    if quorum != WITNESS_QUORUM:
        raise VsdpError(NON_CANONICAL, "receipt quorum cannot override the profile")
    items = list(acceptances)
    if not items:
        raise VsdpError(RECEIPT_QUORUM_MISSING, "no Witness acceptances")
    statement = items[0].get("statement")
    if not isinstance(statement, dict):
        raise VsdpError(NON_CANONICAL, "acceptance statement is missing")
    _validate_acceptance_statement(statement, final_manifest)
    witnesses = witness_map(final_manifest)
    signatures: dict[str, dict[str, Any]] = {}
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != {"schema", "signature", "statement"}
            or item.get("schema") != "vsdp-witness-acceptance/1"
            or item.get("statement") != statement
        ):
            raise VsdpError(CONTEXT_MISMATCH, "Witnesses signed different statements")
        entry = item.get("signature")
        if not isinstance(entry, dict):
            raise VsdpError(NON_CANONICAL, "Witness signature entry is missing")
        verify_signature_entry(statement, entry, witnesses)
        witness_id = entry["witness_id"]
        if witness_id in signatures and signatures[witness_id] != entry:
            raise VsdpError(BOARD_FORK, "Witness produced conflicting signatures")
        signatures[witness_id] = entry
    if len(signatures) < quorum:
        raise VsdpError(
            RECEIPT_QUORUM_MISSING,
            f"receipt has {len(signatures)} signatures, requires {quorum}",
        )
    receipt_without_id = {
        "schema": RECEIPT_SCHEMA,
        "signatures": [signatures[key] for key in sorted(signatures)],
        "statement": statement,
    }
    return {
        "receipt_id": domain_hash_hex("vsdp/quorum-receipt/v1", receipt_without_id),
        **receipt_without_id,
    }


def verify_receipt(
    receipt: dict[str, Any],
    *,
    final_manifest: dict[str, Any],
    quorum: int = 3,
) -> str:
    if quorum != WITNESS_QUORUM:
        raise VsdpError(NON_CANONICAL, "receipt quorum cannot override the profile")
    if not isinstance(receipt, dict):
        raise VsdpError(NON_CANONICAL, "QuorumReceipt must be an object")
    if set(receipt) != {"receipt_id", "schema", "signatures", "statement"}:
        raise VsdpError(NON_CANONICAL, "invalid QuorumReceipt fields")
    if receipt["schema"] != RECEIPT_SCHEMA:
        raise VsdpError(NON_CANONICAL, "unknown QuorumReceipt schema")
    unsigned = dict(receipt)
    receipt_id = require_hex(unsigned.pop("receipt_id"), 64, "receipt_id")
    expected = domain_hash_hex("vsdp/quorum-receipt/v1", unsigned)
    if receipt_id != expected:
        raise VsdpError(CONTEXT_MISMATCH, "QuorumReceipt id mismatch")
    statement = receipt["statement"]
    _validate_acceptance_statement(statement, final_manifest)
    witnesses = witness_map(final_manifest)
    seen: set[str] = set()
    signatures = receipt["signatures"]
    if not isinstance(signatures, list) or not all(
        isinstance(entry, dict) for entry in signatures
    ):
        raise VsdpError(NON_CANONICAL, "receipt signatures must be an array")
    if signatures != sorted(signatures, key=lambda item: item.get("witness_id", "")):
        raise VsdpError(NON_CANONICAL, "receipt signatures are not canonically ordered")
    for entry in signatures:
        verify_signature_entry(statement, entry, witnesses)
        if entry["witness_id"] in seen:
            raise VsdpError(NON_CANONICAL, "duplicate Witness signature")
        seen.add(entry["witness_id"])
    if len(seen) < quorum:
        raise VsdpError(RECEIPT_QUORUM_MISSING, "receipt quorum is missing")
    return receipt_id


def _leaf_hash(record_id: str) -> bytes:
    return sha256_bytes(b"vsdp/board-leaf/v1\x00" + bytes.fromhex(record_id))


def sha256_bytes(raw: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(raw).digest()


def merkle_root(record_ids: Iterable[str]) -> str:
    ordered = sorted(set(record_ids))
    if not ordered:
        return sha256_hex(b"vsdp/board-empty/v1")
    level = [_leaf_hash(record_id) for record_id in ordered]
    while len(level) > 1:
        next_level: list[bytes] = []
        index = 0
        while index < len(level):
            if index + 1 == len(level):
                next_level.append(level[index])
            else:
                next_level.append(
                    sha256_bytes(b"vsdp/board-node/v1\x00" + level[index] + level[index + 1])
                )
            index += 2
        level = next_level
    return level[0].hex()


class WitnessStore:
    """Durable single-Witness state with atomic nullifier and checkpoint locks."""

    def __init__(
        self,
        root: str | Path,
        *,
        final_manifest: dict[str, Any],
        key: WitnessKey,
        proof_verifier: BallotProofVerifier | None = None,
    ):
        validate_final_manifest(final_manifest)
        allowed_keys = set(final_manifest["role_public_keys"]["witnesses"])
        if key.public_key not in allowed_keys:
            raise VsdpError(CONTEXT_MISMATCH, "Witness key is not in the Final Manifest")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "witness.sqlite3"
        self.final_manifest = final_manifest
        self.key = key
        self.proof_verifier = proof_verifier or RejectingProofVerifier()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    record_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    nullifier TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    record_json BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nullifier_locks (
                    decision_id TEXT NOT NULL,
                    nullifier TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    PRIMARY KEY (decision_id, nullifier)
                );
                CREATE TABLE IF NOT EXISTS acceptances (
                    record_id TEXT PRIMARY KEY,
                    acceptance_json BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    record_id TEXT NOT NULL,
                    receipt_json BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoint_locks (
                    decision_id TEXT NOT NULL,
                    epoch TEXT NOT NULL,
                    height INTEGER NOT NULL,
                    checkpoint_hash TEXT NOT NULL,
                    checkpoint_json BLOB NOT NULL,
                    PRIMARY KEY (decision_id, epoch, height)
                );
                """
            )

    def _schedule_time(self, field: str) -> datetime:
        value = self.final_manifest["schedule"][field]
        return datetime.fromisoformat(value[:-1] + "+00:00")

    def assert_voting_open(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        if current < self._schedule_time("vote_opens_at"):
            raise VsdpError(VOTING_NOT_OPEN, "the frozen voting window has not opened")
        if current >= self._schedule_time("vote_closes_at"):
            raise VsdpError(VOTING_CLOSED, "the frozen voting window is closed")

    def assert_voting_closed(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        if current < self._schedule_time("vote_closes_at"):
            raise VsdpError(VOTING_NOT_OPEN, "final Board sealing is not allowed before vote close")

    def submit(self, raw_record: bytes) -> dict[str, Any]:
        self.assert_voting_open()
        record = parse_canonical_json(raw_record)
        if not isinstance(record, dict):
            raise VsdpError(NON_CANONICAL, "BallotRecord must be an object")
        validate_ballot_structure(record, self.final_manifest)
        self.proof_verifier.verify(record, self.final_manifest)
        record_id = board_record_id(record)
        statement = acceptance_statement(record, self.final_manifest)

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            lock = connection.execute(
                "SELECT record_id FROM nullifier_locks WHERE decision_id=? AND nullifier=?",
                (record["decision_id"], record["nullifier"]),
            ).fetchone()
            if lock is not None and lock[0] != record_id:
                connection.execute("ROLLBACK")
                raise VsdpError(
                    NULLIFIER_CONFLICT,
                    f"nullifier is durably locked to record {lock[0]}",
                )
            existing = connection.execute(
                "SELECT acceptance_json FROM acceptances WHERE record_id=?",
                (record_id,),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                value = parse_canonical_json(existing[0])
                if not isinstance(value, dict):
                    raise VsdpError(NON_CANONICAL, "stored acceptance is invalid")
                return value

            connection.execute(
                "INSERT OR IGNORE INTO nullifier_locks(decision_id,nullifier,record_id) VALUES(?,?,?)",
                (record["decision_id"], record["nullifier"], record_id),
            )
            connection.execute(
                "INSERT INTO records(record_id,decision_id,nullifier,candidate_digest,record_json) "
                "VALUES(?,?,?,?,?)",
                (
                    record_id,
                    record["decision_id"],
                    record["nullifier"],
                    record["candidate_digest"],
                    raw_record,
                ),
            )
            signature_entry = {
                "public_key": self.key.public_key,
                "signature": self.key.sign(_acceptance_signing_bytes(statement)),
                "witness_id": self.key.witness_id,
            }
            acceptance = {
                "schema": "vsdp-witness-acceptance/1",
                "signature": signature_entry,
                "statement": statement,
            }
            connection.execute(
                "INSERT INTO acceptances(record_id,acceptance_json) VALUES(?,?)",
                (record_id, canonical_json_bytes(acceptance)),
            )
            connection.execute("COMMIT")
            return acceptance

    def store_receipt(
        self,
        receipt: dict[str, Any],
        *,
        raw_record: bytes | None = None,
    ) -> str:
        receipt_id = verify_receipt(receipt, final_manifest=self.final_manifest)
        statement = receipt["statement"]
        record_id = statement["record_id"]
        with closing(self._connect()) as connection:
            existing_record = connection.execute(
                "SELECT record_json FROM records WHERE record_id=?",
                (record_id,),
            ).fetchone()
        if existing_record is None:
            if raw_record is None:
                raise VsdpError(RECEIPTED_RECORD_MISSING, "receipt record is not stored locally")
            acceptance = self.submit(raw_record)
            if acceptance["statement"] != statement:
                raise VsdpError(CONTEXT_MISMATCH, "receipt does not match the supplied record")
        else:
            local_record = parse_canonical_json(existing_record[0])
            if not isinstance(local_record, dict):
                raise VsdpError(NON_CANONICAL, "stored record is invalid")
            if acceptance_statement(local_record, self.final_manifest) != statement:
                raise VsdpError(CONTEXT_MISMATCH, "receipt does not match the stored record")
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO receipts(receipt_id,record_id,receipt_json) VALUES(?,?,?)",
                (receipt_id, record_id, canonical_json_bytes(receipt)),
            )
        return receipt_id

    def receipted_record_ids(self) -> list[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT record_id FROM receipts ORDER BY record_id"
            ).fetchall()
        return [row[0] for row in rows]

    def load_record(self, record_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record_json FROM records WHERE record_id=?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise VsdpError(RECEIPTED_RECORD_MISSING, f"record not found: {record_id}")
        value = parse_canonical_json(row[0])
        if not isinstance(value, dict):
            raise VsdpError(NON_CANONICAL, "stored record is invalid")
        return value

    def checkpoint(
        self,
        *,
        cutoff_statement_hash: str,
        height: int,
        previous_checkpoint_hash: str,
    ) -> dict[str, Any]:
        supplied_cutoff = require_hex(
            cutoff_statement_hash,
            64,
            "cutoff_statement_hash",
        )
        if supplied_cutoff != expected_cutoff_statement_hash(self.final_manifest):
            raise VsdpError(CONTEXT_MISMATCH, "checkpoint cutoff statement mismatch")
        previous = require_hex(
            previous_checkpoint_hash,
            64,
            "previous_checkpoint_hash",
        )
        if height != 1:
            raise VsdpError(
                NON_CANONICAL,
                "the L1 Board supports exactly one final checkpoint at height 1",
            )
        if previous != self.final_manifest["board_genesis_hash"]:
            raise VsdpError(CONTEXT_MISMATCH, "checkpoint does not extend Board genesis")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT DISTINCT record_id FROM receipts ORDER BY record_id"
            ).fetchall()
            record_ids = [row[0] for row in rows]
            root = merkle_root(record_ids)
            empty_root = merkle_root([])
            body = {
                "accepted_ballot_root": root,
                "challenge_audit_root": empty_root,
                "cutoff_statement_hash": supplied_cutoff,
                "decision_id": decision_id(self.final_manifest),
                "dispute_root": empty_root,
                "epoch": self.final_manifest["board_epoch"],
                "height": height,
                "previous_checkpoint_hash": previous,
                "profile_id": PROFILE_ID,
                "record_count": len(record_ids),
                "record_set_root": root,
                "schema": CHECKPOINT_SCHEMA,
            }
            checkpoint_hash = domain_hash_hex("vsdp/checkpoint/v1", body)
            signature_entry = {
                "checkpoint_hash": checkpoint_hash,
                "public_key": self.key.public_key,
                "signature": self.key.sign(bytes.fromhex(checkpoint_hash)),
                "witness_id": self.key.witness_id,
            }
            response = {
                "checkpoint": body,
                "schema": CHECKPOINT_SIGNATURE_SCHEMA,
                "signature": signature_entry,
            }
            lock = connection.execute(
                "SELECT checkpoint_hash,checkpoint_json FROM checkpoint_locks "
                "WHERE decision_id=? AND epoch=? AND height=?",
                (body["decision_id"], body["epoch"], height),
            ).fetchone()
            if lock is not None:
                if lock[0] != checkpoint_hash:
                    connection.execute("ROLLBACK")
                    raise VsdpError(BOARD_FORK, "Witness already signed a different checkpoint")
                connection.execute("COMMIT")
                value = parse_canonical_json(lock[1])
                if not isinstance(value, dict):
                    raise VsdpError(NON_CANONICAL, "stored checkpoint is invalid")
                return value
            connection.execute(
                "INSERT INTO checkpoint_locks("
                "decision_id,epoch,height,checkpoint_hash,checkpoint_json"
                ") VALUES(?,?,?,?,?)",
                (
                    body["decision_id"],
                    body["epoch"],
                    height,
                    checkpoint_hash,
                    canonical_json_bytes(response),
                ),
            )
            connection.execute("COMMIT")
        return response


def _verify_checkpoint_signature(
    body: dict[str, Any],
    entry: dict[str, Any],
    witnesses: Mapping[str, str],
) -> None:
    if not isinstance(entry, dict) or set(entry) != {
        "checkpoint_hash",
        "public_key",
        "signature",
        "witness_id",
    }:
        raise VsdpError(NON_CANONICAL, "invalid checkpoint signature entry")
    expected_hash = domain_hash_hex("vsdp/checkpoint/v1", body)
    if entry.get("checkpoint_hash") != expected_hash:
        raise VsdpError(CONTEXT_MISMATCH, "checkpoint hash mismatch")
    public_key = require_hex(entry.get("public_key"), 64, "checkpoint public key")
    witness_id = require_hex(entry.get("witness_id"), 64, "checkpoint witness id")
    signature = require_hex(entry.get("signature"), 128, "checkpoint signature")
    if witnesses.get(witness_id) != public_key:
        raise VsdpError(CONTEXT_MISMATCH, "checkpoint signer is not a Witness")
    if witness_id_for_public_key(public_key) != witness_id:
        raise VsdpError(CONTEXT_MISMATCH, "checkpoint Witness id mismatch")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(signature),
            bytes.fromhex(expected_hash),
        )
    except Exception as exc:
        raise VsdpError(CONTEXT_MISMATCH, "invalid checkpoint signature") from exc


def _validate_checkpoint_body(
    body: dict[str, Any],
    final_manifest: dict[str, Any],
) -> None:
    if not isinstance(body, dict) or set(body) != {
        "accepted_ballot_root",
        "challenge_audit_root",
        "cutoff_statement_hash",
        "decision_id",
        "dispute_root",
        "epoch",
        "height",
        "previous_checkpoint_hash",
        "profile_id",
        "record_count",
        "record_set_root",
        "schema",
    }:
        raise VsdpError(NON_CANONICAL, "checkpoint body fields do not match the schema")
    if body.get("schema") != CHECKPOINT_SCHEMA or body.get("profile_id") != PROFILE_ID:
        raise VsdpError(CONTEXT_MISMATCH, "checkpoint profile mismatch")
    for field in (
        "accepted_ballot_root",
        "challenge_audit_root",
        "cutoff_statement_hash",
        "decision_id",
        "dispute_root",
        "previous_checkpoint_hash",
        "record_set_root",
    ):
        require_hex(body.get(field), 64, field)
    if (
        isinstance(body.get("height"), bool)
        or not isinstance(body.get("height"), int)
        or body["height"] != 1
        or isinstance(body.get("record_count"), bool)
        or not isinstance(body.get("record_count"), int)
        or body["record_count"] < 0
    ):
        raise VsdpError(NON_CANONICAL, "checkpoint counters are invalid")
    if body.get("decision_id") != decision_id(final_manifest):
        raise VsdpError(CONTEXT_MISMATCH, "checkpoint decision mismatch")
    if body.get("epoch") != final_manifest["board_epoch"]:
        raise VsdpError(CONTEXT_MISMATCH, "checkpoint epoch mismatch")
    if body.get("previous_checkpoint_hash") != final_manifest["board_genesis_hash"]:
        raise VsdpError(CONTEXT_MISMATCH, "checkpoint does not extend Board genesis")
    if body.get("cutoff_statement_hash") != expected_cutoff_statement_hash(final_manifest):
        raise VsdpError(CONTEXT_MISMATCH, "checkpoint cutoff statement mismatch")


def combine_checkpoints(
    signed_checkpoints: Iterable[dict[str, Any]],
    *,
    final_manifest: dict[str, Any],
    quorum: int = 3,
) -> dict[str, Any]:
    if quorum != WITNESS_QUORUM:
        raise VsdpError(NON_CANONICAL, "checkpoint quorum cannot override the profile")
    values = list(signed_checkpoints)
    if not values:
        raise VsdpError(RECEIPT_QUORUM_MISSING, "no checkpoint signatures")
    body = values[0].get("checkpoint")
    if not isinstance(body, dict):
        raise VsdpError(NON_CANONICAL, "checkpoint body is missing")
    _validate_checkpoint_body(body, final_manifest)
    witnesses = witness_map(final_manifest)
    signatures: dict[str, dict[str, Any]] = {}
    for value in values:
        if (
            not isinstance(value, dict)
            or set(value) != {"checkpoint", "schema", "signature"}
            or value.get("schema") != CHECKPOINT_SIGNATURE_SCHEMA
            or value.get("checkpoint") != body
        ):
            raise VsdpError(BOARD_FORK, "Witnesses signed different checkpoint bodies")
        entry = value.get("signature")
        if not isinstance(entry, dict):
            raise VsdpError(NON_CANONICAL, "checkpoint signature is missing")
        _verify_checkpoint_signature(body, entry, witnesses)
        witness_id = entry["witness_id"]
        if witness_id in signatures and signatures[witness_id] != entry:
            raise VsdpError(BOARD_FORK, "Witness double-signed a checkpoint")
        signatures[witness_id] = entry
    if len(signatures) < quorum:
        raise VsdpError(RECEIPT_QUORUM_MISSING, "checkpoint quorum is missing")
    return {
        "checkpoint": body,
        "checkpoint_hash": domain_hash_hex("vsdp/checkpoint/v1", body),
        "schema": "vsdp-quorum-checkpoint/1",
        "signatures": [signatures[key] for key in sorted(signatures)],
    }


def verify_quorum_checkpoint(
    value: dict[str, Any],
    *,
    final_manifest: dict[str, Any],
    quorum: int = 3,
) -> str:
    if quorum != WITNESS_QUORUM:
        raise VsdpError(NON_CANONICAL, "checkpoint quorum cannot override the profile")
    if not isinstance(value, dict):
        raise VsdpError(NON_CANONICAL, "quorum checkpoint must be an object")
    if set(value) != {"checkpoint", "checkpoint_hash", "schema", "signatures"}:
        raise VsdpError(NON_CANONICAL, "invalid quorum checkpoint fields")
    if value["schema"] != "vsdp-quorum-checkpoint/1":
        raise VsdpError(NON_CANONICAL, "unknown quorum checkpoint schema")
    body = value["checkpoint"]
    _validate_checkpoint_body(body, final_manifest)
    expected_hash = domain_hash_hex("vsdp/checkpoint/v1", body)
    if require_hex(value["checkpoint_hash"], 64, "checkpoint_hash") != expected_hash:
        raise VsdpError(CONTEXT_MISMATCH, "quorum checkpoint hash mismatch")
    witnesses = witness_map(final_manifest)
    signatures = value["signatures"]
    if not isinstance(signatures, list) or not all(
        isinstance(entry, dict) for entry in signatures
    ):
        raise VsdpError(NON_CANONICAL, "checkpoint signatures must be an array")
    if signatures != sorted(signatures, key=lambda entry: entry.get("witness_id", "")):
        raise VsdpError(NON_CANONICAL, "checkpoint signatures are not ordered")
    seen: set[str] = set()
    for entry in signatures:
        _verify_checkpoint_signature(body, entry, witnesses)
        if entry["witness_id"] in seen:
            raise VsdpError(NON_CANONICAL, "duplicate checkpoint signer")
        seen.add(entry["witness_id"])
    if len(seen) < quorum:
        raise VsdpError(RECEIPT_QUORUM_MISSING, "checkpoint quorum is missing")
    return expected_hash


@dataclass(frozen=True)
class BoardVerification:
    valid: bool
    decision_id: str
    accepted_count: int
    accepted_record_ids: tuple[str, ...]
    checkpoint_hash: str
    decryption_ready: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted_count": self.accepted_count,
            "accepted_record_ids": list(self.accepted_record_ids),
            "checkpoint_hash": self.checkpoint_hash,
            "decision_id": self.decision_id,
            "decryption_ready": self.decryption_ready,
            "network_privacy": "linkable_by_first_hop",
            "profile_id": PROFILE_ID,
            "schema": "vsdp-board-verification/1",
            "valid": self.valid,
        }


def verify_board_bundle(
    *,
    final_manifest: dict[str, Any],
    records: Iterable[dict[str, Any]],
    receipts: Iterable[dict[str, Any]],
    checkpoint: dict[str, Any],
    proof_verifier: BallotProofVerifier,
    minimum_anonymity_set: int,
) -> BoardVerification:
    validate_final_manifest(final_manifest)
    frozen_minimum = final_manifest["privacy"]["minimum_anonymity_set"]
    if minimum_anonymity_set != frozen_minimum:
        raise VsdpError(
            NON_CANONICAL,
            "anonymity minimum cannot override the Final Manifest",
        )
    record_map: dict[str, dict[str, Any]] = {}
    nullifiers: dict[str, str] = {}
    for record in records:
        validate_ballot_structure(record, final_manifest)
        proof_verifier.verify(record, final_manifest)
        record_id = board_record_id(record)
        prior = nullifiers.get(record["nullifier"])
        if prior is not None and prior != record_id:
            raise VsdpError(NULLIFIER_CONFLICT, "bundle contains conflicting nullifiers")
        nullifiers[record["nullifier"]] = record_id
        record_map[record_id] = record

    accepted: set[str] = set()
    for receipt in receipts:
        verify_receipt(receipt, final_manifest=final_manifest)
        statement = receipt["statement"]
        record_id = statement["record_id"]
        record = record_map.get(record_id)
        if record is None:
            raise VsdpError(RECEIPTED_RECORD_MISSING, f"missing record {record_id}")
        if statement != acceptance_statement(record, final_manifest):
            raise VsdpError(CONTEXT_MISMATCH, "receipt statement does not match its record")
        accepted.add(record_id)

    checkpoint_hash = verify_quorum_checkpoint(checkpoint, final_manifest=final_manifest)
    body = checkpoint["checkpoint"]
    expected_root = merkle_root(accepted)
    if body["record_set_root"] != expected_root or body["accepted_ballot_root"] != expected_root:
        raise VsdpError(RECEIPTED_RECORD_MISSING, "checkpoint root does not cover the receipt set")
    if body["record_count"] != len(accepted):
        raise VsdpError(CONTEXT_MISMATCH, "checkpoint record count mismatch")
    return BoardVerification(
        valid=True,
        decision_id=decision_id(final_manifest),
        accepted_count=len(accepted),
        accepted_record_ids=tuple(sorted(accepted)),
        checkpoint_hash=checkpoint_hash,
        decryption_ready=len(accepted) >= minimum_anonymity_set,
    )
