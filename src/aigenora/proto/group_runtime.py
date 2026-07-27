from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aigenora.engine.p2p import AsyncJsonLineChannel, ChannelClosed
from aigenora.proto.group import GroupAuthority, GroupProtocolError, GroupReplica
from aigenora.proto.sdk import EventBus


class GroupLeaderHub:
    """Multiplex independent Guest channels into one ordered authority queue."""

    def __init__(self, authority: GroupAuthority):
        self.authority = authority
        self.channels: dict[str, AsyncJsonLineChannel] = {}
        self.receivers: dict[str, asyncio.Task[None]] = {}
        self.queue: asyncio.Queue[tuple[str, str, Any]] = asyncio.Queue()
        self.stopped = asyncio.Event()
        self.local_client_seq = authority.last_client_seq.get(
            authority.leader_public_key, 0
        )
        self.action_path = authority.state_dir / "group-actions.jsonl"
        self.action_offset = 0
        self.events = EventBus(authority.state_dir)
        self.acknowledged_seq: dict[str, int] = {}

    async def attach(
        self,
        member: dict[str, Any],
        channel: AsyncJsonLineChannel,
        *,
        membership_version: int,
    ) -> None:
        public_key = str(member["public_key"]).lower()
        previous = self.channels.pop(public_key, None)
        if previous is not None:
            try:
                await previous.close()
            except Exception:
                pass
        old_task = self.receivers.pop(public_key, None)
        if old_task is not None:
            old_task.cancel()
        self.channels[public_key] = channel
        self.receivers[public_key] = asyncio.create_task(
            self._receiver(public_key, channel)
        )
        envelopes = self.authority.add_member(
            member, membership_version=membership_version
        )
        await self.broadcast(envelopes)
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
        previous = self.channels.pop(public_key, None)
        if previous is not None:
            try:
                await previous.close()
            except Exception:
                pass
        old_task = self.receivers.pop(public_key, None)
        if old_task is not None:
            old_task.cancel()
        self.channels[public_key] = channel
        self.receivers[public_key] = asyncio.create_task(
            self._receiver(public_key, channel)
        )
        if send_snapshot:
            envelope = self.authority.bootstrap_envelopes().get(public_key)
            if envelope is None:
                raise GroupProtocolError(
                    "authority has no bootstrap view for the existing member"
                )
            await channel.send(envelope)
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
        failures: list[str] = []
        for public_key, channel in list(self.channels.items()):
            envelope = envelopes.get(public_key)
            if envelope is None:
                continue
            try:
                await channel.send(envelope)
            except (ChannelClosed, OSError):
                failures.append(public_key)
        for public_key in failures:
            await self.detach(public_key, reason="send_failed")

    async def detach(self, public_key: str, *, reason: str) -> None:
        channel = self.channels.pop(public_key, None)
        task = self.receivers.pop(public_key, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if channel is not None:
            try:
                await channel.close()
            except Exception:
                pass
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
                    if (
                        isinstance(seq, int)
                        and not isinstance(seq, bool)
                        and isinstance(frame_hash, str)
                        and self.authority.acknowledge(
                            public_key, seq, frame_hash
                        )
                    ):
                        self.acknowledged_seq[public_key] = max(
                            seq, self.acknowledged_seq.get(public_key, -1)
                        )
                    continue
                if source != "action" or not isinstance(payload, dict):
                    continue
                try:
                    envelopes, receipt = self.authority.apply_input(
                        actor_public_key=public_key,
                        client_seq=int(payload["client_seq"]),
                        action=payload["action"],
                    )
                except (KeyError, TypeError, ValueError, GroupProtocolError) as exc:
                    await self._send_receipt(
                        public_key,
                        {
                            "_group": "receipt",
                            "status": "rejected",
                            "reason": str(exc)[:256],
                            "client_seq": payload.get("client_seq"),
                        },
                    )
                    self.events.emit(
                        "group_action_rejected",
                        {
                            "public_key": public_key,
                            "reason": str(exc)[:256],
                        },
                    )
                    continue
                await self.broadcast(envelopes)
                await self._send_receipt(
                    public_key, {"_group": "receipt", **receipt}
                )
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
            for task in list(self.receivers.values()):
                task.cancel()
            await asyncio.gather(
                action_task, *self.receivers.values(), return_exceptions=True
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
                            },
                        )
                        continue
                    await self.queue.put(("action", public_key, message))
                elif kind == "ack":
                    await self.queue.put(("ack", public_key, message))
                elif kind == "ping":
                    await channel.send(
                        {
                            "_group": "pong",
                            "group_id": self.authority.group_id,
                            "leader_epoch": self.authority.leader_epoch,
                            "nonce": message.get("nonce"),
                        }
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
        try:
            await channel.send(receipt)
        except (ChannelClosed, OSError):
            await self.detach(public_key, reason="receipt_send_failed")

    async def _local_action_reader(self) -> None:
        while not self.stopped.is_set():
            entries, self.action_offset = _read_jsonl(
                self.action_path, self.action_offset
            )
            for entry in entries:
                action = entry.get("action") if isinstance(entry, dict) else None
                if not isinstance(action, dict):
                    continue
                self.local_client_seq += 1
                await self.queue.put(
                    (
                        "action",
                        self.authority.leader_public_key,
                        {
                            "_group": "input",
                            "group_id": self.authority.group_id,
                            "leader_epoch": self.authority.leader_epoch,
                            "actor_public_key": self.authority.leader_public_key,
                            "client_seq": self.local_client_seq,
                            "action": action,
                        },
                    )
                )
            await asyncio.sleep(0.1)


async def run_group_guest_channel(
    *,
    channel: AsyncJsonLineChannel,
    replica: GroupReplica,
    state_dir: str | Path,
    first_envelope: dict[str, Any],
) -> dict[str, Any]:
    """Run one Guest connection until completion or Leader disconnect."""
    root = Path(state_dir)
    events = EventBus(root)
    action_path = root / "group-actions.jsonl"
    action_offset = 0
    client_seq_path = root / "group-client-seq"
    try:
        client_seq = int(client_seq_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        client_seq = 0

    replica.apply(first_envelope, bootstrap=True)
    await channel.send(
        {
            "_group": "ack",
            "group_id": replica.group_id,
            "leader_epoch": replica.leader_epoch,
            "seq": replica.seq,
            "frame_hash": replica.frame_hash,
        }
    )
    if first_envelope.get("completed"):
        return {
            "completed": True,
            "outcome": first_envelope.get("outcome"),
            "checkpoint": replica.checkpoint,
        }

    async def sender() -> None:
        nonlocal action_offset, client_seq
        while True:
            entries, action_offset = _read_jsonl(action_path, action_offset)
            for entry in entries:
                action = entry.get("action") if isinstance(entry, dict) else None
                if not isinstance(action, dict):
                    continue
                client_seq += 1
                client_seq_path.write_text(str(client_seq), encoding="utf-8")
                await channel.send(
                    {
                        "_group": "input",
                        "group_id": replica.group_id,
                        "leader_epoch": replica.leader_epoch,
                        "actor_public_key": replica.viewer_public_key,
                        "client_seq": client_seq,
                        "action": action,
                    }
                )
            await asyncio.sleep(0.1)

    sender_task = asyncio.create_task(sender())
    try:
        while True:
            message = await channel.recv()
            if not isinstance(message, dict):
                continue
            kind = message.get("_group")
            if kind in {"frame", "membership", "epoch_start", "snapshot"}:
                applied = replica.apply(message)
                if applied:
                    await channel.send(
                        {
                            "_group": "ack",
                            "group_id": replica.group_id,
                            "leader_epoch": replica.leader_epoch,
                            "seq": replica.seq,
                            "frame_hash": replica.frame_hash,
                        }
                    )
                if message.get("completed"):
                    return {
                        "completed": True,
                        "outcome": message.get("outcome"),
                        "checkpoint": replica.checkpoint,
                    }
            elif kind == "receipt":
                events.emit("group_action_receipt", message)
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
    result: list[dict[str, Any]] = []
    for raw in complete.splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            result.append(value)
    return result, new_offset
