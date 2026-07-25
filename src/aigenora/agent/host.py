from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from aigenora.engine.config import get_server
from aigenora.control import HUMAN, control_mode_from_args, ensure_control_mode_supported
from aigenora.engine.crypto import protocol_hash, transport_binding_canonical
from aigenora.engine.keys import load_keys, sign_raw
from aigenora.engine.p2p import AsyncReplayChannel, create_host_node
from aigenora.engine.rest import RestClient
from aigenora.agent._daemon import read_log_excerpt, terminate_process, wait_for_event, update_session_meta, write_session_meta
from aigenora.proto.session import close_session, report_result, sign_session
from aigenora.proto.engine import parse_options, run_host_async
from aigenora.proto.loader import load_hooks
from aigenora.proto.sdk import EventBus
from aigenora.proto.spec_version import check_spec_version
from aigenora.proto.validate import validate_extra_args
from aigenora.agent.skeleton import assert_hooks_implemented
from aigenora.agent.protocol_ui_p2p import (
    build_host_ui_artifact,
)
from aigenora.agent.protocol_bundle import (
    build_host_bundle_artifact,
    build_session_binding,
    is_received_bundle,
    sign_bundle_offer,
)
from aigenora.agent.protocol_bundle_p2p import (
    has_exact_bundle_capability,
    serve_host_artifacts_and_wait_ready,
)


