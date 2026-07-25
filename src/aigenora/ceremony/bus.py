from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .artifacts import verify_public_artifact
from .canonical import (
    b64u_decode,
    canonical_json_bytes,
    domain_hash_hex,
    parse_canonical_json,
    require_hex,
)
from .errors import (
    BUS_DISCONNECTED,
    CEREMONY_ABORTED,
    CONTRIBUTION_CONFLICT,
    CONTEXT_MISMATCH,
    NON_CANONICAL,
    QUORUM_TIMEOUT,
    ROLE_CHANNEL_VIOLATION,
    VsdpError,
)
from .manifest import PROFILE_ID
from .roles import RoleAssignment, RoleKind, verify_role_assignment


_ANONYMOUS_BALLOT_KINDS = {
    "ballot",
    "ballot_record",
    "ballot_bundle",
    "eligibility_proof",
}
_MAX_DIRECT_PAYLOAD_BYTES = 1_048_576


@dataclass(frozen=True)
class FaultPolicy:
    delay_seconds: float = 0.0
    disconnect_roles: frozenset[str] = field(default_factory=frozenset)
    drop_every: int = 0
    duplicate_every: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.delay_seconds, bool)
            or not isinstance(self.delay_seconds, (int, float))
            or not math.isfinite(self.delay_seconds)
            or self.delay_seconds < 0
            or isinstance(self.drop_every, bool)
            or not isinstance(self.drop_every, int)
            or self.drop_every < 0
            or isinstance(self.duplicate_every, bool)
            or not isinstance(self.duplicate_every, int)
            or self.duplicate_every < 0
        ):
            raise ValueError("fault policy values must be non-negative")


@dataclass(frozen=True)
class QuorumRule:
    allowed_roles: frozenset[RoleKind]
    minimum: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum, bool)
            or not isinstance(self.minimum, int)
            or self.minimum < 1
            or not self.allowed_roles
            or not all(isinstance(role, RoleKind) for role in self.allowed_roles)
        ):
            raise ValueError("invalid frozen quorum rule")


@dataclass(frozen=True)
class DirectMessage:
    kind: str
    sender_role_id: str
    target_role_id: str
    encrypted_payload: str


def build_signed_contribution(
    *,
    ceremony_id: str,
    kind: str,
    payload: dict[str, Any],
    role_id: str,
    private_key: Ed25519PrivateKey,
) -> dict[str, Any]:
    require_hex(ceremony_id, 64, "ceremony_id")
    if not isinstance(kind, str) or not kind or len(kind) > 128:
        raise VsdpError(NON_CANONICAL, "contribution kind is invalid")
    if not isinstance(role_id, str) or not role_id or len(role_id) > 128:
        raise VsdpError(NON_CANONICAL, "contribution role_id is invalid")
    if not isinstance(payload, dict):
        raise VsdpError(NON_CANONICAL, "contribution payload must be an object")
    statement = {
        "ceremony_id": ceremony_id,
        "kind": kind,
        "payload_hash": domain_hash_hex(
            "vsdp/role-contribution-payload/v1", payload
        ),
        "profile_id": PROFILE_ID,
        "role_id": role_id,
        "schema": "vsdp-role-contribution-statement/1",
    }
    signature = private_key.sign(canonical_json_bytes(statement)).hex()
    return {
        "payload": payload,
        "signature": signature,
        "statement": statement,
    }


