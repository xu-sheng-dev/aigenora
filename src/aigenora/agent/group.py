from __future__ import annotations

import asyncio
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aigenora.agent._daemon import update_session_meta
from aigenora.agent.skeleton import assert_hooks_implemented
from aigenora.control import control_mode_from_args, ensure_control_mode_supported
from aigenora.engine.config import get_server
from aigenora.engine.crypto import (
    protocol_hash,
    transport_binding_canonical,
)
from aigenora.engine.keys import KeyPair, load_keys, sign_raw, verify_raw
from aigenora.engine.p2p import (
    AsyncJsonLineChannel,
    ChannelClosed,
    connect_by_ticket,
    create_host_node,
)
from aigenora.engine.rest import RestClient
from aigenora.proto.engine import parse_options
from aigenora.proto.group import (
    GroupAuthority,
    GroupConfig,
    GroupProtocolError,
    GroupReplica,
    canonical_json,
)
from aigenora.proto.group_control import (
    admission_canonical,
    admit_member,
    claim_leader,
    create_group,
    get_group,
    get_group_by_post,
    heartbeat_member,
    renew_leader,
    update_group_status,
)
from aigenora.proto.group_peer import GroupPeerOverlay
from aigenora.proto.group_runtime import GroupLeaderHub, run_group_guest_channel
from aigenora.proto.loader import load_hooks
from aigenora.proto.sdk import EventBus
from aigenora.proto.validate import validate_extra_args, validate_options


def is_authoritative_group(spec: dict[str, Any]) -> bool:
    flow = spec.get("flow")
    return isinstance(flow, dict) and flow.get("mode") == "authoritative_group"