def _resolve_state_dir(args) -> Path:
    data_dir = Path(args.data_dir) if args.data_dir else Path.cwd() / ".aigenora"
    sessions_dir = data_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    state_dir = sessions_dir / f"host-{int(time.time() * 1000)}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _run_daemon(args) -> int:
    state_dir = _resolve_state_dir(args)
    protocol_dir = str(Path(args.protocol_dir).resolve())
    state_dir_str = str(state_dir)
    control_mode = control_mode_from_args(args)
    from aigenora.agent._web_mode import resolve_web_mode
    web_mode = resolve_web_mode(args, control_mode=control_mode)
    controller_required = control_mode == HUMAN and web_mode != "off"
    if getattr(args, "share_bundle", False):
        print(
            "[host] WARNING: --share-bundle offers executable hooks.py to a "
            "trusted peer. Their restricted subprocess is not a security sandbox.",
            file=sys.stderr,
        )

    cmd = [sys.executable, "-m", "aigenora", "host",
           "--protocol-dir", protocol_dir,
           "--_internal-run",
           "--_state-dir", state_dir_str]
    if args.data_dir:
        cmd.extend(["--data-dir", str(Path(args.data_dir).resolve())])
    if args.server:
        cmd.extend(["--server", args.server])
    if args.options:
        cmd.extend(["--options", args.options])
    cmd.extend(["--control-mode", control_mode])
    if controller_required:
        cmd.append("--_controller-required")
    pace_val = getattr(args, "pace", 0) or 0
    if pace_val > 0:
        cmd.extend(["--pace", str(pace_val)])
    hb_interval = getattr(args, "heartbeat_interval", 10.0)
    hb_timeout = getattr(args, "heartbeat_timeout", 30.0)
    cmd.extend(["--heartbeat-interval", str(hb_interval)])
    cmd.extend(["--heartbeat-timeout", str(hb_timeout)])
    ttl_minutes = getattr(args, "invitation_ttl_minutes", 30) or 30
    cmd.extend(["--invitation-ttl-minutes", str(ttl_minutes)])
    if getattr(args, "no_invitation_renew", False):
        cmd.append("--no-invitation-renew")
    if getattr(args, "allow_skeleton_hooks", False):
        cmd.append("--allow-skeleton-hooks")
    if getattr(args, "share_ui", False):
        cmd.append("--share-ui")
    if getattr(args, "share_bundle", False):
        cmd.append("--share-bundle")
    if args.extra_args:
        cmd.extend(["--"] + args.extra_args)

    session_meta = {
        "role": "host",
        "status": "starting",
        "protocol_dir": protocol_dir,
        "state_dir": state_dir_str,
        "local_control_mode": control_mode,
        "web_mode": web_mode,
        "controller_required": controller_required,
        "share_ui": bool(
            getattr(args, "share_ui", False)
            or getattr(args, "share_bundle", False)
        ),
        "share_bundle": bool(getattr(args, "share_bundle", False)),
        "started_at": time.time(),
    }
    (state_dir / "session.json").write_text(json.dumps(session_meta, ensure_ascii=False), encoding="utf-8")

    err_log = open(state_dir / "daemon.err.log", "ab", buffering=0)
    out_log = open(state_dir / "daemon.out.log", "ab", buffering=0)
    try:
        proc = subprocess.Popen(cmd, stdout=out_log, stderr=err_log,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0)
    finally:
        err_log.close()
        out_log.close()
    session_meta["pid"] = proc.pid
    session_meta["status"] = "running"
    write_session_meta(state_dir, session_meta)

    startup_event = wait_for_event(
        state_dir,
        "invite_created",
        required_data_keys=("post_id", "protocol_id"),
    )
    if startup_event is None:
        excerpt = read_log_excerpt(state_dir)
        exit_code = proc.poll()
        if isinstance(exit_code, int):
            # Subprocess already exited (crash) -- report the real exit code, mirroring guest.
            session_meta["status"] = "startup_failed"
            session_meta["exit_code"] = exit_code
            session_meta["startup_error"] = "process exited before invite_created"
            if excerpt:
                session_meta["last_error_excerpt"] = excerpt
            write_session_meta(state_dir, session_meta)
            result = {
                "status": "error",
                "reason": "process exited before invite_created",
                "state_dir": state_dir_str,
                "exit_code": exit_code,
            }
            if excerpt:
                result["error_excerpt"] = excerpt
            print(json.dumps(result, ensure_ascii=False))
            return 1
        # Subprocess still alive but the startup event did not arrive in time -- kill it so it
        # does not hold the invitation TTL open, and surface a clear timeout diagnosis.
        session_meta["status"] = "startup_timeout"
        session_meta["startup_error"] = "timeout waiting for invite_created"
        if excerpt:
            session_meta["last_error_excerpt"] = excerpt
        write_session_meta(state_dir, session_meta)
        terminate_process(proc)
        result = {
            "status": "error",
            "reason": "timeout waiting for invite_created",
            "state_dir": state_dir_str,
        }
        if excerpt:
            result["error_excerpt"] = excerpt
        print(json.dumps(result, ensure_ascii=False))
        return 1

    startup_data = startup_event.get("data") or {}
    post_id = str(startup_data.get("post_id") or "")
    protocol_id = str(startup_data.get("protocol_id") or "")
    shared_ui_manifest_hash = str(startup_data.get("ui_manifest_hash") or "")
    shared_bundle_manifest_hash = str(
        startup_data.get("bundle_manifest_hash") or ""
    )
    session_meta["post_id"] = post_id
    session_meta["protocol_id"] = protocol_id
    if shared_ui_manifest_hash:
        session_meta["shared_ui_manifest_hash"] = shared_ui_manifest_hash
    if shared_bundle_manifest_hash:
        session_meta["shared_bundle_manifest_hash"] = shared_bundle_manifest_hash
    write_session_meta(state_dir, session_meta)

    # Based on web_mode, decide whether to start the relay subprocess and whether to open a browser
    from aigenora.agent._controller import mark_controller_ready
    from aigenora.agent.web import spawn_broadcast
    bc = None
    if web_mode != "off":
        bc = spawn_broadcast(state_dir, open_browser=(web_mode == "auto"))
        if bc:
            session_meta["broadcast_pid"] = bc["pid"]
            session_meta["broadcast_url"] = bc["url"]
            if controller_required:
                mark_controller_ready(state_dir)
        elif controller_required:
            session_meta["status"] = "startup_failed"
            session_meta["startup_error"] = "human Web controller failed to start"
            write_session_meta(state_dir, session_meta)
            terminate_process(proc)
            print(json.dumps({
                "status": "error",
                "reason": "human Web controller failed to start",
                "state_dir": state_dir_str,
                "control_mode": control_mode,
            }, ensure_ascii=False))
            return 1
    write_session_meta(state_dir, session_meta)

    result = {
        "status": "hosting",
        "state_dir": state_dir_str,
        "post_id": post_id,
        "protocol_id": protocol_id,
        "control_mode": control_mode,
        "web_mode": web_mode,
        "share_ui": bool(
            getattr(args, "share_ui", False)
            or getattr(args, "share_bundle", False)
        ),
        "share_bundle": bool(getattr(args, "share_bundle", False)),
    }
    if shared_ui_manifest_hash:
        result["ui_manifest_hash"] = shared_ui_manifest_hash
    if shared_bundle_manifest_hash:
        result["bundle_manifest_hash"] = shared_bundle_manifest_hash
    if bc:
        result["broadcast_url"] = bc["url"]
    print(json.dumps(result, ensure_ascii=False))
    return 0


