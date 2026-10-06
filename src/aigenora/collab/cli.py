"""Argparse wiring for the ``aigenora collab`` command namespace.

Subcommands:
- ``pair gen|trust|list`` — PSK + trusted-peer registry management
- ``host``                — B-side task service (invitation + admission + RPC)
- ``submit``              — A-side task submission with artifact upload
- ``status|chat|cancel|pull|tasks`` — task lifecycle from the A side
- ``worker``              — B-side worker agent interface (see worker_cli)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

from aigenora.collab import guest_app
from aigenora.collab.errors import AdmissionError
from aigenora.collab.psk import (
    PeerEntry,
    PeerRegistry,
    generate_psk,
    load_psk,
    psk_path,
)


def build_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="collab_cmd", required=True)

    pair = sub.add_parser("pair", help="manage the shared PSK and trusted peer registry")
    pair_sub = pair.add_subparsers(dest="pair_cmd", required=True)
    pair_gen = pair_sub.add_parser("gen", help="generate a new PSK file (share it with the peer out-of-band)")
    pair_gen.add_argument("--force", action="store_true", help="overwrite an existing PSK")
    pair_gen.add_argument("--out", default=None, help="alternative PSK output path")
    pair_trust = pair_sub.add_parser("trust", help="pin a peer's community public key")
    pair_trust.add_argument("--alias", required=True, help="local alias for the peer device")
    pair_trust.add_argument("--public-key", required=True, help="peer community Ed25519 public key (64 hex)")
    pair_trust.add_argument("--note", default="", help="free-form local note")
    pair_list = pair_sub.add_parser("list", help="list pairing material and trusted peers")
    for p in (pair, pair_gen, pair_trust, pair_list):
        p.add_argument("--data-dir", default=None)
        p.add_argument("--server", default=None)

    host = sub.add_parser("host", help="run the B-side collaboration task service")
    host.add_argument("--adapter", default="file", choices=["zcode", "file", "echo"],
                      help="worker adapter (zcode=headless CLI session, file=attended, echo=test)")
    host.add_argument("--zcode-mode", default="yolo", help="zcode headless --mode (local permission policy)")
    host.add_argument("--data-dir", default=None)
    host.add_argument("--server", default=None)

    submit = sub.add_parser("submit", help="submit a task to a paired peer host")
    submit.add_argument("--to", required=True, help="peer alias")
    submit.add_argument("--goal", required=True, help="task goal text, or @file to read from a file")
    submit.add_argument("--class", dest="execution_class", default="assist",
                        choices=["project_write", "compute", "assist"])
    submit.add_argument("--input", action="append", default=[], help="input file/dir to transfer (repeatable)")
    submit.add_argument("--wait", action="store_true", help="poll until a terminal state")
    submit.add_argument("--timeout", type=float, default=0, help="wait timeout seconds (0=unlimited)")
    submit.add_argument("--pull-out", default=None, help="directory to pull result files into (implies ack)")
    submit.add_argument("--task-id", default=None, help="explicit task id (default: generated)")
    submit.add_argument("--data-dir", default=None)
    submit.add_argument("--server", default=None)

    status = sub.add_parser("status", help="query task status from a peer host")
    status.add_argument("--to", required=True)
    status.add_argument("--task", required=True)
    status.add_argument("--wait", action="store_true")
    status.add_argument("--timeout", type=float, default=0)
    status.add_argument("--pull-out", default=None)
    status.add_argument("--data-dir", default=None)
    status.add_argument("--server", default=None)

    chat = sub.add_parser("chat", help="send a guidance note to the worker and fetch replies")
    chat.add_argument("--to", required=True)
    chat.add_argument("--task", required=True)
    chat.add_argument("--message", required=True)
    chat.add_argument("--data-dir", default=None)
    chat.add_argument("--server", default=None)

    answer = sub.add_parser("answer", help="answer a pending input_request from the peer host")
    answer.add_argument("--to", required=True)
    answer.add_argument("--task", required=True)
    answer.add_argument("--input-id", required=True)
    answer.add_argument("--text", required=True)
    answer.add_argument("--data-dir", default=None)
    answer.add_argument("--server", default=None)

    cancel = sub.add_parser("cancel", help="request task cancellation")
    cancel.add_argument("--to", required=True)
    cancel.add_argument("--task", required=True)
    cancel.add_argument("--reason", default="")
    cancel.add_argument("--data-dir", default=None)
    cancel.add_argument("--server", default=None)

    pull = sub.add_parser("pull", help="pull result files for a terminal task")
    pull.add_argument("--to", required=True)
    pull.add_argument("--task", required=True)
    pull.add_argument("--out-dir", required=True)
    pull.add_argument("--data-dir", default=None)
    pull.add_argument("--server", default=None)

    tasks = sub.add_parser("tasks", help="list locally cached collab tasks (this device's submissions)")
    tasks.add_argument("--data-dir", default=None)
    tasks.add_argument("--server", default=None)

    worker = sub.add_parser("worker", help="worker-side interface (see `worker --help`)")
    worker_sub = worker.add_subparsers(dest="worker_cmd", required=True)
    w_list = worker_sub.add_parser("list", help="list tasks on this host")
    w_list.add_argument("--state", action="append", default=[])
    for name, help_text in (
        ("show", "show the task package"),
        ("inbox", "read coordinator guidance"),
        ("note", "send a note to the coordinator"),
        ("ask", "ask the coordinator a question"),
        ("publish", "publish a workspace file/dir as a result"),
        ("finish", "finish the task successfully"),
        ("fail", "fail the task"),
    ):
        w = worker_sub.add_parser(name, help=help_text)
        w.add_argument("--task", required=True)
        if name in ("note",):
            w.add_argument("--message", required=True)
        if name in ("ask",):
            w.add_argument("--question", required=True)
        if name in ("publish",):
            w.add_argument("--path", required=True, help="workspace-relative path to publish")
        if name in ("finish", "fail"):
            w.add_argument("--summary", default="")
        if name in ("show", "inbox", "note", "ask", "publish", "finish", "fail"):
            w.add_argument("--src", default=None, help="source device public-key prefix (disambiguation)")
        if name == "inbox":
            w.add_argument("--peek", action="store_true", help="do not mark notes as read")
    for p in (worker, w_list, *worker_sub.choices.values()):
        if "--data-dir" not in p._option_string_actions:
            p.add_argument("--data-dir", default=None)
        if "--server" not in p._option_string_actions:
            p.add_argument("--server", default=None)


# -- pair commands ---------------------------------------------------------------


def _cmd_pair(args) -> int:
    data_dir = args.data_dir
    if args.pair_cmd == "gen":
        path = Path(args.out) if args.out else psk_path(data_dir)
        if args.force and path.exists():
            path.unlink()
        psk = generate_psk(out_path=path)
        print(
            json.dumps(
                {
                    "status": "generated",
                    "path": str(path),
                    "key_id": psk.key_id,
                    "key_epoch": psk.key_epoch,
                    "hint": "copy this file to the peer device and run `collab pair trust` there; never paste the secret into chats or repos",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.pair_cmd == "trust":
        import re

        pub = args.public_key.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", pub):
            print("public key must be 64 lowercase hex chars", file=sys.stderr)
            return 2
        psk = load_psk(data_dir)
        registry = PeerRegistry(data_dir)
        existing = registry.by_alias(args.alias)
        if existing is not None and existing.public_key != pub:
            print(f"alias {args.alias!r} already trusted a different key", file=sys.stderr)
            return 2
        registry.trust(
            PeerEntry(
                alias=args.alias,
                public_key=pub,
                key_id=psk.key_id,
                key_epoch=psk.key_epoch,
                note=args.note,
            )
        )
        print(json.dumps({"status": "trusted", "alias": args.alias, "public_key": pub[:16] + "..."}, ensure_ascii=False))
        return 0
    if args.pair_cmd == "list":
        try:
            psk = load_psk(data_dir)
            psk_info = {"key_id": psk.key_id, "key_epoch": psk.key_epoch}
        except (FileNotFoundError, ValueError):
            psk_info = None
        registry = PeerRegistry(data_dir)
        peers = [
            {"alias": p.alias, "public_key": p.public_key, "disabled": p.disabled, "note": p.note}
            for p in registry.all()
        ]
        print(json.dumps({"psk": psk_info, "peers": peers}, ensure_ascii=False, indent=2))
        return 0
    return 2


# -- guest commands ----------------------------------------------------------------


def _read_goal(spec: str) -> str:
    if spec.startswith("@"):
        return Path(spec[1:]).read_text(encoding="utf-8")
    return spec


async def _cmd_submit(args) -> int:
    client = guest_app.GuestClient(args, args.to)
    goal = _read_goal(args.goal)
    pairs = guest_app.collect_inputs(args.input)
    entries = [entry for _, entry in pairs]
    task_id = args.task_id or f"task-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    digest = guest_app.request_digest(goal, args.execution_class, entries)
    session = await client.connect()
    try:
        submit_msg = {
            "action": "task_submit",
            "task_id": task_id,
            "request_digest": digest,
            "task_goal": goal,
            "execution_class": args.execution_class,
            "expires_at_ms": int(time.time() * 1000) + guest_app.DEFAULT_DEADLINE_MS,
        }
        if pairs:
            submit_msg["input_manifest"] = {
                "artifacts": [
                    {"artifact_id": f"input-{i}", "files": [entry for _, entry in page]}
                    for i, page in enumerate(guest_app.paginate(pairs))
                    if page
                ]
            }
        response = await session.rpc(submit_msg)
        if not response.get("accepted"):
            print(
                json.dumps({"task_id": task_id, "accepted": False, "reason": response.get("reason_code")}, ensure_ascii=False)
            )
            return 1
        duplicate = response.get("reason_code") == "duplicate"
        if pairs and not (duplicate and str(response.get("state", "")) in guest_app.TERMINAL):
            await guest_app.upload_artifacts(session, task_id, guest_app.paginate(pairs))
    finally:
        await session.close()
    client.cache_task(task_id, goal_head=goal[:160], execution_class=args.execution_class, last_state=response.get("state"))
    print(
        json.dumps(
            {"task_id": task_id, "state": response.get("state"), "reason": response.get("reason_code")},
            ensure_ascii=False,
        )
    )
    if args.wait:
        pull_out = Path(args.pull_out) if args.pull_out else None
        await guest_app.wait_terminal(client.connect, client, task_id, args.timeout, pull_out)
    return 0


async def _cmd_status(args) -> int:
    client = guest_app.GuestClient(args, args.to)
    if args.wait:
        pull_out = Path(args.pull_out) if args.pull_out else None
        await guest_app.wait_terminal(client.connect, client, args.task, args.timeout, pull_out)
        return 0
    session = await client.connect()
    try:
        response = await session.rpc({"action": "status_query", "task_id": args.task, "after_event_id": 0})
    finally:
        await session.close()
    client.cache_task(args.task, last_state=response.get("state"))
    execution = response.get("execution") or {}
    print(
        json.dumps(
            {
                "task_id": args.task,
                "state": response.get("state"),
                "revision": response.get("revision"),
                "adapter": execution.get("adapter"),
                "summary": (execution.get("summary") or "")[:500],
                "events": [f"{e.get('kind')}: {e.get('summary')}" for e in response.get("events", [])],
                "notes": [n.get("text") for n in response.get("notes", [])],
                "input_request": response.get("input_request"),
                "result_files": [f.get("path") for f in (response.get("result_manifest") or {}).get("files", [])],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    for note in response.get("notes", []):
        print(f"[worker] {note.get('text')}")
    return 0


async def _cmd_chat(args) -> int:
    client = guest_app.GuestClient(args, args.to)
    note_id = f"cn-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    session = await client.connect()
    try:
        await session.rpc(
            {"action": "agent_note", "task_id": args.task, "note_id": note_id, "note_text": args.message}
        )
        response = await session.rpc({"action": "status_query", "task_id": args.task, "after_event_id": 0})
    finally:
        await session.close()
    for note in response.get("notes", []):
        print(f"[worker] {note.get('text')}")
    state = response.get("state")
    print(f"(task state: {state})")
    return 0


async def _cmd_answer(args) -> int:
    client = guest_app.GuestClient(args, args.to)
    session = await client.connect()
    try:
        response = await session.rpc(
            {
                "action": "input_submit",
                "task_id": args.task,
                "input_id": args.input_id,
                "answer": args.text,
            }
        )
    finally:
        await session.close()
    print(json.dumps({"task_id": args.task, "input_id": args.input_id, "acked": True}, ensure_ascii=False))
    return 0


async def _cmd_cancel(args) -> int:
    client = guest_app.GuestClient(args, args.to)
    session = await client.connect()
    try:
        msg = {"action": "cancel_request", "task_id": args.task}
        if args.reason:
            msg["reason"] = args.reason
        response = await session.rpc(msg)
    finally:
        await session.close()
    print(json.dumps({"task_id": args.task, "state": response.get("state")}, ensure_ascii=False))
    return 0


async def _cmd_pull(args) -> int:
    client = guest_app.GuestClient(args, args.to)
    out_dir = Path(args.out_dir)
    session = await client.connect()
    try:
        response = await session.rpc({"action": "status_query", "task_id": args.task, "after_event_id": 0})
        manifest = response.get("result_manifest") or {}
        if not manifest.get("files"):
            print(f"no result manifest (state={response.get('state')})", file=sys.stderr)
            return 1
        written = await guest_app.pull_results(session, args.task, manifest, out_dir)
        receipt = await session.rpc({"action": "result_ack", "task_id": args.task})
    finally:
        await session.close()
    for path in written:
        print(f"[pulled] {out_dir / path}")
    if receipt.get("summary"):
        print(f"[receipt] {str(receipt['summary'])[:2000]}")
    return 0


def _cmd_tasks(args) -> int:
    cache = Path(guest_app.resolve_data_dir(args.data_dir)) / "collab" / "client-tasks.json"
    if not cache.exists():
        print("[]")
        return 0
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    rows = sorted(data.values(), key=lambda t: -int(t.get("updated_at", 0)))
    print(
        json.dumps(
            [
                {
                    "task_id": t.get("task_id"),
                    "host": t.get("host_alias"),
                    "state": t.get("last_state"),
                    "goal": t.get("goal_head"),
                }
                for t in rows
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def run(args) -> int:
    cmd = args.collab_cmd
    if cmd == "pair":
        return _cmd_pair(args)
    if cmd == "worker":
        from aigenora.collab import worker_cli

        return worker_cli.run(args)
    if cmd == "tasks":
        return _cmd_tasks(args)
    if cmd == "host":
        from aigenora.collab import host_app

        return host_app.run(args)
    try:
        if cmd == "submit":
            return asyncio.run(_cmd_submit(args))
        if cmd == "status":
            return asyncio.run(_cmd_status(args))
        if cmd == "chat":
            return asyncio.run(_cmd_chat(args))
        if cmd == "answer":
            return asyncio.run(_cmd_answer(args))
        if cmd == "cancel":
            return asyncio.run(_cmd_cancel(args))
        if cmd == "pull":
            return asyncio.run(_cmd_pull(args))
    except AdmissionError as exc:
        print(json.dumps({"error": "admission_failed", "code": exc.code}, ensure_ascii=False), file=sys.stderr)
        return 3
    except guest_app.GuestError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail[:300]}, ensure_ascii=False), file=sys.stderr)
        return 3
    return 2
