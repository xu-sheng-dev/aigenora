from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aigenora.engine.keys import KeyPair, sign_raw, verify_raw
from aigenora.engine.p2p import (
    AsyncJsonLineChannel,
    connect_by_ticket,
    create_host_node,
)
from aigenora.proto.group import GroupProtocolError, canonical_json, json_hash
from aigenora.proto.sdk import EventBus


PEER_WIRE_VERSION = 1
MAX_TICKET_BYTES = 16384
MAX_PEER_ACTION_ATTEMPTS = 3
ZERO_HASH = "0" * 64


def build_peer_advertisement(
    *,
    group_id: str,
    protocol_id: str,
    leader_epoch: int,
    ticket: str,
    keypair: KeyPair,
) -> dict[str, Any]:
    _require_ticket(ticket)
    body = {
        "_group": "peer_advertise",
        "wire_version": PEER_WIRE_VERSION,
        "group_id": group_id,
        "protocol_id": protocol_id,
        "leader_epoch": leader_epoch,
        "member_public_key": keypair.public_key,
        "ticket": ticket,
        "ticket_hash": _ticket_hash(ticket),
    }
    return {**body, "signature": sign_raw(keypair.private_key, canonical_json(body))}


def verify_peer_advertisement(
    value: Any,
    *,
    group_id: str,
    protocol_id: str,
    leader_epoch: int,
    member_public_key: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GroupProtocolError("peer advertisement must be an object")
    body = {key: item for key, item in value.items() if key != "signature"}
    public_key = body.get("member_public_key")
    if (
        body.get("_group") != "peer_advertise"
        or body.get("wire_version") != PEER_WIRE_VERSION
        or body.get("group_id") != group_id
        or body.get("protocol_id") != protocol_id
        or body.get("leader_epoch") != leader_epoch
        or not _is_public_key(public_key)
        or (member_public_key is not None and public_key != member_public_key.lower())
    ):
        raise GroupProtocolError("peer advertisement context mismatch")
    ticket = body.get("ticket")
    _require_ticket(ticket)
    if body.get("ticket_hash") != _ticket_hash(ticket):
        raise GroupProtocolError("peer advertisement ticket hash mismatch")
    _verify_signature(public_key, body, value.get("signature"), "peer advertisement")
    return copy.deepcopy(value)


def build_peer_directory(
    *,
    group_id: str,
    protocol_id: str,
    leader_epoch: int,
    authority_seq: int,
    membership_version: int,
    viewer_public_key: str,
    routes: dict[str, tuple[str, ...]],
    advertisements: dict[str, dict[str, Any]],
    leader_keypair: KeyPair,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for recipient_public_key in sorted(routes):
        advertisement = advertisements.get(recipient_public_key)
        if advertisement is None:
            continue
        verified = verify_peer_advertisement(
            advertisement,
            group_id=group_id,
            protocol_id=protocol_id,
            leader_epoch=leader_epoch,
            member_public_key=recipient_public_key,
        )
        channels = list(routes[recipient_public_key])
        grant_body = {
            "wire_version": PEER_WIRE_VERSION,
            "group_id": group_id,
            "protocol_id": protocol_id,
            "leader_public_key": leader_keypair.public_key,
            "leader_epoch": leader_epoch,
            "authority_seq": authority_seq,
            "membership_version": membership_version,
            "sender_public_key": viewer_public_key.lower(),
            "recipient_public_key": recipient_public_key.lower(),
            "recipient_ticket_hash": verified["ticket_hash"],
            "channels": channels,
        }
        grant = {
            **grant_body,
            "signature": sign_raw(
                leader_keypair.private_key, canonical_json(grant_body)
            ),
        }
        entries.append(
            {
                "recipient_public_key": recipient_public_key.lower(),
                "ticket": verified["ticket"],
                "advertisement": verified,
                "grant": grant,
            }
        )
    body = {
        "_group": "peer_directory",
        "wire_version": PEER_WIRE_VERSION,
        "group_id": group_id,
        "protocol_id": protocol_id,
        "leader_public_key": leader_keypair.public_key,
        "leader_epoch": leader_epoch,
        "authority_seq": authority_seq,
        "membership_version": membership_version,
        "viewer_public_key": viewer_public_key.lower(),
        "entries": entries,
    }
    return {**body, "signature": sign_raw(leader_keypair.private_key, canonical_json(body))}


def verify_peer_directory(
    value: Any,
    *,
    group_id: str,
    protocol_id: str,
    leader_public_key: str,
    leader_epoch: int,
    authority_seq: int,
    membership_version: int,
    viewer_public_key: str,
    allowed_channels: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise GroupProtocolError("peer directory must be an object")
    body = {key: item for key, item in value.items() if key != "signature"}
    if (
        body.get("_group") != "peer_directory"
        or body.get("wire_version") != PEER_WIRE_VERSION
        or body.get("group_id") != group_id
        or body.get("protocol_id") != protocol_id
        or body.get("leader_public_key") != leader_public_key.lower()
        or body.get("leader_epoch") != leader_epoch
        or body.get("authority_seq") != authority_seq
        or body.get("membership_version") != membership_version
        or body.get("viewer_public_key") != viewer_public_key.lower()
    ):
        raise GroupProtocolError("peer directory context mismatch")
    _verify_signature(
        leader_public_key, body, value.get("signature"), "peer directory"
    )
    raw_entries = body.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) > 31:
        raise GroupProtocolError("peer directory entries are invalid")
    allowed = set(allowed_channels)
    entries: dict[str, dict[str, Any]] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise GroupProtocolError("peer directory entry must be an object")
        recipient = raw.get("recipient_public_key")
        if not _is_public_key(recipient) or recipient in entries:
            raise GroupProtocolError("peer directory recipient is invalid")
        advertisement = verify_peer_advertisement(
            raw.get("advertisement"),
            group_id=group_id,
            protocol_id=protocol_id,
            leader_epoch=leader_epoch,
            member_public_key=recipient,
        )
        if raw.get("ticket") != advertisement["ticket"]:
            raise GroupProtocolError("peer directory ticket mismatch")
        grant = _verify_peer_grant(
            raw.get("grant"),
            group_id=group_id,
            protocol_id=protocol_id,
            leader_public_key=leader_public_key,
            leader_epoch=leader_epoch,
            authority_seq=authority_seq,
            membership_version=membership_version,
            sender_public_key=viewer_public_key,
            recipient_public_key=recipient,
            recipient_ticket_hash=advertisement["ticket_hash"],
        )
        channels = grant.get("channels")
        if (
            not isinstance(channels, list)
            or not channels
            or len(channels) != len(set(channels))
            or not set(channels) <= allowed
        ):
            raise GroupProtocolError("peer directory grant channels are invalid")
        entries[recipient] = copy.deepcopy(raw)
    return entries


def build_peer_message(
    *,
    group_id: str,
    protocol_id: str,
    leader_public_key: str,
    leader_epoch: int,
    authority_seq: int,
    membership_version: int,
    message_id: str,
    sender_keypair: KeyPair,
    recipient_public_key: str,
    channel: str,
    payload: dict[str, Any],
    grant: dict[str, Any],
    max_message_bytes: int,
) -> dict[str, Any]:
    if not _is_message_id(message_id):
        raise GroupProtocolError("peer message_id must be 32-char lowercase hex")
    if not isinstance(payload, dict):
        raise GroupProtocolError("peer payload must be an object")
    if len(canonical_json(payload)) > max_message_bytes:
        raise GroupProtocolError("peer payload exceeds max_message_bytes")
    body = {
        "_peer": "message",
        "wire_version": PEER_WIRE_VERSION,
        "group_id": group_id,
        "protocol_id": protocol_id,
        "leader_public_key": leader_public_key.lower(),
        "leader_epoch": leader_epoch,
        "authority_seq": authority_seq,
        "membership_version": membership_version,
        "message_id": message_id,
        "sender_public_key": sender_keypair.public_key,
        "recipient_public_key": recipient_public_key.lower(),
        "channel": channel,
        "payload": copy.deepcopy(payload),
        "grant": copy.deepcopy(grant),
        "sent_at": _now(),
    }
    return {**body, "signature": sign_raw(sender_keypair.private_key, canonical_json(body))}


def verify_peer_message(
    value: Any,
    *,
    group_id: str,
    protocol_id: str,
    leader_public_key: str,
    leader_epoch: int,
    authority_seq: int,
    membership_version: int,
    recipient_public_key: str,
    recipient_ticket_hash: str,
    allowed_channels: tuple[str, ...],
    max_message_bytes: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GroupProtocolError("peer message must be an object")
    body = {key: item for key, item in value.items() if key != "signature"}
    sender = body.get("sender_public_key")
    channel = body.get("channel")
    message_id = body.get("message_id")
    if (
        body.get("_peer") != "message"
        or body.get("wire_version") != PEER_WIRE_VERSION
        or body.get("group_id") != group_id
        or body.get("protocol_id") != protocol_id
        or body.get("leader_public_key") != leader_public_key.lower()
        or body.get("leader_epoch") != leader_epoch
        or body.get("authority_seq") != authority_seq
        or body.get("membership_version") != membership_version
        or body.get("recipient_public_key") != recipient_public_key.lower()
        or not _is_public_key(sender)
        or not _is_message_id(message_id)
        or channel not in allowed_channels
    ):
        raise GroupProtocolError("peer message context mismatch")
    payload = body.get("payload")
    if not isinstance(payload, dict) or len(canonical_json(payload)) > max_message_bytes:
        raise GroupProtocolError("peer message payload is invalid")
    _verify_peer_grant(
        body.get("grant"),
        group_id=group_id,
        protocol_id=protocol_id,
        leader_public_key=leader_public_key,
        leader_epoch=leader_epoch,
        authority_seq=authority_seq,
        membership_version=membership_version,
        sender_public_key=sender,
        recipient_public_key=recipient_public_key,
        recipient_ticket_hash=recipient_ticket_hash,
        channel=channel,
    )
    _verify_signature(sender, body, value.get("signature"), "peer message")
    return copy.deepcopy(value)


def build_peer_receipt(
    *,
    message: dict[str, Any],
    envelope_hash: str,
    status: str,
    recipient_keypair: KeyPair,
) -> dict[str, Any]:
    if status not in {"accepted", "duplicate"}:
        raise GroupProtocolError("peer receipt status is invalid")
    body = {
        "_peer": "receipt",
        "wire_version": PEER_WIRE_VERSION,
        "group_id": message["group_id"],
        "protocol_id": message["protocol_id"],
        "leader_public_key": message["leader_public_key"],
        "leader_epoch": message["leader_epoch"],
        "authority_seq": message["authority_seq"],
        "membership_version": message["membership_version"],
        "message_id": message["message_id"],
        "envelope_hash": envelope_hash,
        "sender_public_key": message["sender_public_key"],
        "recipient_public_key": recipient_keypair.public_key,
        "status": status,
        "received_at": _now(),
    }
    return {**body, "signature": sign_raw(recipient_keypair.private_key, canonical_json(body))}


def verify_peer_receipt(
    value: Any,
    *,
    message: dict[str, Any],
    envelope_hash: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GroupProtocolError("peer receipt must be an object")
    body = {key: item for key, item in value.items() if key != "signature"}
    expected = {
        "_peer": "receipt",
        "wire_version": PEER_WIRE_VERSION,
        "group_id": message["group_id"],
        "protocol_id": message["protocol_id"],
        "leader_public_key": message["leader_public_key"],
        "leader_epoch": message["leader_epoch"],
        "authority_seq": message["authority_seq"],
        "membership_version": message["membership_version"],
        "message_id": message["message_id"],
        "envelope_hash": envelope_hash,
        "sender_public_key": message["sender_public_key"],
        "recipient_public_key": message["recipient_public_key"],
    }
    if any(body.get(key) != item for key, item in expected.items()):
        raise GroupProtocolError("peer receipt context mismatch")
    if body.get("status") not in {"accepted", "duplicate"}:
        raise GroupProtocolError("peer receipt status is invalid")
    _verify_signature(
        message["recipient_public_key"],
        body,
        value.get("signature"),
        "peer receipt",
    )
    return copy.deepcopy(value)


class PeerEvidenceLog:
    """Append-only, hash-chained local evidence for official peer messages."""

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / "group-peer-evidence.jsonl"
        self.record_seq = 0
        self.previous_hash = ZERO_HASH
        self.seen_received: dict[str, str] = {}
        for record in self.read_all(verify=True):
            self.record_seq = int(record["record_seq"])
            self.previous_hash = str(record["record_hash"])
            if record.get("direction") == "received":
                self.seen_received[str(record["message_id"])] = str(
                    record["envelope_hash"]
                )

    def append(self, **payload: Any) -> dict[str, Any]:
        body = {
            "record_seq": self.record_seq + 1,
            "previous_hash": self.previous_hash,
            "recorded_at": _now(),
            **copy.deepcopy(payload),
        }
        body["record_hash"] = json_hash(body)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
        self.record_seq = int(body["record_seq"])
        self.previous_hash = str(body["record_hash"])
        if body.get("direction") == "received":
            self.seen_received[str(body["message_id"])] = str(
                body["envelope_hash"]
            )
        return body

    def read_all(self, *, verify: bool = False) -> list[dict[str, Any]]:
        records = _read_jsonl(self.path)
        if not verify:
            return records
        previous = ZERO_HASH
        for index, record in enumerate(records, start=1):
            claimed = record.get("record_hash")
            unsigned = dict(record)
            unsigned.pop("record_hash", None)
            if (
                record.get("record_seq") != index
                or record.get("previous_hash") != previous
                or not isinstance(claimed, str)
                or json_hash(unsigned) != claimed
            ):
                raise GroupProtocolError("peer evidence hash chain is invalid")
            previous = claimed
        return records


class _PeerActionOutbox:
    VERSION = 1

    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir)
        self.action_path = self.root / "group-peer-actions.jsonl"
        self.state_path = self.root / "group-peer-outbox.json"
        self.committed_offset = 0
        self.pending: dict[str, Any] | None = None
        self._load()

    def prepare(self) -> dict[str, Any] | None:
        if self.pending is not None:
            return copy.deepcopy(self.pending)
        records = _read_jsonl_with_offsets(self.action_path, self.committed_offset)
        for entry, end_offset in records:
            if not _valid_peer_action(entry):
                self.committed_offset = end_offset
                continue
            self.pending = {
                **copy.deepcopy(entry),
                "end_offset": end_offset,
                "attempts": 0,
            }
            self._persist()
            return copy.deepcopy(self.pending)
        self._persist()
        return None

    def finish(self) -> None:
        if self.pending is None:
            return
        self.committed_offset = max(
            self.committed_offset, int(self.pending["end_offset"])
        )
        self.pending = None
        self._persist()

    def fail(self, reason: str) -> bool:
        if self.pending is None:
            return False
        self.pending["attempts"] = int(self.pending.get("attempts", 0)) + 1
        self.pending["last_error"] = reason[:256]
        terminal = int(self.pending["attempts"]) >= MAX_PEER_ACTION_ATTEMPTS
        self._persist()
        return terminal

    def _load(self) -> None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if not isinstance(value, dict) or value.get("version") != self.VERSION:
            return
        offset = value.get("committed_offset")
        pending = value.get("pending")
        if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
            self.committed_offset = offset
        if isinstance(pending, dict) and _valid_peer_action(pending):
            self.pending = copy.deepcopy(pending)

    def _persist(self) -> None:
        _atomic_json(
            self.state_path,
            {
                "version": self.VERSION,
                "committed_offset": self.committed_offset,
                "pending": self.pending,
            },
        )


class GroupPeerOverlay:
    """Per-Member Iroh listener and durable official side-channel outbox."""

    def __init__(
        self,
        *,
        runtime: Any,
        node: Any,
        accepted: asyncio.Queue[AsyncJsonLineChannel],
        ticket: str,
        state_dir: str | Path,
        group_id: str,
        protocol_id: str,
        keypair: KeyPair,
        allowed_channels: tuple[str, ...],
        max_message_bytes: int,
    ):
        self.runtime = runtime
        self.node = node
        self.accepted = accepted
        self.ticket = ticket
        self.ticket_hash = _ticket_hash(ticket)
        self.state_dir = Path(state_dir)
        self.group_id = group_id
        self.protocol_id = protocol_id
        self.keypair = keypair
        self.allowed_channels = allowed_channels
        self.max_message_bytes = max_message_bytes
        self.leader_public_key = ""
        self.leader_epoch = -1
        self.authority_seq = -1
        self.membership_version = -1
        self.directory: dict[str, dict[str, Any]] = {}
        self.events = EventBus(self.state_dir)
        self.evidence = PeerEvidenceLog(self.state_dir)
        self.outbox = _PeerActionOutbox(self.state_dir)
        self.tasks: list[asyncio.Task[None]] = []
        self.handler_tasks: set[asyncio.Task[None]] = set()
        self.stopped = asyncio.Event()

    @classmethod
    async def create(
        cls,
        *,
        state_dir: str | Path,
        group_id: str,
        protocol_id: str,
        keypair: KeyPair,
        allowed_channels: tuple[str, ...],
        max_message_bytes: int,
    ) -> "GroupPeerOverlay":
        runtime, node, accepted = await create_host_node()
        node_addr = await node.net().node_addr()
        ticket = runtime.ticket_from_addr(node_addr)
        overlay = cls(
            runtime=runtime,
            node=node,
            accepted=accepted,
            ticket=ticket,
            state_dir=state_dir,
            group_id=group_id,
            protocol_id=protocol_id,
            keypair=keypair,
            allowed_channels=allowed_channels,
            max_message_bytes=max_message_bytes,
        )
        _atomic_json(
            overlay.state_dir / "group-peer-listener.json",
            {
                "wire_version": PEER_WIRE_VERSION,
                "group_id": group_id,
                "protocol_id": protocol_id,
                "member_public_key": keypair.public_key,
                "ticket_hash": overlay.ticket_hash,
                "channels": list(allowed_channels),
                "max_message_bytes": max_message_bytes,
                "started_at": _now(),
            },
        )
        overlay.tasks = [
            asyncio.create_task(overlay._accept_loop()),
            asyncio.create_task(overlay._outbox_loop()),
        ]
        overlay.events.emit(
            "group_peer_listener_started",
            {
                "group_id": group_id,
                "ticket_hash": overlay.ticket_hash,
                "channels": list(allowed_channels),
            },
        )
        return overlay

    def update_context(
        self,
        *,
        leader_public_key: str,
        leader_epoch: int,
        authority_seq: int,
        membership_version: int,
    ) -> None:
        context = (
            leader_public_key.lower(),
            int(leader_epoch),
            int(authority_seq),
            int(membership_version),
        )
        current = (
            self.leader_public_key,
            self.leader_epoch,
            self.authority_seq,
            self.membership_version,
        )
        if context != current:
            self.directory = {}
        (
            self.leader_public_key,
            self.leader_epoch,
            self.authority_seq,
            self.membership_version,
        ) = context

    def advertisement(self) -> dict[str, Any]:
        if self.leader_epoch < 0:
            raise GroupProtocolError("peer overlay has no authority context")
        return build_peer_advertisement(
            group_id=self.group_id,
            protocol_id=self.protocol_id,
            leader_epoch=self.leader_epoch,
            ticket=self.ticket,
            keypair=self.keypair,
        )

    def install_directory(self, value: dict[str, Any]) -> None:
        entries = verify_peer_directory(
            value,
            group_id=self.group_id,
            protocol_id=self.protocol_id,
            leader_public_key=self.leader_public_key,
            leader_epoch=self.leader_epoch,
            authority_seq=self.authority_seq,
            membership_version=self.membership_version,
            viewer_public_key=self.keypair.public_key,
            allowed_channels=self.allowed_channels,
        )
        self.directory = entries
        _atomic_json(self.state_dir / "group-peer-directory.json", value)
        self.events.emit(
            "group_peer_directory_updated",
            {
                "group_id": self.group_id,
                "leader_epoch": self.leader_epoch,
                "authority_seq": self.authority_seq,
                "recipient_count": len(entries),
            },
        )

    async def close(self) -> None:
        if self.stopped.is_set():
            return
        self.stopped.set()
        for task in self.tasks:
            task.cancel()
        for task in self.handler_tasks:
            task.cancel()
        await asyncio.gather(
            *self.tasks, *self.handler_tasks, return_exceptions=True
        )
        await self.node.node().shutdown()

    async def _accept_loop(self) -> None:
        while not self.stopped.is_set():
            channel = await self.accepted.get()
            task = asyncio.create_task(self._handle_incoming(channel))
            self.handler_tasks.add(task)
            task.add_done_callback(self.handler_tasks.discard)

    async def _handle_incoming(self, channel: AsyncJsonLineChannel) -> None:
        try:
            raw = await channel.recv(timeout=10.0)
            message = verify_peer_message(
                raw,
                group_id=self.group_id,
                protocol_id=self.protocol_id,
                leader_public_key=self.leader_public_key,
                leader_epoch=self.leader_epoch,
                authority_seq=self.authority_seq,
                membership_version=self.membership_version,
                recipient_public_key=self.keypair.public_key,
                recipient_ticket_hash=self.ticket_hash,
                allowed_channels=self.allowed_channels,
                max_message_bytes=self.max_message_bytes,
            )
            envelope_hash = json_hash(message)
            seen_hash = self.evidence.seen_received.get(message["message_id"])
            if seen_hash is not None and seen_hash != envelope_hash:
                raise GroupProtocolError("peer message_id equivocation detected")
            status = "duplicate" if seen_hash is not None else "accepted"
            receipt = build_peer_receipt(
                message=message,
                envelope_hash=envelope_hash,
                status=status,
                recipient_keypair=self.keypair,
            )
            if status == "accepted":
                self.evidence.append(
                    direction="received",
                    group_id=self.group_id,
                    protocol_id=self.protocol_id,
                    leader_epoch=self.leader_epoch,
                    authority_seq=self.authority_seq,
                    message_id=message["message_id"],
                    envelope_hash=envelope_hash,
                    peer_public_key=message["sender_public_key"],
                    channel=message["channel"],
                    message=message,
                    receipt=receipt,
                )
                self.events.emit(
                    "group_peer_message_received",
                    {
                        "group_id": self.group_id,
                        "leader_epoch": self.leader_epoch,
                        "authority_seq": self.authority_seq,
                        "message_id": message["message_id"],
                        "sender_public_key": message["sender_public_key"],
                        "channel": message["channel"],
                        "envelope_hash": envelope_hash,
                    },
                )
            await channel.send(receipt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.events.emit(
                "group_peer_message_rejected",
                {"group_id": self.group_id, "reason": str(exc)[:256]},
            )
        finally:
            try:
                await channel.close()
            except Exception:
                pass

    async def _outbox_loop(self) -> None:
        while not self.stopped.is_set():
            pending = self.outbox.prepare()
            if pending is None:
                await asyncio.sleep(0.1)
                continue
            pending_context = (
                int(pending.get("leader_epoch", -1)),
                int(pending.get("authority_seq", -1)),
                int(pending.get("membership_version", -1)),
            )
            current_context = (
                self.leader_epoch,
                self.authority_seq,
                self.membership_version,
            )
            if pending_context != current_context:
                self.events.emit(
                    "group_peer_message_expired",
                    {
                        "group_id": self.group_id,
                        "message_id": pending["message_id"],
                        "queued_context": list(pending_context),
                        "current_context": list(current_context),
                    },
                )
                self.outbox.finish()
                continue
            recipient = str(pending["recipient_public_key"]).lower()
            entry = self.directory.get(recipient)
            if entry is None:
                await asyncio.sleep(0.2)
                continue
            try:
                await self._send_pending(pending, entry)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                terminal = self.outbox.fail(f"{type(exc).__name__}:{exc}")
                self.events.emit(
                    "group_peer_message_send_failed",
                    {
                        "group_id": self.group_id,
                        "message_id": pending["message_id"],
                        "recipient_public_key": recipient,
                        "terminal": terminal,
                        "reason": str(exc)[:256],
                    },
                )
                if terminal:
                    self.outbox.finish()
                await asyncio.sleep(0.5)
                continue
            self.outbox.finish()

    async def _send_pending(
        self, pending: dict[str, Any], entry: dict[str, Any]
    ) -> None:
        grant = entry["grant"]
        message = build_peer_message(
            group_id=self.group_id,
            protocol_id=self.protocol_id,
            leader_public_key=self.leader_public_key,
            leader_epoch=self.leader_epoch,
            authority_seq=self.authority_seq,
            membership_version=self.membership_version,
            message_id=str(pending["message_id"]),
            sender_keypair=self.keypair,
            recipient_public_key=str(pending["recipient_public_key"]),
            channel=str(pending["channel"]),
            payload=copy.deepcopy(pending["payload"]),
            grant=grant,
            max_message_bytes=self.max_message_bytes,
        )
        envelope_hash = json_hash(message)
        _, node, channel = await connect_by_ticket(str(entry["ticket"]))
        try:
            await channel.send(message)
            raw_receipt = await channel.recv(timeout=10.0)
            receipt = verify_peer_receipt(
                raw_receipt, message=message, envelope_hash=envelope_hash
            )
        finally:
            try:
                await channel.close()
            finally:
                await node.node().shutdown()
        self.evidence.append(
            direction="sent",
            group_id=self.group_id,
            protocol_id=self.protocol_id,
            leader_epoch=self.leader_epoch,
            authority_seq=self.authority_seq,
            message_id=message["message_id"],
            envelope_hash=envelope_hash,
            peer_public_key=message["recipient_public_key"],
            channel=message["channel"],
            message=message,
            receipt=receipt,
        )
        self.events.emit(
            "group_peer_message_sent",
            {
                "group_id": self.group_id,
                "leader_epoch": self.leader_epoch,
                "authority_seq": self.authority_seq,
                "message_id": message["message_id"],
                "recipient_public_key": message["recipient_public_key"],
                "channel": message["channel"],
                "envelope_hash": envelope_hash,
                "receipt_status": receipt["status"],
            },
        )


def queue_peer_action(
    state_dir: str | Path,
    *,
    recipient_public_key: str,
    channel: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not _is_public_key(recipient_public_key):
        raise GroupProtocolError("peer recipient must be a 64-char public key")
    if not isinstance(channel, str) or not channel:
        raise GroupProtocolError("peer channel must be non-empty")
    if not isinstance(payload, dict):
        raise GroupProtocolError("peer payload must be an object")
    root = Path(state_dir)
    listener = _read_json(root / "group-peer-listener.json")
    if not isinstance(listener, dict):
        raise GroupProtocolError("peer side channel is not active for this session")
    allowed_channels = listener.get("channels")
    maximum = listener.get("max_message_bytes")
    if not isinstance(allowed_channels, list) or channel not in allowed_channels:
        raise GroupProtocolError("peer channel is not declared by the protocol")
    if not isinstance(maximum, int) or len(canonical_json(payload)) > maximum:
        raise GroupProtocolError("peer payload exceeds max_message_bytes")
    directory = _read_json(root / "group-peer-directory.json")
    if not isinstance(directory, dict):
        raise GroupProtocolError("current peer route directory is unavailable")
    try:
        entries = verify_peer_directory(
            directory,
            group_id=str(listener.get("group_id") or ""),
            protocol_id=str(listener.get("protocol_id") or ""),
            leader_public_key=str(directory.get("leader_public_key") or ""),
            leader_epoch=int(directory.get("leader_epoch", -1)),
            authority_seq=int(directory.get("authority_seq", -1)),
            membership_version=int(directory.get("membership_version", -1)),
            viewer_public_key=str(listener.get("member_public_key") or ""),
            allowed_channels=tuple(str(item) for item in allowed_channels),
        )
    except (TypeError, ValueError, GroupProtocolError) as exc:
        raise GroupProtocolError("current peer route directory is invalid") from exc
    route = entries.get(recipient_public_key.lower())
    grant = route.get("grant") if isinstance(route, dict) else None
    granted_channels = grant.get("channels") if isinstance(grant, dict) else None
    if not isinstance(granted_channels, list) or channel not in granted_channels:
        raise GroupProtocolError("recipient and channel are not currently authorized")
    entry = {
        "message_id": secrets.token_hex(16),
        "recipient_public_key": recipient_public_key.lower(),
        "channel": channel,
        "payload": copy.deepcopy(payload),
        "leader_epoch": directory["leader_epoch"],
        "authority_seq": directory["authority_seq"],
        "membership_version": directory["membership_version"],
        "queued_at": _now(),
    }
    path = root / "group-peer-actions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
    return entry


def read_peer_messages(state_dir: str | Path) -> list[dict[str, Any]]:
    return PeerEvidenceLog(state_dir).read_all(verify=True)


def _verify_peer_grant(
    value: Any,
    *,
    group_id: str,
    protocol_id: str,
    leader_public_key: str,
    leader_epoch: int,
    authority_seq: int,
    membership_version: int,
    sender_public_key: str,
    recipient_public_key: str,
    recipient_ticket_hash: str,
    channel: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GroupProtocolError("peer grant must be an object")
    body = {key: item for key, item in value.items() if key != "signature"}
    expected = {
        "wire_version": PEER_WIRE_VERSION,
        "group_id": group_id,
        "protocol_id": protocol_id,
        "leader_public_key": leader_public_key.lower(),
        "leader_epoch": leader_epoch,
        "authority_seq": authority_seq,
        "membership_version": membership_version,
        "sender_public_key": sender_public_key.lower(),
        "recipient_public_key": recipient_public_key.lower(),
        "recipient_ticket_hash": recipient_ticket_hash,
    }
    if any(body.get(key) != item for key, item in expected.items()):
        raise GroupProtocolError("peer grant context mismatch")
    channels = body.get("channels")
    if not isinstance(channels, list) or channel is not None and channel not in channels:
        raise GroupProtocolError("peer grant does not allow this channel")
    _verify_signature(
        leader_public_key, body, value.get("signature"), "peer grant"
    )
    return copy.deepcopy(value)


def _verify_signature(
    public_key: str, body: dict[str, Any], signature: Any, label: str
) -> None:
    if not isinstance(signature, str):
        raise GroupProtocolError(f"{label} signature is missing")
    try:
        verify_raw(public_key, canonical_json(body), signature)
    except Exception as exc:
        raise GroupProtocolError(f"{label} signature is invalid") from exc


def _ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def _require_ticket(ticket: Any) -> None:
    if (
        not isinstance(ticket, str)
        or not ticket
        or len(ticket.encode("utf-8")) > MAX_TICKET_BYTES
    ):
        raise GroupProtocolError("peer ticket is invalid")


def _is_public_key(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_message_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_peer_action(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _is_message_id(value.get("message_id"))
        and _is_public_key(value.get("recipient_public_key"))
        and isinstance(value.get("channel"), str)
        and bool(value.get("channel"))
        and isinstance(value.get("payload"), dict)
        and isinstance(value.get("leader_epoch", -1), int)
        and not isinstance(value.get("leader_epoch", -1), bool)
        and isinstance(value.get("authority_seq", -1), int)
        and not isinstance(value.get("authority_seq", -1), bool)
        and isinstance(value.get("membership_version", -1), int)
        and not isinstance(value.get("membership_version", -1), bool)
        and isinstance(value.get("end_offset", 0), int)
        and not isinstance(value.get("end_offset", 0), bool)
        and isinstance(value.get("attempts", 0), int)
        and not isinstance(value.get("attempts", 0), bool)
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GroupProtocolError(f"invalid JSONL record in {path.name}") from exc
        if not isinstance(value, dict):
            raise GroupProtocolError(f"non-object JSONL record in {path.name}")
        records.append(value)
    return records


def _read_jsonl_with_offsets(
    path: Path, offset: int
) -> list[tuple[dict[str, Any], int]]:
    if not path.exists():
        return []
    records: list[tuple[dict[str, Any], int]] = []
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            line = handle.readline()
            if not line:
                break
            end_offset = handle.tell()
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                records.append(({}, end_offset))
                continue
            if isinstance(value, dict):
                records.append((value, end_offset))
            else:
                records.append(({}, end_offset))
    return records


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "GroupPeerOverlay",
    "PeerEvidenceLog",
    "build_peer_advertisement",
    "build_peer_directory",
    "build_peer_message",
    "build_peer_receipt",
    "queue_peer_action",
    "read_peer_messages",
    "verify_peer_advertisement",
    "verify_peer_directory",
    "verify_peer_message",
    "verify_peer_receipt",
]
