"""Host-side task service for trusted-agent-task-v1.

Implements the §6/§7 contract: durable receive-then-ACK, idempotent
resubmission keyed by ``(source_device, task_id, request_digest)``, block ACK
semantics with file-level digest verification before any input is unpacked,
and the §7.1 state machine. Message handlers run inside the host connection
loop; the dispatcher thread owns adapter invocations and never blocks the
wire loop.
"""
from __future__ import annotations

import json
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from aigenora.collab import transfer
from aigenora.collab.adapter import (
    OBSERVE_EXITED_FAIL,
    OBSERVE_EXITED_OK,
    OBSERVE_RUNNING,
    RECONCILE_NEVER_STARTED,
    BaseAdapter,
    TaskPackage,
    build_adapter,
)
from aigenora.collab.errors import (
    ERROR_ARTIFACT_INCOMPLETE,
    ERROR_ARTIFACT_UNKNOWN,
    ERROR_IDEMPOTENCY_CONFLICT,
    ERROR_INTERNAL,
    ERROR_EXPIRED_MESSAGE,
    ERROR_PATH_UNSAFE,
    ERROR_STATE_CONFLICT,
    ERROR_STORAGE_FULL,
    ERROR_UNKNOWN_TASK,
    CollabError,
)
from aigenora.collab.store import (
    ACTIVE_STATES,
    TaskStore,
    TERMINAL_STATES,
    task_root,
)
from aigenora.engine.config import data_dir as resolve_data_dir

# §4.2 clock tolerance applied to peer message acceptance deadlines.
CLOCK_SKEW_TOLERANCE_MS = 60_000
DISPATCH_POLL_SECONDS = 0.5
MIN_FREE_BYTES_FOR_ARTIFACT = 64 * 1024 * 1024  # protect the host's own files

TASK_STATES_ENUM = [
    "received", "waiting_inputs", "queued", "dispatching", "running",
    "waiting_local_operator", "cancel_requested", "execution_unknown",
    "export_check", "completed", "failed", "rejected", "cancelled", "expired",
]


