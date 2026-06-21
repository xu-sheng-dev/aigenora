from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from aigenora.agent.protocol import prepare_protocol
from aigenora.agent.skeleton import assert_hooks_implemented
from aigenora.agent._daemon import read_log_excerpt, wait_for_event, update_session_meta, write_session_meta
from aigenora.engine.config import get_server
from aigenora.engine.crypto import session_canonical, transport_binding_canonical
from aigenora.engine.keys import load_keys
from aigenora.engine.keys import verify_raw
from aigenora.engine.p2p import connect_by_ticket
from aigenora.engine.rest import RestClient
from aigenora.proto.engine import run_guest_async
from aigenora.proto.sdk import EventBus
from aigenora.proto.session import SessionProof, close_session, new_session_nonce, report_result, sign_session, submit_session
from aigenora.proto.spec_version import check_spec_version
from aigenora.proto.validate import load_spec, validate_extra_args


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

    cmd = [sys.executable, "-m", "aigenora", "join",
           args.post_id,
           "--_internal-run",
           "--_state-dir", state_dir_str]
    if args.data_dir:
        cmd.extend(["--data-dir", str(Path(args.data_dir).resolve())])
    if args.server:
        cmd.extend(["--server", args.server])
    daemon_flag = args.daemon
    coach_flag = getattr(args, "coach", False) or daemon_flag
    pace_val = getattr(args, "pace", 0) or 0
    if coach_flag:
        cmd.append("--coach")
    if pace_val > 0:
        cmd.extend(["--pace", str(pace_val)])
    hb_interval = getattr(args, "heartbeat_interval", 10.0)
    hb_timeout = getattr(args, "heartbeat_timeout", 30.0)
    cmd.extend(["--heartbeat-interval", str(hb_interval)])
    cmd.extend(["--heartbeat-timeout", str(hb_timeout)])
    if getattr(args, "allow_skeleton_hooks", False):
        cmd.append("--allow-skeleton-hooks")
    if args.extra_args:
        cmd.extend(["--"] + args.extra_args)

    session_meta = {
        "role": "guest",
        "status": "starting",
        "post_id": args.post_id,
        "state_dir": state_dir_str,
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
        required_data_keys=("session_id",),
    )
    session_id = ""
    if startup_event is not None:
        startup_data = startup_event.get("data") or {}
        session_id = str(startup_data.get("session_id") or "")
        protocol_dir = str(startup_data.get("protocol_dir") or "")
        if session_id:
            session_meta["session_id"] = session_id
        if protocol_dir:
            session_meta["protocol_dir"] = protocol_dir
        if session_id or protocol_dir:
            write_session_meta(state_dir, session_meta)
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

    # Based on web_mode, decide whether to start the relay subprocess and whether to open a browser
    from aigenora.agent._web_mode import resolve_web_mode
    from aigenora.agent.web import spawn_broadcast
    web_mode = resolve_web_mode(args)
    session_meta["web_mode"] = web_mode
    bc = None
    if web_mode != "off":
        bc = spawn_broadcast(state_dir, open_browser=(web_mode == "auto"))
        if bc:
            session_meta["broadcast_pid"] = bc["pid"]
            session_meta["broadcast_url"] = bc["url"]
    write_session_meta(state_dir, session_meta)

    result = {
        "status": "joining",
        "post_id": args.post_id,
        "state_dir": state_dir_str,
        "web_mode": web_mode,
    }
    if session_id:
        result["session_id"] = session_id
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
        raise RuntimeError("invitation has no transport_binding_signature — possible MITM attack")
    canonical = transport_binding_canonical(post.get("public_key", ""), "iroh", ticket, proto_id)
    verify_raw(post.get("public_key", ""), canonical.encode("utf-8"), post["transport_binding_signature"])
    proto_dir, created_hooks = prepare_protocol(client, proto_id, args.data_dir)
    # P3 fix (second Codex round consensus): go through assert_hooks_implemented uniformly so that
    # --allow-skeleton-hooks / AIGENORA_ALLOW_SKELETON_HOOKS behave identically on first fetch and
    # subsequent join. The exception is wrapped with context to preserve the "just fetched X" hint.
    try:
        assert_hooks_implemented(
            proto_dir,
            allow_skeleton=getattr(args, "allow_skeleton_hooks", False),
        )
    except RuntimeError as exc:
        if created_hooks:
            raise RuntimeError(
                f"protocol fetched to {proto_dir}; hooks.py is a freshly generated skeleton.\n"
                f"{exc}"
            ) from exc
        raise
    spec = load_spec(proto_dir / "spec.json")
    check_spec_version(spec, reject_unknown=True)
    validate_extra_args(spec, args.extra_args)
    options = post.get("options") if isinstance(post.get("options"), dict) else {}
    print(f"[join] protocol_dir: {proto_dir}")

    coach = getattr(args, "coach", False)
    pace = getattr(args, "pace", 0) or 0
    state_base = getattr(args, "_state_dir", None)
    event_bus = EventBus(state_base) if state_base else None

    _, node, channel = await connect_by_ticket(ticket)
    try:
        nonce = new_session_nonce()
        await channel.send({"_session_init": True, "guest_public_key": kp.public_key, "session_nonce": nonce})
        proof_msg = await channel.recv()
        if proof_msg.get("_session_proof") is not True:
            raise RuntimeError("host did not return session proof")
        host_public_key = proof_msg.get("host_public_key") or post.get("public_key", "")
        if host_public_key != post.get("public_key"):
            raise RuntimeError("session proof host_public_key does not match invitation")
        host_signature = proof_msg.get("host_signature", "")
        canonical_protocol_id = proof_msg.get("protocol_id") or proto_id
        verify_raw(
            host_public_key,
            session_canonical(args.post_id, host_public_key, kp.public_key, canonical_protocol_id, nonce).encode("utf-8"),
            host_signature,
        )
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
        await channel.send({"_session_ready": True, "session_id": session_id})
        print(f"[join] session_id: {session_id}")
        if event_bus:
            # Carry protocol_dir back to the daemon parent so its session.json records it
            # (mirrors host._run_daemon). Without this, web.resolve_protocol_dir cannot
            # reverse-lookup the protocol on the guest side and /api/ui-available stays false.
            event_bus.emit("peer_joined", {
                "host_public_key": host_public_key,
                "session_id": session_id,
                "protocol_dir": str(proto_dir),
            })
        result = await run_guest_async(proto_dir, channel, options=options, args=args.extra_args,
                              state_base=state_base, event_bus=event_bus, coach=coach, pace=pace,
                              heartbeat_interval=getattr(args, "heartbeat_interval", 10.0),
                              heartbeat_timeout=getattr(args, "heartbeat_timeout", 30.0)) or {}
        game_over = bool(result.get("game_over", True))
        end_reason = result.get("reason")
        print("done")
        # engine emits session_ended on every terminal path (game_over=True for a completed match,
        # game_over=False + peer_disconnected on disconnect). Only mirror the normal path here;
        # never re-emit game_over=True over a disconnect (would fake a successful match outcome).
        if event_bus and game_over:
            event_bus.emit("session_ended", {"game_over": True})
        close_session(client, session_id, status="failed" if not game_over else "closed",
                      event_bus=event_bus)
        # v012 批次3：胜负上报走 /result（双方一致才结算 ELO），与 close 解耦
        if game_over and result.get("winner"):
            report_result(client, session_id, result.get("winner"))
        update_session_meta(state_base, status="aborted" if not game_over else "closed",
                            game_over=game_over, ended_at=time.time(), end_reason=end_reason)
        return 0
    finally:
        await node.node().shutdown()
