from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aigenora.engine.keys import KeyPair, sign_raw, verify_raw
from aigenora.proto.sdk import DetailLog, EventBus, SnapshotBus


ZERO_HASH = "0" * 64
MAX_JSON_DEPTH = 16
MAX_CONTAINER_ITEMS = 1024
MAX_STRING_BYTES = 65536
_CORE_FIELDS = (
    "_group",
    "group_id",
    "leader_public_key",
    "leader_epoch",
    "seq",
    "previous_hash",
    "membership_version",
    "authority_state_hash",
    "events_hash",
    "completed",
    "outcome",
)


class GroupProtocolError(RuntimeError):
    """A peer frame or protocol hook violated the authoritative-group contract."""


@dataclass(frozen=True)
class GroupConfig:
    min_participants: int
    max_participants: int
    allow_late_join: bool
    recovery_mode: str
    start_policy: str
    checkpoint_every_events: int = 1
    max_action_bytes: int = 8192
    max_events_per_action: int = 64

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "GroupConfig":
        flow = spec.get("flow")
        group = flow.get("group") if isinstance(flow, dict) else None
        if not isinstance(group, dict):
            raise GroupProtocolError("authoritative_group spec has no flow.group")
        return cls(
            min_participants=int(group["min_participants"]),
            max_participants=int(group["max_participants"]),
            allow_late_join=bool(group["allow_late_join"]),
            recovery_mode=str(group["recovery_mode"]),
            start_policy=str(group["start_policy"]),
            checkpoint_every_events=int(group.get("checkpoint_every_events", 1)),
            max_action_bytes=int(group.get("max_action_bytes", 8192)),
            max_events_per_action=int(group.get("max_events_per_action", 64)),
        )