class InMemoryCeremonyBus:
    """Deterministic async transport for role-scoped ceremony tests."""

    def __init__(
        self,
        *,
        ceremony_id: str,
        authorized_assignments: Mapping[str, RoleAssignment],
        quorum_rules: Mapping[str, QuorumRule] | None = None,
        decision_id: str | None = None,
        fault_policy: FaultPolicy | None = None,
    ):
        self.ceremony_id = require_hex(ceremony_id, 64, "ceremony_id")
        self.decision_id = (
            require_hex(decision_id, 64, "decision_id")
            if decision_id is not None
            else None
        )
        self.fault_policy = fault_policy or FaultPolicy()
        self._authorized_roles: dict[str, RoleAssignment] = {}
        for role_id, assignment in authorized_assignments.items():
            if role_id != assignment.role_id:
                raise VsdpError(
                    ROLE_CHANNEL_VIOLATION,
                    "authorized role key does not match its assignment",
                )
            verify_role_assignment(
                assignment,
                valid_at=datetime.now(timezone.utc),
            )
            if assignment.ceremony_id != self.ceremony_id:
                raise VsdpError(CONTEXT_MISMATCH, "authorized role ceremony mismatch")
            self._authorized_roles[role_id] = assignment
        self._quorum_rules = dict(quorum_rules or {})
        for kind, rule in self._quorum_rules.items():
            if not isinstance(kind, str) or not kind or len(kind) > 128:
                raise VsdpError(NON_CANONICAL, "quorum contribution kind is invalid")
            if not isinstance(rule, QuorumRule):
                raise VsdpError(NON_CANONICAL, "quorum rule is invalid")
            eligible = sum(
                1
                for assignment in self._authorized_roles.values()
                if assignment.role_kind in rule.allowed_roles
            )
            if eligible < rule.minimum:
                raise VsdpError(
                    ROLE_CHANNEL_VIOLATION,
                    f"{kind} quorum exceeds the authorized role set",
                )
        self._roles: dict[str, RoleAssignment] = {}
        self._public_artifacts: list[dict[str, Any]] = []
        self._artifact_ids: set[str] = set()
        self._direct: dict[str, list[DirectMessage]] = {}
        self._contributions: dict[tuple[str, str], dict[str, Any]] = {}
        self._abort_record: dict[str, Any] | None = None
        self._delivery_count = 0
        self._condition = asyncio.Condition()

    async def register_role(self, assignment: RoleAssignment) -> None:
        verify_role_assignment(
            assignment,
            valid_at=datetime.now(timezone.utc),
        )
        if assignment.ceremony_id != self.ceremony_id:
            raise VsdpError(CONTEXT_MISMATCH, "role ceremony_id mismatch")
        if self._authorized_roles.get(assignment.role_id) != assignment:
            raise VsdpError(
                ROLE_CHANNEL_VIOLATION,
                "role assignment is not frozen in the ceremony authorization set",
            )
        async with self._condition:
            self._raise_if_aborted()
            existing = self._roles.get(assignment.role_id)
            if existing is not None and existing != assignment:
                raise VsdpError(
                    CONTRIBUTION_CONFLICT,
                    "role_id is already assigned differently",
                )
            self._roles[assignment.role_id] = assignment
            self._condition.notify_all()

    async def broadcast(self, public_artifact: dict[str, Any]) -> bool:
        artifact_id = verify_public_artifact(public_artifact)
        if public_artifact.get("profile_id") != PROFILE_ID:
            raise VsdpError(CONTEXT_MISMATCH, "artifact profile mismatch")
        if "ceremony_id" in public_artifact:
            if public_artifact["ceremony_id"] != self.ceremony_id:
                raise VsdpError(CONTEXT_MISMATCH, "artifact ceremony_id mismatch")
        elif self.decision_id is None or public_artifact.get("decision_id") != self.decision_id:
            raise VsdpError(CONTEXT_MISMATCH, "artifact decision_id mismatch")
        delivered, _ = await self._before_delivery()
        if not delivered:
            return False
        canonical = parse_canonical_json(canonical_json_bytes(public_artifact))
        async with self._condition:
            self._raise_if_aborted()
            if artifact_id not in self._artifact_ids:
                self._artifact_ids.add(artifact_id)
                self._public_artifacts.append(canonical)
            self._condition.notify_all()
        return True

    async def send_to_role(
        self,
        *,
        sender_role_id: str,
        target_role_id: str,
        kind: str,
        encrypted_payload: str,
    ) -> bool:
        sender = self._require_role(sender_role_id)
        self._require_role(target_role_id)
        if not isinstance(kind, str) or not kind or len(kind) > 128:
            raise VsdpError(NON_CANONICAL, "direct message kind is invalid")
        if sender.role_kind == RoleKind.VOTER and kind in _ANONYMOUS_BALLOT_KINDS:
            raise VsdpError(
                ROLE_CHANNEL_VIOLATION,
                "anonymous ballot material cannot use an identified role channel",
            )
        decoded = b64u_decode(encrypted_payload)
        if len(decoded) > _MAX_DIRECT_PAYLOAD_BYTES:
            raise VsdpError(NON_CANONICAL, "direct message payload exceeds the limit")
        delivered, duplicate = await self._before_delivery(
            sender_role_id=sender_role_id,
            target_role_id=target_role_id,
        )
        if not delivered:
            return False
        message = DirectMessage(
            kind=kind,
            sender_role_id=sender_role_id,
            target_role_id=target_role_id,
            encrypted_payload=encrypted_payload,
        )
        async with self._condition:
            self._raise_if_aborted()
            inbox = self._direct.setdefault(target_role_id, [])
            inbox.append(message)
            if duplicate:
                inbox.append(message)
            self._condition.notify_all()
        return True

    async def submit_contribution(self, value: dict[str, Any]) -> bool:
        statement, payload, signature = self._validate_contribution(value)
        role_id = statement["role_id"]
        role = self._require_role(role_id)
        if role.role_kind == RoleKind.VOTER and statement["kind"] in _ANONYMOUS_BALLOT_KINDS:
            raise VsdpError(
                ROLE_CHANNEL_VIOLATION,
                "anonymous ballot material cannot be a signed role contribution",
            )
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(role.public_key)).verify(
                bytes.fromhex(signature),
                canonical_json_bytes(statement),
            )
        except (InvalidSignature, ValueError) as exc:
            raise VsdpError(
                ROLE_CHANNEL_VIOLATION,
                "role contribution signature is invalid",
            ) from exc
        delivered, _ = await self._before_delivery(sender_role_id=role_id)
        if not delivered:
            return False
        key = (statement["kind"], role_id)
        canonical = {
            "payload": parse_canonical_json(canonical_json_bytes(payload)),
            "signature": signature,
            "statement": statement,
        }
        async with self._condition:
            self._raise_if_aborted()
            existing = self._contributions.get(key)
            if existing is not None and existing != canonical:
                raise VsdpError(
                    CONTRIBUTION_CONFLICT,
                    "role already submitted a conflicting contribution",
                )
            self._contributions[key] = canonical
            self._condition.notify_all()
        return True

    async def collect_quorum(
        self,
        *,
        kind: str,
        deadline: float,
    ) -> tuple[dict[str, Any], ...]:
        rule = self._quorum_rules.get(kind)
        if rule is None:
            raise VsdpError(
                ROLE_CHANNEL_VIOLATION,
                f"no frozen quorum rule exists for contribution kind: {kind}",
            )
        try:
            async with self._condition:
                while True:
                    self._raise_if_aborted()
                    selected = [
                        value
                        for (stored_kind, role_id), value in self._contributions.items()
                        if stored_kind == kind
                        and self._roles[role_id].role_kind in rule.allowed_roles
                    ]
                    if len(selected) >= rule.minimum:
                        return tuple(
                            sorted(
                                selected,
                                key=lambda item: item["statement"]["role_id"],
                            )
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise VsdpError(
                            QUORUM_TIMEOUT,
                            f"{kind} quorum was not reached before the deadline",
                        )
                    try:
                        await asyncio.wait_for(self._condition.wait(), remaining)
                    except asyncio.TimeoutError as exc:
                        raise VsdpError(
                            QUORUM_TIMEOUT,
                            f"{kind} quorum was not reached before the deadline",
                        ) from exc
        except asyncio.CancelledError:
            await self.abort(
                "runtime_cancelled",
                {"kind": kind, "schema": "vsdp-bus-cancellation-evidence/1"},
            )
            raise

    async def subscribe_public_artifacts(
        self,
        cursor: int,
        *,
        deadline: float | None = None,
    ) -> tuple[tuple[dict[str, Any], ...], int]:
        if cursor < 0:
            raise VsdpError(NON_CANONICAL, "cursor cannot be negative")
        async with self._condition:
            while cursor >= len(self._public_artifacts):
                self._raise_if_aborted()
                if deadline is None:
                    return (), cursor
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return (), cursor
                try:
                    await asyncio.wait_for(self._condition.wait(), remaining)
                except asyncio.TimeoutError:
                    return (), cursor
            values = tuple(self._public_artifacts[cursor:])
            return values, len(self._public_artifacts)

    async def receive_direct(self, role_id: str) -> tuple[DirectMessage, ...]:
        self._require_role(role_id)
        async with self._condition:
            self._raise_if_aborted()
            values = tuple(self._direct.get(role_id, ()))
            self._direct[role_id] = []
            return values

    async def abort(self, reason: str, evidence: dict[str, Any]) -> dict[str, Any]:
        if not reason:
            raise VsdpError(NON_CANONICAL, "abort reason is required")
        record = {
            "ceremony_id": self.ceremony_id,
            "evidence": parse_canonical_json(canonical_json_bytes(evidence)),
            "reason": reason,
            "schema": "vsdp-bus-abort/1",
        }
        async with self._condition:
            if self._abort_record is None:
                self._abort_record = record
            self._condition.notify_all()
            return self._abort_record

    @property
    def abort_record(self) -> dict[str, Any] | None:
        return self._abort_record

    def _require_role(self, role_id: str) -> RoleAssignment:
        role = self._roles.get(role_id)
        if role is None:
            raise VsdpError(ROLE_CHANNEL_VIOLATION, f"unknown role: {role_id}")
        return role

    def _validate_contribution(
        self, value: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        if not isinstance(value, dict) or set(value) != {
            "payload",
            "signature",
            "statement",
        }:
            raise VsdpError(NON_CANONICAL, "invalid role contribution envelope")
        statement = value["statement"]
        payload = value["payload"]
        signature = value["signature"]
        expected_statement = {
            "ceremony_id",
            "kind",
            "payload_hash",
            "profile_id",
            "role_id",
            "schema",
        }
        if (
            not isinstance(statement, dict)
            or set(statement) != expected_statement
            or not isinstance(payload, dict)
            or not isinstance(signature, str)
        ):
            raise VsdpError(NON_CANONICAL, "invalid role contribution fields")
        if (
            not isinstance(statement["kind"], str)
            or not statement["kind"]
            or len(statement["kind"]) > 128
            or not isinstance(statement["role_id"], str)
            or not statement["role_id"]
            or len(statement["role_id"]) > 128
        ):
            raise VsdpError(NON_CANONICAL, "invalid role contribution identifiers")
        if (
            statement["schema"] != "vsdp-role-contribution-statement/1"
            or statement["ceremony_id"] != self.ceremony_id
            or statement["profile_id"] != PROFILE_ID
        ):
            raise VsdpError(CONTEXT_MISMATCH, "role contribution context mismatch")
        expected_hash = domain_hash_hex(
            "vsdp/role-contribution-payload/v1", payload
        )
        if statement["payload_hash"] != expected_hash:
            raise VsdpError(CONTEXT_MISMATCH, "role contribution payload mismatch")
        return statement, payload, signature

    async def _before_delivery(
        self,
        *,
        sender_role_id: str | None = None,
        target_role_id: str | None = None,
    ) -> tuple[bool, bool]:
        self._raise_if_aborted()
        if (
            sender_role_id in self.fault_policy.disconnect_roles
            or target_role_id in self.fault_policy.disconnect_roles
        ):
            raise VsdpError(BUS_DISCONNECTED, "role transport is disconnected")
        self._delivery_count += 1
        delivery_number = self._delivery_count
        if self.fault_policy.delay_seconds:
            await asyncio.sleep(self.fault_policy.delay_seconds)
        delivered = not (
            self.fault_policy.drop_every
            and delivery_number % self.fault_policy.drop_every == 0
        )
        duplicate = bool(
            self.fault_policy.duplicate_every
            and delivery_number % self.fault_policy.duplicate_every == 0
        )
        return delivered, duplicate

    def _raise_if_aborted(self) -> None:
        if self._abort_record is not None:
            raise VsdpError(CEREMONY_ABORTED, self._abort_record["reason"])
