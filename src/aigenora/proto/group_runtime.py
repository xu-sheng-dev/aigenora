from __future__ import annotations

import asyncio
import copy
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from aigenora.engine.p2p import AsyncJsonLineChannel, ChannelClosed
from aigenora.proto.group import GroupAuthority, GroupProtocolError, GroupReplica
from aigenora.proto.group_peer import (
    GroupPeerOverlay,
    build_peer_directory,
    verify_peer_advertisement,
)
from aigenora.proto.sdk import EventBus


class _GroupActionOutbox:
    """Durable single-flight queue for local authoritative-group actions.

    The append-only action log belongs to the user-facing session API.  This
    sidecar records which byte range is in flight and the exact ``client_seq``
    assigned to it.  Reconnects therefore retry an unacknowledged action with
    the same sequence number instead of replaying the whole log as new input.
    """

    VERSION = 2

    def __init__(self, state_dir: str | Path, *, accepted_client_seq: int):
        self.root = Path(state_dir)
        self.action_path = self.root / "group-actions.jsonl"
        self.state_path = self.root / "group-action-outbox.json"
        self.sequence_path = self.root / "group-client-seq"
        self.accepted_client_seq = max(0, int(accepted_client_seq))
        self.committed_offset = 0
        self.pending: dict[str, Any] | None = None
        if not self._load():
            self.committed_offset = _legacy_committed_offset(
                self.action_path, self.accepted_client_seq
            )
        self.reconcile(self.accepted_client_seq)

    def reconcile(self, accepted_client_seq: int) -> None:
        """Align an in-flight action with an authoritative checkpoint."""
        accepted = max(0, int(accepted_client_seq))
        changed = False
        if self.pending is not None:
            pending_seq = int(self.pending["client_seq"])
            if pending_seq <= accepted:
                self.committed_offset = max(
                    self.committed_offset, int(self.pending["end_offset"])
                )
                self.pending = None
                changed = True
            elif pending_seq != accepted + 1:
                self.pending["client_seq"] = accepted + 1
                changed = True
        self.accepted_client_seq = accepted
        if changed or not self.state_path.exists():
            self._persist()
        self._write_sequence()

    def prepare(self) -> dict[str, Any] | None:
        """Return the sole in-flight action, allocating it durably if needed."""
        if self.pending is not None:
            return copy.deepcopy(self.pending)
        records, scanned_offset = _read_jsonl_records(
            self.action_path, self.committed_offset
        )
        for entry, end_offset in records:
            action = entry.get("action") if isinstance(entry, dict) else None
            if not isinstance(action, dict):
                continue
            self.pending = {
                "client_seq": self.accepted_client_seq + 1,
                "action_id": secrets.token_hex(16),
                "end_offset": end_offset,
                "action": copy.deepcopy(action),
            }
            self._persist()
            self._write_sequence()
            return copy.deepcopy(self.pending)
        if scanned_offset > self.committed_offset:
            self.committed_offset = scanned_offset
            self._persist()
        return None

    def handle_receipt(self, receipt: dict[str, Any]) -> bool:
        """Commit one action after a terminal receipt.

        ``False`` means the receipt is unrelated or explicitly retryable, so
        the sender keeps the same pending action and sequence number.
        """
        if self.pending is None:
            return False
        client_seq = receipt.get("client_seq")
        action_id = receipt.get("action_id")
        if (
            not isinstance(client_seq, int)
            or isinstance(client_seq, bool)
            or client_seq != self.pending["client_seq"]
            or action_id != self.pending["action_id"]
        ):
            return False
        status = receipt.get("status")
        if status not in {"accepted", "duplicate", "rejected"}:
            return False
        if status == "rejected" and receipt.get("retryable") is True:
            return False
        if status in {"accepted", "duplicate"}:
            self.accepted_client_seq = max(
                self.accepted_client_seq, client_seq
            )
        self.committed_offset = max(
            self.committed_offset, int(self.pending["end_offset"])
        )
        self.pending = None
        self._persist()
        self._write_sequence()
        return True

    def _load(self) -> bool:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(value, dict):
            return False
        version = value.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version not in {1, self.VERSION}
        ):
            return False
        committed_offset = value.get("committed_offset")
        pending = value.get("pending")
        if (
            not isinstance(committed_offset, int)
            or isinstance(committed_offset, bool)
            or committed_offset < 0
        ):
            return False
        if pending is not None:
            if version == 1:
                if not _valid_legacy_pending_action(pending):
                    return False
                pending = copy.deepcopy(pending)
                pending["action_id"] = secrets.token_hex(16)
            elif not _valid_pending_action(pending):
                return False
        self.committed_offset = committed_offset
        self.pending = copy.deepcopy(pending)
        if version == 1:
            self._persist()
        return True

    def _persist(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_json(
            self.state_path,
            {
                "version": self.VERSION,
                "committed_offset": self.committed_offset,
                "pending": self.pending,
            },
        )

    def _write_sequence(self) -> None:
        sequence = self.accepted_client_seq
        if self.pending is not None:
            sequence = max(sequence, int(self.pending["client_seq"]))
        _atomic_text(self.sequence_path, str(sequence))


class GroupLeaderHub:
    """Multiplex independent Guest channels into one ordered authority queue."""

    def __init__(
        self,
        authority: GroupAuthority,
        *,
        protocol_id: str | None = None,
        peer_overlay: GroupPeerOverlay | None = None,
        ack_timeout: float = 15.0,
        ack_check_interval: float = 0.5,
        send_timeout: float = 5.0,
    ):
        self.authority = authority
        self.protocol_id = protocol_id
        self.peer_overlay = peer_overlay
        self.peer_advertisements: dict[str, dict[str, Any]] = {}
        self.channels: dict[str, AsyncJsonLineChannel] = {}
        self.receivers: dict[str, asyncio.Task[None]] = {}
        self.queue: asyncio.Queue[tuple[str, str, Any]] = asyncio.Queue()
        self.stopped = asyncio.Event()
        self.local_outbox = _GroupActionOutbox(
            authority.state_dir,
            accepted_client_seq=authority.last_client_seq.get(
                authority.leader_public_key, 0
            ),
        )
        self.local_action_inflight: int | None = None
        self.local_action_ready = asyncio.Event()
        self.events = EventBus(authority.state_dir)
        self.acknowledged_seq: dict[str, int] = {}
        self.pending_ack: dict[str, tuple[int, float]] = {}
        self.ack_timeout = max(0.05, float(ack_timeout))
        self.ack_check_interval = max(0.01, float(ack_check_interval))
        self.send_timeout = max(0.05, float(send_timeout))
        self._broadcast_lock = asyncio.Lock()
        self._send_locks: dict[str, asyncio.Lock] = {}
        if self.peer_overlay is not None:
            if not self.protocol_id:
                raise GroupProtocolError("peer overlay requires protocol_id")
            self.peer_advertisements[
                authority.leader_public_key
            ] = self.peer_overlay.advertisement()

    async def attach(
        self,
        member: dict[str, Any],
        channel: AsyncJsonLineChannel,
        *,
        membership_version: int,
    ) -> None:
        public_key = str(member["public_key"]).lower()
        self._reset_ack_tracking(public_key)
        previous = self.channels.pop(public_key, None)
        if previous is not None:
            await self._bounded_close(previous)
        old_task = self.receivers.pop(public_key, None)
        if old_task is not None:
            old_task.cancel()
        self._send_locks[public_key] = asyncio.Lock()
        self.channels[public_key] = channel
        self.receivers[public_key] = asyncio.create_task(
            self._receiver(public_key, channel)
        )
        async with self._broadcast_lock:
            envelopes = self.authority.add_member(
                member, membership_version=membership_version
            )
            await self._broadcast_unlocked(envelopes)
            await self._publish_peer_directories_unlocked()
        self.events.emit(
            "group_member_connected",
            {
                "group_id": self.authority.group_id,
                "public_key": public_key,
                "seat": member.get("seat"),
                "membership_version": membership_version,
            },
        )

    async def attach_existing(
        self,
        member: dict[str, Any],
        channel: AsyncJsonLineChannel,
        *,
        send_snapshot: bool = True,
    ) -> None:
        """Attach/re-attach an already represented authority member."""
        public_key = str(member["public_key"]).lower()
        self._reset_ack_tracking(public_key)
        previous = self.channels.pop(public_key, None)
        if previous is not None:
            await self._bounded_close(previous)
        old_task = self.receivers.pop(public_key, None)
        if old_task is not None:
            old_task.cancel()
        self._send_locks[public_key] = asyncio.Lock()
        self.channels[public_key] = channel
        self.receivers[public_key] = asyncio.create_task(
            self._receiver(public_key, channel)
        )
        if send_snapshot:
            async with self._broadcast_lock:
                envelope = self.authority.bootstrap_envelope(public_key)
                if envelope is None:
                    raise GroupProtocolError(
                        "authority has no bootstrap view for the existing member"
                    )
                sent = await self._send_to_member(
                    public_key,
                    channel,
                    envelope,
                    failure_reason="bootstrap_send_failed",
                    track_envelope=True,
                )
            if not sent:
                return
        async with self._broadcast_lock:
            await self._publish_peer_directories_unlocked()
        self.events.emit(
            "group_member_reconnected",
            {
                "group_id": self.authority.group_id,
                "public_key": public_key,
                "seat": member.get("seat"),
            },
        )

    async def broadcast(
        self, envelopes: dict[str, dict[str, Any]]
    ) -> None:
        async with self._broadcast_lock:
            await self._broadcast_unlocked(envelopes)
            await self._publish_peer_directories_unlocked()

    async def _broadcast_unlocked(
        self, envelopes: dict[str, dict[str, Any]]
    ) -> None:
        sends = [
            self._send_to_member(
                public_key,
                channel,
                envelope,
                failure_reason="send_failed",
                track_envelope=True,
            )
            for public_key, channel in list(self.channels.items())
            if (envelope := envelopes.get(public_key)) is not None
        ]
        if sends:
            await asyncio.gather(*sends)

    async def _send_to_member(
        self,
        public_key: str,
        channel: AsyncJsonLineChannel,
        message: dict[str, Any],
        *,
        failure_reason: str,
        track_envelope: bool = False,
    ) -> bool:
        lock = self._send_locks.setdefault(public_key, asyncio.Lock())
        async with lock:
            if self.channels.get(public_key) is not channel:
                return False
            try:
                await asyncio.wait_for(
                    channel.send(message), timeout=self.send_timeout
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                if self.channels.get(public_key) is channel:
                    await self.detach(
                        public_key, reason=f"{failure_reason}_timeout"
                    )
                return False
            except Exception:
                if self.channels.get(public_key) is channel:
                    await self.detach(public_key, reason=failure_reason)
                return False
            if self.channels.get(public_key) is not channel:
                return False
            if track_envelope:
                self._track_sent_envelope(public_key, message)
            return True

    async def _bounded_close(
        self, channel: AsyncJsonLineChannel
    ) -> None:
        try:
            await asyncio.wait_for(
                channel.close(), timeout=self.send_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def detach(self, public_key: str, *, reason: str) -> None:
        channel = self.channels.pop(public_key, None)
        task = self.receivers.pop(public_key, None)
        self._send_locks.pop(public_key, None)
        self._reset_ack_tracking(public_key)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if channel is not None:
            await self._bounded_close(channel)
        self.events.emit(
            "group_member_disconnected",
            {
                "group_id": self.authority.group_id,
                "public_key": public_key,
                "reason": reason,
            },
        )

    async def run(self) -> dict[str, Any]:
        action_task = asyncio.create_task(self._local_action_reader())
        ack_task = asyncio.create_task(self._ack_watchdog())
        try:
            while not self.stopped.is_set():
                source, public_key, payload = await self.queue.get()
                if source == "disconnect":
                    await self.detach(public_key, reason=str(payload))
                    continue
                if source == "ack":
                    seq = payload.get("seq") if isinstance(payload, dict) else None
                    frame_hash = (
                        payload.get("frame_hash")
                        if isinstance(payload, dict)
                        else None
                    )
                    checkpoint_hash = (
                        payload.get("checkpoint_hash")
                        if isinstance(payload, dict)
                        else None
                    )
                    if (
                        isinstance(seq, int)
                        and not isinstance(seq, bool)
                        and isinstance(frame_hash, str)
                        and isinstance(checkpoint_hash, str)
                        and self.authority.acknowledge(
                            public_key,
                            seq,
                            frame_hash,
                            checkpoint_hash,
                        )
                    ):
                        previous_seq = self.acknowledged_seq.get(public_key, -1)
                        self.acknowledged_seq[public_key] = max(
                            seq, previous_seq
                        )
                        pending = self.pending_ack.get(public_key)
                        if pending is not None:
                            required_seq, pending_since = pending
                            if seq >= required_seq:
                                self.pending_ack.pop(public_key, None)
                            elif seq > previous_seq:
                                self.pending_ack[public_key] = (
                                    required_seq,
                                    time.monotonic(),
                                )
                    continue
                if source == "peer_advertise":
                    if self.peer_overlay is None or not self.protocol_id:
                        continue
                    try:
                        advertisement = verify_peer_advertisement(
                            payload,
                            group_id=self.authority.group_id,
                            protocol_id=self.protocol_id,
                            leader_epoch=self.authority.leader_epoch,
                            member_public_key=public_key,
                        )
                    except GroupProtocolError as exc:
                        self.events.emit(
                            "group_peer_advertisement_rejected",
                            {
                                "public_key": public_key,
                                "reason": str(exc)[:256],
                            },
                        )
                        continue
                    self.peer_advertisements[public_key] = advertisement
                    async with self._broadcast_lock:
                        await self._publish_peer_directories_unlocked()
                    self.events.emit(
                        "group_peer_advertisement_accepted",
                        {
                            "public_key": public_key,
                            "ticket_hash": advertisement["ticket_hash"],
                        },
                    )
                    continue
                if source != "action" or not isinstance(payload, dict):
                    continue
                receipt: dict[str, Any]
                action_id = payload.get("action_id")
                try:
                    if not _valid_action_id(action_id):
                        raise GroupProtocolError(
                            "action_id must be 32-char lowercase hex"
                        )
                    async with self._broadcast_lock:
                        envelopes, receipt = self.authority.apply_input(
                            actor_public_key=public_key,
                            client_seq=int(payload["client_seq"]),
                            action=payload["action"],
                        )
                        if receipt.get("status") != "duplicate":
                            await self._broadcast_unlocked(envelopes)
                            await self._publish_peer_directories_unlocked()
                except (KeyError, TypeError, ValueError, GroupProtocolError) as exc:
                    receipt = {
                        "_group": "receipt",
                        "status": "rejected",
                        "reason": str(exc)[:256],
                        "client_seq": payload.get("client_seq"),
                        "action_id": action_id,
                    }
                    self._finish_local_action(public_key, receipt)
                    await self._send_receipt(public_key, receipt)
                    self.events.emit(
                        "group_action_rejected",
                        {
                            "public_key": public_key,
                            "reason": str(exc)[:256],
                        },
                    )
                    continue
                wire_receipt = {
                    "_group": "receipt",
                    **receipt,
                    "action_id": action_id,
                }
                self._finish_local_action(public_key, wire_receipt)
                await self._send_receipt(public_key, wire_receipt)
                if self.authority.completed:
                    self.stopped.set()
            return {
                "completed": self.authority.completed,
                "outcome": self.authority.outcome,
                "seq": self.authority.seq,
                "checkpoint": self.authority.checkpoint(),
            }
        finally:
            action_task.cancel()
            ack_task.cancel()
            for task in list(self.receivers.values()):
                task.cancel()
            await asyncio.gather(
                action_task,
                ack_task,
                *self.receivers.values(),
                return_exceptions=True,
            )

    async def close(self) -> None:
        self.stopped.set()
        for public_key in list(self.channels):
            await self.detach(public_key, reason="leader_shutdown")

    async def _receiver(
        self, public_key: str, channel: AsyncJsonLineChannel
    ) -> None:
        try:
            while not self.stopped.is_set():
                message = await channel.recv()
                if not isinstance(message, dict):
                    continue
                kind = message.get("_group")
                if kind == "input":
                    if (
                        message.get("group_id") != self.authority.group_id
                        or message.get("leader_epoch")
                        != self.authority.leader_epoch
                        or message.get("actor_public_key") != public_key
                    ):
                        await self._send_receipt(
                            public_key,
                            {
                                "_group": "receipt",
                                "status": "rejected",
                                "reason": "group, epoch, or actor mismatch",
                                "client_seq": message.get("client_seq"),
                                "action_id": message.get("action_id"),
                                "retryable": True,
                            },
                        )
                        continue
                    await self.queue.put(("action", public_key, message))
                elif kind == "ack":
                    await self.queue.put(("ack", public_key, message))
                elif kind == "peer_advertise":
                    await self.queue.put(("peer_advertise", public_key, message))
                elif kind == "ping":
                    await self._send_to_member(
                        public_key,
                        channel,
                        {
                            "_group": "pong",
                            "group_id": self.authority.group_id,
                            "leader_epoch": self.authority.leader_epoch,
                            "nonce": message.get("nonce"),
                        },
                        failure_reason="pong_send_failed",
                    )
        except (ChannelClosed, asyncio.CancelledError):
            if not self.stopped.is_set():
                await self.queue.put(("disconnect", public_key, "channel_closed"))
        except Exception as exc:
            if not self.stopped.is_set():
                await self.queue.put(
                    ("disconnect", public_key, f"receiver_error:{type(exc).__name__}")
                )

    async def _send_receipt(
        self, public_key: str, receipt: dict[str, Any]
    ) -> None:
        if public_key == self.authority.leader_public_key:
            self.events.emit("group_action_receipt", receipt)
            return
        channel = self.channels.get(public_key)
        if channel is None:
            return
        await self._send_to_member(
            public_key,
            channel,
            receipt,
            failure_reason="receipt_send_failed",
        )

    async def _local_action_reader(self) -> None:
        while not self.stopped.is_set():
            pending = self.local_outbox.prepare()
            if pending is None:
                await asyncio.sleep(0.1)
                continue
            self.local_action_inflight = int(pending["client_seq"])
            await self.queue.put(
                (
                    "action",
                    self.authority.leader_public_key,
                    {
                        "_group": "input",
                        "group_id": self.authority.group_id,
                        "leader_epoch": self.authority.leader_epoch,
                        "actor_public_key": self.authority.leader_public_key,
                        "client_seq": pending["client_seq"],
                        "action_id": pending["action_id"],
                        "action": pending["action"],
                    },
                )
            )
            await self.local_action_ready.wait()
            self.local_action_ready.clear()

    def _finish_local_action(
        self, public_key: str, receipt: dict[str, Any]
    ) -> None:
        if public_key != self.authority.leader_public_key:
            return
        if self.local_outbox.handle_receipt(receipt):
            self.local_action_inflight = None
            self.local_action_ready.set()

    def _track_sent_envelope(
        self, public_key: str, envelope: dict[str, Any]
    ) -> None:
        seq = envelope.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return
        if seq <= self.acknowledged_seq.get(public_key, -1):
            return
        pending = self.pending_ack.get(public_key)
        if pending is None:
            self.pending_ack[public_key] = (seq, time.monotonic())
            return
        required_seq, pending_since = pending
        self.pending_ack[public_key] = (
            max(required_seq, seq),
            pending_since,
        )

    def _reset_ack_tracking(self, public_key: str) -> None:
        self.acknowledged_seq.pop(public_key, None)
        self.pending_ack.pop(public_key, None)

    async def _ack_watchdog(self) -> None:
        while not self.stopped.is_set():
            await asyncio.sleep(self.ack_check_interval)
            now = time.monotonic()
            stale = [
                public_key
                for public_key, (_, pending_since) in self.pending_ack.items()
                if now - pending_since >= self.ack_timeout
            ]
            for public_key in stale:
                if public_key in self.channels:
                    await self.detach(public_key, reason="frame_ack_timeout")

    async def _publish_peer_directories_unlocked(self) -> None:
        if self.peer_overlay is None or not self.protocol_id:
            return
        self.peer_overlay.update_context(
            leader_public_key=self.authority.leader_public_key,
            leader_epoch=self.authority.leader_epoch,
            authority_seq=self.authority.seq,
            membership_version=self.authority.membership_version,
        )
        deliveries = []
        for member in self.authority.members:
            if member.get("status") != "active":
                continue
            viewer_public_key = str(member["public_key"])
            try:
                routes = self.authority.peer_routes(viewer_public_key)
            except GroupProtocolError as exc:
                self.events.emit(
                    "group_peer_routes_rejected",
                    {
                        "viewer_public_key": viewer_public_key,
                        "reason": str(exc)[:256],
                    },
                )
                routes = {}
            directory = build_peer_directory(
                group_id=self.authority.group_id,
                protocol_id=self.protocol_id,
                leader_epoch=self.authority.leader_epoch,
                authority_seq=self.authority.seq,
                membership_version=self.authority.membership_version,
                viewer_public_key=viewer_public_key,
                routes=routes,
                advertisements=self.peer_advertisements,
                leader_keypair=self.authority.keypair,
            )
            if viewer_public_key == self.authority.leader_public_key:
                self.peer_overlay.install_directory(directory)
                continue
            channel = self.channels.get(viewer_public_key)
            if channel is None:
                continue
            deliveries.append(
                self._send_to_member(
                    viewer_public_key,
                    channel,
                    directory,
                    failure_reason="peer_directory_send_failed",
                )
            )
        if deliveries:
            await asyncio.gather(*deliveries)


async def run_group_guest_channel(
    *,
    channel: AsyncJsonLineChannel,
    replica: GroupReplica,
    state_dir: str | Path,
    first_envelope: dict[str, Any],
    peer_overlay: GroupPeerOverlay | None = None,
) -> dict[str, Any]:
    """Run one Guest connection until completion or Leader disconnect."""
    root = Path(state_dir)
    events = EventBus(root)

    replica.apply(first_envelope, bootstrap=True)
    if peer_overlay is not None:
        peer_overlay.update_context(
            leader_public_key=replica.leader_public_key,
            leader_epoch=replica.leader_epoch,
            authority_seq=int(replica.seq or 0),
            membership_version=replica.membership_version,
        )
        await channel.send(peer_overlay.advertisement())
    accepted_client_seq = _checkpoint_client_seq(
        replica.checkpoint, replica.viewer_public_key
    )
    outbox = _GroupActionOutbox(
        root, accepted_client_seq=accepted_client_seq
    )
    await channel.send(
        {
            "_group": "ack",
            "group_id": replica.group_id,
            "leader_epoch": replica.leader_epoch,
            "seq": replica.seq,
            "frame_hash": replica.frame_hash,
            "checkpoint_hash": replica.checkpoint["checkpoint_hash"],
        }
    )
    if first_envelope.get("completed"):
        return {
            "completed": True,
            "outcome": first_envelope.get("outcome"),
            "checkpoint": replica.checkpoint,
        }

    async def sender() -> None:
        while True:
            pending = outbox.prepare()
            if pending is None:
                await asyncio.sleep(0.1)
                continue
            await channel.send(
                {
                    "_group": "input",
                    "group_id": replica.group_id,
                    "leader_epoch": replica.leader_epoch,
                    "actor_public_key": replica.viewer_public_key,
                    "client_seq": pending["client_seq"],
                    "action_id": pending["action_id"],
                    "action": pending["action"],
                }
            )
            try:
                await asyncio.wait_for(action_ready.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            action_ready.clear()

    action_ready = asyncio.Event()
    sender_task = asyncio.create_task(sender())
    terminal_result: dict[str, Any] | None = None
    terminal_client_seq: int | None = None
    terminal_action_id: str | None = None

    def finish_from_terminal_checkpoint() -> dict[str, Any]:
        assert terminal_result is not None
        assert terminal_client_seq is not None
        assert terminal_action_id is not None
        events.emit(
            "group_action_receipt",
            {
                "_group": "receipt",
                "status": "accepted",
                "client_seq": terminal_client_seq,
                "action_id": terminal_action_id,
                "authority_seq": replica.seq,
                "frame_hash": replica.frame_hash,
                "completed": True,
                "source": "terminal_checkpoint",
            },
        )
        return terminal_result

    try:
        while True:
            try:
                message = await channel.recv(
                    timeout=2.0 if terminal_result is not None else None
                )
            except TimeoutError:
                if terminal_result is not None:
                    return finish_from_terminal_checkpoint()
                raise
            if not isinstance(message, dict):
                continue
            kind = message.get("_group")
            if kind in {"frame", "membership", "epoch_start", "snapshot"}:
                applied = replica.apply(message)
                if applied:
                    if peer_overlay is not None:
                        peer_overlay.update_context(
                            leader_public_key=replica.leader_public_key,
                            leader_epoch=replica.leader_epoch,
                            authority_seq=int(replica.seq or 0),
                            membership_version=replica.membership_version,
                        )
                    await channel.send(
                        {
                            "_group": "ack",
                            "group_id": replica.group_id,
                            "leader_epoch": replica.leader_epoch,
                            "seq": replica.seq,
                            "frame_hash": replica.frame_hash,
                            "checkpoint_hash": replica.checkpoint[
                                "checkpoint_hash"
                            ],
                        }
                    )
                if message.get("completed"):
                    result = {
                        "completed": True,
                        "outcome": message.get("outcome"),
                        "checkpoint": replica.checkpoint,
                    }
                    pending = outbox.pending
                    accepted_seq = _checkpoint_client_seq(
                        replica.checkpoint, replica.viewer_public_key
                    )
                    if (
                        isinstance(pending, dict)
                        and int(pending["client_seq"]) <= accepted_seq
                    ):
                        terminal_result = result
                        terminal_client_seq = int(pending["client_seq"])
                        terminal_action_id = str(pending["action_id"])
                        outbox.reconcile(accepted_seq)
                        sender_task.cancel()
                        await asyncio.gather(
                            sender_task, return_exceptions=True
                        )
                        continue
                    return result
            elif kind == "receipt":
                events.emit("group_action_receipt", message)
                if outbox.handle_receipt(message):
                    action_ready.set()
                if (
                    terminal_result is not None
                    and message.get("client_seq") == terminal_client_seq
                    and message.get("action_id") == terminal_action_id
                    and message.get("status") in {"accepted", "duplicate"}
                ):
                    return terminal_result
            elif kind == "peer_directory" and peer_overlay is not None:
                peer_overlay.install_directory(message)
            elif kind == "pong":
                events.emit(
                    "group_pong",
                    {
                        "group_id": replica.group_id,
                        "leader_epoch": replica.leader_epoch,
                        "nonce": message.get("nonce"),
                    },
                )
    except ChannelClosed:
        if terminal_result is not None:
            return finish_from_terminal_checkpoint()
        return {
            "completed": False,
            "reason": "leader_disconnected",
            "checkpoint": replica.checkpoint,
            "seq": replica.seq,
            "frame_hash": replica.frame_hash,
        }
    finally:
        sender_task.cancel()
        await asyncio.gather(sender_task, return_exceptions=True)


def _read_jsonl(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    records, new_offset = _read_jsonl_records(path, offset)
    return [entry for entry, _end_offset in records], new_offset


def _read_jsonl_records(
    path: Path, offset: int
) -> tuple[list[tuple[dict[str, Any], int]], int]:
    if not path.exists():
        return [], offset
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return [], offset
    if not chunk:
        return [], offset
    last_newline = chunk.rfind(b"\n")
    if last_newline < 0:
        return [], offset
    complete = chunk[: last_newline + 1]
    new_offset = offset + len(complete)
    result: list[tuple[dict[str, Any], int]] = []
    line_offset = offset
    for raw_with_newline in complete.splitlines(keepends=True):
        line_offset += len(raw_with_newline)
        raw = raw_with_newline.rstrip(b"\r\n")
        if not raw.strip():
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            result.append((value, line_offset))
    return result, new_offset


def _legacy_committed_offset(path: Path, accepted_client_seq: int) -> int:
    """Migrate the old cursor-less format using checkpoint sequence evidence."""
    if accepted_client_seq <= 0:
        return 0
    records, scanned_offset = _read_jsonl_records(path, 0)
    consumed = 0
    committed_offset = 0
    for entry, end_offset in records:
        if isinstance(entry.get("action"), dict):
            consumed += 1
            committed_offset = end_offset
            if consumed >= accepted_client_seq:
                return committed_offset
    return scanned_offset


def _checkpoint_client_seq(
    checkpoint: dict[str, Any] | None, public_key: str
) -> int:
    if not isinstance(checkpoint, dict):
        return 0
    values = checkpoint.get("last_client_seq")
    if not isinstance(values, dict):
        return 0
    value = values.get(public_key.lower(), values.get(public_key))
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value


def _valid_pending_action(value: Any) -> bool:
    return _valid_legacy_pending_action(value) and _valid_action_id(
        value.get("action_id")
    )


def _valid_legacy_pending_action(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    client_seq = value.get("client_seq")
    end_offset = value.get("end_offset")
    return (
        isinstance(client_seq, int)
        and not isinstance(client_seq, bool)
        and client_seq > 0
        and isinstance(end_offset, int)
        and not isinstance(end_offset, bool)
        and end_offset >= 0
        and isinstance(value.get("action"), dict)
    )


def _valid_action_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)