def canonical_json(value: Any) -> bytes:
    _validate_json(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GroupProtocolError(f"value is not canonical JSON: {exc}") from exc


def json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def checkpoint_certificate_canonical(checkpoint: dict[str, Any]) -> str:
    """Return the cross-language v1 certificate signed by the frame Leader."""
    return (
        "aigenora-group-checkpoint-v1:"
        f"{checkpoint.get('group_id')}:{checkpoint.get('leader_public_key')}:"
        f"{checkpoint.get('leader_epoch')}:{checkpoint.get('seq')}:"
        f"{checkpoint.get('frame_hash')}:{checkpoint.get('membership_version')}:"
        f"{checkpoint.get('checkpoint_hash')}"
    )


def normalize_members(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    identities: set[str] = set()
    seats: set[int] = set()
    for raw in members:
        if not isinstance(raw, dict):
            raise GroupProtocolError("group member must be an object")
        public_key = raw.get("public_key")
        seat = raw.get("seat")
        if (
            not isinstance(public_key, str)
            or len(public_key) != 64
            or any(ch not in "0123456789abcdefABCDEF" for ch in public_key)
        ):
            raise GroupProtocolError("group member public_key must be 64-char hex")
        if not isinstance(seat, int) or isinstance(seat, bool) or seat < 0 or seat > 31:
            raise GroupProtocolError("group member seat must be between 0 and 31")
        if public_key in identities or seat in seats:
            raise GroupProtocolError("group members must have unique identity and seat")
        identities.add(public_key)
        seats.add(seat)
        normalized.append(
            {
                "member_id": str(raw.get("member_id") or public_key),
                "public_key": public_key.lower(),
                "seat": seat,
                "status": str(raw.get("status") or "active"),
            }
        )
    normalized.sort(key=lambda item: item["seat"])
    return normalized


class GroupAuthority:
    """Deterministic Host-side state machine for one leader epoch.

    Networking is deliberately outside this class. Tests can feed several
    MemoryChannels, while the CLI supplies independent Iroh channels.
    """

    def __init__(
        self,
        *,
        spec: dict[str, Any],
        hooks: Any,
        options: dict[str, Any],
        state_dir: str | Path,
        group_id: str,
        leader_public_key: str,
        leader_epoch: int,
        membership_version: int,
        members: list[dict[str, Any]],
        keypair: KeyPair,
        checkpoint: dict[str, Any] | None = None,
    ):
        self.spec = spec
        self.hooks = hooks
        self.options = options
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.group_id = group_id
        self.leader_public_key = leader_public_key
        self.leader_epoch = leader_epoch
        self.membership_version = membership_version
        self.members = normalize_members(members)
        self.keypair = keypair
        self.config = GroupConfig.from_spec(spec)
        self.snapshot = SnapshotBus(self.state_dir)
        self.details = DetailLog(self.state_dir)
        self.events = EventBus(self.state_dir)
        self.seq = 0
        self.previous_hash = ZERO_HASH
        self.last_client_seq: dict[str, int] = {}
        self.completed = False
        self.outcome: str | None = None
        self._last_envelopes: dict[str, dict[str, Any]] = {}
        self._checkpoint_history: dict[int, dict[str, Any]] = {}
        self._acknowledged_seq: dict[str, int] = {}
        self._replicated_checkpoint: dict[str, Any] | None = None

        self.hooks.proto_init(options, "host", [], self.state_dir)
        initial_events: list[dict[str, Any]] = []
        restored = checkpoint is not None
        if checkpoint is None:
            self.state = self.hooks.proto_group_initial_state(
                copy.deepcopy(self.members)
            )
        else:
            old_leader = str(checkpoint.get("leader_public_key") or "")
            self._restore_metadata(checkpoint)
            protocol_state = checkpoint.get("protocol_state")
            if not isinstance(protocol_state, dict):
                raise GroupProtocolError("checkpoint protocol_state must be an object")
            self.state = self.hooks.proto_group_restore(
                copy.deepcopy(protocol_state),
                copy.deepcopy(self.members),
                self.leader_epoch,
            )
            if not isinstance(self.state, dict):
                raise GroupProtocolError(
                    "proto_group_restore must return an object"
                )
            for membership_result in self._reconcile_restored_members(
                checkpoint
            ):
                normalized_membership = self._normalize_result(
                    membership_result
                )
                self.state = normalized_membership["state"]
                initial_events.extend(normalized_membership["events"])
                self.completed = normalized_membership["completed"]
                self.outcome = normalized_membership["outcome"]
            changed = self.hooks.proto_group_on_leader_changed(
                copy.deepcopy(self.state),
                old_leader,
                self.leader_public_key,
            )
            normalized = self._normalize_result(changed)
            self.state = normalized["state"]
            initial_events = normalized["events"]
            self.completed = normalized["completed"]
            self.outcome = normalized["outcome"]
        if not isinstance(self.state, dict):
            raise GroupProtocolError("group authority state must be an object")
        _validate_json(self.state)
        self._build_envelopes(
            events=initial_events,
            frame_kind="epoch_start" if restored else "snapshot",
            advance=restored,
        )

    @property
    def checkpoint_path(self) -> Path:
        return self.state_dir / "group-checkpoint.json"

    def checkpoint(self, *, frame_hash: str | None = None) -> dict[str, Any]:
        protocol_state = self.hooks.proto_group_recovery_snapshot(
            copy.deepcopy(self.state)
        )
        if not isinstance(protocol_state, dict):
            raise GroupProtocolError(
                "proto_group_recovery_snapshot must return an object"
            )
        checkpoint = {
            "version": 1,
            "group_id": self.group_id,
            "leader_public_key": self.leader_public_key,
            "leader_epoch": self.leader_epoch,
            "seq": self.seq,
            "frame_hash": frame_hash or self.previous_hash,
            "membership_version": self.membership_version,
            "members": copy.deepcopy(self.members),
            "last_client_seq": dict(self.last_client_seq),
            "recovery_mode": self.config.recovery_mode,
            "protocol_state": protocol_state,
            "completed": self.completed,
            "outcome": self.outcome,
        }
        checkpoint["checkpoint_hash"] = json_hash(checkpoint)
        checkpoint["checkpoint_signature"] = sign_raw(
            self.keypair.private_key,
            checkpoint_certificate_canonical(checkpoint).encode("utf-8"),
        )
        return checkpoint

    def persist_checkpoint(
        self, *, frame_hash: str | None = None
    ) -> dict[str, Any]:
        checkpoint = self.checkpoint(frame_hash=frame_hash)
        _atomic_json(self.checkpoint_path, checkpoint)
        return checkpoint

    def bootstrap_envelopes(self) -> dict[str, dict[str, Any]]:
        """Return signed current-state envelopes for initial/late joins."""
        return copy.deepcopy(self._last_envelopes)

    def acknowledge(
        self, public_key: str, seq: int, frame_hash: str
    ) -> bool:
        """Record proof that a non-Leader Member holds a recovery checkpoint."""
        member = self.member(public_key)
        if (
            member is None
            or member.get("status") != "active"
            or public_key == self.leader_public_key
            or not isinstance(seq, int)
            or isinstance(seq, bool)
        ):
            return False
        checkpoint = self._checkpoint_history.get(seq)
        if (
            checkpoint is None
            or checkpoint.get("frame_hash") != frame_hash
            or seq < self._acknowledged_seq.get(public_key, -1)
        ):
            return False
        self._acknowledged_seq[public_key] = seq
        if (
            self._replicated_checkpoint is None
            or seq > int(self._replicated_checkpoint["seq"])
        ):
            self._replicated_checkpoint = copy.deepcopy(checkpoint)
        return True

    def replicated_checkpoint(self) -> dict[str, Any] | None:
        """Return the newest checkpoint acknowledged by at least one successor."""
        return copy.deepcopy(self._replicated_checkpoint)

    def apply_input(
        self,
        *,
        actor_public_key: str,
        client_seq: int,
        action: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        if self.completed:
            raise GroupProtocolError("group is already completed")
        actor = self.member(actor_public_key)
        if actor is None or actor.get("status") != "active":
            raise GroupProtocolError("actor is not an active group member")
        if not isinstance(client_seq, int) or isinstance(client_seq, bool) or client_seq < 1:
            raise GroupProtocolError("client_seq must be a positive integer")
        previous_client_seq = self.last_client_seq.get(actor_public_key, 0)
        if client_seq <= previous_client_seq:
            return copy.deepcopy(self._last_envelopes), {
                "status": "duplicate",
                "client_seq": client_seq,
                "authority_seq": self.seq,
            }
        if client_seq != previous_client_seq + 1:
            raise GroupProtocolError(
                f"client_seq gap: expected {previous_client_seq + 1}, got {client_seq}"
            )
        if not isinstance(action, dict):
            raise GroupProtocolError("group action must be an object")
        action_bytes = canonical_json(action)
        if len(action_bytes) > self.config.max_action_bytes:
            raise GroupProtocolError("group action exceeds max_action_bytes")

        result = self.hooks.proto_group_handle(
            copy.deepcopy(self.state),
            copy.deepcopy(actor),
            copy.deepcopy(action),
        )
        normalized = self._normalize_result(result)
        self.state = normalized["state"]
        self.completed = normalized["completed"]
        self.outcome = normalized["outcome"]
        self.last_client_seq[actor_public_key] = client_seq
        envelopes = self._build_envelopes(
            events=normalized["events"],
            direct=normalized["direct"],
            frame_kind="frame",
            advance=True,
        )
        receipt = {
            "status": "accepted",
            "client_seq": client_seq,
            "authority_seq": self.seq,
            "frame_hash": self.previous_hash,
            "completed": self.completed,
        }
        return envelopes, receipt

    def add_member(
        self,
        member: dict[str, Any],
        *,
        membership_version: int,
    ) -> dict[str, dict[str, Any]]:
        normalized = normalize_members([member])[0]
        existing = self.member(normalized["public_key"])
        if existing is None:
            if len(self.members) >= self.config.max_participants:
                raise GroupProtocolError("group is full")
            if any(item["seat"] == normalized["seat"] for item in self.members):
                raise GroupProtocolError("member seat is already occupied")
            self.members.append(normalized)
            self.members.sort(key=lambda item: item["seat"])
        else:
            existing.update(normalized)
            existing["status"] = "active"
        self.membership_version = membership_version
        result = self.hooks.proto_group_member_joined(
            copy.deepcopy(self.state), copy.deepcopy(normalized)
        )
        normalized_result = self._normalize_result(result)
        self.state = normalized_result["state"]
        return self._build_envelopes(
            events=normalized_result["events"],
            direct=normalized_result["direct"],
            frame_kind="membership",
            advance=True,
        )

    def remove_member(
        self,
        public_key: str,
        *,
        membership_version: int,
        reason: str,
    ) -> dict[str, dict[str, Any]]:
        member = self.member(public_key)
        if member is None:
            raise GroupProtocolError("unknown group member")
        member["status"] = "left"
        self.membership_version = membership_version
        result = self.hooks.proto_group_member_left(
            copy.deepcopy(self.state), copy.deepcopy(member), reason
        )
        normalized = self._normalize_result(result)
        self.state = normalized["state"]
        self.completed = normalized["completed"]
        self.outcome = normalized["outcome"]
        return self._build_envelopes(
            events=normalized["events"],
            direct=normalized["direct"],
            frame_kind="membership",
            advance=True,
        )

    def leader_changed(
        self, old_leader: str, new_leader: str
    ) -> dict[str, dict[str, Any]]:
        result = self.hooks.proto_group_on_leader_changed(
            copy.deepcopy(self.state), old_leader, new_leader
        )
        normalized = self._normalize_result(result)
        self.state = normalized["state"]
        return self._build_envelopes(
            events=normalized["events"],
            direct=normalized["direct"],
            frame_kind="epoch_start",
            advance=True,
        )

    def member(self, public_key: str) -> dict[str, Any] | None:
        lowered = public_key.lower()
        for member in self.members:
            if member["public_key"] == lowered:
                return member
        return None

    def _reconcile_restored_members(
        self, checkpoint: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Apply membership changes that reached the server after the checkpoint."""
        raw_previous = checkpoint.get("members")
        if not isinstance(raw_previous, list):
            raise GroupProtocolError("checkpoint members must be an array")
        previous = {
            item["public_key"]: item
            for item in normalize_members(raw_previous)
        }
        current = {item["public_key"]: item for item in self.members}
        results: list[dict[str, Any]] = []
        for public_key, old_member in previous.items():
            if (
                old_member.get("status") == "active"
                and public_key not in current
            ):
                results.append(
                    self.hooks.proto_group_member_left(
                        copy.deepcopy(self.state),
                        copy.deepcopy(old_member),
                        "membership_changed_during_failover",
                    )
                )
                candidate = results[-1]
                if isinstance(candidate, dict) and isinstance(
                    candidate.get("state"), dict
                ):
                    self.state = candidate["state"]
        for public_key, member in current.items():
            old_member = previous.get(public_key)
            if old_member is None or old_member.get("status") != "active":
                results.append(
                    self.hooks.proto_group_member_joined(
                        copy.deepcopy(self.state), copy.deepcopy(member)
                    )
                )
                candidate = results[-1]
                if isinstance(candidate, dict) and isinstance(
                    candidate.get("state"), dict
                ):
                    self.state = candidate["state"]
        return results

    def _restore_metadata(self, checkpoint: dict[str, Any]) -> None:
        expected_hash = checkpoint.get("checkpoint_hash")
        checkpoint_signature = checkpoint.get("checkpoint_signature")
        unsigned = dict(checkpoint)
        unsigned.pop("checkpoint_hash", None)
        unsigned.pop("checkpoint_signature", None)
        if not isinstance(expected_hash, str) or json_hash(unsigned) != expected_hash:
            raise GroupProtocolError("checkpoint hash mismatch")
        if not isinstance(checkpoint_signature, str):
            raise GroupProtocolError("checkpoint signature is missing")
        try:
            verify_raw(
                str(checkpoint.get("leader_public_key") or ""),
                checkpoint_certificate_canonical(checkpoint).encode("utf-8"),
                checkpoint_signature,
            )
        except Exception as exc:
            raise GroupProtocolError("checkpoint certificate is invalid") from exc
        if checkpoint.get("group_id") != self.group_id:
            raise GroupProtocolError("checkpoint group_id mismatch")
        seq = checkpoint.get("seq")
        previous_hash = checkpoint.get("frame_hash")
        if not isinstance(seq, int) or seq < 0:
            raise GroupProtocolError("checkpoint seq is invalid")
        if not _is_hash(previous_hash):
            raise GroupProtocolError("checkpoint frame_hash is invalid")
        self.seq = seq
        self.previous_hash = previous_hash
        restored_client_seq = checkpoint.get("last_client_seq") or {}
        if not isinstance(restored_client_seq, dict):
            raise GroupProtocolError("checkpoint last_client_seq is invalid")
        self.last_client_seq = {
            str(key): int(value)
            for key, value in restored_client_seq.items()
            if isinstance(key, str)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        }
        self.completed = bool(checkpoint.get("completed", False))
        outcome = checkpoint.get("outcome")
        self.outcome = outcome if isinstance(outcome, str) else None

    def _normalize_result(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise GroupProtocolError("group hook result must be an object")
        state = result.get("state")
        events = result.get("events", [])
        direct = result.get("direct", {})
        completed = result.get("completed", False)
        outcome = result.get("outcome")
        if not isinstance(state, dict):
            raise GroupProtocolError("group hook result.state must be an object")
        if not isinstance(events, list):
            raise GroupProtocolError("group hook result.events must be an array")
        if len(events) > self.config.max_events_per_action:
            raise GroupProtocolError("group hook emitted too many events")
        if not isinstance(direct, dict):
            raise GroupProtocolError("group hook result.direct must be an object")
        if not isinstance(completed, bool):
            raise GroupProtocolError("group hook result.completed must be boolean")
        if outcome is not None and not isinstance(outcome, str):
            raise GroupProtocolError("group hook result.outcome must be string or null")
        _validate_json(state)
        _validate_json(events)
        _validate_json(direct)
        return {
            "state": state,
            "events": events,
            "direct": direct,
            "completed": completed,
            "outcome": outcome,
        }

    def _build_envelopes(
        self,
        *,
        events: list[dict[str, Any]],
        frame_kind: str,
        advance: bool,
        direct: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        direct = direct or {}
        if advance:
            self.seq += 1
        core = {
            "_group": frame_kind,
            "group_id": self.group_id,
            "leader_public_key": self.leader_public_key,
            "leader_epoch": self.leader_epoch,
            "seq": self.seq,
            "previous_hash": self.previous_hash,
            "membership_version": self.membership_version,
            "authority_state_hash": json_hash(self.state),
            "events_hash": json_hash(events),
            "completed": self.completed,
            "outcome": self.outcome,
        }
        frame_hash = json_hash(core)
        checkpoint = self.persist_checkpoint(frame_hash=frame_hash)
        self._checkpoint_history[self.seq] = copy.deepcopy(checkpoint)
        while len(self._checkpoint_history) > 256:
            self._checkpoint_history.pop(min(self._checkpoint_history))
        envelopes: dict[str, dict[str, Any]] = {}
        for member in self.members:
            if member.get("status") != "active":
                continue
            public_key = member["public_key"]
            view = self.hooks.proto_group_view(
                copy.deepcopy(self.state), copy.deepcopy(member)
            )
            if not isinstance(view, dict):
                raise GroupProtocolError("proto_group_view must return an object")
            private_payload = {
                "viewer_public_key": public_key,
                "view": view,
                "direct": direct.get(public_key, []),
                "checkpoint": checkpoint,
            }
            view_hash = json_hash(private_payload)
            signature_payload = {
                "frame_hash": frame_hash,
                "view_hash": view_hash,
                "viewer_public_key": public_key,
            }
            envelope = {
                **core,
                "frame_hash": frame_hash,
                "checkpoint_hash": checkpoint["checkpoint_hash"],
                "events": copy.deepcopy(events),
                **private_payload,
                "view_hash": view_hash,
                "signature": sign_raw(
                    self.keypair.private_key, canonical_json(signature_payload)
                ),
            }
            envelopes[public_key] = envelope
        self.previous_hash = frame_hash
        self._last_envelopes = copy.deepcopy(envelopes)
        self.details.append(
            type="group_frame",
            group_id=self.group_id,
            leader_epoch=self.leader_epoch,
            seq=self.seq,
            frame_kind=frame_kind,
            frame_hash=frame_hash,
            events=events,
            completed=self.completed,
            outcome=self.outcome,
        )
        self.events.emit(
            "group_frame",
            {
                "group_id": self.group_id,
                "leader_epoch": self.leader_epoch,
                "seq": self.seq,
                "frame_kind": frame_kind,
                "frame_hash": frame_hash,
                "completed": self.completed,
            },
        )
        self._publish_local_snapshot(
            phase="group_completed" if self.completed else "group_active",
            events=events,
        )
        return envelopes

    def _publish_local_snapshot(
        self, *, phase: str, events: list[dict[str, Any]] | None = None
    ) -> None:
        local_member = self.member(self.leader_public_key)
        view: dict[str, Any] = {}
        if local_member is not None:
            candidate = self.hooks.proto_group_view(
                copy.deepcopy(self.state), copy.deepcopy(local_member)
            )
            if isinstance(candidate, dict):
                view = candidate
        self.snapshot.update(
            phase=phase,
            role="host",
            protocol_name=self.spec.get("name", "Group Protocol"),
            group={
                "group_id": self.group_id,
                "leader_public_key": self.leader_public_key,
                "leader_epoch": self.leader_epoch,
                "seq": self.seq,
                "membership_version": self.membership_version,
                "members": copy.deepcopy(self.members),
                "checkpoint_hash": self.checkpoint().get("checkpoint_hash"),
                "recovery_mode": self.config.recovery_mode,
            },
            group_view=view,
            group_events=copy.deepcopy(events or []),
            completed=self.completed,
            outcome=self.outcome,
        )


class GroupReplica:
    """Guest-side signature, epoch, hash-chain and private-view verifier."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        group_id: str,
        viewer_public_key: str,
        leader_public_key: str,
        leader_epoch: int,
        protocol_name: str,
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.group_id = group_id
        self.viewer_public_key = viewer_public_key.lower()
        self.leader_public_key = leader_public_key.lower()
        self.leader_epoch = leader_epoch
        self.protocol_name = protocol_name
        self.seq: int | None = None
        self.frame_hash: str | None = None
        self.membership_version = 0
        self.checkpoint: dict[str, Any] | None = None
        self.snapshot = SnapshotBus(self.state_dir)
        self.details = DetailLog(self.state_dir)
        self.events = EventBus(self.state_dir)

    def apply(self, envelope: dict[str, Any], *, bootstrap: bool = False) -> bool:
        if not isinstance(envelope, dict):
            raise GroupProtocolError("group envelope must be an object")
        if envelope.get("group_id") != self.group_id:
            raise GroupProtocolError("group envelope group_id mismatch")
        if envelope.get("leader_public_key") != self.leader_public_key:
            raise GroupProtocolError("group envelope leader mismatch")
        if envelope.get("leader_epoch") != self.leader_epoch:
            raise GroupProtocolError("group envelope epoch mismatch")
        if envelope.get("viewer_public_key") != self.viewer_public_key:
            raise GroupProtocolError("group envelope viewer mismatch")
        core = {key: envelope.get(key) for key in _CORE_FIELDS}
        frame_hash = envelope.get("frame_hash")
        if not _is_hash(frame_hash) or json_hash(core) != frame_hash:
            raise GroupProtocolError("group frame hash mismatch")
        events = envelope.get("events")
        if not isinstance(events, list) or json_hash(events) != envelope.get("events_hash"):
            raise GroupProtocolError("group events hash mismatch")
        private_payload = {
            "viewer_public_key": envelope.get("viewer_public_key"),
            "view": envelope.get("view"),
            "direct": envelope.get("direct"),
            "checkpoint": envelope.get("checkpoint"),
        }
        view_hash = envelope.get("view_hash")
        if not _is_hash(view_hash) or json_hash(private_payload) != view_hash:
            raise GroupProtocolError("group view hash mismatch")
        signature = envelope.get("signature")
        if not isinstance(signature, str):
            raise GroupProtocolError("group envelope signature is missing")
        verify_raw(
            self.leader_public_key,
            canonical_json(
                {
                    "frame_hash": frame_hash,
                    "view_hash": view_hash,
                    "viewer_public_key": self.viewer_public_key,
                }
            ),
            signature,
        )
        seq = envelope.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise GroupProtocolError("group frame seq is invalid")
        if self.seq is not None:
            if seq == self.seq and frame_hash == self.frame_hash:
                return False
            if seq <= self.seq:
                raise GroupProtocolError("group frame sequence moved backwards")
            if not bootstrap and seq != self.seq + 1:
                raise GroupProtocolError("group frame sequence has a gap")
            if envelope.get("previous_hash") != self.frame_hash:
                raise GroupProtocolError("group frame hash chain mismatch")
        elif not bootstrap:
            raise GroupProtocolError("first group frame must be a bootstrap")

        checkpoint = envelope.get("checkpoint")
        if not isinstance(checkpoint, dict):
            raise GroupProtocolError("group envelope checkpoint must be an object")
        expected_checkpoint_hash = checkpoint.get("checkpoint_hash")
        checkpoint_signature = checkpoint.get("checkpoint_signature")
        unsigned_checkpoint = dict(checkpoint)
        unsigned_checkpoint.pop("checkpoint_hash", None)
        unsigned_checkpoint.pop("checkpoint_signature", None)
        if (
            not _is_hash(expected_checkpoint_hash)
            or json_hash(unsigned_checkpoint) != expected_checkpoint_hash
            or envelope.get("checkpoint_hash") != expected_checkpoint_hash
        ):
            raise GroupProtocolError("group checkpoint hash mismatch")
        if (
            checkpoint.get("group_id") != self.group_id
            or checkpoint.get("leader_public_key") != self.leader_public_key
            or checkpoint.get("leader_epoch") != self.leader_epoch
            or checkpoint.get("seq") != seq
            or checkpoint.get("frame_hash") != frame_hash
            or checkpoint.get("membership_version")
            != envelope.get("membership_version")
        ):
            raise GroupProtocolError("group checkpoint does not certify this frame")
        if not isinstance(checkpoint_signature, str):
            raise GroupProtocolError("group checkpoint signature is missing")
        try:
            verify_raw(
                self.leader_public_key,
                checkpoint_certificate_canonical(checkpoint).encode("utf-8"),
                checkpoint_signature,
            )
        except Exception as exc:
            raise GroupProtocolError(
                "group checkpoint certificate is invalid"
            ) from exc

        self.seq = seq
        self.frame_hash = frame_hash
        self.membership_version = int(envelope.get("membership_version") or 0)
        self.checkpoint = copy.deepcopy(checkpoint)
        _atomic_json(self.state_dir / "group-checkpoint.json", checkpoint)
        phase = "group_completed" if envelope.get("completed") else "group_active"
        self.snapshot.update(
            phase=phase,
            role="guest",
            protocol_name=self.protocol_name,
            group={
                "group_id": self.group_id,
                "leader_public_key": self.leader_public_key,
                "leader_epoch": self.leader_epoch,
                "seq": seq,
                "membership_version": self.membership_version,
                "members": checkpoint.get("members", []),
                "checkpoint_hash": expected_checkpoint_hash,
                "recovery_mode": checkpoint.get("recovery_mode"),
            },
            group_view=envelope.get("view") or {},
            group_events=envelope.get("events") or [],
            direct=envelope.get("direct") or [],
            completed=bool(envelope.get("completed", False)),
            outcome=envelope.get("outcome"),
        )
        self.details.append(
            type="group_frame",
            group_id=self.group_id,
            leader_epoch=self.leader_epoch,
            seq=seq,
            frame_hash=frame_hash,
            frame_kind=envelope.get("_group"),
            completed=bool(envelope.get("completed", False)),
        )
        self.events.emit(
            "group_frame_received",
            {
                "group_id": self.group_id,
                "leader_epoch": self.leader_epoch,
                "seq": seq,
                "frame_hash": frame_hash,
            },
        )
        return True

    def advance_epoch(
        self, *, leader_public_key: str, leader_epoch: int
    ) -> None:
        if leader_epoch <= self.leader_epoch:
            raise GroupProtocolError("new leader epoch must increase")
        self.leader_public_key = leader_public_key.lower()
        self.leader_epoch = leader_epoch
        # The first frame from the new epoch must chain from the persisted
        # checkpoint frame hash. Sequence continuity is retained.
        self.events.emit(
            "group_leader_changed",
            {
                "group_id": self.group_id,
                "leader_public_key": self.leader_public_key,
                "leader_epoch": self.leader_epoch,
            },
        )


def _validate_json(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise GroupProtocolError("JSON nesting exceeds the group protocol limit")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        raise GroupProtocolError("floating-point values are not allowed in group state")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_STRING_BYTES:
            raise GroupProtocolError("string exceeds the group protocol limit")
        return
    if isinstance(value, list):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise GroupProtocolError("array exceeds the group protocol limit")
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise GroupProtocolError("object exceeds the group protocol limit")
        for key, item in value.items():
            if not isinstance(key, str):
                raise GroupProtocolError("JSON object keys must be strings")
            _validate_json(key, depth=depth + 1)
            _validate_json(item, depth=depth + 1)
        return
    raise GroupProtocolError(f"unsupported JSON value type: {type(value).__name__}")


def _is_hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value.lower())
    )


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)