async def run_group_host_command(args, spec: dict[str, Any]) -> int:
    protocol_dir = Path(args.protocol_dir).resolve()
    if getattr(args, "share_bundle", False) or getattr(args, "share_ui", False):
        raise RuntimeError(
            "authoritative_group currently uses installed protocol bundles; "
            "--share-ui/--share-bundle are not accepted for a multi-member room"
        )
    assert_hooks_implemented(
        protocol_dir,
        allow_skeleton=getattr(args, "allow_skeleton_hooks", False),
    )
    validate_extra_args(spec, args.extra_args)
    control_mode = control_mode_from_args(args)
    keypair = load_keys(args.data_dir)
    hooks = load_hooks(protocol_dir)
    ensure_control_mode_supported(hooks, control_mode)
    options = parse_options(args.options)
    validate_options(spec, options)
    protocol_id = protocol_hash(protocol_dir / "spec.json")
    config = GroupConfig.from_spec(spec)
    state_dir = _state_dir(args, "host")
    event_bus = EventBus(state_dir)
    rest = RestClient(get_server(args.server), keypair)

    # Resolve metadata and hook defaults without polluting the real room state.
    with tempfile.TemporaryDirectory(prefix="aigenora-group-meta-") as tmp:
        metadata_hooks = load_hooks(protocol_dir)
        metadata_hooks.proto_init(
            options, "host", args.extra_args or [], Path(tmp)
        )
        display_name, tags, invite_type, hook_options = (
            metadata_hooks.proto_host_metadata()
        )
    options = {
        **(hook_options if isinstance(hook_options, dict) else {}),
        **options,
    }
    validate_options(spec, options)

    runtime, node, accepted = await create_host_node()
    invitation_task: asyncio.Task[None] | None = None
    leader_task: asyncio.Task[None] | None = None
    peer_overlay: GroupPeerOverlay | None = None
    lease_lost = asyncio.Event()
    authority_holder: list[GroupAuthority | None] = [None]
    try:
        node_addr = await node.net().node_addr()
        ticket = runtime.ticket_from_addr(node_addr)
        binding = transport_binding_canonical(
            keypair.public_key, "iroh", ticket, protocol_id
        )
        body: dict[str, Any] = {
            "message": display_name,
            "tags": [item.strip() for item in str(tags).split(",") if item.strip()],
            "iroh_ticket": ticket,
            "transport": "iroh",
            "transport_info": {
                "version": 1,
                "endpoint_id": keypair.public_key,
                "ticket": ticket,
            },
            "transport_binding_signature": sign_raw(
                keypair.private_key, binding.encode("utf-8")
            ),
            "protocol_id": protocol_id,
            "host_control_mode": control_mode,
            "type": invite_type or "supply",
        }
        if options:
            body["options"] = options
        invitation = await asyncio.to_thread(
            rest.json,
            "POST",
            "/api/v1/invitations",
            body,
            {201},
        )
        if not isinstance(invitation, dict):
            raise RuntimeError("invitation create response must be an object")
        post_id = str(invitation["post_id"])
        group = await asyncio.to_thread(
            create_group,
            rest,
            post_id=post_id,
            min_participants=config.min_participants,
            max_participants=config.max_participants,
            allow_late_join=config.allow_late_join,
            recovery_mode=config.recovery_mode,
        )
        group_id = str(group["group_id"])
        if config.peer_channels_enabled:
            peer_overlay = await GroupPeerOverlay.create(
                state_dir=state_dir,
                group_id=group_id,
                protocol_id=protocol_id,
                keypair=keypair,
                allowed_channels=config.peer_channel_names,
                max_message_bytes=config.max_peer_message_bytes,
            )
        print("invite_created: true")
        print(f"post_id: {post_id}")
        print(f"group_id: {group_id}")
        event_bus.emit(
            "invite_created",
            {
                "post_id": post_id,
                "protocol_id": protocol_id,
                "group_id": group_id,
                "min_participants": config.min_participants,
                "max_participants": config.max_participants,
            },
        )
        update_session_meta(
            state_dir,
            post_id=post_id,
            protocol_id=protocol_id,
            group_id=group_id,
            group_role="leader",
            leader_epoch=0,
            peer_channels_enabled=config.peer_channels_enabled,
            peer_ticket_hash=(
                peer_overlay.ticket_hash if peer_overlay is not None else None
            ),
        )

        invitation_task = asyncio.create_task(
            _renew_invitation_loop(
                rest,
                post_id,
                event_bus,
                max_minutes=int(
                    getattr(args, "invitation_ttl_minutes", 30) or 30
                ),
            )
        )
        leader_task = asyncio.create_task(
            _leader_lease_loop(
                rest=rest,
                keypair=keypair,
                group_id=group_id,
                protocol_id=protocol_id,
                ticket=ticket,
                leader_epoch=0,
                authority_holder=authority_holder,
                lease_lost=lease_lost,
                event_bus=event_bus,
                lease_seconds=_leader_lease_seconds(group),
                initial_checkpoint_seq=int(group["checkpoint_seq"]),
                initial_checkpoint_hash=str(group["checkpoint_hash"]),
            )
        )

        pending: dict[str, tuple[dict[str, Any], AsyncJsonLineChannel]] = {}
        print("waiting_for_peers: true")
        while not _start_ready(group, config):
            if lease_lost.is_set():
                raise RuntimeError("leader lease expired before group start")
            try:
                channel = await asyncio.wait_for(accepted.get(), timeout=1.0)
            except asyncio.TimeoutError:
                group = await asyncio.to_thread(get_group, rest, group_id)
                continue
            try:
                member = await _leader_admit_channel(
                    channel=channel,
                    rest=rest,
                    keypair=keypair,
                    group=group,
                    event_bus=event_bus,
                )
            except Exception as exc:
                event_bus.emit(
                    "group_join_rejected",
                    {"reason": str(exc)[:256]},
                )
                try:
                    await asyncio.wait_for(channel.close(), timeout=1.0)
                except Exception:
                    pass
                group = await asyncio.to_thread(
                    get_group, rest, group_id
                )
                continue
            pending[str(member["public_key"]).lower()] = (member, channel)
            group = await asyncio.to_thread(get_group, rest, group_id)

        if getattr(args, "_controller_required", False):
            from aigenora.agent._controller import wait_for_controller_ready

            await asyncio.to_thread(wait_for_controller_ready, state_dir)

        result = await _run_leader_runtime(
            args=args,
            protocol_dir=protocol_dir,
            spec=spec,
            options=options,
            keypair=keypair,
            rest=rest,
            group=group,
            node=node,
            accepted=accepted,
            ticket=ticket,
            state_dir=state_dir,
            event_bus=event_bus,
            authority_holder=authority_holder,
            lease_lost=lease_lost,
            existing_connections=pending,
            checkpoint=None,
            peer_overlay=peer_overlay,
        )
        completed = bool(result.get("completed", False))
        update_session_meta(
            state_dir,
            status="closed" if completed else "aborted",
            completed=completed,
            ended_at=time.time(),
            end_reason=result.get("reason"),
        )
        return 0 if completed or result.get("reason") == "room_open_ended" else 1
    finally:
        for task in (invitation_task, leader_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *[task for task in (invitation_task, leader_task) if task is not None],
            return_exceptions=True,
        )
        if peer_overlay is not None:
            await peer_overlay.close()
        await node.node().shutdown()


