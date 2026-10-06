"""``aigenora collab worker`` — the B-side worker agent's local interface.

The spawned worker agent (e.g. a zcode session) uses these commands to read
coordinator guidance, reply, publish results, and finish/fail the task. They
operate directly on the shared SQLite ledger (WAL) — no P2P involved.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from aigenora.collab.service import TaskService
from aigenora.collab.store import TERMINAL_STATES


def _resolve_task(service: TaskService, task_id: str) -> tuple[str, str]:
    matches = [t for t in service.store.list_tasks() if t["task_id"] == task_id]
    if not matches:
        raise SystemExit(f"no task with id {task_id} on this host")
    if len(matches) > 1:
        sources = ", ".join(t["source_device"][:16] for t in matches)
        raise SystemExit(f"task id {task_id} is ambiguous across sources {sources}; use --from")
    task = matches[0]
    return task["source_device"], task["task_id"]


def _resolve_task_from(service: TaskService, task_id: str, from_prefix: str) -> tuple[str, str]:
    matches = [
        t for t in service.store.list_tasks()
        if t["task_id"] == task_id and t["source_device"].startswith(from_prefix.lower())
    ]
    if not matches:
        raise SystemExit(f"no task {task_id} from source starting with {from_prefix}")
    task = matches[0]
    return task["source_device"], task["task_id"]


def run(args) -> int:
    service = TaskService(args.data_dir, autostart_dispatcher=False)
    cmd = args.worker_cmd
    if cmd == "list":
        rows = []
        for task in service.store.list_tasks():
            if args.state and task["state"] not in args.state:
                continue
            rows.append(
                {
                    "task_id": task["task_id"],
                    "source": task["source_device"][:16],
                    "state": task["state"],
                    "class": task["execution_class"],
                    "goal": task["task_goal"][:120],
                }
            )
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if cmd == "show":
        source, task_id = (
            _resolve_task_from(service, args.task, args.src) if getattr(args, "src", None) else _resolve_task(service, args.task)
        )
        task = service.store.get_task(source, task_id)
        arts = service.store.list_artifacts(source, task_id)
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "source": task["source_device"][:16],
                    "state": task["state"],
                    "class": task["execution_class"],
                    "goal": task["task_goal"],
                    "input_artifacts": [
                        {"artifact_id": a["artifact_id"], "state": a["state"], "files": len(a["files"])}
                        for a in arts
                        if a["artifact_kind"] == "input"
                    ],
                    "pending_inputs": service.store.pending_inputs(source, task_id),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if cmd == "inbox":
        source, task_id = (
            _resolve_task_from(service, args.task, args.src) if getattr(args, "src", None) else _resolve_task(service, args.task)
        )
        notes = service.store.notes_for_worker(source, task_id, mark_delivered=not getattr(args, "peek", False))
        for note in notes:
            print(f"[coordinator {note['created_at']}] {note['text']}")
        if not notes:
            print("(no unread coordinator notes)")
        return 0

    source, task_id = (
        _resolve_task_from(service, args.task, args.src) if getattr(args, "src", None) else _resolve_task(service, args.task)
    )
    if cmd == "note":
        service.worker_note(source, task_id, args.message)
        print("note queued for the coordinator")
        return 0
    if cmd == "ask":
        service.worker_ask(source, task_id, args.question)
        print(f"input request queued (question shown to the coordinator on next poll)")
        return 0
    if cmd == "publish":
        result = service.worker_publish(source, task_id, args.path)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if cmd == "finish":
        task = service.store.get_task(source, task_id)
        if task is None:
            raise SystemExit("task not found")
        if task["state"] in TERMINAL_STATES:
            print(f"task already terminal: {task['state']}")
            return 0
        service.worker_finish(source, task_id, "completed", args.summary or "worker finished")
        print("task completed")
        return 0
    if cmd == "fail":
        task = service.store.get_task(source, task_id)
        if task is None:
            raise SystemExit("task not found")
        if task["state"] in TERMINAL_STATES:
            print(f"task already terminal: {task['state']}")
            return 0
        service.worker_finish(source, task_id, "failed", args.summary or "worker failed")
        print("task failed")
        return 0
    print(f"unknown worker command: {cmd}", file=sys.stderr)
    return 2