class TaskService:
    def __init__(
        self,
        data_dir_value: str | None,
        adapter: BaseAdapter | None = None,
        adapter_name: str = "file",
        adapter_kwargs: dict[str, Any] | None = None,
        store: TaskStore | None = None,
        autostart_dispatcher: bool = True,
    ) -> None:
        self.data_dir_value = data_dir_value
        self.store = store or TaskStore(data_dir_value)
        if adapter is None:
            adapter = build_adapter(adapter_name, **(adapter_kwargs or {}))
        self.adapter = adapter
        self._dispatch_stop = threading.Event()
        self._dispatch_thread: threading.Thread | None = None
        self._watchers: dict[str, threading.Thread] = {}
        self._watched_tasks: set[tuple[str, str]] = set()
        self._watch_lock = threading.Lock()
        self._diagnostics: list[str] = []
        if autostart_dispatcher:
            self.start_dispatcher()

    # -- construction helpers -------------------------------------------------

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> "TaskService":
        data_dir_value = str(options.get("collab_data_dir") or "") or None
        adapter_name = str(options.get("collab_adapter") or "file")
        return cls(data_dir_value, adapter_name=adapter_name)

    def diag(self, message: str) -> None:
        """Local operator diagnostics only; never sent over the wire."""
        entry = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {message[:800]}"
        self._diagnostics.append(entry)
        if len(self._diagnostics) > 500:
            self._diagnostics = self._diagnostics[-500:]

    # -- dispatcher -------------------------------------------------------------

    def start_dispatcher(self) -> None:
        self.recover_interrupted_tasks()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, name="collab-dispatch", daemon=True
        )
        self._dispatch_thread.start()

    def stop_dispatcher(self) -> None:
        self._dispatch_stop.set()

    def recover_interrupted_tasks(self) -> None:
        """Startup reconcile (§7.2): never blindly re-dispatch a possibly-started task."""
        for task in self.store.list_tasks({"dispatching", "running", "export_check"}):
            source, task_id = task["source_device"], task["task_id"]
            invocation = self.store.latest_invocation(source, task_id)
            if invocation is None:
                # No invocation row means the dispatcher never claimed the task.
                # From dispatching the machine allows a proven-never-started
                # requeue; a "running" row without an invocation is a ledger
                # inconsistency and parks in execution_unknown instead.
                target = "queued" if task["state"] == "dispatching" else "execution_unknown"
                try:
                    self.store.transition(source, task_id, target,
                                          event_kind="requeued" if target == "queued" else "execution_unknown",
                                          event_summary=f"restart: no invocation recorded ({task['state']})")
                except ValueError:
                    pass
                continue
            verdict = self.adapter.reconcile(invocation["id"], invocation["handle"] or "")
            if verdict == RECONCILE_NEVER_STARTED:
                try:
                    self.store.transition(source, task_id, "queued",
                                          event_kind="requeued", event_summary="restart: adapter proved never started")
                except ValueError:
                    pass
            else:
                try:
                    self.store.transition(source, task_id, "execution_unknown",
                                          event_kind="execution_unknown",
                                          event_summary="restart with unfinished invocation; awaiting observation or operator")
                except ValueError:
                    pass
                self.diag(f"task {task_id} marked execution_unknown after restart")

    def _dispatch_loop(self) -> None:
        while not self._dispatch_stop.is_set():
            try:
                for task in self.store.list_tasks({"queued"}):
                    if self._dispatch_stop.is_set():
                        break
                    self._try_dispatch(task)
            except Exception as exc:  # dispatcher must survive handler bugs
                self.diag(f"dispatch loop error: {type(exc).__name__}")
            self._dispatch_stop.wait(DISPATCH_POLL_SECONDS)

    def _try_dispatch(self, task: dict[str, Any]) -> None:
        source, task_id = task["source_device"], task["task_id"]
        with self._watch_lock:
            if (source, task_id) in self._watched_tasks:
                return
            self._watched_tasks.add((source, task_id))
        try:
            package = self.build_package(source, task_id)
            probe = self.adapter.probe()
            if not probe.get("ready"):
                self.store.transition(
                    source, task_id, "waiting_local_operator",
                    event_kind="adapter_not_ready",
                    event_summary=f"adapter not ready: {probe.get('detail', '')[:200]}",
                )
                return
            invocation_id = f"inv-{secrets.token_hex(8)}"
            # Record the invocation BEFORE submit so the crash window is
            # reconciled rather than lost (§7.2 dispatch contract).
            self.store.insert_invocation(
                invocation_id=invocation_id, source_device=source, task_id=task_id,
                adapter=self.adapter.name,
            )
            self.store.transition(source, task_id, "dispatching",
                                  event_kind="dispatching", event_summary=f"invocation {invocation_id}")
            try:
                handle = self.adapter.submit(invocation_id, package)
            except Exception as exc:
                # Adapter raised before returning: execution may or may not
                # have started. Only a proven-not-started failure may requeue.
                self.diag(f"adapter submit raised: {type(exc).__name__}")
                self.store.set_invocation_status(invocation_id, "submit_error", finished=True)
                self.store.transition(
                    source, task_id, "execution_unknown",
                    event_kind="execution_unknown",
                    event_summary="adapter submit raised; cannot prove execution never started",
                )
                return
            self.store.set_invocation_handle(invocation_id, handle)
            package.protocol_info["invocation_id"] = invocation_id
            self.store.transition(source, task_id, "running",
                                  event_kind="running", event_summary=f"adapter {self.adapter.name} accepted the task")
            if self.adapter.attended:
                self.store.transition(source, task_id, "waiting_local_operator",
                                      event_kind="waiting_local_operator",
                                      event_summary="attended adapter: local operator must drive the task")
                return
            watcher = threading.Thread(
                target=self._watch_invocation,
                args=(source, task_id, invocation_id, handle, package),
                name=f"collab-watch-{task_id}", daemon=True,
            )
            with self._watch_lock:
                self._watchers[invocation_id] = watcher
            watcher.start()
        finally:
            with self._watch_lock:
                self._watched_tasks.discard((source, task_id))

    def build_package(self, source_device: str, task_id: str) -> TaskPackage:
        task = self.store.get_task(source_device, task_id)
        if task is None:
            raise KeyError("task not found")
        root = task_root(self.data_dir_value, source_device, task_id)
        root.mkdir(parents=True, exist_ok=True)
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        artifacts = []
        staging: dict[str, Path] = {}
        for art in self.store.list_artifacts(source_device, task_id):
            if art["artifact_kind"] != "input":
                continue
            staging[art["artifact_id"]] = root / "in" / art["artifact_id"]
            artifacts.append({"artifact_id": art["artifact_id"], "files": art["files"]})
        return TaskPackage(
            source_device=source_device,
            task_id=task_id,
            goal=task["task_goal"],
            execution_class=task["execution_class"],
            workspace=root / "workspace",
            task_root=root,
            data_dir_value=self.data_dir_value,
            input_artifacts=artifacts,
            staging_roots=staging,
        )

    def _watch_invocation(self, source: str, task_id: str, invocation_id: str, handle: str, package: TaskPackage) -> None:
        """Watch one running invocation until exit, resume-on-guidance, or cancel.

        Resume only fires while UNREAD coordinator notes exist and marks them
        delivered before respawning, so the loop terminates by construction
        rather than by a numeric quota.
        """
        resumed = 0
        while not self._dispatch_stop.is_set():
            status = self.adapter.observe(handle, package)
            if status == OBSERVE_RUNNING:
                time.sleep(1.0)
                continue
            if status == OBSERVE_EXITED_OK:
                task = self.store.get_task(source, task_id)
                if task and task["state"] in TERMINAL_STATES:
                    return  # worker finished explicitly through the CLI
                unread = self.store.pending_notes(source, task_id, "coordinator")
                if unread:
                    guidance = " | ".join(n["text"][:400] for n in unread[:5])
                    try:
                        new_handle = self.adapter.resume(handle, package, guidance, invocation_id)
                    except Exception:
                        new_handle = None
                    if new_handle:
                        resumed += 1
                        handle = new_handle
                        self.store.set_invocation_handle(invocation_id, handle)
                        self.store.mark_notes_delivered(source, task_id, "coordinator", [n["note_id"] for n in unread])
                        self.store.append_event(source, task_id, "resumed", "worker resumed with new coordinator guidance")
                        continue
                self.store.set_invocation_status(invocation_id, "exited_ok", finished=True)
                self._finalize_from_result_file(source, task_id, invocation_id)
                return
            if status == OBSERVE_EXITED_FAIL:
                task = self.store.get_task(source, task_id)
                if task and task["state"] in TERMINAL_STATES:
                    return
                tail = self.adapter.output_tail(handle, 1200)
                self.diag(f"invocation {invocation_id} exited nonzero; tail={tail[:400]}")
                self.store.set_invocation_status(invocation_id, "exited_fail", finished=True)
                try:
                    self.store.transition(
                        source, task_id, "failed",
                        event_kind="failed", event_summary="worker process exited with an error",
                        extra_updates={"result_state": "failed", "result_summary": "worker process exited with an error"},
                    )
                except ValueError:
                    pass
                return
            # OBSERVE_UNKNOWN (e.g. after host restart): park, never re-dispatch blindly.
            task = self.store.get_task(source, task_id)
            if task and task["state"] not in TERMINAL_STATES:
                try:
                    self.store.transition(source, task_id, "execution_unknown",
                                          event_kind="execution_unknown",
                                          event_summary="invocation observation unknown; awaiting operator or worker CLI")
                except ValueError:
                    pass
            return

    def _finalize_from_result_file(self, source: str, task_id: str, invocation_id: str) -> None:
        """Worker exited ok without explicit finish: adopt out/ + result.json if present."""
        root = task_root(self.data_dir_value, source, task_id)
        result_file = root / "result.json"
        out_dir = root / "out"
        has_results = out_dir.exists() and any(out_dir.rglob("*"))
        if result_file.exists():
            try:
                payload = json.loads(result_file.read_text(encoding="utf-8"))
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("state") in ("completed", "failed"):
                self._apply_worker_outcome(source, task_id, payload.get("state"),
                                           str(payload.get("summary", "")))
                return
        if has_results:
            self._apply_worker_outcome(source, task_id, "completed", "worker exited; published results adopted")
        else:
            self.store.set_invocation_status(invocation_id, "exited_ok", finished=True)
            try:
                self.store.transition(
                    source, task_id, "failed",
                    event_kind="failed", event_summary="worker exited without finishing or publishing results",
                    extra_updates={"result_state": "failed", "result_summary": "worker exited without a result"},
                )
            except ValueError:
                pass

    def _apply_worker_outcome(self, source: str, task_id: str, state: str, summary: str) -> None:
        manifest = self.build_result_manifest(source, task_id)
        try:
            if state == "completed":
                self.store.transition(
                    source, task_id, "export_check", event_kind="export_check", event_summary="checking exported results",
                )
                self.store.transition(
                    source, task_id, "completed",
                    event_kind="completed", event_summary=summary[:400] or "task completed",
                    extra_updates={"result_state": "completed", "result_summary": summary[:60000], "result_manifest": json.dumps(manifest)},
                )
            else:
                self.store.transition(
                    source, task_id, "failed",
                    event_kind="failed", event_summary=summary[:400] or "task failed",
                    extra_updates={"result_state": "failed", "result_summary": summary[:60000], "result_manifest": json.dumps(manifest)},
                )
        except ValueError as exc:
            self.diag(f"apply outcome rejected by state machine: {exc}")

    def build_result_manifest(self, source: str, task_id: str) -> dict[str, Any]:
        out_dir = task_root(self.data_dir_value, source, task_id) / "out"
        files: list[dict[str, Any]] = []
        if out_dir.exists():
            try:
                files = transfer.collect_tree(out_dir)
            except (OSError, ValueError):
                files = []
        return {"files": files, "artifact_id": "result"}

    # -- wire handlers ------------------------------------------------------------

    def handle(self, msg: dict[str, Any], *, source_device: str) -> dict[str, Any]:
        action = str(msg.get("action", ""))
        handler = getattr(self, f"_on_{action}", None)
        if handler is None:
            raise CollabError(ERROR_INTERNAL, f"unhandled action {action}")
        return handler(msg, source_device)

    def _require_task(self, msg: dict[str, Any], source_device: str) -> dict[str, Any]:
        task_id = str(msg.get("task_id", ""))
        task = self.store.get_task(source_device, task_id)
        if task is None:
            raise CollabError(ERROR_UNKNOWN_TASK, f"no task {task_id} for source")
        return task

    def _on_task_submit(self, msg: dict[str, Any], source_device: str) -> dict[str, Any]:
        task_id = str(msg.get("task_id", ""))
        digest = str(msg.get("request_digest", ""))
        goal = str(msg.get("task_goal", ""))
        execution_class = str(msg.get("execution_class", "assist"))
        expires_at_ms = msg.get("expires_at_ms")
        manifest = msg.get("input_manifest")

        # Message acceptance window (§6.2 expiry semantics class 1).
        if isinstance(expires_at_ms, int) and not isinstance(expires_at_ms, bool):
            now = int(time.time() * 1000)
            if now > expires_at_ms + CLOCK_SKEW_TOLERANCE_MS:
                raise CollabError(ERROR_EXPIRED_MESSAGE, f"deadline exceeded by {now - expires_at_ms}ms")

        existing = self.store.get_task(source_device, task_id)
        if existing is not None:
            if existing["request_digest"] != digest:
                raise CollabError(ERROR_IDEMPOTENCY_CONFLICT, "same task id with different digest")
            state = existing["state"]
            accepted = state not in ("rejected",)
            return {
                "action": "task_received",
                "task_id": task_id,
                "accepted": accepted,
                "state": state,
                "reason_code": "duplicate",
                "revision": existing["revision"],
            }

        expected_files = 0
        if isinstance(manifest, dict):
            for art in manifest.get("artifacts", []):
                if isinstance(art, dict):
                    expected_files += len(art.get("files", []))
        if expected_files:
            free = shutil.disk_usage(resolve_data_dir(self.data_dir_value)).free
            if free < MIN_FREE_BYTES_FOR_ARTIFACT:
                raise CollabError(ERROR_STORAGE_FULL, f"free={free}")

        task = self.store.insert_task(
            source_device=source_device,
            task_id=task_id,
            request_digest=digest,
            task_goal=goal,
            execution_class=execution_class,
            expires_at_ms=expires_at_ms if isinstance(expires_at_ms, int) else None,
        )
        if isinstance(manifest, dict) and manifest:
            self.store.kv_set(f"input-manifest:{source_device}:{task_id}", manifest)
        if expected_files:
            self.store.transition(source_device, task_id, "waiting_inputs",
                                  event_kind="waiting_inputs",
                                  event_summary=f"waiting for {expected_files} input files")
            state = "waiting_inputs"
        else:
            self.store.transition(source_device, task_id, "queued",
                                  event_kind="queued", event_summary="no inputs required; queued")
            state = "queued"
        return {
            "action": "task_received",
            "task_id": task_id,
            "accepted": True,
            "state": state,
            "reason_code": "ok",
            "revision": self.store.get_task(source_device, task_id)["revision"],
        }

    def _on_status_query(self, msg: dict[str, Any], source_device: str) -> dict[str, Any]:
        task = self._require_task(msg, source_device)
        after = msg.get("after_event_id", 0)
        after_id = after if isinstance(after, int) and not isinstance(after, bool) else 0
        events = self.store.events_after(source_device, task["task_id"], after_id)
        pending = self.store.pending_notes(source_device, task["task_id"], "worker")
        notes = [
            {"note_id": n["note_id"], "text": n["text"], "at": n["created_at"]}
            for n in pending
        ]
        self.store.mark_notes_delivered(source_device, task["task_id"], "worker", [n["note_id"] for n in pending])
        response: dict[str, Any] = {
            "action": "task_status",
            "task_id": task["task_id"],
            "state": task["state"],
            "revision": task["revision"],
            "events": [
                {"event_id": e["event_id"], "kind": e["kind"], "summary": e["summary"], "at": e["at"]}
                for e in events
            ],
            "notes": notes,
        }
        invocation = self.store.latest_invocation(source_device, task["task_id"])
        if invocation is not None:
            response["execution"] = {
                "invocation_id": invocation["id"],
                "adapter": invocation["adapter"],
                "status": invocation["status"],
            }
            if task.get("result_summary"):
                response["execution"]["summary"] = task["result_summary"][:8000]
        if task.get("input_request"):
            try:
                response["input_request"] = json.loads(task["input_request"])
            except ValueError:
                pass
        if task["state"] in TERMINAL_STATES and task.get("result_manifest"):
            try:
                response["result_manifest"] = json.loads(task["result_manifest"])
            except ValueError:
                pass
        return response

    def _on_agent_note(self, msg: dict[str, Any], source_device: str) -> dict[str, Any]:
        task = self._require_task(msg, source_device)
        note_id = str(msg.get("note_id", ""))
        text = str(msg.get("note_text", ""))
        inserted = self.store.add_note(source_device, task["task_id"], "coordinator", note_id, text)
        if inserted:
            self.store.append_event(source_device, task["task_id"], "coordinator_note", "coordinator sent guidance")
        return {"action": "note_ack", "task_id": task["task_id"], "note_id": note_id, "duplicate": not inserted}

    def _on_artifact_offer(self, msg: dict[str, Any], source_device: str) -> dict[str, Any]:
        task = self._require_task(msg, source_device)
        artifact_id = str(msg.get("artifact_id", ""))
        kind = str(msg.get("artifact_kind", "input"))
        files = msg.get("files")
        if not isinstance(files, list) or not files:
            raise CollabError(ERROR_INTERNAL, "artifact files must be a non-empty array")
        if len(files) > transfer.MAX_MANIFEST_FILES:
            raise CollabError(ERROR_INTERNAL, "artifact manifest page too large")
        try:
            normalized = []
            seen: set[str] = set()
            total = 0
            for entry in files:
                if not isinstance(entry, dict):
                    raise ValueError("entry must be an object")
                path = transfer.safe_rel_path(str(entry.get("path", "")))
                if path in seen:
                    raise ValueError("duplicate path in manifest")
                seen.add(path)
                size = entry.get("size")
                sha = entry.get("sha256")
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise ValueError("bad size")
                if not isinstance(sha, str):
                    raise ValueError("bad sha256")
                total += size
                normalized.append({"path": path, "size": size, "sha256": sha})
        except ValueError as exc:
            raise CollabError(ERROR_PATH_UNSAFE, str(exc))
        free = shutil.disk_usage(resolve_data_dir(self.data_dir_value)).free
        if free < total + MIN_FREE_BYTES_FOR_ARTIFACT:
            raise CollabError(ERROR_STORAGE_FULL, f"free={free} need={total}")
        existing = self.store.get_artifact(source_device, task["task_id"], artifact_id)
        if existing is not None and existing["files"] == normalized and existing["state"] in ("partial", "complete"):
            # Idempotent re-offer after reconnect: keep the received chunk map
            # so the guest resumes with only the missing chunks.
            got = existing["received"]
            self.store.append_event(source_device, task["task_id"], "artifact_reoffered",
                                    f"artifact {artifact_id} re-offered; keeping received state")
            if existing["state"] == "complete":
                self._maybe_inputs_ready(source_device, task["task_id"])
            return {
                "action": "artifact_ack",
                "task_id": task["task_id"],
                "artifact_id": artifact_id,
                "artifact_state": existing["state"],
                "files": [
                    {
                        "path": f["path"],
                        "received_chunks": sorted(got.get(f["path"], [])),
                        "complete": transfer.chunk_count(int(f["size"])) <= len(got.get(f["path"], [])),
                    }
                    for f in normalized
                ],
            }
        staging_root = task_root(self.data_dir_value, source_device, task["task_id"]) / "in" / artifact_id
        # Size-0 files never receive chunks; materialize them now so the
        # file-level digest verification can pass.
        for entry in normalized:
            if int(entry["size"]) == 0:
                target = staging_root / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(b"")
        self.store.upsert_artifact(
            source_device=source_device, task_id=task["task_id"],
            artifact_id=artifact_id, artifact_kind=kind, files=normalized,
        )
        self.store.append_event(source_device, task["task_id"], "artifact_offered",
                                f"artifact {artifact_id}: {len(normalized)} files")
        return {
            "action": "artifact_ack",
            "task_id": task["task_id"],
            "artifact_id": artifact_id,
            "artifact_state": "partial",
            "files": [{"path": f["path"], "received_chunks": [], "complete": False} for f in normalized],
        }

    def _on_artifact_put(self, msg: dict[str, Any], source_device: str) -> dict[str, Any]:
        task = self._require_task(msg, source_device)
        artifact_id = str(msg.get("artifact_id", ""))
        chunks = msg.get("chunks")
        art = self.store.get_artifact(source_device, task["task_id"], artifact_id)
        if art is None:
            raise CollabError(ERROR_ARTIFACT_UNKNOWN, f"unknown artifact {artifact_id}")
        if not isinstance(chunks, list) or not chunks:
            raise CollabError(ERROR_INTERNAL, "chunks must be a non-empty array")
        if len(chunks) > transfer.MAX_BATCH_CHUNKS:
            raise CollabError(ERROR_INTERNAL, "chunk batch exceeds the batch bound")
        manifest_files = {f["path"]: f for f in art["files"]}
        staging_root = task_root(self.data_dir_value, source_device, task["task_id"]) / "in" / artifact_id
        received = dict(art["received"])
        for chunk in chunks:
            try:
                parsed = transfer.validate_chunk_object(chunk, manifest_files)
            except ValueError as exc:
                raise CollabError(ERROR_PATH_UNSAFE, str(exc))
            transfer.write_chunk(staging_root, parsed["path"], parsed["offset"], parsed["raw"])
            indexes = received.setdefault(parsed["path"], [])
            idx = parsed["offset"] // transfer.CHUNK_SIZE
            if idx not in indexes:
                indexes.append(idx)
        # File-level verification once every chunk of a file has arrived.
        state = "partial"
        files_response = []
        all_complete = True
        for entry in art["files"]:
            path = entry["path"]
            got = sorted(received.get(path, []))
            need = transfer.chunk_count(int(entry["size"]))
            complete = len(got) >= need
            if complete and not transfer.verify_staged_file(staging_root, entry):
                state = "verified_failed"
                complete = False
                self.store.append_event(source_device, task["task_id"], "artifact_corrupt",
                                        f"file {path} failed the file-level digest check")
            if not complete:
                all_complete = False
            files_response.append({"path": path, "received_chunks": got, "complete": complete})
        if all_complete and state != "verified_failed":
            state = "complete"
        self.store.update_artifact(source_device, task["task_id"], artifact_id,
                                   received=received, state=state)
        if state == "complete":
            self.store.append_event(source_device, task["task_id"], "artifact_complete",
                                    f"artifact {artifact_id} fully received and verified")
            self._maybe_inputs_ready(source_device, task["task_id"])
        return {
            "action": "artifact_ack",
            "task_id": task["task_id"],
            "artifact_id": artifact_id,
            "artifact_state": state,
            "files": files_response,
        }

    def _maybe_inputs_ready(self, source_device: str, task_id: str) -> None:
        task = self.store.get_task(source_device, task_id)
        if task is None or task["state"] not in ("received", "waiting_inputs"):
            return
        declared = self.store.kv_get(f"input-manifest:{source_device}:{task_id}")
        arts = self.store.list_artifacts(source_device, task_id)
        inputs = [a for a in arts if a["artifact_kind"] == "input"]
        if not inputs:
            return
        if isinstance(declared, dict):
            declared_ids = {str(a.get("artifact_id")) for a in declared.get("artifacts", []) if isinstance(a, dict)}
            received_ids = {a["artifact_id"] for a in inputs}
            if not declared_ids.issubset(received_ids):
                return
        if any(a["state"] != "complete" for a in inputs):
            return
        # Unpack verified inputs into the workspace.
        root = task_root(self.data_dir_value, source_device, task_id)
        workspace = root / "workspace"
        for art in inputs:
            staging = root / "in" / art["artifact_id"]
            for entry in art["files"]:
                src = staging / entry["path"]
                dest = workspace / entry["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists() and not dest.is_dir():
                    dest.unlink()
                shutil.copyfile(src, dest)
        self.store.transition(source_device, task_id, "queued",
                              event_kind="inputs_ready",
                              event_summary="all input artifacts verified and unpacked")

    def _on_artifact_pull(self, msg: dict[str, Any], source_device: str) -> dict[str, Any]:
        task = self._require_task(msg, source_device)
        artifact_id = str(msg.get("artifact_id", "result"))
        paths = msg.get("paths")
        cursor = msg.get("cursor") if isinstance(msg.get("cursor"), dict) else {}
        if artifact_id == "result":
            if not task.get("result_manifest"):
                raise CollabError(ERROR_ARTIFACT_INCOMPLETE, "no result manifest yet")
            manifest = json.loads(task["result_manifest"])
            root = task_root(self.data_dir_value, source_device, task["task_id"]) / "out"
            art = {"files": manifest.get("files", []), "state": "complete"}
        else:
            art = self.store.get_artifact(source_device, task["task_id"], artifact_id)
            if art is None:
                raise CollabError(ERROR_ARTIFACT_UNKNOWN, f"unknown artifact {artifact_id}")
            if art["state"] != "complete":
                raise CollabError(ERROR_ARTIFACT_INCOMPLETE, "artifact not fully verified")
            root = task_root(self.data_dir_value, source_device, task["task_id"]) / "in" / artifact_id
        if not isinstance(paths, list) or not paths:
            raise CollabError(ERROR_INTERNAL, "pull paths must be a non-empty array")
        files_by_path = {f["path"]: f for f in art["files"]}
        batch: list[dict[str, Any]] = []
        new_cursor = dict(cursor)
        done = True
        for path in paths:
            try:
                safe = transfer.safe_rel_path(str(path))
            except ValueError as exc:
                raise CollabError(ERROR_PATH_UNSAFE, str(exc))
            entry = files_by_path.get(safe)
            if entry is None:
                raise CollabError(ERROR_PATH_UNSAFE, "path not in artifact manifest")
            start = max(0, int(cursor.get(safe, 0)))
            total_chunks = transfer.chunk_count(int(entry["size"]))
            idx = start
            while idx < total_chunks and len(batch) < 4:
                chunk = transfer.encode_chunk(root, entry, idx)
                if chunk is None:
                    break
                batch.append(chunk)
                idx += 1
            new_cursor[safe] = idx
            if idx < total_chunks:
                done = False
        return {
            "action": "artifact_data",
            "task_id": task["task_id"],
            "artifact_id": artifact_id,
            "chunks": batch,
            "cursor": new_cursor,
            "done": done,
        }

    def _on_result_ack(self, msg: dict[str, Any], source_device: str) -> dict[str, Any]:
        task = self._require_task(msg, source_device)
        if task["state"] not in TERMINAL_STATES or not task.get("result_state"):
            raise CollabError(ERROR_STATE_CONFLICT, "task has no final result to acknowledge")
        self.store.set_task_fields(source_device, task["task_id"], {"result_acked": 1})
        self.store.append_event(source_device, task["task_id"], "result_acked", "coordinator acknowledged the final receipt")
        manifest = None
        if task.get("result_manifest"):
            try:
                manifest = json.loads(task["result_manifest"])
            except ValueError:
                manifest = None
        return {
            "action": "receipt",
            "task_id": task["task_id"],
            "result_state": task["result_state"],
            "summary": task.get("result_summary") or "",
            "result_manifest": manifest,
        }

    def _on_cancel_request(self, msg: dict[str, Any], source_device: str) -> dict[str, Any]:
        task = self._require_task(msg, source_device)
        state = task["state"]
        if state in TERMINAL_STATES:
            return {"action": "cancel_ack", "task_id": task["task_id"], "state": state}
        if state in ("received", "waiting_inputs", "queued", "waiting_local_operator"):
            self.store.transition(source_device, task["task_id"], "cancelled",
                                  event_kind="cancelled", event_summary="cancelled before execution")
            return {"action": "cancel_ack", "task_id": task["task_id"], "state": "cancelled"}
        invocation = self.store.latest_invocation(source_device, task["task_id"])
        requested_stop = False
        if invocation is not None and invocation["handle"]:
            requested_stop = self.adapter.cancel(invocation["handle"])
        if state == "execution_unknown":
            self.store.append_event(source_device, task["task_id"], "cancel_recorded", "cancel recorded; execution state unknown")
            return {"action": "cancel_ack", "task_id": task["task_id"], "state": "execution_unknown"}
        self.store.transition(source_device, task["task_id"], "cancel_requested",
                              event_kind="cancel_requested",
                              event_summary="stop requested" + ("" if requested_stop else "; adapter stop not confirmed"))
        return {"action": "cancel_ack", "task_id": task["task_id"], "state": "cancel_requested"}

    def _on_input_submit(self, msg: dict[str, Any], source_device: str) -> dict[str, Any]:
        task = self._require_task(msg, source_device)
        input_id = str(msg.get("input_id", ""))
        answer = str(msg.get("answer", ""))
        if not self.store.answer_input(source_device, task["task_id"], input_id, answer):
            raise CollabError(ERROR_STATE_CONFLICT, f"no pending input {input_id}")
        self.store.append_event(source_device, task["task_id"], "input_answered", f"input {input_id} answered")
        if task["state"] == "waiting_inputs" and not self.store.pending_inputs(source_device, task["task_id"]):
            self.store.transition(source_device, task["task_id"], "queued",
                                  event_kind="queued", event_summary="inputs answered; queued")
        return {"action": "input_ack", "task_id": task["task_id"], "input_id": input_id}

    # -- worker CLI backend ------------------------------------------------------

    def worker_show(self, source_device: str, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(source_device, task_id)
        if task is None:
            raise KeyError("task not found")
        return task

    def worker_note(self, source_device: str, task_id: str, text: str) -> None:
        note_id = f"wn-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        self.store.add_note(source_device, task_id, "worker", note_id, text)
        self.store.append_event(source_device, task_id, "worker_note", "worker sent a message to the coordinator")

    def worker_ask(self, source_device: str, task_id: str, question: str) -> None:
        input_id = f"in-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        self.store.request_input(source_device, task_id, input_id, question)
        self.store.append_event(source_device, task_id, "input_requested", question[:200])

    def worker_publish(self, source_device: str, task_id: str, rel_path: str) -> dict[str, Any]:
        """Copy a workspace file/dir into the task's result output directory."""
        root = task_root(self.data_dir_value, source_device, task_id)
        workspace = root / "workspace"
        target_rel = transfer.safe_rel_path(rel_path)
        src = workspace / target_rel
        if not src.exists():
            raise FileNotFoundError(str(src))
        out = root / "out" / target_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if out.exists():
                shutil.rmtree(out)
            shutil.copytree(src, out)
        else:
            shutil.copyfile(src, out)
        return {"published": target_rel}

    def worker_finish(self, source_device: str, task_id: str, state: str, summary: str) -> None:
        root = task_root(self.data_dir_value, source_device, task_id)
        root.mkdir(parents=True, exist_ok=True)
        (root / "result.json").write_text(
            json.dumps({"state": state, "summary": summary, "at": int(time.time() * 1000)}, ensure_ascii=False),
            encoding="utf-8",
        )
        task = self.store.get_task(source_device, task_id)
        if task is None:
            raise KeyError("task not found")
        if task["state"] in TERMINAL_STATES:
            return
        self._apply_worker_outcome(source_device, task_id, state, summary)