def _record_group_member_ready(
    *,
    state_dir: Path,
    event_bus: EventBus,
    group: dict[str, Any],
    member: dict[str, Any],
    protocol_dir: Path,
    control_mode: str,
    config: GroupConfig,
    peer_overlay: GroupPeerOverlay | None,
) -> None:
    """Persist replay identity before publishing the daemon readiness event."""
    protocol_id = str(group["protocol_id"])
    update_session_meta(
        state_dir,
        session_id=group["group_id"],
        group_id=group["group_id"],
        protocol_id=protocol_id,
        group_role="member",
        member_id=member["member_id"],
        seat=member["seat"],
        leader_epoch=group["leader_epoch"],
        protocol_dir=str(protocol_dir),
        local_protocol_dir=str(protocol_dir),
        active_hooks_source="trusted_local",
        peer_channels_enabled=config.peer_channels_enabled,
        peer_ticket_hash=(
            peer_overlay.ticket_hash if peer_overlay is not None else None
        ),
    )
    event_bus.emit(
        "peer_joined",
        {
            "host_public_key": group["leader_public_key"],
            "session_id": group["group_id"],
            "group_id": group["group_id"],
            "protocol_id": protocol_id,
            "member_id": member["member_id"],
            "seat": member["seat"],
            "protocol_dir": str(protocol_dir),
            "local_protocol_dir": str(protocol_dir),
            "local_control_mode": control_mode,
            "peer_control_mode": "hybrid",
            "active_hooks_source": "trusted_local",
        },
    )


async def run_group_join_command(
    args,
    *,
    post: dict[str, Any],
    protocol_dir: Path,
    spec: dict[str, Any],
) -> int:
    if getattr(args, "accept_host_bundle", False) or getattr(
        args, "accept_host_ui", False
    ):
        raise RuntimeError(
            "authoritative_group currently uses the installed protocol hooks/UI; "
            "Host-provided P2P bundles are not accepted for a multi-member room"
        )
    assert_hooks_implemented(
        protocol_dir,
        allow_skeleton=getattr(args, "allow_skeleton_hooks", False),
    )
    validate_extra_args(spec, args.extra_args)
    control_mode = control_mode_from_args(args)
    hooks = load_hooks(protocol_dir)
    ensure_control_mode_supported(hooks, control_mode)
    options = post.get("options") if isinstance(post.get("options"), dict) else {}
    validate_options(spec, options)
    keypair = load_keys(args.data_dir)
    rest = RestClient(get_server(args.server), keypair)
    state_dir = _state_dir(args, "guest")
    event_bus = EventBus(state_dir)
    group = await asyncio.to_thread(get_group_by_post, rest, args.post_id)
    protocol_id = str(group["protocol_id"])
    if protocol_id != post.get("protocol_id"):
        raise RuntimeError("group protocol_id does not match the invitation")

    config = GroupConfig.from_spec(spec)
    peer_overlay: GroupPeerOverlay | None = None
    if config.peer_channels_enabled:
        peer_overlay = await GroupPeerOverlay.create(
            state_dir=state_dir,
            group_id=str(group["group_id"]),
            protocol_id=protocol_id,
            keypair=keypair,
            allowed_channels=config.peer_channel_names,
            max_message_bytes=config.max_peer_message_bytes,
        )
    member_task = asyncio.create_task(
        _member_heartbeat_loop(rest, str(group["group_id"]), event_bus)
    )
    current_node: Any = None
    replica: GroupReplica | None = None
    try:
        while True:
            try:
                (
                    current_node,
                    channel,
                    group,
                    member,
                ) = await _connect_and_admit(
                    rest=rest,
                    keypair=keypair,
                    group=group,
                    protocol_dir=protocol_dir,
                    control_mode=control_mode,
                )
                if replica is None:
                    replica = GroupReplica(
                        state_dir=state_dir,
                        group_id=str(group["group_id"]),
                        viewer_public_key=keypair.public_key,
                        leader_public_key=str(group["leader_public_key"]),
                        leader_epoch=int(group["leader_epoch"]),
                        protocol_name=str(
                            spec.get("name") or "Group Protocol"
                        ),
                    )
                    _record_group_member_ready(
                        state_dir=state_dir,
                        event_bus=event_bus,
                        group=group,
                        member=member,
                        protocol_dir=protocol_dir,
                        control_mode=control_mode,
                        config=config,
                        peer_overlay=peer_overlay,
                    )
                elif int(group["leader_epoch"]) > replica.leader_epoch:
                    replica.advance_epoch(
                        leader_public_key=str(group["leader_public_key"]),
                        leader_epoch=int(group["leader_epoch"]),
                    )
                    update_session_meta(
                        state_dir,
                        group_role=(
                            "leader"
                            if group["leader_public_key"]
                            == keypair.public_key
                            else "member"
                        ),
                        leader_epoch=group["leader_epoch"],
                    )

                first_envelope = await _receive_first_group_envelope(
                    current_node, channel
                )
                result = await run_group_guest_channel(
                    channel=channel,
                    replica=replica,
                    state_dir=state_dir,
                    first_envelope=first_envelope,
                    peer_overlay=peer_overlay,
                )
            except asyncio.CancelledError:
                raise
            except (
                ChannelClosed,
                TimeoutError,
                OSError,
                RuntimeError,
                GroupProtocolError,
            ) as exc:
                if replica is None:
                    raise
                event_bus.emit(
                    "group_reconnect_failed",
                    {
                        "group_id": replica.group_id,
                        "leader_epoch": replica.leader_epoch,
                        "reason": (
                            f"{type(exc).__name__}:{str(exc)[:192]}"
                        ),
                    },
                )
                result = {
                    "completed": False,
                    "seq": replica.seq,
                    "reason": "reconnect_failed",
                }
            if current_node is not None:
                await current_node.node().shutdown()
                current_node = None
            if result.get("completed"):
                update_session_meta(
                    state_dir,
                    status="closed",
                    completed=True,
                    ended_at=time.time(),
                )
                return 0

            event_bus.emit(
                "group_leader_disconnected",
                {
                    "group_id": group["group_id"],
                    "leader_epoch": group["leader_epoch"],
                    "seq": result.get("seq"),
                },
            )
            recovery = await _recover_or_follow(
                args=args,
                rest=rest,
                keypair=keypair,
                protocol_dir=protocol_dir,
                spec=spec,
                options=options,
                state_dir=state_dir,
                event_bus=event_bus,
                replica=replica,
                peer_overlay=peer_overlay,
            )
            if recovery["role"] == "leader":
                member_task.cancel()
                await asyncio.gather(member_task, return_exceptions=True)
                return int(recovery["exit_code"])
            if recovery["role"] == "closed":
                closed_group = recovery["group"]
                completed = closed_group.get("status") == "closed"
                update_session_meta(
                    state_dir,
                    status="closed" if completed else "failed",
                    completed=completed,
                    ended_at=time.time(),
                    end_reason=f"group_{closed_group.get('status')}",
                )
                return 0 if completed else 1
            group = recovery["group"]
            await asyncio.sleep(0.5)
    finally:
        member_task.cancel()
        await asyncio.gather(member_task, return_exceptions=True)
        if current_node is not None:
            await current_node.node().shutdown()
        if peer_overlay is not None:
            await peer_overlay.close()


