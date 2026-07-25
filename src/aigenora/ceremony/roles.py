from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import domain_hash_hex, require_hex
from .errors import ROLE_ASSIGNMENT_INVALID, VsdpError
from .manifest import PROFILE_ID


class RoleKind(str, Enum):
    ORGANIZER = "organizer"
    REGISTRAR = "registrar"
    AUDITOR = "auditor"
    GUARDIAN = "guardian"
    WITNESS = "witness"
    RELAY = "relay"
    VOTER = "voter"


_ROLE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True)
class RoleAssignment:
    assignment_id: str
    ceremony_id: str
    identity_signature: str
    profile_id: str
    public_key: str
    role_id: str
    role_kind: RoleKind
    valid_until: str

    def unsigned_dict(self) -> dict[str, str]:
        return {
            "ceremony_id": self.ceremony_id,
            "profile_id": self.profile_id,
            "public_key": self.public_key,
            "role_id": self.role_id,
            "role_kind": self.role_kind.value,
            "schema": "vsdp-role-assignment/1",
            "valid_until": self.valid_until,
        }

    def as_dict(self) -> dict[str, str]:
        return {
            "assignment_id": self.assignment_id,
            **self.unsigned_dict(),
            "identity_signature": self.identity_signature,
        }


def _parse_utc_seconds(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z") or "." in value:
        raise VsdpError(
            ROLE_ASSIGNMENT_INVALID,
            "valid_until must be UTC RFC3339 seconds",
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise VsdpError(
            ROLE_ASSIGNMENT_INVALID,
            "valid_until must be UTC RFC3339 seconds",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise VsdpError(ROLE_ASSIGNMENT_INVALID, "valid_until must use UTC")
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise VsdpError(
            ROLE_ASSIGNMENT_INVALID,
            "valid_until must use canonical UTC RFC3339 seconds",
        )
    return parsed


def _validate_role_id(value: Any) -> str:
    if not isinstance(value, str) or _ROLE_ID_PATTERN.fullmatch(value) is None:
        raise VsdpError(ROLE_ASSIGNMENT_INVALID, "role_id is not canonical")
    return value


def build_role_assignment(
    *,
    ceremony_id: str,
    role_id: str,
    role_kind: RoleKind,
    valid_until: str,
    private_key: Ed25519PrivateKey,
) -> RoleAssignment:
    require_hex(ceremony_id, 64, "ceremony_id")
    role_id = _validate_role_id(role_id)
    if not isinstance(role_kind, RoleKind):
        raise VsdpError(ROLE_ASSIGNMENT_INVALID, "unknown role kind")
    if _parse_utc_seconds(valid_until) <= datetime.now(timezone.utc):
        raise VsdpError(ROLE_ASSIGNMENT_INVALID, "role assignment is already expired")
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    unsigned = {
        "ceremony_id": ceremony_id,
        "profile_id": PROFILE_ID,
        "public_key": public_key,
        "role_id": role_id,
        "role_kind": role_kind.value,
        "schema": "vsdp-role-assignment/1",
        "valid_until": valid_until,
    }
    assignment_id = domain_hash_hex("vsdp/role-assignment/v1", unsigned)
    signature = private_key.sign(assignment_id.encode("ascii")).hex()
    return RoleAssignment(
        assignment_id=assignment_id,
        ceremony_id=ceremony_id,
        identity_signature=signature,
        profile_id=PROFILE_ID,
        public_key=public_key,
        role_id=role_id,
        role_kind=role_kind,
        valid_until=valid_until,
    )


def parse_role_assignment(
    value: dict[str, Any],
    *,
    valid_at: datetime | None = None,
) -> RoleAssignment:
    expected = {
        "assignment_id",
        "ceremony_id",
        "identity_signature",
        "profile_id",
        "public_key",
        "role_id",
        "role_kind",
        "schema",
        "valid_until",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise VsdpError(
            ROLE_ASSIGNMENT_INVALID,
            "role assignment fields do not match the schema",
        )
    if value.get("schema") != "vsdp-role-assignment/1":
        raise VsdpError(ROLE_ASSIGNMENT_INVALID, "unknown role assignment schema")
    try:
        role_kind = RoleKind(value["role_kind"])
    except (TypeError, ValueError) as exc:
        raise VsdpError(ROLE_ASSIGNMENT_INVALID, "unknown role kind") from exc
    assignment = RoleAssignment(
        assignment_id=require_hex(value["assignment_id"], 64, "assignment_id"),
        ceremony_id=require_hex(value["ceremony_id"], 64, "ceremony_id"),
        identity_signature=require_hex(
            value["identity_signature"], 128, "identity_signature"
        ),
        profile_id=value["profile_id"],
        public_key=require_hex(value["public_key"], 64, "public_key"),
        role_id=_validate_role_id(value["role_id"]),
        role_kind=role_kind,
        valid_until=value["valid_until"],
    )
    verify_role_assignment(assignment, valid_at=valid_at)
    return assignment


def verify_role_assignment(
    assignment: RoleAssignment,
    *,
    valid_at: datetime | None = None,
) -> None:
    if assignment.profile_id != PROFILE_ID:
        raise VsdpError(ROLE_ASSIGNMENT_INVALID, "role profile is unsupported")
    _validate_role_id(assignment.role_id)
    expires_at = _parse_utc_seconds(assignment.valid_until)
    if valid_at is not None:
        if valid_at.tzinfo is None:
            raise ValueError("valid_at must be timezone-aware")
        if expires_at <= valid_at.astimezone(timezone.utc):
            raise VsdpError(ROLE_ASSIGNMENT_INVALID, "role assignment is expired")
    expected_id = domain_hash_hex(
        "vsdp/role-assignment/v1", assignment.unsigned_dict()
    )
    if assignment.assignment_id != expected_id:
        raise VsdpError(ROLE_ASSIGNMENT_INVALID, "assignment_id mismatch")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(assignment.public_key)
        )
        public_key.verify(
            bytes.fromhex(assignment.identity_signature),
            assignment.assignment_id.encode("ascii"),
        )
    except (InvalidSignature, ValueError) as exc:
        raise VsdpError(
            ROLE_ASSIGNMENT_INVALID,
            "role assignment signature is invalid",
        ) from exc
