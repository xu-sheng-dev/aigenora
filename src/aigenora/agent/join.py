from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from aigenora.agent.protocol import prepare_protocol
from aigenora.control import HUMAN, control_mode_from_args, ensure_control_mode_supported
from aigenora.agent.skeleton import assert_hooks_implemented
from aigenora.agent._daemon import (
    DEFAULT_JOIN_STARTUP_WAIT_SECONDS,
    read_log_excerpt,
    startup_wait_seconds,
    terminate_process,
    update_session_meta,
    wait_for_event,
    write_session_meta,
)
from aigenora.engine.config import get_server
from aigenora.engine.crypto import session_canonical, transport_binding_canonical
from aigenora.engine.keys import load_keys
from aigenora.engine.keys import verify_raw
from aigenora.engine.p2p import connect_by_ticket
from aigenora.engine.rest import RestClient
from aigenora.proto.engine import run_guest_async
from aigenora.proto.loader import load_hooks
from aigenora.proto.sdk import EventBus
from aigenora.proto.session import SessionProof, close_session, new_session_nonce, report_result, sign_session, submit_session
from aigenora.proto.spec_version import check_spec_version
from aigenora.proto.validate import load_spec, validate_extra_args
from aigenora.agent.protocol_ui_p2p import describe_local_ui, maybe_receive_host_ui
from aigenora.agent.protocol_bundle import (
    BUNDLE_INSTALL_DIRNAME,
    build_session_binding,
    is_received_bundle,
)
from aigenora.agent.protocol_bundle_p2p import maybe_receive_host_bundle
from aigenora.proto.remote_hooks import close_remote_hook_workers


def _ticket_from_post(post: dict[str, Any]) -> str:
    transport = post.get("transport") or "iroh"
    if transport != "iroh":
        raise RuntimeError(f"unsupported invitation transport: {transport}")
    info = post.get("transport_info")
    if isinstance(info, dict) and info.get("ticket"):
        return str(info["ticket"])
    ticket = post.get("iroh_ticket")
    if ticket:
        return str(ticket)
    raise RuntimeError("invitation has no iroh ticket")


