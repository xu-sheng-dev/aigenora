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
from aigenora.proto.json_delta import (
    JsonDeltaError,
    apply_json_delta,
    make_json_delta,
)
from aigenora.proto.sdk import DetailLog, EventBus, SnapshotBus


ZERO_HASH = "0" * 64
MAX_JSON_DEPTH = 16
MAX_CONTAINER_ITEMS = 1024
MAX_STRING_BYTES = 65536
_CORE_FIELDS = (
    "_group",
    "wire_version",
    "group_id",
    "leader_public_key",
    "leader_epoch",
    "seq",
    "previous_hash",
    "membership_version",
    "authority_state_hash",
    "recovery_state_hash",
    "events_hash",
    "completed",
    "outcome",
)
_CHECKPOINT_STATE_FIELDS = (
    "members",
    "last_client_seq",
    "recovery_mode",
    "protocol_state",
    "completed",
    "outcome",
)
_PRIVATE_PAYLOAD_FIELDS = (
    "viewer_public_key",
    "view_mode",
    "view",
    "view_delta",
    "view_state_hash",
    "direct",
    "checkpoint_mode",
    "checkpoint",
    "checkpoint_delta",
    "checkpoint_hash",
    "checkpoint_signature",
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
    checkpoint_every_events: int = 20
    max_action_bytes: int = 8192
    max_events_per_action: int = 64
    peer_channels_enabled: bool = False
    peer_routing: str = "disabled"
    peer_channel_names: tuple[str, ...] = ()
    max_peer_message_bytes: int = 16384

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "GroupConfig":
        flow = spec.get("flow")
        group = flow.get("group") if isinstance(flow, dict) else None
        if not isinstance(group, dict):
            raise GroupProtocolError("authoritative_group spec has no flow.group")
        checkpoint_every_events = group.get("checkpoint_every_events", 20)
        if (
            not isinstance(checkpoint_every_events, int)
            or isinstance(checkpoint_every_events, bool)
            or checkpoint_every_events < 1
            or checkpoint_every_events > 256
        ):
            raise GroupProtocolError(
                "checkpoint_every_events must be between 1 and 256"
            )
        peer_channels = group.get("peer_channels")
        peer_channels_enabled = bool(
            isinstance(peer_channels, dict) and peer_channels.get("enabled") is True
        )
        return cls(
            min_participants=int(group["min_participants"]),
            max_participants=int(group["max_participants"]),
            allow_late_join=bool(group["allow_late_join"]),
            recovery_mode=str(group["recovery_mode"]),
            start_policy=str(group["start_policy"]),
            checkpoint_every_events=checkpoint_every_events,
            max_action_bytes=int(group.get("max_action_bytes", 8192)),
            max_events_per_action=int(group.get("max_events_per_action", 64)),
            peer_channels_enabled=peer_channels_enabled,
            peer_routing=(
                str(peer_channels.get("routing"))
                if peer_channels_enabled and isinstance(peer_channels, dict)
                else "disabled"
            ),
            peer_channel_names=(
                tuple(str(item) for item in peer_channels.get("channels", []))
                if peer_channels_enabled and isinstance(peer_channels, dict)
                else ()
            ),
            max_peer_message_bytes=(
                int(peer_channels.get("max_message_bytes", 16384))
                if peer_channels_enabled and isinstance(peer_channels, dict)
                else 16384
            ),
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
        self._last_views: dict[str, dict[str, Any]] = {}
        self._last_core: dict[str, Any] | None = None
        self._last_events: list[dict[str, Any]] = []
        self._checkpoint_state: dict[str, Any] | None = None
        self._current_checkpoint: dict[str, Any] | None = None
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
        if frame_hash is None and self._current_checkpoint is not None:
            return copy.deepcopy(self._current_checkpoint)
        checkpoint_state = self._make_checkpoint_state()
        return self._checkpoint_from_state(
            checkpoint_state, frame_hash or self.previous_hash
        )

    def persist_checkpoint(
        self, *, frame_hash: str | None = None
    ) -> dict[str, Any]:
        checkpoint = self.checkpoint(frame_hash=frame_hash)
        _atomic_json(self.checkpoint_path, checkpoint)
        return checkpoint

    def bootstrap_envelopes(self) -> dict[str, dict[str, Any]]:
        """Return self-contained current-state envelopes for initial/reconnect."""
        envelopes: dict[str, dict[str, Any]] = {}
        for member in self.members:
            if member.get("status") != "active":
                continue
            envelope = self.bootstrap_envelope(member["public_key"])
            if envelope is not None:
                envelopes[member["public_key"]] = envelope
        return envelopes

    def bootstrap_envelope(
        self, public_key: str
    ) -> dict[str, Any] | None:
        """Build one self-contained bootstrap without rendering other views."""
        if self._last_core is None or self._current_checkpoint is None:
            return None
        member = self.member(public_key)
        if member is None or member.get("status") != "active":
            return None
        frame_hash = self._current_checkpoint["frame_hash"]
        lowered = public_key.lower()
        view = self._member_view(member)
        return self._build_member_envelope(
            core=self._last_core,
            frame_hash=frame_hash,
            events=self._last_events,
            public_key=lowered,
            view=view,
            view_mode="full",
            view_delta=[],
            direct=[],
            checkpoint=self._current_checkpoint,
            checkpoint_mode="full",
            checkpoint_delta=[],
        )

    def acknowledge(
        self,
        public_key: str,
        seq: int,
        frame_hash: str,
        checkpoint_hash: str | None = None,
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
            or (
                checkpoint_hash is not None
                and checkpoint.get("checkpoint_hash") != checkpoint_hash
            )
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

    def peer_routes(self, viewer_public_key: str) -> dict[str, tuple[str, ...]]:
        """Return the current directed side-channel routes for one Member."""
        if not self.config.peer_channels_enabled:
            return {}
        viewer = self.member(viewer_public_key)
        if viewer is None or viewer.get("status") != "active":
            raise GroupProtocolError("peer route viewer is not an active member")
        allowed_channels = set(self.config.peer_channel_names)
        if self.config.peer_routing == "all_members":
            return {
                member["public_key"]: self.config.peer_channel_names
                for member in self.members
                if member.get("status") == "active"
                and member["public_key"] != viewer["public_key"]
            }
        raw = self.hooks.proto_group_peer_routes(
            copy.deepcopy(self.state), copy.deepcopy(viewer)
        )
        if not isinstance(raw, dict):
            raise GroupProtocolError(
                "proto_group_peer_routes must return an object"
            )
        routes: dict[str, tuple[str, ...]] = {}
        for recipient_public_key, raw_channels in raw.items():
            recipient = self.member(str(recipient_public_key))
            if (
                recipient is None
                or recipient.get("status") != "active"
                or recipient["public_key"] == viewer["public_key"]
            ):
                raise GroupProtocolError(
                    "proto_group_peer_routes returned an invalid recipient"
                )
            if not isinstance(raw_channels, list) or not raw_channels:
                raise GroupProtocolError(
                    "proto_group_peer_routes channels must be a non-empty array"
                )
            normalized = tuple(str(item) for item in raw_channels)
            if (
                len(set(normalized)) != len(normalized)
                or not set(normalized) <= allowed_channels
            ):
                raise GroupProtocolError(
                    "proto_group_peer_routes returned an undeclared channel"
                )
            routes[recipient["public_key"]] = normalized
        return routes

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
        if checkpoint.get("version") != 1:
            raise GroupProtocolError("checkpoint version is invalid")
        _validate_checkpoint_state(_checkpoint_state_from(checkpoint))
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

    def _make_checkpoint_state(self) -> dict[str, Any]:
        protocol_state = self.hooks.proto_group_recovery_snapshot(
            copy.deepcopy(self.state)
        )
        if not isinstance(protocol_state, dict):
            raise GroupProtocolError(
                "proto_group_recovery_snapshot must return an object"
            )
        checkpoint_state = {
            "members": copy.deepcopy(self.members),
            "last_client_seq": dict(self.last_client_seq),
            "recovery_mode": self.config.recovery_mode,
            "protocol_state": protocol_state,
            "completed": self.completed,
            "outcome": self.outcome,
        }
        _validate_json(checkpoint_state)
        return checkpoint_state

    def _checkpoint_from_state(
        self, checkpoint_state: dict[str, Any], frame_hash: str
    ) -> dict[str, Any]:
        checkpoint = {
            "version": 1,
            "group_id": self.group_id,
            "leader_public_key": self.leader_public_key,
            "leader_epoch": self.leader_epoch,
            "seq": self.seq,
            "frame_hash": frame_hash,
            "membership_version": self.membership_version,
            **copy.deepcopy(checkpoint_state),
        }
        checkpoint["checkpoint_hash"] = json_hash(checkpoint)
        checkpoint["checkpoint_signature"] = sign_raw(
            self.keypair.private_key,
            checkpoint_certificate_canonical(checkpoint).encode("utf-8"),
        )
        return checkpoint

    def _member_view(self, member: dict[str, Any]) -> dict[str, Any]:
        view = self.hooks.proto_group_view(
            copy.deepcopy(self.state), copy.deepcopy(member)
        )
        if not isinstance(view, dict):
            raise GroupProtocolError("proto_group_view must return an object")
        _validate_json(view)
        return view

    def _build_member_envelope(
        self,
        *,
        core: dict[str, Any],
        frame_hash: str,
        events: list[dict[str, Any]],
        public_key: str,
        view: dict[str, Any],
        view_mode: str,
        view_delta: list[dict[str, Any]],
        direct: Any,
        checkpoint: dict[str, Any],
        checkpoint_mode: str,
        checkpoint_delta: list[dict[str, Any]],
    ) -> dict[str, Any]:
        private_payload = {
            "viewer_public_key": public_key,
            "view_mode": view_mode,
            "view": copy.deepcopy(view) if view_mode == "full" else None,
            "view_delta": copy.deepcopy(view_delta),
            "view_state_hash": json_hash(view),
            "direct": copy.deepcopy(direct),
            "checkpoint_mode": checkpoint_mode,
            "checkpoint": (
                copy.deepcopy(checkpoint)
                if checkpoint_mode == "full"
                else None
            ),
            "checkpoint_delta": copy.deepcopy(checkpoint_delta),
            "checkpoint_hash": checkpoint["checkpoint_hash"],
            "checkpoint_signature": checkpoint["checkpoint_signature"],
        }
        view_hash = json_hash(private_payload)
        signature_payload = {
            "frame_hash": frame_hash,
            "view_hash": view_hash,
            "viewer_public_key": public_key,
        }
        return {
            **core,
            "frame_hash": frame_hash,
            "events": copy.deepcopy(events),
            **private_payload,
            "view_hash": view_hash,
            "signature": sign_raw(
                self.keypair.private_key, canonical_json(signature_payload)
            ),
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
        checkpoint_state = self._make_checkpoint_state()
        core = {
            "_group": frame_kind,
            "wire_version": 2,
            "group_id": self.group_id,
            "leader_public_key": self.leader_public_key,
            "leader_epoch": self.leader_epoch,
            "seq": self.seq,
            "previous_hash": self.previous_hash,
            "membership_version": self.membership_version,
            "authority_state_hash": json_hash(self.state),
            "recovery_state_hash": json_hash(checkpoint_state),
            "events_hash": json_hash(events),
            "completed": self.completed,
            "outcome": self.outcome,
        }
        frame_hash = json_hash(core)
        checkpoint = self._checkpoint_from_state(checkpoint_state, frame_hash)
        force_full_checkpoint = (
            self._checkpoint_state is None
            or frame_kind != "frame"
            or self.completed
            or self.seq % self.config.checkpoint_every_events == 0
        )
        checkpoint_mode = "full"
        checkpoint_delta: list[dict[str, Any]] = []
        if not force_full_checkpoint:
            candidate_delta = make_json_delta(
                self._checkpoint_state, checkpoint_state
            )
            if (
                len(canonical_json(candidate_delta)) + 256
                < len(canonical_json(checkpoint))
            ):
                checkpoint_mode = "delta"
                checkpoint_delta = candidate_delta

        envelopes: dict[str, dict[str, Any]] = {}
        current_views: dict[str, dict[str, Any]] = {}
        for member in self.members:
            if member.get("status") != "active":
                continue
            public_key = member["public_key"]
            view = self._member_view(member)
            current_views[public_key] = view
            view_mode = "full"
            view_delta: list[dict[str, Any]] = []
            previous_view = self._last_views.get(public_key)
            if previous_view is not None and frame_kind == "frame":
                candidate_delta = make_json_delta(previous_view, view)
                if (
                    len(canonical_json(candidate_delta)) + 96
                    < len(canonical_json(view))
                ):
                    view_mode = "delta"
                    view_delta = candidate_delta
            envelopes[public_key] = self._build_member_envelope(
                core=core,
                frame_hash=frame_hash,
                events=events,
                public_key=public_key,
                view=view,
                view_mode=view_mode,
                view_delta=view_delta,
                direct=direct.get(public_key, []),
                checkpoint=checkpoint,
                checkpoint_mode=checkpoint_mode,
                checkpoint_delta=checkpoint_delta,
            )

        _atomic_json(self.checkpoint_path, checkpoint)
        self._checkpoint_history[self.seq] = copy.deepcopy(checkpoint)
        while len(self._checkpoint_history) > 256:
            self._checkpoint_history.pop(min(self._checkpoint_history))
        self.previous_hash = frame_hash
        self._last_envelopes = copy.deepcopy(envelopes)
        self._last_views = copy.deepcopy(current_views)
        self._last_core = copy.deepcopy(core)
        self._last_events = copy.deepcopy(events)
        self._checkpoint_state = copy.deepcopy(checkpoint_state)
        self._current_checkpoint = copy.deepcopy(checkpoint)
        self.details.append(
            type="group_recovery_record",
            group_id=self.group_id,
            leader_public_key=self.leader_public_key,
            leader_epoch=self.leader_epoch,
            seq=self.seq,
            frame_hash=frame_hash,
            membership_version=self.membership_version,
            recovery_state_hash=core["recovery_state_hash"],
            checkpoint_mode=checkpoint_mode,
            checkpoint=(
                checkpoint if checkpoint_mode == "full" else None
            ),
            checkpoint_delta=checkpoint_delta,
            checkpoint_hash=checkpoint["checkpoint_hash"],
            checkpoint_signature=checkpoint["checkpoint_signature"],
        )
        self.details.append(
            type="group_frame",
            group_id=self.group_id,
            leader_public_key=self.leader_public_key,
            leader_epoch=self.leader_epoch,
            seq=self.seq,
            frame_kind=frame_kind,
            wire_version=core["wire_version"],
            previous_hash=core["previous_hash"],
            frame_hash=frame_hash,
            membership_version=self.membership_version,
            authority_state_hash=core["authority_state_hash"],
            recovery_state_hash=core["recovery_state_hash"],
            events_hash=core["events_hash"],
            events=events,
            checkpoint_mode=checkpoint_mode,
            checkpoint_delta_ops=len(checkpoint_delta),
            max_envelope_bytes=max(
                (len(canonical_json(item)) for item in envelopes.values()),
                default=0,
            ),
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
                "checkpoint_mode": checkpoint_mode,
                "completed": self.completed,
            },
        )
        self._publish_local_snapshot(
            phase="group_completed" if self.completed else "group_active",
            events=events,
            checkpoint_hash=checkpoint["checkpoint_hash"],
        )
        return envelopes

    def _publish_local_snapshot(
        self,
        *,
        phase: str,
        checkpoint_hash: str,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        view = copy.deepcopy(self._last_views.get(self.leader_public_key, {}))
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
                "checkpoint_hash": checkpoint_hash,
                "recovery_mode": self.config.recovery_mode,
                "peer_channels": {
                    "enabled": self.config.peer_channels_enabled,
                    "routing": self.config.peer_routing,
                    "channels": list(self.config.peer_channel_names),
                    "max_message_bytes": self.config.max_peer_message_bytes,
                },
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
        self.view: dict[str, Any] | None = None
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
        if envelope.get("wire_version") != 2:
            raise GroupProtocolError("group envelope wire version mismatch")
        core = {key: envelope.get(key) for key in _CORE_FIELDS}
        frame_hash = envelope.get("frame_hash")
        if not _is_hash(frame_hash) or json_hash(core) != frame_hash:
            raise GroupProtocolError("group frame hash mismatch")
        events = envelope.get("events")
        if not isinstance(events, list) or json_hash(events) != envelope.get("events_hash"):
            raise GroupProtocolError("group events hash mismatch")
        private_payload = {
            key: envelope.get(key) for key in _PRIVATE_PAYLOAD_FIELDS
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
            if (
                not bootstrap
                and envelope.get("previous_hash") != self.frame_hash
            ):
                raise GroupProtocolError("group frame hash chain mismatch")
        elif not bootstrap:
            raise GroupProtocolError("first group frame must be a bootstrap")

        checkpoint = self._decode_checkpoint(
            envelope,
            seq=seq,
            frame_hash=frame_hash,
            bootstrap=bootstrap,
        )
        view = self._decode_view(envelope, bootstrap=bootstrap)
        expected_checkpoint_hash = checkpoint.get("checkpoint_hash")

        self.seq = seq
        self.frame_hash = frame_hash
        self.membership_version = int(envelope.get("membership_version") or 0)
        self.checkpoint = copy.deepcopy(checkpoint)
        self.view = copy.deepcopy(view)
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
            group_view=view,
            group_events=envelope.get("events") or [],
            direct=envelope.get("direct") or [],
            completed=bool(envelope.get("completed", False)),
            outcome=envelope.get("outcome"),
        )
        self.details.append(
            type="group_frame",
            group_id=self.group_id,
            leader_public_key=self.leader_public_key,
            leader_epoch=self.leader_epoch,
            seq=seq,
            frame_hash=frame_hash,
            frame_kind=envelope.get("_group"),
            wire_version=envelope.get("wire_version"),
            previous_hash=envelope.get("previous_hash"),
            membership_version=self.membership_version,
            authority_state_hash=envelope.get("authority_state_hash"),
            recovery_state_hash=envelope.get("recovery_state_hash"),
            events_hash=envelope.get("events_hash"),
            events=envelope.get("events") or [],
            checkpoint_mode=envelope.get("checkpoint_mode"),
            checkpoint_delta_ops=len(envelope.get("checkpoint_delta") or []),
            view_mode=envelope.get("view_mode"),
            view_delta_ops=len(envelope.get("view_delta") or []),
            completed=bool(envelope.get("completed", False)),
            outcome=envelope.get("outcome"),
        )
        self.details.append(
            type="group_recovery_record",
            group_id=self.group_id,
            leader_public_key=self.leader_public_key,
            leader_epoch=self.leader_epoch,
            seq=seq,
            frame_hash=frame_hash,
            membership_version=self.membership_version,
            recovery_state_hash=envelope.get("recovery_state_hash"),
            checkpoint_mode=envelope.get("checkpoint_mode"),
            checkpoint=(
                envelope.get("checkpoint")
                if envelope.get("checkpoint_mode") == "full"
                else None
            ),
            checkpoint_delta=envelope.get("checkpoint_delta") or [],
            checkpoint_hash=expected_checkpoint_hash,
            checkpoint_signature=checkpoint.get("checkpoint_signature"),
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

    def _decode_checkpoint(
        self,
        envelope: dict[str, Any],
        *,
        seq: int,
        frame_hash: str,
        bootstrap: bool,
    ) -> dict[str, Any]:
        mode = envelope.get("checkpoint_mode")
        checkpoint_delta = envelope.get("checkpoint_delta")
        if not isinstance(checkpoint_delta, list):
            raise GroupProtocolError("group checkpoint delta must be an array")
        if mode == "full":
            checkpoint = envelope.get("checkpoint")
            if not isinstance(checkpoint, dict) or checkpoint_delta:
                raise GroupProtocolError(
                    "full group checkpoint payload is invalid"
                )
            checkpoint = copy.deepcopy(checkpoint)
        elif mode == "delta":
            if bootstrap or envelope.get("checkpoint") is not None:
                raise GroupProtocolError(
                    "bootstrap requires a full group checkpoint"
                )
            if self.checkpoint is None:
                raise GroupProtocolError(
                    "group checkpoint delta has no replay base"
                )
            try:
                checkpoint_state = apply_json_delta(
                    _checkpoint_state_from(self.checkpoint),
                    checkpoint_delta,
                )
            except JsonDeltaError as exc:
                raise GroupProtocolError(
                    f"group checkpoint delta is invalid: {exc}"
                ) from exc
            _validate_checkpoint_state(checkpoint_state)
            checkpoint = {
                "version": 1,
                "group_id": self.group_id,
                "leader_public_key": self.leader_public_key,
                "leader_epoch": self.leader_epoch,
                "seq": seq,
                "frame_hash": frame_hash,
                "membership_version": envelope.get("membership_version"),
                **copy.deepcopy(checkpoint_state),
                "checkpoint_hash": envelope.get("checkpoint_hash"),
                "checkpoint_signature": envelope.get(
                    "checkpoint_signature"
                ),
            }
        else:
            raise GroupProtocolError("group checkpoint mode is invalid")

        expected_checkpoint_hash = checkpoint.get("checkpoint_hash")
        checkpoint_signature = checkpoint.get("checkpoint_signature")
        unsigned_checkpoint = dict(checkpoint)
        unsigned_checkpoint.pop("checkpoint_hash", None)
        unsigned_checkpoint.pop("checkpoint_signature", None)
        if (
            checkpoint.get("version") != 1
            or not _is_hash(expected_checkpoint_hash)
            or json_hash(unsigned_checkpoint) != expected_checkpoint_hash
            or envelope.get("checkpoint_hash") != expected_checkpoint_hash
            or envelope.get("checkpoint_signature") != checkpoint_signature
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
            or checkpoint.get("completed")
            != envelope.get("completed")
            or checkpoint.get("outcome") != envelope.get("outcome")
        ):
            raise GroupProtocolError(
                "group checkpoint does not certify this frame"
            )
        checkpoint_state = _checkpoint_state_from(checkpoint)
        _validate_checkpoint_state(checkpoint_state)
        if json_hash(checkpoint_state) != envelope.get("recovery_state_hash"):
            raise GroupProtocolError("group recovery state hash mismatch")
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
        return checkpoint

    def _decode_view(
        self, envelope: dict[str, Any], *, bootstrap: bool
    ) -> dict[str, Any]:
        mode = envelope.get("view_mode")
        view_delta = envelope.get("view_delta")
        if not isinstance(view_delta, list):
            raise GroupProtocolError("group view delta must be an array")
        if mode == "full":
            view = envelope.get("view")
            if not isinstance(view, dict) or view_delta:
                raise GroupProtocolError("full group view payload is invalid")
            view = copy.deepcopy(view)
        elif mode == "delta":
            if bootstrap or envelope.get("view") is not None:
                raise GroupProtocolError("bootstrap requires a full group view")
            if self.view is None:
                raise GroupProtocolError("group view delta has no replay base")
            try:
                view = apply_json_delta(self.view, view_delta)
            except JsonDeltaError as exc:
                raise GroupProtocolError(
                    f"group view delta is invalid: {exc}"
                ) from exc
            if not isinstance(view, dict):
                raise GroupProtocolError("group view delta produced a non-object")
        else:
            raise GroupProtocolError("group view mode is invalid")
        _validate_json(view)
        if json_hash(view) != envelope.get("view_state_hash"):
            raise GroupProtocolError("group view state hash mismatch")
        return view

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


def _checkpoint_state_from(checkpoint: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in _CHECKPOINT_STATE_FIELDS if key not in checkpoint]
    if missing:
        raise GroupProtocolError(
            f"group checkpoint state is missing {missing[0]}"
        )
    return {
        key: copy.deepcopy(checkpoint[key])
        for key in _CHECKPOINT_STATE_FIELDS
    }


def _validate_checkpoint_state(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != set(
        _CHECKPOINT_STATE_FIELDS
    ):
        raise GroupProtocolError("group checkpoint state fields are invalid")
    members = value.get("members")
    if not isinstance(members, list):
        raise GroupProtocolError("group checkpoint members must be an array")
    normalize_members(members)
    last_client_seq = value.get("last_client_seq")
    if not isinstance(last_client_seq, dict) or any(
        not isinstance(public_key, str)
        or not isinstance(seq, int)
        or isinstance(seq, bool)
        or seq < 0
        for public_key, seq in last_client_seq.items()
    ):
        raise GroupProtocolError(
            "group checkpoint last_client_seq is invalid"
        )
    if value.get("recovery_mode") not in {
        "exact",
        "restart_round",
        "abort",
    }:
        raise GroupProtocolError("group checkpoint recovery_mode is invalid")
    if not isinstance(value.get("protocol_state"), dict):
        raise GroupProtocolError(
            "group checkpoint protocol_state must be an object"
        )
    if not isinstance(value.get("completed"), bool):
        raise GroupProtocolError("group checkpoint completed is invalid")
    if value.get("outcome") is not None and not isinstance(
        value.get("outcome"), str
    ):
        raise GroupProtocolError("group checkpoint outcome is invalid")
    _validate_json(value)


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