def run(args) -> int:
    if getattr(args, "_internal_run", False):
        return asyncio.run(_network_host(args))
    if getattr(args, "daemon", False):
        return _run_daemon(args)
    return asyncio.run(_network_host(args))


async def _network_host(args) -> int:
    control_mode = control_mode_from_args(args)
    protocol_dir = Path(args.protocol_dir)
    if is_received_bundle(protocol_dir):
        raise RuntimeError(
            "a received Host bundle is scoped to its original Session and cannot "
            "be hosted or re-shared"
        )
    spec = json.loads((protocol_dir / "spec.json").read_text(encoding="utf-8"))
    check_spec_version(spec, reject_unknown=True)
    assert_hooks_implemented(
        protocol_dir,
        allow_skeleton=getattr(args, "allow_skeleton_hooks", False),
    )
    options = parse_options(args.options)
    validate_extra_args(spec, args.extra_args)
    kp = load_keys(args.data_dir)
    proto_id = protocol_hash(protocol_dir / "spec.json")
    hooks = load_hooks(protocol_dir)
    ensure_control_mode_supported(hooks, control_mode)
    share_bundle = bool(getattr(args, "share_bundle", False))
    share_ui = bool(getattr(args, "share_ui", False) or share_bundle)
    shared_bundle_artifact = None
    if share_bundle:
        print(
            "[host] WARNING: --share-bundle offers executable hooks.py to a "
            "trusted peer. Their restricted subprocess is not a security sandbox.",
            file=sys.stderr,
        )
        shared_bundle_artifact = build_host_bundle_artifact(
            protocol_dir,
            protocol_id=proto_id,
        )
    shared_ui_artifact = None
    if share_ui:
        shared_ui_artifact = build_host_ui_artifact(protocol_dir)
        if shared_ui_artifact is None:
            flag = "--share-bundle" if share_bundle else "--share-ui"
            raise RuntimeError(f"{flag} requires protocol_dir/ui/index.html")
    # Pre-initialize hooks only to obtain metadata; use a temporary dir to avoid snapshot polluting CWD
    import tempfile
    _tmp_state = Path(tempfile.mkdtemp(prefix="aigenora-meta-"))
    hooks.proto_init(options, "host", args.extra_args or [], _tmp_state)
    display_name, tags, invite_type, hook_options = hooks.proto_host_metadata()
    import shutil
    shutil.rmtree(_tmp_state, ignore_errors=True)
    # Unify the options value published to the invitation post with the one fed to the
    # real-time engine for match_config_hash. Before this normalization they diverged:
    # the engine received the raw (possibly empty) ``options`` while the post carried
    # ``publish_options = options or hook_options``, so Host and Guest derived different
    # match_config_hash values and the handshake aborted with match_config_mismatch.
    # proto_init above already ran with the raw options; normalizing afterwards is safe.
    options = options or hook_options or {}

    pace = getattr(args, "pace", 0) or 0
    state_base = getattr(args, "_state_dir", None)
    event_bus = EventBus(state_base) if state_base else None

    runtime, node, accepted = await create_host_node()
    rest_client = RestClient(get_server(args.server), kp)
    renew_task: asyncio.Task | None = None
    try:
        node_addr = await node.net().node_addr()
        ticket = runtime.ticket_from_addr(node_addr)
        binding = transport_binding_canonical(kp.public_key, "iroh", ticket, proto_id)
        body = {
            "message": display_name,
            "tags": [t.strip() for t in str(tags).split(",") if t.strip()],
            "iroh_ticket": ticket,
            "transport": "iroh",
            "transport_info": {"version": 1, "endpoint_id": kp.public_key, "ticket": ticket},
            "transport_binding_signature": sign_raw(kp.private_key, binding.encode("utf-8")),
            "protocol_id": proto_id,
            "host_control_mode": control_mode,
            "type": invite_type or "supply",
        }
        if options:
            body["options"] = options
        data = rest_client.json("POST", "/api/v1/invitations", body, expected={201})
        post_id = data["post_id"]
        print(f"invite_created: true")
        print(f"post_id: {post_id}")
        invite_event = {"post_id": post_id, "protocol_id": proto_id}
        if shared_ui_artifact is not None:
            invite_event["ui_manifest_hash"] = shared_ui_artifact.offer["manifest_hash"]
        if shared_bundle_artifact is not None:
            invite_event["bundle_manifest_hash"] = (
                shared_bundle_artifact.manifest_hash
            )
        _emit(event_bus, "invite_created", invite_event)

        # P2: start the invitation auto-renewal loop (unless --no-invitation-renew is set)
        if not getattr(args, "no_invitation_renew", False):
            ttl_minutes = getattr(args, "invitation_ttl_minutes", 30) or 30
            renew_task = asyncio.create_task(
                _renew_invitation_loop(rest_client, post_id, event_bus,
                                       interval_seconds=120,
                                       max_session_minutes=ttl_minutes)
            )

        print("waiting_for_peer: true")
        channel = await accepted.get()
        first = await channel.recv()
        if not isinstance(first, dict):
            raise RuntimeError("Guest session initialization must be a JSON object")
        session_id_val = ""
        guest_public_key = ""
        guest_control_mode = "hybrid"
        bundle_shared: dict | None = None
        ready_ui: dict | None = None
        if first.get("_session_init") is True:
            guest_public_key = first.get("guest_public_key", "")
            candidate_guest_mode = first.get("guest_control_mode")
            if candidate_guest_mode in ("autonomous", "hybrid", "human"):
                guest_control_mode = candidate_guest_mode
            session_nonce = first.get("session_nonce", "")
            host_signature = sign_session(kp, post_id, kp.public_key, guest_public_key, proto_id, session_nonce)
            guest_ui_capabilities = first.get("ui_capabilities")
            guest_bundle_capabilities = first.get("bundle_capabilities")
            guest_bundle_capable = has_exact_bundle_capability(
                guest_bundle_capabilities
            )
            update_session_meta(
                state_base,
                peer_bundle_capability=guest_bundle_capable,
            )
            _emit(
                event_bus,
                "bundle_capability_received",
                {
                    "enabled": guest_bundle_capable,
                    "guest_public_key": guest_public_key,
                },
            )
            offered_ui = (
                shared_ui_artifact
                if isinstance(guest_ui_capabilities, dict)
                and guest_ui_capabilities.get("p2p_ui_v1") is True
                else None
            )
            offered_bundle = (
                shared_bundle_artifact
                if guest_bundle_capable
                else None
            )
            bundle_offer = None
            if offered_bundle is not None:
                bundle_binding = build_session_binding(
                    post_id=post_id,
                    host_public_key=kp.public_key,
                    guest_public_key=guest_public_key,
                    protocol_id=proto_id,
                    session_nonce=session_nonce,
                )
                bundle_offer = sign_bundle_offer(
                    offered_bundle,
                    binding=bundle_binding,
                    private_key=kp.private_key,
                )
                offer_summary = {
                    "source_peer": kp.public_key,
                    "protocol_id": proto_id,
                    "manifest_hash": bundle_offer["manifest_hash"],
                    "session_binding_hash": bundle_offer[
                        "session_binding_hash"
                    ],
                    "file_count": bundle_offer["file_count"],
                    "total_size_bytes": bundle_offer["total_size_bytes"],
                }
                update_session_meta(state_base, bundle_offer=offer_summary)
                _emit(event_bus, "bundle_offer_sent", offer_summary)
            elif shared_bundle_artifact is not None:
                _emit(
                    event_bus,
                    "bundle_artifact_not_offered",
                    {"reason": "guest_capability_not_enabled"},
                )
            elif guest_bundle_capable:
                _emit(
                    event_bus,
                    "bundle_artifact_not_offered",
                    {"reason": "host_share_not_enabled"},
                )
            proof_payload = {
                "_session_proof": True,
                "host_public_key": kp.public_key,
                "host_signature": host_signature,
                "protocol_id": proto_id,
                "host_control_mode": control_mode,
            }
            if offered_ui is not None:
                proof_payload["ui_offer"] = offered_ui.offer
            if bundle_offer is not None:
                proof_payload["bundle_offer"] = bundle_offer
            await channel.send(proof_payload)
            try:
                ready, artifact_kind = await serve_host_artifacts_and_wait_ready(
                    channel,
                    bundle_artifact=offered_bundle,
                    bundle_offer=bundle_offer,
                    ui_artifact=offered_ui,
                )
            except Exception as exc:
                if bundle_offer is not None:
                    _emit(
                        event_bus,
                        "bundle_artifact_rejected",
                        {
                            "manifest_hash": bundle_offer["manifest_hash"],
                            "reason": str(exc)[:200],
                        },
                    )
                raise
            if ready.get("_session_ready") is not True or not ready.get("session_id"):
                raise RuntimeError("Guest did not send a valid _session_ready frame")
            session_id_val = str(ready["session_id"])
            print(f"session_id: {session_id_val}")
            ready_ui_value = ready.get("ui_artifact")
            if isinstance(ready_ui_value, dict):
                ready_ui = ready_ui_value
                update_session_meta(state_base, ui_artifact=ready_ui)
                if ready_ui.get("source_kind") == "host_p2p":
                    _emit(event_bus, "ui_artifact_shared", ready_ui)
            if artifact_kind == "bundle":
                ready_bundle = ready.get("bundle_artifact")
                if (
                    not isinstance(ready_bundle, dict)
                    or ready_bundle.get("source_kind") != "host_p2p_bundle"
                    or ready_bundle.get("protocol_id") != proto_id
                    or ready_bundle.get("manifest_hash")
                    != bundle_offer["manifest_hash"]
                    or ready_bundle.get("session_binding_hash")
                    != bundle_offer["session_binding_hash"]
                ):
                    raise RuntimeError(
                        "Guest bundle provenance differs from the signed offer"
                    )
                bundle_shared = {
                    "status": "installed_by_guest",
                    "source_kind": "host_p2p_bundle",
                    "source_peer": kp.public_key,
                    "protocol_id": proto_id,
                    "manifest_hash": bundle_offer["manifest_hash"],
                    "session_binding_hash": bundle_offer[
                        "session_binding_hash"
                    ],
                    "file_count": bundle_offer["file_count"],
                    "total_size_bytes": bundle_offer["total_size_bytes"],
                }
                update_session_meta(state_base, bundle_artifact=bundle_shared)
                _emit(event_bus, "bundle_artifact_shared", bundle_shared)
            elif bundle_offer is not None:
                _emit(
                    event_bus,
                    "bundle_artifact_not_requested",
                    {
                        "manifest_hash": bundle_offer["manifest_hash"],
                        "reason": "guest_used_non_bundle_path",
                    },
                )
        else:
            channel = AsyncReplayChannel(channel, first)
        print(f"peer_joined: {guest_public_key[:16]}...")
        update_session_meta(state_base, peer_control_mode=guest_control_mode)
        peer_event = {
            "guest_public_key": guest_public_key,
            "session_id": session_id_val,
            "local_control_mode": control_mode,
            "peer_control_mode": guest_control_mode,
        }
        if bundle_shared is not None:
            peer_event["bundle_artifact"] = bundle_shared
        if ready_ui is not None:
            peer_event["ui_artifact"] = ready_ui
        _emit(event_bus, "peer_joined", peer_event)
        # Paired; stop the renewal loop
        if renew_task is not None and not renew_task.done():
            renew_task.cancel()
            try:
                await renew_task
            except (asyncio.CancelledError, Exception):
                pass
            renew_task = None
        try:
            if getattr(args, "_controller_required", False) and state_base:
                from aigenora.agent._controller import wait_for_controller_ready
                await asyncio.to_thread(wait_for_controller_ready, state_base)
            result = await run_host_async(protocol_dir, channel, options=options, args=args.extra_args,
                                 state_base=state_base, event_bus=event_bus,
                                 control_mode=control_mode, pace=pace,
                                 heartbeat_interval=getattr(args, "heartbeat_interval", 10.0),
                                 heartbeat_timeout=getattr(args, "heartbeat_timeout", 30.0),
                                 session_id=session_id_val, keypair=kp,
                                 peer_public_key=guest_public_key) or {}
        except Exception as exc:
            msg = str(exc)[:200]
            _emit(event_bus, "session_ended", {"completed": False, "reason": "engine_error", "error": msg})
            close_session(rest_client, session_id_val, status="failed", event_bus=event_bus)
            update_session_meta(state_base, status="crashed", completed=False,
                                ended_at=time.time(), end_reason="engine_error", error=msg)
            raise
        completed = bool(result.get("completed", True))
        end_reason = result.get("reason")
        print("done")
        # engine emits the authoritative session_ended on every terminal path: completed=True
        # for a completed match, completed=False + peer_disconnected on disconnect (P1-6). Only
        # mirror the normal completed=True path here; never re-emit a spurious completed=True
        # over a disconnect, which would fake a successful match outcome (proof/score pollution).
        if completed:
            _emit(event_bus, "session_ended", {"completed": True})
        close_session(rest_client, session_id_val, status="failed" if not completed else "closed",
                      event_bus=event_bus)
        # v012 批次3：胜负上报走 /result（双方一致才结算 ELO），与 close 解耦
        # v016: mental_poker 协议仅在 audit_passed=True 且 receipt 双签通过后才上报
        if completed and result.get("outcome") and result.get("audit_passed", True):
            report_result(rest_client, session_id_val, result.get("outcome"))
        # daemon parent returns right after startup, so the business subprocess must persist the
        # final session.json status itself — otherwise console/list shows a stale "running" session.
        update_session_meta(state_base, status="aborted" if not completed else "closed",
                            completed=completed, ended_at=time.time(), end_reason=end_reason)
        return 0
    finally:
        if renew_task is not None and not renew_task.done():
            renew_task.cancel()
            try:
                await renew_task
            except (asyncio.CancelledError, Exception):
                pass
        await node.node().shutdown()