def _resolve_state_dir(args) -> Path:
    data_dir = Path(args.data_dir) if args.data_dir else Path.cwd() / ".aigenora"
    sessions_dir = data_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    state_dir = sessions_dir / f"guest-{int(time.time() * 1000)}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _run_daemon(args) -> int:
    state_dir = _resolve_state_dir(args)
    state_dir_str = str(state_dir)
    control_mode = control_mode_from_args(args)
    from aigenora.agent._web_mode import resolve_web_mode
    web_mode = resolve_web_mode(args, control_mode=control_mode)
    controller_required = control_mode == HUMAN and web_mode != "off"

    cmd = [sys.executable, "-m", "aigenora", "join",
           args.post_id,
           "--_internal-run",
           "--_state-dir", state_dir_str]
    if args.data_dir:
        cmd.extend(["--data-dir", str(Path(args.data_dir).resolve())])
    if args.server:
        cmd.extend(["--server", args.server])
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
    if getattr(args, "allow_skeleton_hooks", False):
        cmd.append("--allow-skeleton-hooks")
    if getattr(args, "accept_ui", False):
        cmd.append("--accept-ui")
    if getattr(args, "accept_host_ui", False):
        cmd.append("--accept-host-ui")
    if getattr(args, "accept_host_bundle", False):
        print(
            "[join] WARNING: --accept-host-bundle executes this Host's hooks.py "
            "in a restricted subprocess. It is not a security sandbox. Continue "
            "only because you trust this Host.",
            file=sys.stderr,
        )
        cmd.append("--accept-host-bundle")
    if args.extra_args:
        cmd.extend(["--"] + args.extra_args)

    session_meta = {
        "role": "guest",
        "status": "starting",
        "post_id": args.post_id,
        "state_dir": state_dir_str,
        "local_control_mode": control_mode,
        "web_mode": web_mode,
        "controller_required": controller_required,
        "accept_platform_ui": bool(getattr(args, "accept_ui", False)),
        "accept_host_ui_p2p": bool(
            getattr(args, "accept_host_ui", False)
            or getattr(args, "accept_host_bundle", False)
        ),
        "accept_host_bundle_p2p": bool(
            getattr(args, "accept_host_bundle", False)
        ),
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
        "peer_joined",
        timeout_seconds=startup_wait_seconds(
            DEFAULT_JOIN_STARTUP_WAIT_SECONDS
        ),
        required_data_keys=("session_id",),
    )
    session_id = ""
    if startup_event is not None:
        startup_data = startup_event.get("data") or {}
        session_id = str(startup_data.get("session_id") or "")
        group_id = str(startup_data.get("group_id") or "")
        member_id = str(startup_data.get("member_id") or "")
        seat = startup_data.get("seat")
        protocol_dir = str(startup_data.get("protocol_dir") or "")
        local_protocol_dir = str(startup_data.get("local_protocol_dir") or "")
        active_hooks_source = str(
            startup_data.get("active_hooks_source") or ""
        )
        peer_control_mode = str(startup_data.get("peer_control_mode") or "")
        ui_artifact = startup_data.get("ui_artifact")
        bundle_artifact = startup_data.get("bundle_artifact")
        ui_dir = str(startup_data.get("ui_dir") or "")
        if session_id:
            session_meta["session_id"] = session_id
        if group_id:
            session_meta["group_id"] = group_id
            session_meta["group_role"] = "member"
        if member_id:
            session_meta["member_id"] = member_id
        if isinstance(seat, int) and not isinstance(seat, bool):
            session_meta["seat"] = seat
        if protocol_dir:
            session_meta["protocol_dir"] = protocol_dir
        if local_protocol_dir:
            session_meta["local_protocol_dir"] = local_protocol_dir
        if active_hooks_source:
            session_meta["active_hooks_source"] = active_hooks_source
        if peer_control_mode:
            session_meta["peer_control_mode"] = peer_control_mode
        if isinstance(ui_artifact, dict):
            session_meta["ui_artifact"] = ui_artifact
        if isinstance(bundle_artifact, dict):
            session_meta["bundle_artifact"] = bundle_artifact
        if ui_dir:
            session_meta["ui_dir"] = ui_dir
        if (
            session_id
            or group_id
            or member_id
            or isinstance(seat, int)
            or protocol_dir
            or local_protocol_dir
            or active_hooks_source
            or peer_control_mode
            or isinstance(ui_artifact, dict)
            or isinstance(bundle_artifact, dict)
            or ui_dir
        ):
            update_session_meta(
                state_dir,
                **{
                    key: session_meta[key]
                    for key in (
                        "session_id",
                        "group_id",
                        "group_role",
                        "member_id",
                        "seat",
                        "protocol_dir",
                        "local_protocol_dir",
                        "active_hooks_source",
                        "peer_control_mode",
                        "ui_artifact",
                        "bundle_artifact",
                        "ui_dir",
                    )
                    if key in session_meta
                },
            )
    else:
        exit_code = proc.poll()
        if isinstance(exit_code, int):
            session_meta["status"] = "startup_failed"
            session_meta["exit_code"] = exit_code
            session_meta["startup_error"] = "process exited before peer_joined"
            excerpt = read_log_excerpt(state_dir)
            if excerpt:
                session_meta["last_error_excerpt"] = excerpt
            write_session_meta(state_dir, session_meta)
            result = {
                "status": "error",
                "reason": "process exited before peer_joined",
                "post_id": args.post_id,
                "state_dir": state_dir_str,
                "exit_code": exit_code,
            }
            if excerpt:
                result["error_excerpt"] = excerpt
            print(json.dumps(result, ensure_ascii=False))
            return 1
        excerpt = read_log_excerpt(state_dir)
        session_meta["status"] = "startup_timeout"
        session_meta["startup_error"] = "timeout waiting for peer_joined"
        if excerpt:
            session_meta["last_error_excerpt"] = excerpt
        write_session_meta(state_dir, session_meta)
        terminate_process(proc)
        result = {
            "status": "error",
            "reason": "timeout waiting for peer_joined",
            "post_id": args.post_id,
            "state_dir": state_dir_str,
        }
        if excerpt:
            result["error_excerpt"] = excerpt
        print(json.dumps(result, ensure_ascii=False))
        return 1

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
                "post_id": args.post_id,
                "state_dir": state_dir_str,
                "control_mode": control_mode,
            }, ensure_ascii=False))
            return 1
    if bc:
        update_session_meta(
            state_dir,
            broadcast_pid=session_meta["broadcast_pid"],
            broadcast_url=session_meta["broadcast_url"],
        )

    result = {
        "status": "joining",
        "post_id": args.post_id,
        "state_dir": state_dir_str,
        "control_mode": control_mode,
        "web_mode": web_mode,
    }
    if session_id:
        result["session_id"] = session_id
    if session_meta.get("group_id"):
        result["group_id"] = session_meta["group_id"]
        result["seat"] = session_meta.get("seat")
    if isinstance(session_meta.get("ui_artifact"), dict):
        result["ui_artifact"] = session_meta["ui_artifact"]
    if isinstance(session_meta.get("bundle_artifact"), dict):
        result["bundle_artifact"] = session_meta["bundle_artifact"]
    if bc:
        result["broadcast_url"] = bc["url"]
    print(json.dumps(result, ensure_ascii=False))
    return 0


