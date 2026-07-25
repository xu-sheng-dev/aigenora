from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .canonical import (
    canonical_json_bytes,
    domain_hash_hex,
    parse_canonical_json,
    require_hex,
    sha256_hex,
)
from .errors import ARTIFACT_HASH_MISMATCH, NON_CANONICAL, VsdpError
from .manifest import PROFILE_ID


PUBLIC_SCHEMA = "vsdp-public-artifact/1"
_KIND_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_REGISTERED_KINDS = frozenset(
    {
        "aggregate_authorization",
        "ballot_record",
        "board_checkpoint",
        "candidate_abandoned",
        "candidate_commitment",
        "challenge_opening",
        "checkpoint_fork_evidence",
        "decision_bundle",
        "decryption_authorization_conflict_evidence",
        "dkg_public",
        "eligibility_checkpoint",
        "final_approval",
        "final_manifest",
        "finalization_record",
        "invalid_record_acceptance_evidence",
        "nullifier_conflict_evidence",
        "partial_decryption",
        "provisional_decision_proof",
        "quorum_receipt",
        "receipted_record_missing_evidence",
        "setup_approval",
        "setup_manifest",
        "tally_result",
        "witness_double_accept_evidence",
    }
)
_CEREMONY_SCOPED_KINDS = frozenset(
    {
        "dkg_public",
        "eligibility_checkpoint",
        "setup_approval",
        "setup_manifest",
    }
)
_FORBIDDEN_PUBLIC_FIELDS = frozenset(
    {
        "identity_secret",
        "invitation_id",
        "ip",
        "ip_address",
        "iroh_ticket",
        "membership_path",
        "nickname",
        "node_id",
        "plaintext",
        "post_id",
        "private_key",
        "randomness",
        "referer",
        "relay_id",
        "remote_addr",
        "session_id",
        "submitted_at",
        "trace_id",
        "user_agent",
        "voter_id",
    }
)
_INDEX_STATUSES = frozenset(
    {
        "aborted",
        "disputed",
        "finalized",
        "final_frozen",
        "provisional",
        "setup_frozen",
    }
)


def _validate_kind(kind: Any) -> str:
    if (
        not isinstance(kind, str)
        or _KIND_PATTERN.fullmatch(kind) is None
        or kind not in _REGISTERED_KINDS
    ):
        raise VsdpError(NON_CANONICAL, "public artifact kind is not registered")
    return kind


def _find_forbidden_field(
    value: Any,
    *,
    allowed_fields: frozenset[str] = frozenset(),
) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FORBIDDEN_PUBLIC_FIELDS and key not in allowed_fields:
                return key
            found = _find_forbidden_field(item, allowed_fields=allowed_fields)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_forbidden_field(item, allowed_fields=allowed_fields)
            if found is not None:
                return found
    return None


def _privacy_exemptions(kind: str) -> frozenset[str]:
    if kind == "challenge_opening":
        return frozenset({"plaintext", "randomness"})
    return frozenset()