async def _renew_invitation_loop(client: RestClient, post_id: str, event_bus: EventBus | None, *,
                                  interval_seconds: int = 120,
                                  max_session_minutes: int = 30) -> None:
    """P2: Invitation auto-renewal loop.

    - Calls POST /api/v1/invitations/{post_id}/renew every interval_seconds seconds
    - 200 → emit invitation_renewed
    - 4xx/5xx → emit invitation_renew_failed and exit the loop (does not affect the main match)
    - Cumulative runtime exceeding max_session_minutes → exit proactively, so a zombie Host
      does not occupy the post_id forever
    - On outer cancel(), exits cleanly via CancelledError

    Concurrency cancellation semantics (Codex review Q1 consensus): on cancel the asyncio task
    immediately receives CancelledError, but a synchronous RestClient currently blocking inside
    to_thread cannot be force-killed — it may keep running until its HTTP timeout and produce one
    harmless renew RPC. This is an accepted limitation; RestClient is not rewritten as async for it.
    """
    deadline = time.time() + max_session_minutes * 60
    while True:
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
        if time.time() >= deadline:
            _emit(event_bus, "invitation_renew_stopped",
                  {"post_id": post_id, "reason": "max_session_minutes_reached",
                   "max_session_minutes": max_session_minutes})
            return
        try:
            data = await asyncio.to_thread(
                client.json, "POST", f"/api/v1/invitations/{post_id}/renew", {}, {200},
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _emit(event_bus, "invitation_renew_failed",
                  {"post_id": post_id, "error": str(exc)[:200]})
            return
        expires_at = (data or {}).get("expires_at") if isinstance(data, dict) else None
        _emit(event_bus, "invitation_renewed",
              {"post_id": post_id, "expires_at": expires_at})


def _emit(bus: EventBus | None, event_type: str, data: dict | None = None) -> None:
    if bus is not None:
        bus.emit(event_type, data=data)