def run(args) -> int:
    if getattr(args, "_internal_run", False):
        return asyncio.run(_join(args))
    if getattr(args, "daemon", False):
        return _run_daemon(args)
    return asyncio.run(_join(args))


async def _join(args) -> int:
    control_mode = control_mode_from_args(args)
    accept_host_bundle = bool(getattr(args, "accept_host_bundle", False))
    accept_host_ui = bool(
        getattr(args, "accept_host_ui", False) or accept_host_bundle
    )
    if accept_host_bundle and not getattr(args, "_internal_run", False):
        print(
            "[join] WARNING: --accept-host-bundle executes this Host's hooks.py "
            "in a restricted subprocess. It is not a security sandbox. Continue "
            "only because you trust this Host.",
            file=sys.stderr,
        )
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    post = client.json("GET", f"/api/v1/invitations/{args.post_id}", expected={200})
    if post.get("public_key") == kp.public_key:
        raise RuntimeError("this is your own post")
    proto_id = post.get("protocol_id") or ""
    if not proto_id:
        raise RuntimeError("invitation does not declare a protocol")
    ticket = _ticket_from_post(post)
    if not post.get("transport_binding_signature"):
        raise RuntimeError(
            "invitation has no transport_binding_signature — possible MITM attack"
        )
    canonical = transport_binding_canonical(
        post.get("public_key", ""), "iroh", ticket, proto_id
    )
    verify_raw(
        post.get("public_key", ""),
        canonical.encode("utf-8"),
        post["transport_binding_signature"],
    )
    proto_dir, created_hooks = prepare_protocol(
        client,
        proto_id,
        args.data_dir,
        accept_ui=bool(getattr(args, "accept_ui", False)),
    )
    if is_received_bundle(proto_dir):
        raise RuntimeError(
            "a received Host bundle cannot be reused as a trusted local protocol"
        )
    spec = load_spec(proto_dir / "spec.json")
    check_spec_version(spec, reject_unknown=True)
    from aigenora.agent.group import (
        is_authoritative_group,
        run_group_join_command,
    )

    if is_authoritative_group(spec):
        return await run_group_join_command(
            args,
            post=post,
            protocol_dir=proto_dir,
            spec=spec,
        )
    local_hooks_error: Exception | None = None
    try:
        assert_hooks_implemented(
            proto_dir,
            allow_skeleton=getattr(args, "allow_skeleton_hooks", False),
        )
        ensure_control_mode_supported(load_hooks(proto_dir), control_mode)
    except Exception as exc:
        local_hooks_error = exc
        if created_hooks and isinstance(exc, RuntimeError):
            local_hooks_error = RuntimeError(
                f"protocol fetched to {proto_dir}; hooks.py is a freshly generated skeleton.\n"
                f"{exc}"
            )
        if not accept_host_bundle:
            if local_hooks_error is exc:
                raise
            raise local_hooks_error from exc
    validate_extra_args(spec, args.extra_args)
    options = post.get("options") if isinstance(post.get("options"), dict) else {}
    host_control_mode = post.get("host_control_mode") or "hybrid"
    if host_control_mode not in ("autonomous", "hybrid", "human"):
        host_control_mode = "hybrid"
    print(f"[join] protocol_dir: {proto_dir}")

    pace = getattr(args, "pace", 0) or 0
    state_base = getattr(args, "_state_dir", None)
    event_bus = EventBus(state_base) if state_base else None

    _, node, channel = await connect_by_ticket(ticket)
    temporary_artifact_root: Path | None = None
    try:
        nonce = new_session_nonce()
        session_init = {
            "_session_init": True,
            "guest_public_key": kp.public_key,
            "session_nonce": nonce,
            "guest_control_mode": control_mode,
        }
        if accept_host_ui:
            session_init["ui_capabilities"] = {"p2p_ui_v1": True}
        if accept_host_bundle:
            session_init["bundle_capabilities"] = {"p2p_bundle_v1": True}
        update_session_meta(
            state_base,
            bundle_capability_declared=accept_host_bundle,
        )
        if event_bus:
            event_bus.emit(
                "bundle_capability_declared",
                {"enabled": accept_host_bundle},
            )
        await channel.send(session_init)
        proof_msg = await channel.recv()
        if (
            not isinstance(proof_msg, dict)
            or proof_msg.get("_session_proof") is not True
        ):
            raise RuntimeError("host did not return session proof")
        host_public_key = proof_msg.get("host_public_key") or post.get("public_key", "")
        if host_public_key != post.get("public_key"):
            raise RuntimeError("session proof host_public_key does not match invitation")
        host_signature = proof_msg.get("host_signature", "")
        canonical_protocol_id = proof_msg.get("protocol_id") or proto_id
        if canonical_protocol_id != proto_id:
            raise RuntimeError("session proof protocol_id does not match invitation")
        proof_host_mode = proof_msg.get("host_control_mode")
        if proof_host_mode in ("autonomous", "hybrid", "human"):
            host_control_mode = proof_host_mode
        verify_raw(
            host_public_key,
            session_canonical(args.post_id, host_public_key, kp.public_key, canonical_protocol_id, nonce).encode("utf-8"),
            host_signature,
        )
        artifact_root: Path | None = None
        if accept_host_ui:
            if state_base:
                artifact_root = Path(state_base).resolve()
            else:
                temporary_artifact_root = Path(
                    tempfile.mkdtemp(prefix="aigenora-host-artifacts-")
                ).resolve()
                artifact_root = temporary_artifact_root
        bundle_install_dir = (
            artifact_root / BUNDLE_INSTALL_DIRNAME
            if artifact_root is not None
            else None
        )
        bundle_binding = build_session_binding(
            post_id=args.post_id,
            host_public_key=host_public_key,
            guest_public_key=kp.public_key,
            protocol_id=canonical_protocol_id,
            session_nonce=nonce,
        )
        bundle_offer = proof_msg.get("bundle_offer")
        if isinstance(bundle_offer, dict):
            offer_summary = {
                key: bundle_offer.get(key)
                for key in (
                    "protocol_id",
                    "manifest_hash",
                    "session_binding_hash",
                    "file_count",
                    "total_size_bytes",
                )
            }
            offer_summary["source_peer"] = host_public_key
            update_session_meta(state_base, bundle_offer=offer_summary)
            if event_bus:
                event_bus.emit("bundle_offer_received", offer_summary)
        if not accept_host_bundle:
            if event_bus:
                event_bus.emit(
                    "bundle_artifact_refused",
                    {"reason": "guest_consent_not_enabled"},
                )
        elif bundle_offer is None and event_bus:
            event_bus.emit(
                "bundle_artifact_unavailable",
                {"reason": "host_did_not_offer_bundle"},
            )
        try:
            bundle_artifact = await maybe_receive_host_bundle(
                channel,
                offer=bundle_offer,
                accept_host_bundle=accept_host_bundle,
                session_binding=bundle_binding,
                protocol_id=canonical_protocol_id,
                local_spec_path=proto_dir / "spec.json",
                install_dir=bundle_install_dir,
            )
        except Exception as exc:
            if event_bus:
                staging_clean = True
                if artifact_root is not None:
                    staging_clean = not any(
                        artifact_root.glob(".bundle-staging-*")
                    )
                event_bus.emit(
                    "bundle_artifact_rejected",
                    {
                        "reason": str(exc)[:200],
                        "staging_clean": staging_clean,
                    },
                )
            raise

        active_protocol_dir = proto_dir
        active_ui_dir: str | None = None
        if bundle_artifact is not None:
            if bundle_install_dir is None:
                raise RuntimeError("bundle installation directory is unavailable")
            active_protocol_dir = bundle_install_dir
            ensure_control_mode_supported(
                load_hooks(active_protocol_dir),
                control_mode,
            )
            active_ui_dir = str((active_protocol_dir / "ui").resolve())
            ui_artifact: dict[str, Any] | None = {
                "status": "installed",
                "source_kind": "host_p2p",
                "source_peer": host_public_key,
                "manifest_hash": bundle_artifact["ui_manifest_hash"],
                "file_count": bundle_artifact["ui_file_count"],
                "total_size_bytes": bundle_artifact["ui_total_size_bytes"],
                "bundle_manifest_hash": bundle_artifact["manifest_hash"],
            }
            engine_state_base: str | Path | None = (
                state_base if state_base else artifact_root
            )
        else:
            if local_hooks_error is not None:
                raise local_hooks_error
            p2p_ui_root = (
                artifact_root / "ui-artifact"
                if artifact_root is not None
                else None
            )
            ui_artifact = await maybe_receive_host_ui(
                channel,
                offer=proof_msg.get("ui_offer"),
                protocol_dir=proto_dir,
                protocol_id=canonical_protocol_id,
                host_public_key=host_public_key,
                accept_host_ui=accept_host_ui,
                install_dir=p2p_ui_root,
            )
            if ui_artifact is None:
                ui_artifact = describe_local_ui(proto_dir)
            if (
                ui_artifact is not None
                and ui_artifact.get("source_kind") == "host_p2p"
                and p2p_ui_root is not None
            ):
                active_ui_dir = str((p2p_ui_root / "ui").resolve())
            engine_state_base = state_base

        session_updates: dict[str, Any] = {
            "protocol_dir": str(active_protocol_dir.resolve()),
            "local_protocol_dir": str(proto_dir.resolve()),
            "active_hooks_source": (
                "host_p2p_bundle"
                if bundle_artifact is not None
                else "trusted_local"
            ),
        }
        if bundle_artifact is not None:
            session_updates["bundle_artifact"] = bundle_artifact
        if ui_artifact is not None:
            session_updates["ui_artifact"] = ui_artifact
            if active_ui_dir:
                session_updates["ui_dir"] = active_ui_dir
        update_session_meta(state_base, **session_updates)
        if event_bus:
            if bundle_artifact is not None:
                event_bus.emit("bundle_artifact_accepted", bundle_artifact)
                event_bus.emit("bundle_artifact_ready", bundle_artifact)
            if ui_artifact is not None:
                event_bus.emit("ui_artifact_ready", ui_artifact)
        guest_signature = sign_session(kp, args.post_id, host_public_key, kp.public_key, canonical_protocol_id, nonce)
        proof = SessionProof(
            post_id=args.post_id,
            host_public_key=host_public_key,
            guest_public_key=kp.public_key,
            protocol_id=canonical_protocol_id,
            session_nonce=nonce,
            host_signature=host_signature,
            guest_signature=guest_signature,
        )
        session_id = submit_session(client, proof)
        ready_payload = {"_session_ready": True, "session_id": session_id}
        if ui_artifact is not None and ui_artifact.get("source_kind") == "host_p2p":
            ready_payload["ui_artifact"] = ui_artifact
        if bundle_artifact is not None:
            ready_payload["bundle_artifact"] = bundle_artifact
        await channel.send(ready_payload)
        print(f"[join] session_id: {session_id}")
        update_session_meta(state_base, peer_control_mode=host_control_mode)
        if event_bus:
            # Carry protocol_dir back to the daemon parent so its session.json records it
            # (mirrors host._run_daemon). Without this, web.resolve_protocol_dir cannot
            # reverse-lookup the protocol on the guest side and /api/ui-available stays false.
            event_bus.emit("peer_joined", {
                "host_public_key": host_public_key,
                "session_id": session_id,
                "protocol_dir": str(active_protocol_dir),
                "local_protocol_dir": str(proto_dir),
                "local_control_mode": control_mode,
                "peer_control_mode": host_control_mode,
                "active_hooks_source": (
                    "host_p2p_bundle"
                    if bundle_artifact is not None
                    else "trusted_local"
                ),
                **(
                    {"bundle_artifact": bundle_artifact}
                    if bundle_artifact is not None
                    else {}
                ),
                **({"ui_artifact": ui_artifact} if ui_artifact is not None else {}),
                **({"ui_dir": active_ui_dir} if active_ui_dir else {}),
            })
        try:
            if getattr(args, "_controller_required", False) and state_base:
                from aigenora.agent._controller import wait_for_controller_ready
                await asyncio.to_thread(wait_for_controller_ready, state_base)
            result = await run_guest_async(active_protocol_dir, channel, options=options, args=args.extra_args,
                                  state_base=engine_state_base, event_bus=event_bus,
                                  control_mode=control_mode, pace=pace,
                                  heartbeat_interval=getattr(args, "heartbeat_interval", 10.0),
                                  heartbeat_timeout=getattr(args, "heartbeat_timeout", 30.0),
                                  session_id=session_id, keypair=kp,
                                  peer_public_key=host_public_key) or {}
        except Exception as exc:
            msg = str(exc)[:200]
            if event_bus:
                event_bus.emit("session_ended", {"completed": False, "reason": "engine_error", "error": msg})
            close_session(client, session_id, status="failed", event_bus=event_bus)
            update_session_meta(state_base, status="crashed", completed=False,
                                ended_at=time.time(), end_reason="engine_error", error=msg)
            raise
        completed = bool(result.get("completed", True))
        end_reason = result.get("reason")
        print("done")
        # engine emits session_ended on every terminal path (completed=True for a completed match,
        # completed=False + peer_disconnected on disconnect). Only mirror the normal path here;
        # never re-emit completed=True over a disconnect (would fake a successful match outcome).
        if event_bus and completed:
            event_bus.emit("session_ended", {"completed": True})
        close_session(client, session_id, status="failed" if not completed else "closed",
                      event_bus=event_bus)
        # v012 批次3：胜负上报走 /result（双方一致才结算 ELO），与 close 解耦
        # v016: mental_poker 协议仅在 audit_passed=True 且 receipt 双签通过后才上报
        if completed and result.get("outcome") and result.get("audit_passed", True):
            report_result(client, session_id, result.get("outcome"))
        update_session_meta(state_base, status="aborted" if not completed else "closed",
                            completed=completed, ended_at=time.time(), end_reason=end_reason)
        return 0
    finally:
        close_remote_hook_workers(reason="join_finally")
        await node.node().shutdown()
        if temporary_artifact_root is not None:
            shutil.rmtree(temporary_artifact_root, ignore_errors=True)