def make_public_artifact(
    *,
    kind: str,
    body: dict[str, Any],
    ceremony_id: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    if bool(ceremony_id) == bool(decision_id):
        raise VsdpError(
            NON_CANONICAL,
            "a public artifact must bind exactly one of ceremony_id or decision_id",
        )
    kind = _validate_kind(kind)
    if not isinstance(body, dict):
        raise VsdpError(NON_CANONICAL, "public artifact body must be an object")
    forbidden = _find_forbidden_field(
        body,
        allowed_fields=_privacy_exemptions(kind),
    )
    if forbidden is not None:
        raise VsdpError(
            NON_CANONICAL,
            f"public artifact contains forbidden privacy field: {forbidden}",
        )
    if kind in _CEREMONY_SCOPED_KINDS:
        if ceremony_id is None:
            raise VsdpError(NON_CANONICAL, f"{kind} must be ceremony-scoped")
        require_hex(ceremony_id, 64, "ceremony_id")
    else:
        if decision_id is None:
            raise VsdpError(NON_CANONICAL, f"{kind} must be decision-scoped")
        require_hex(decision_id, 64, "decision_id")
    envelope: dict[str, Any] = {
        "body": body,
        "kind": kind,
        "profile_id": PROFILE_ID,
        "schema": PUBLIC_SCHEMA,
    }
    if ceremony_id is not None:
        envelope["ceremony_id"] = ceremony_id
    if decision_id is not None:
        envelope["decision_id"] = decision_id
    artifact_id = domain_hash_hex("vsdp/public-artifact/v1", envelope)
    return {"artifact_id": artifact_id, **envelope}


def verify_public_artifact(value: dict[str, Any]) -> str:
    if not isinstance(value, dict):
        raise VsdpError(NON_CANONICAL, "public artifact must be an object")
    decision_scoped = set(value) == {
        "artifact_id",
        "body",
        "decision_id",
        "kind",
        "profile_id",
        "schema",
    }
    ceremony_scoped = set(value) == {
        "artifact_id",
        "body",
        "ceremony_id",
        "kind",
        "profile_id",
        "schema",
    }
    if not decision_scoped and not ceremony_scoped:
        raise VsdpError(NON_CANONICAL, "public artifact fields do not match the schema")
    artifact_id = require_hex(value.get("artifact_id"), 64, "artifact_id")
    kind = _validate_kind(value.get("kind"))
    body = value.get("body")
    if not isinstance(body, dict):
        raise VsdpError(NON_CANONICAL, "public artifact body must be an object")
    forbidden = _find_forbidden_field(
        body,
        allowed_fields=_privacy_exemptions(kind),
    )
    if forbidden is not None:
        raise VsdpError(
            NON_CANONICAL,
            f"public artifact contains forbidden privacy field: {forbidden}",
        )
    if ceremony_scoped:
        require_hex(value["ceremony_id"], 64, "ceremony_id")
        if kind not in _CEREMONY_SCOPED_KINDS:
            raise VsdpError(NON_CANONICAL, f"{kind} must be decision-scoped")
    else:
        require_hex(value["decision_id"], 64, "decision_id")
        if kind in _CEREMONY_SCOPED_KINDS:
            raise VsdpError(NON_CANONICAL, f"{kind} must be ceremony-scoped")
    unsigned = dict(value)
    unsigned.pop("artifact_id", None)
    expected = domain_hash_hex("vsdp/public-artifact/v1", unsigned)
    if artifact_id != expected:
        raise VsdpError(ARTIFACT_HASH_MISMATCH, "public artifact id mismatch")
    if value.get("schema") != PUBLIC_SCHEMA or value.get("profile_id") != PROFILE_ID:
        raise VsdpError(NON_CANONICAL, "unsupported public artifact envelope")
    return artifact_id


class ArtifactStore:
    """Append-only content-addressed public artifact store."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.public_dir = self.root / "public"
        self.public_dir.mkdir(parents=True, exist_ok=True)

    def append(self, artifact: dict[str, Any]) -> str:
        artifact_id = verify_public_artifact(artifact)
        raw = canonical_json_bytes(artifact)
        path = self.public_dir / f"{artifact_id}.json"
        if path.exists():
            existing = path.read_bytes()
            if existing != raw:
                raise VsdpError(ARTIFACT_HASH_MISMATCH, "artifact id collision")
            return artifact_id

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{artifact_id}.",
            suffix=".tmp",
            dir=str(self.public_dir),
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        return artifact_id

    def load(self, artifact_id: str) -> dict[str, Any]:
        artifact_id = require_hex(artifact_id, 64, "artifact_id")
        path = self.public_dir / f"{artifact_id}.json"
        value = parse_canonical_json(path.read_bytes())
        if not isinstance(value, dict):
            raise VsdpError(NON_CANONICAL, "artifact file must contain an object")
        verify_public_artifact(value)
        return value

    def build_index(self, *, status: str, decision_id: str) -> dict[str, Any]:
        if status not in _INDEX_STATUSES:
            raise VsdpError(NON_CANONICAL, "bundle status is not registered")
        decision_id = require_hex(decision_id, 64, "decision_id")
        entries: list[dict[str, Any]] = []
        for path in sorted(self.public_dir.glob("*.json"), key=lambda item: item.name):
            raw = path.read_bytes()
            value = parse_canonical_json(raw)
            if not isinstance(value, dict):
                raise VsdpError(NON_CANONICAL, f"{path.name} is not an object")
            artifact_id = verify_public_artifact(value)
            if path.stem != artifact_id:
                raise VsdpError(
                    ARTIFACT_HASH_MISMATCH,
                    f"{path.name} does not match its artifact id",
                )
            entries.append(
                {
                    "artifact_id": artifact_id,
                    "kind": value["kind"],
                    "length": len(raw),
                    "path": f"public/{path.name}",
                    "sha256": sha256_hex(raw),
                }
            )
        index_without_root = {
            "decision_id": decision_id,
            "entries": entries,
            "file_count": len(entries),
            "profile_id": PROFILE_ID,
            "schema": "vsdp-bundle-index/1",
            "status": status,
            "total_bytes": sum(item["length"] for item in entries),
        }
        return {
            **index_without_root,
            "index_root": domain_hash_hex("vsdp/bundle-index/v1", index_without_root),
        }

    def write_index(self, *, status: str, decision_id: str) -> Path:
        index = self.build_index(status=status, decision_id=decision_id)
        raw = canonical_json_bytes(index)
        path = self.root / "bundle-index.json"
        fd, tmp_name = tempfile.mkstemp(
            prefix=".bundle-index.",
            suffix=".tmp",
            dir=str(self.root),
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        return path


def load_index(path: str | Path) -> dict[str, Any]:
    value = parse_canonical_json(Path(path).read_bytes())
    expected_fields = {
        "decision_id",
        "entries",
        "file_count",
        "index_root",
        "profile_id",
        "schema",
        "status",
        "total_bytes",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or value.get("schema") != "vsdp-bundle-index/1"
        or value.get("profile_id") != PROFILE_ID
    ):
        raise VsdpError(NON_CANONICAL, "invalid bundle index")
    require_hex(value["decision_id"], 64, "decision_id")
    if value["status"] not in _INDEX_STATUSES:
        raise VsdpError(NON_CANONICAL, "invalid bundle status")
    entries = value["entries"]
    if not isinstance(entries, list):
        raise VsdpError(NON_CANONICAL, "bundle entries must be an array")
    prior_path = ""
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "artifact_id",
            "kind",
            "length",
            "path",
            "sha256",
        }:
            raise VsdpError(NON_CANONICAL, "invalid bundle index entry")
        require_hex(entry["artifact_id"], 64, "artifact_id")
        require_hex(entry["sha256"], 64, "sha256")
        _validate_kind(entry["kind"])
        if (
            isinstance(entry["length"], bool)
            or not isinstance(entry["length"], int)
            or entry["length"] < 0
        ):
            raise VsdpError(NON_CANONICAL, "invalid bundle entry length")
        expected_path = f"public/{entry['artifact_id']}.json"
        if entry["path"] != expected_path or entry["path"] <= prior_path:
            raise VsdpError(NON_CANONICAL, "bundle entry path is invalid or unordered")
        prior_path = entry["path"]
        total_bytes += entry["length"]
    if (
        isinstance(value["file_count"], bool)
        or value["file_count"] != len(entries)
        or isinstance(value["total_bytes"], bool)
        or value["total_bytes"] != total_bytes
    ):
        raise VsdpError(NON_CANONICAL, "bundle index counters do not match its entries")
    unsigned = dict(value)
    root = require_hex(unsigned.pop("index_root", None), 64, "index_root")
    expected = domain_hash_hex("vsdp/bundle-index/v1", unsigned)
    if root != expected:
        raise VsdpError(ARTIFACT_HASH_MISMATCH, "bundle index root mismatch")
    return value