async def _run_leader_runtime(
    *,
    args,
    protocol_dir: Path,
    spec: dict[str, Any],
    options: dict[str, Any],
    keypair: KeyPair,
    rest: RestClient,
    group: dict[str, Any],
    node: Any,
    accepted: asyncio.Queue[AsyncJsonLineChannel],
    ticket: str,
    state_dir: Path,
    event_bus: EventBus,
    authority_holder: list[GroupAuthority | None],
    lease_lost: asyncio.Event,
    existing_connections: dict[
        str, tuple[dict[str, Any], AsyncJsonLineChannel]
    ],
    checkpoint: dict[str, Any] | None,
    peer_overlay: GroupPeerOverlay | None = None,
) -> dict[str, Any]:
    members = _active_members(group)
    authority = GroupAuthority(
        spec=spec,
        hooks=load_hooks(protocol_dir),
        options=options,
        state_dir=state_dir,
        group_id=str(group["group_id"]),
        leader_public_key=keypair.public_key,
        leader_epoch=int(group["leader_epoch"]),
        membership_version=int(group["membership_version"]),
        members=members,
        keypair=keypair,
        checkpoint=checkpoint,
    )
    authority_holder[0] = authority
    if peer_overlay is not None:
        peer_overlay.update_context(
            leader_public_key=authority.leader_public_key,
            leader_epoch=authority.leader_epoch,
            authority_seq=authority.seq,
            membership_version=authority.membership_version,
        )
    hub = GroupLeaderHub(
        authority,
        protocol_id=str(group["protocol_id"]),
        peer_overlay=peer_overlay,
    )
    for public_key, (member, channel) in existing_connections.items():
        if public_key == keypair.public_key:
            continue
        await hub.attach_existing(member, channel, send_snapshot=False)
    await hub.broadcast(authority.bootstrap_envelopes())

    if group.get("status") == "lobby":
        group = await asyncio.to_thread(
            update_group_status,
            rest,
            group_id=group["group_id"],
            leader_epoch=int(group["leader_epoch"]),
            status="active",
        )
    event_bus.emit(
        "group_started",
        {
            "group_id": group["group_id"],
            "leader_epoch": group["leader_epoch"],
            "participant_count": group["participant_count"],
        },
    )
    update_session_meta(
        state_dir,
        group_role="leader",
        leader_epoch=group["leader_epoch"],
        participant_count=group["participant_count"],
        status="running",
    )

    accept_task = asyncio.create_task(
        _late_accept_loop(
            accepted=accepted,
            hub=hub,
            rest=rest,
            keypair=keypair,
            group_id=str(group["group_id"]),
            event_bus=event_bus,
        )
    )
    hub_task = asyncio.create_task(hub.run())
    lost_task = asyncio.create_task(lease_lost.wait())
    try:
        done, _ = await asyncio.wait(
            {hub_task, lost_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if lost_task in done and lease_lost.is_set():
            event_bus.emit(
                "group_leader_fenced",
                {
                    "group_id": group["group_id"],
                    "leader_epoch": group["leader_epoch"],
                    "reason": "server_lease_lost",
                },
            )
            hub_task.cancel()
            await asyncio.gather(hub_task, return_exceptions=True)
            return {
                "completed": False,
                "reason": "leader_lease_lost",
                "checkpoint": authority.checkpoint(),
            }
        result = await hub_task
        if result.get("completed"):
            try:
                await asyncio.to_thread(
                    update_group_status,
                    rest,
                    group_id=group["group_id"],
                    leader_epoch=int(group["leader_epoch"]),
                    status="closed",
                )
            except Exception as exc:
                event_bus.emit(
                    "group_status_update_failed",
                    {"status": "closed", "error": str(exc)[:256]},
                )
        return result
    finally:
        accept_task.cancel()
        lost_task.cancel()
        await hub.close()
        await asyncio.gather(accept_task, lost_task, return_exceptions=True)


async def _late_accept_loop(
    *,
    accepted: asyncio.Queue[AsyncJsonLineChannel],
    hub: GroupLeaderHub,
    rest: RestClient,
    keypair: KeyPair,
    group_id: str,
    event_bus: EventBus,
) -> None:
    while not hub.stopped.is_set():
        channel = await accepted.get()
        try:
            group = await asyncio.to_thread(get_group, rest, group_id)
            member = await _leader_admit_channel(
                channel=channel,
                rest=rest,
                keypair=keypair,
                group=group,
                event_bus=event_bus,
            )
            group = await asyncio.to_thread(get_group, rest, group_id)
            existing = hub.authority.member(str(member["public_key"]))
            if existing is not None:
                await hub.attach_existing(member, channel)
            else:
                await hub.attach(
                    member,
                    channel,
                    membership_version=int(group["membership_version"]),
                )
            update_session_meta(
                hub.authority.state_dir,
                participant_count=group.get("participant_count"),
                membership_version=group.get("membership_version"),
            )
        except Exception as exc:
            event_bus.emit(
                "group_join_rejected",
                {"reason": str(exc)[:256]},
            )
            try:
                await asyncio.wait_for(
                    channel.send(
                        {"_group": "error", "reason": str(exc)[:256]}
                    ),
                    timeout=1.0,
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(channel.close(), timeout=1.0)
            except Exception:
                pass


def _channel_ready_canonical(
    *,
    group_id: str,
    protocol_id: str,
    leader_epoch: int,
    member_public_key: str,
    member_id: str,
    seat: int,
    join_nonce: str,
    channel_challenge: str,
) -> bytes:
    """Bind one member-key proof to one freshly challenged P2P channel."""
    return b"aigenora-group-channel-ready-v1:" + canonical_json(
        {
            "group_id": group_id,
            "protocol_id": protocol_id,
            "leader_epoch": leader_epoch,
            "member_public_key": member_public_key,
            "member_id": member_id,
            "seat": seat,
            "join_nonce": join_nonce,
            "channel_challenge": channel_challenge,
        }
    )


async def _leader_admit_channel(
    *,
    channel: AsyncJsonLineChannel,
    rest: RestClient,
    keypair: KeyPair,
    group: dict[str, Any],
    event_bus: EventBus,
) -> dict[str, Any]:
    first = await channel.recv(timeout=30.0)
    if not isinstance(first, dict) or first.get("_group") != "join":
        raise GroupProtocolError("first multiplayer frame must be group join")
    group_id = str(group["group_id"])
    member_public_key = str(first.get("member_public_key") or "").lower()
    join_nonce = str(first.get("join_nonce") or "")
    if first.get("group_id") != group_id:
        raise GroupProtocolError("group join targets a different group")
    if (
        len(member_public_key) != 64
        or any(ch not in "0123456789abcdef" for ch in member_public_key)
    ):
        raise GroupProtocolError("member_public_key must be 64-char hex")
    if not join_nonce or len(join_nonce) > 64:
        raise GroupProtocolError("join_nonce is invalid")
    current = await asyncio.to_thread(get_group, rest, group_id)
    if (
        current.get("leader_public_key") != keypair.public_key
        or int(current.get("leader_epoch", -1)) != int(group["leader_epoch"])
    ):
        raise GroupProtocolError("this process is not the current leader epoch")
    canonical = admission_canonical(
        group_id,
        keypair.public_key,
        member_public_key,
        str(group["protocol_id"]),
        int(group["leader_epoch"]),
        join_nonce,
    )
    leader_signature = sign_raw(
        keypair.private_key, canonical.encode("utf-8")
    )
    channel_challenge = secrets.token_hex(16)
    await channel.send(
        {
            "_group": "admission",
            "group_id": group_id,
            "protocol_id": group["protocol_id"],
            "leader_public_key": keypair.public_key,
            "leader_epoch": group["leader_epoch"],
            "join_nonce": join_nonce,
            "channel_challenge": channel_challenge,
            "leader_signature": leader_signature,
        }
    )
    ready = await channel.recv(timeout=30.0)
    if (
        not isinstance(ready, dict)
        or ready.get("_group") != "ready"
        or ready.get("group_id") != group_id
        or ready.get("protocol_id") != group["protocol_id"]
        or ready.get("leader_epoch") != group["leader_epoch"]
        or ready.get("member_public_key") != member_public_key
        or ready.get("join_nonce") != join_nonce
        or ready.get("channel_challenge") != channel_challenge
    ):
        raise GroupProtocolError("member did not complete server admission")
    member_id = ready.get("member_id")
    seat = ready.get("seat")
    member_signature = ready.get("member_signature")
    if (
        not isinstance(member_id, str)
        or not member_id
        or len(member_id) > 128
        or not isinstance(seat, int)
        or isinstance(seat, bool)
        or seat < 0
        or not isinstance(member_signature, str)
    ):
        raise GroupProtocolError("member channel proof is invalid")
    member_proof = _channel_ready_canonical(
        group_id=group_id,
        protocol_id=str(group["protocol_id"]),
        leader_epoch=int(group["leader_epoch"]),
        member_public_key=member_public_key,
        member_id=member_id,
        seat=seat,
        join_nonce=join_nonce,
        channel_challenge=channel_challenge,
    )
    try:
        verify_raw(member_public_key, member_proof, member_signature)
    except Exception as exc:
        raise GroupProtocolError(
            "member channel proof signature is invalid"
        ) from exc
    latest = await asyncio.to_thread(get_group, rest, group_id)
    member = _find_active_member(latest, member_public_key)
    if member is None:
        raise GroupProtocolError("server does not list the admitted member")
    if member.get("member_id") != member_id or member.get("seat") != seat:
        raise GroupProtocolError(
            "member channel proof does not match server admission"
        )
    event_bus.emit(
        "group_member_admitted",
        {
            "group_id": group_id,
            "public_key": member_public_key,
            "seat": member["seat"],
            "leader_epoch": group["leader_epoch"],
        },
    )
    return member


async def _connect_and_admit(
    *,
    rest: RestClient,
    keypair: KeyPair,
    group: dict[str, Any],
    protocol_dir: Path,
    control_mode: str,
) -> tuple[Any, AsyncJsonLineChannel, dict[str, Any], dict[str, Any]]:
    del protocol_dir
    leader_public_key = str(group["leader_public_key"])
    protocol_id = str(group["protocol_id"])
    ticket = str(group["iroh_ticket"])
    binding_signature = str(group["transport_binding_signature"])
    binding = transport_binding_canonical(
        leader_public_key, "iroh", ticket, protocol_id
    )
    verify_raw(
        leader_public_key, binding.encode("utf-8"), binding_signature
    )
    _, node, channel = await connect_by_ticket(ticket)
    try:
        join_nonce = secrets.token_hex(16)
        await channel.send(
            {
                "_group": "join",
                "group_id": group["group_id"],
                "member_public_key": keypair.public_key,
                "join_nonce": join_nonce,
                "member_control_mode": control_mode,
            }
        )
        admission = await channel.recv(timeout=30.0)
        if (
            not isinstance(admission, dict)
            or admission.get("_group") != "admission"
        ):
            raise GroupProtocolError(
                "leader did not return a group admission"
            )
        if (
            admission.get("group_id") != group["group_id"]
            or admission.get("protocol_id") != protocol_id
            or admission.get("leader_public_key") != leader_public_key
            or admission.get("leader_epoch") != group["leader_epoch"]
            or admission.get("join_nonce") != join_nonce
        ):
            raise GroupProtocolError(
                "group admission does not match server state"
            )
        channel_challenge = admission.get("channel_challenge")
        if (
            not isinstance(channel_challenge, str)
            or len(channel_challenge) != 32
            or any(
                character not in "0123456789abcdef"
                for character in channel_challenge
            )
        ):
            raise GroupProtocolError("group channel challenge is invalid")
        canonical = admission_canonical(
            str(group["group_id"]),
            leader_public_key,
            keypair.public_key,
            protocol_id,
            int(group["leader_epoch"]),
            join_nonce,
        )
        verify_raw(
            leader_public_key,
            canonical.encode("utf-8"),
            str(admission.get("leader_signature") or ""),
        )
        member = await asyncio.to_thread(
            admit_member,
            rest,
            keypair,
            group=group,
            join_nonce=join_nonce,
            leader_signature=str(admission["leader_signature"]),
        )
        member_proof = _channel_ready_canonical(
            group_id=str(group["group_id"]),
            protocol_id=protocol_id,
            leader_epoch=int(group["leader_epoch"]),
            member_public_key=keypair.public_key,
            member_id=str(member["member_id"]),
            seat=int(member["seat"]),
            join_nonce=join_nonce,
            channel_challenge=channel_challenge,
        )
        await channel.send(
            {
                "_group": "ready",
                "group_id": group["group_id"],
                "protocol_id": protocol_id,
                "leader_epoch": group["leader_epoch"],
                "member_public_key": keypair.public_key,
                "member_id": member["member_id"],
                "seat": member["seat"],
                "join_nonce": join_nonce,
                "channel_challenge": channel_challenge,
                "member_signature": sign_raw(
                    keypair.private_key, member_proof
                ),
            }
        )
        return node, channel, group, member
    except BaseException:
        await node.node().shutdown()
        raise


async def _receive_first_group_envelope(
    node: Any, channel: AsyncJsonLineChannel
) -> dict[str, Any]:
    """Wait for gameplay state after admission has already been announced."""
    first_envelope = await channel.recv(timeout=60.0)
    if not isinstance(first_envelope, dict) or first_envelope.get("_group") not in {
        "snapshot",
        "membership",
        "epoch_start",
        "frame",
    }:
        await node.node().shutdown()
        raise GroupProtocolError("leader did not send an authority snapshot")
    return first_envelope


async def _recover_or_follow(
    *,
    args,
    rest: RestClient,
    keypair: KeyPair,
    protocol_dir: Path,
    spec: dict[str, Any],
    options: dict[str, Any],
    state_dir: Path,
    event_bus: EventBus,
    replica: GroupReplica,
    peer_overlay: GroupPeerOverlay | None = None,
) -> dict[str, Any]:
    group_id = replica.group_id
    while True:
        group = await asyncio.to_thread(get_group, rest, group_id)
        if group.get("status") in {"closed", "failed"}:
            return {"role": "closed", "group": group}
        if int(group["leader_epoch"]) > replica.leader_epoch:
            return {"role": "follower", "group": group}
        if not _lease_expired(group):
            return {"role": "follower", "group": group}
        checkpoint = replica.checkpoint
        if not isinstance(checkpoint, dict):
            raise GroupProtocolError("cannot claim leadership without a checkpoint")
        # A bounded random arrival offset preserves the requested first-arrival
        # race without permanently privileging a seat number.
        await asyncio.sleep(secrets.randbelow(250) / 1000.0)
        runtime, node, accepted = await create_host_node()
        node_addr = await node.net().node_addr()
        ticket = runtime.ticket_from_addr(node_addr)
        try:
            claimed = await asyncio.to_thread(
                claim_leader,
                rest,
                keypair,
                group_id=group_id,
                protocol_id=str(group["protocol_id"]),
                expected_epoch=int(group["leader_epoch"]),
                checkpoint=checkpoint,
                iroh_ticket=ticket,
            )
        except RuntimeError:
            await node.node().shutdown()
            await asyncio.sleep(0.2)
            continue
        event_bus.emit(
            "group_leadership_claimed",
            {
                "group_id": group_id,
                "leader_epoch": claimed["leader_epoch"],
                "checkpoint_seq": checkpoint["seq"],
            },
        )
        update_session_meta(
            state_dir,
            group_role="leader",
            leader_epoch=claimed["leader_epoch"],
        )
        lease_lost = asyncio.Event()
        authority_holder: list[GroupAuthority | None] = [None]
        leader_task = asyncio.create_task(
            _leader_lease_loop(
                rest=rest,
                keypair=keypair,
                group_id=group_id,
                protocol_id=str(group["protocol_id"]),
                ticket=ticket,
                leader_epoch=int(claimed["leader_epoch"]),
                authority_holder=authority_holder,
                lease_lost=lease_lost,
                event_bus=event_bus,
                lease_seconds=_leader_lease_seconds(claimed),
                initial_checkpoint_seq=int(claimed["checkpoint_seq"]),
                initial_checkpoint_hash=str(claimed["checkpoint_hash"]),
            )
        )
        try:
            result = await _run_leader_runtime(
                args=args,
                protocol_dir=protocol_dir,
                spec=spec,
                options=options,
                keypair=keypair,
                rest=rest,
                group=claimed,
                node=node,
                accepted=accepted,
                ticket=ticket,
                state_dir=state_dir,
                event_bus=event_bus,
                authority_holder=authority_holder,
                lease_lost=lease_lost,
                existing_connections={},
                checkpoint=checkpoint,
                peer_overlay=peer_overlay,
            )
            completed = bool(result.get("completed", False))
            update_session_meta(
                state_dir,
                status="closed" if completed else "aborted",
                completed=completed,
                ended_at=time.time(),
                end_reason=result.get("reason"),
            )
            return {
                "role": "leader",
                "exit_code": (
                    0
                    if completed or result.get("reason") == "room_open_ended"
                    else 1
                ),
            }
        finally:
            leader_task.cancel()
            await asyncio.gather(leader_task, return_exceptions=True)
            await node.node().shutdown()


async def _leader_lease_loop(
    *,
    rest: RestClient,
    keypair: KeyPair,
    group_id: str,
    protocol_id: str,
    ticket: str,
    leader_epoch: int,
    authority_holder: list[GroupAuthority | None],
    lease_lost: asyncio.Event,
    event_bus: EventBus,
    lease_seconds: int = 15,
    initial_checkpoint_seq: int = 0,
    initial_checkpoint_hash: str = "0" * 64,
) -> None:
    last_success = time.monotonic()
    advertised_seq = initial_checkpoint_seq
    advertised_hash = initial_checkpoint_hash
    interval = max(1.0, lease_seconds / 3.0)
    while True:
        await asyncio.sleep(interval)
        authority = authority_holder[0]
        if authority is None:
            checkpoint_seq = advertised_seq
            checkpoint_hash = advertised_hash
        else:
            checkpoint = authority.replicated_checkpoint()
            if checkpoint is None:
                checkpoint_seq = advertised_seq
                checkpoint_hash = advertised_hash
            else:
                checkpoint_seq = int(checkpoint["seq"])
                checkpoint_hash = str(checkpoint["checkpoint_hash"])
        try:
            value = await asyncio.to_thread(
                renew_leader,
                rest,
                keypair,
                group_id=group_id,
                protocol_id=protocol_id,
                leader_epoch=leader_epoch,
                checkpoint_seq=checkpoint_seq,
                checkpoint_hash=checkpoint_hash,
                iroh_ticket=ticket,
            )
            last_success = time.monotonic()
            advertised_seq = checkpoint_seq
            advertised_hash = checkpoint_hash
            event_bus.emit(
                "group_leader_lease_renewed",
                {
                    "group_id": group_id,
                    "leader_epoch": leader_epoch,
                    "checkpoint_seq": checkpoint_seq,
                    "leader_lease_expires_at": value.get(
                        "leader_lease_expires_at"
                    ),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            event_bus.emit(
                "group_leader_lease_renew_failed",
                {
                    "group_id": group_id,
                    "leader_epoch": leader_epoch,
                    "error": str(exc)[:256],
                },
            )
            if time.monotonic() - last_success >= lease_seconds:
                lease_lost.set()
                return


async def _member_heartbeat_loop(
    rest: RestClient, group_id: str, event_bus: EventBus
) -> None:
    while True:
        await asyncio.sleep(5.0)
        try:
            await asyncio.to_thread(heartbeat_member, rest, group_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            event_bus.emit(
                "group_member_heartbeat_failed",
                {"group_id": group_id, "error": str(exc)[:256]},
            )


async def _renew_invitation_loop(
    rest: RestClient,
    post_id: str,
    event_bus: EventBus,
    *,
    max_minutes: int,
) -> None:
    deadline = time.monotonic() + max_minutes * 60
    while time.monotonic() < deadline:
        await asyncio.sleep(120.0)
        try:
            value = await asyncio.to_thread(
                rest.json,
                "POST",
                f"/api/v1/invitations/{post_id}/renew",
                {},
                {200},
            )
            event_bus.emit(
                "invitation_renewed",
                {
                    "post_id": post_id,
                    "expires_at": (
                        value.get("expires_at")
                        if isinstance(value, dict)
                        else None
                    ),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            event_bus.emit(
                "invitation_renew_failed",
                {"post_id": post_id, "error": str(exc)[:256]},
            )
            return


def _state_dir(args, role: str) -> Path:
    explicit = getattr(args, "_state_dir", None)
    if explicit:
        path = Path(explicit).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    data_dir = (
        Path(args.data_dir).resolve()
        if getattr(args, "data_dir", None)
        else (Path.cwd() / ".aigenora").resolve()
    )
    path = data_dir / "sessions" / f"{role}-group-{int(time.time() * 1000)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _active_members(group: dict[str, Any]) -> list[dict[str, Any]]:
    members = group.get("members")
    if not isinstance(members, list):
        raise GroupProtocolError("server group members must be an array")
    return [
        dict(member)
        for member in members
        if isinstance(member, dict) and member.get("status") == "active"
    ]


def _find_active_member(
    group: dict[str, Any], public_key: str
) -> dict[str, Any] | None:
    for member in _active_members(group):
        if member.get("public_key") == public_key:
            return member
    return None


def _start_ready(group: dict[str, Any], config: GroupConfig) -> bool:
    count = int(group.get("participant_count") or 0)
    if config.start_policy in {"full", "fixed_full"}:
        return count >= config.max_participants
    return count >= config.min_participants


def _lease_expired(group: dict[str, Any]) -> bool:
    raw_expiry = group.get("leader_lease_expires_at")
    raw_server = group.get("server_time")
    if not isinstance(raw_expiry, str) or not isinstance(raw_server, str):
        return False
    try:
        expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        server_now = datetime.fromisoformat(raw_server.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if server_now.tzinfo is None:
        server_now = server_now.replace(tzinfo=timezone.utc)
    return server_now >= expiry


def _leader_lease_seconds(group: dict[str, Any]) -> int:
    value = group.get("leader_lease_seconds")
    if isinstance(value, int) and not isinstance(value, bool) and 3 <= value <= 300:
        return value
    return 15
