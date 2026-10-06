"""Durable task store for the collaboration host (SQLite WAL).

One database per device at ``<data_dir>/collab/tasks.db`` shared by the host
daemon, the dispatcher thread, and the local worker CLI (§7.2: durable
receive-then-ACK; WAL + busy timeout for multi-process access). Task rows are
keyed by ``(source_device, task_id)`` — the guest-side task namespace — so
idempotent resubmission after reconnects is exact.

Result files live under the task directory; the DB row is the ledger. Task
tombstones (digest + terminal state) are kept for the lifetime of the device
identity per the design §7.2 ruling — no retention-based re-execution.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from aigenora.engine.config import data_dir as resolve_data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  source_device TEXT NOT NULL,
  task_id TEXT NOT NULL,
  state TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  task_goal TEXT NOT NULL,
  execution_class TEXT NOT NULL,
  expires_at_ms INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  session_id TEXT,
  revision INTEGER NOT NULL DEFAULT 0,
  result_manifest TEXT,
  result_summary TEXT,
  result_state TEXT,
  result_acked INTEGER NOT NULL DEFAULT 0,
  input_request TEXT,
  PRIMARY KEY(source_device, task_id)
);
CREATE TABLE IF NOT EXISTS events(
  source_device TEXT NOT NULL,
  task_id TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  summary TEXT NOT NULL,
  at INTEGER NOT NULL,
  PRIMARY KEY(source_device, task_id, event_id)
);
CREATE TABLE IF NOT EXISTS artifacts(
  source_device TEXT NOT NULL,
  task_id TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  artifact_kind TEXT NOT NULL,
  files TEXT NOT NULL,
  received TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(source_device, task_id, artifact_id)
);
CREATE TABLE IF NOT EXISTS notes(
  source_device TEXT NOT NULL,
  task_id TEXT NOT NULL,
  from_role TEXT NOT NULL,
  note_id TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  delivered INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(source_device, task_id, from_role, note_id)
);
CREATE TABLE IF NOT EXISTS invocations(
  id TEXT PRIMARY KEY,
  source_device TEXT NOT NULL,
  task_id TEXT NOT NULL,
  adapter TEXT NOT NULL,
  handle TEXT,
  status TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  finished_at INTEGER
);
CREATE TABLE IF NOT EXISTS inputs(
  source_device TEXT NOT NULL,
  task_id TEXT NOT NULL,
  input_id TEXT NOT NULL,
  question TEXT NOT NULL,
  answer TEXT,
  created_at INTEGER NOT NULL,
  answered_at INTEGER,
  PRIMARY KEY(source_device, task_id, input_id)
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_notes_undelivered ON notes(source_device, task_id, delivered);
"""

TERMINAL_STATES = {"completed", "failed", "rejected", "cancelled", "expired"}
ACTIVE_STATES = {"received", "waiting_inputs", "queued", "dispatching", "running", "waiting_local_operator", "cancel_requested", "execution_unknown", "export_check"}

# §7.1 state machine edges (from -> allowed targets).
TRANSITIONS: dict[str, set[str]] = {
    "received": {"queued", "waiting_inputs", "cancelled", "rejected"},
    "waiting_inputs": {"queued", "cancelled", "expired"},
    "queued": {"dispatching", "cancelled", "expired"},
    "dispatching": {"running", "failed", "queued"},
    "running": {"waiting_local_operator", "cancel_requested", "export_check", "failed", "execution_unknown"},
    "waiting_local_operator": {"running", "cancelled", "failed", "export_check"},
    "cancel_requested": {"cancelled", "execution_unknown", "completed", "failed"},
    "execution_unknown": {"running", "export_check", "failed", "cancelled", "completed"},
    "export_check": {"completed", "waiting_local_operator", "cancelled", "failed"},
    "completed": set(),
    "failed": set(),
    "rejected": set(),
    "cancelled": set(),
    "expired": set(),
}


def now_ms() -> int:
    return int(time.time() * 1000)


def task_root(data_dir_value: str | None, source_device: str, task_id: str) -> Path:
    safe_source = "".join(c for c in source_device if c.isalnum() or c in "-_")[:64] or "peer"
    safe_task = "".join(c for c in task_id if c.isalnum() or c in "-_")[:128] or "task"
    return resolve_data_dir(data_dir_value) / "collab" / "tasks" / safe_source / safe_task


class TaskStore:
    """Thread- and process-safe wrapper over the collab SQLite ledger."""

    def __init__(self, data_dir_value: str | None = None, db_path: Path | None = None) -> None:
        if db_path is None:
            db_path = resolve_data_dir(data_dir_value) / "collab" / "tasks.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    # -- low-level helpers -------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- tasks -------------------------------------------------------------

    def get_task(self, source_device: str, task_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT * FROM tasks WHERE source_device=? AND task_id=?",
            (source_device, task_id),
        )
        return dict(row) if row else None

    def list_tasks(self, states: set[str] | None = None) -> list[dict[str, Any]]:
        rows = self._query_all("SELECT * FROM tasks ORDER BY created_at")
        tasks = [dict(r) for r in rows]
        if states is not None:
            tasks = [t for t in tasks if t["state"] in states]
        return tasks

    def insert_task(
        self,
        *,
        source_device: str,
        task_id: str,
        request_digest: str,
        task_goal: str,
        execution_class: str,
        expires_at_ms: int | None,
    ) -> dict[str, Any]:
        existing = self.get_task(source_device, task_id)
        if existing is not None:
            return existing
        ts = now_ms()
        self._exec(
            "INSERT INTO tasks(source_device, task_id, state, request_digest, task_goal,"
            " execution_class, expires_at_ms, created_at, updated_at, revision)"
            " VALUES(?,?,?,?,?,?,?,?,?,1)",
            (source_device, task_id, "received", request_digest, task_goal, execution_class, expires_at_ms, ts, ts),
        )
        self.append_event(source_device, task_id, "received", "task durably received")
        return self.get_task(source_device, task_id)  # type: ignore[return-value]

    def transition(
        self,
        source_device: str,
        task_id: str,
        new_state: str,
        *,
        event_kind: str | None = None,
        event_summary: str = "",
        extra_updates: dict[str, Any] | None = None,
        allow_same: bool = True,
    ) -> dict[str, Any]:
        """State-machine-guarded transition; raises ValueError on an illegal edge."""
        task = self.get_task(source_device, task_id)
        if task is None:
            raise KeyError("task not found")
        current = task["state"]
        if current == new_state:
            if allow_same:
                return task
            raise ValueError(f"task already in state {current}")
        if new_state not in TRANSITIONS.get(current, set()):
            raise ValueError(f"illegal transition {current} -> {new_state}")
        sets = ["state=?", "updated_at=?", "revision=revision+1"]
        params: list[Any] = [new_state, now_ms()]
        for key, value in (extra_updates or {}).items():
            sets.append(f"{key}=?")
            params.append(value)
        params.extend([source_device, task_id])
        self._exec(f"UPDATE tasks SET {', '.join(sets)} WHERE source_device=? AND task_id=?", tuple(params))
        if event_kind:
            self.append_event(source_device, task_id, event_kind, event_summary or new_state)
        return self.get_task(source_device, task_id)  # type: ignore[return-value]

    def set_task_fields(self, source_device: str, task_id: str, updates: dict[str, Any]) -> None:
        if not updates:
            return
        sets = [f"{k}=?" for k in updates]
        params = list(updates.values()) + [now_ms(), source_device, task_id]
        self._exec(
            f"UPDATE tasks SET {', '.join(sets)}, updated_at=? WHERE source_device=? AND task_id=?",
            tuple(params),
        )

    # -- events ------------------------------------------------------------

    def append_event(self, source_device: str, task_id: str, kind: str, summary: str) -> int:
        row = self._query_one(
            "SELECT COALESCE(MAX(event_id),0) AS m FROM events WHERE source_device=? AND task_id=?",
            (source_device, task_id),
        )
        next_id = int(row["m"]) + 1 if row else 1
        self._exec(
            "INSERT INTO events(source_device, task_id, event_id, kind, summary, at) VALUES(?,?,?,?,?,?)",
            (source_device, task_id, next_id, kind, summary[:500], now_ms()),
        )
        return next_id

    def events_after(self, source_device: str, task_id: str, after_event_id: int, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._query_all(
            "SELECT * FROM events WHERE source_device=? AND task_id=? AND event_id>? ORDER BY event_id LIMIT ?",
            (source_device, task_id, after_event_id, limit),
        )
        return [dict(r) for r in rows]

    # -- artifacts ----------------------------------------------------------

    def upsert_artifact(
        self,
        *,
        source_device: str,
        task_id: str,
        artifact_id: str,
        artifact_kind: str,
        files: list[dict[str, Any]],
    ) -> dict[str, Any]:
        ts = now_ms()
        self._exec(
            "INSERT INTO artifacts(source_device, task_id, artifact_id, artifact_kind, files,"
            " received, state, created_at, updated_at) VALUES(?,?,?,?,?,?, 'partial', ?, ?)"
            " ON CONFLICT(source_device, task_id, artifact_id) DO UPDATE SET"
            " files=excluded.files, received=excluded.received, state='partial', updated_at=excluded.updated_at",
            (source_device, task_id, artifact_id, artifact_kind, json.dumps(files), "{}", ts, ts),
        )
        return self.get_artifact(source_device, task_id, artifact_id)  # type: ignore[return-value]

    def get_artifact(self, source_device: str, task_id: str, artifact_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT * FROM artifacts WHERE source_device=? AND task_id=? AND artifact_id=?",
            (source_device, task_id, artifact_id),
        )
        if not row:
            return None
        art = dict(row)
        art["files"] = json.loads(art["files"])
        art["received"] = json.loads(art["received"])
        return art

    def update_artifact(self, source_device: str, task_id: str, artifact_id: str, *, files: list | None = None, received: dict | None = None, state: str | None = None) -> None:
        sets = ["updated_at=?"]
        params: list[Any] = [now_ms()]
        if files is not None:
            sets.append("files=?")
            params.append(json.dumps(files))
        if received is not None:
            sets.append("received=?")
            params.append(json.dumps(received))
        if state is not None:
            sets.append("state=?")
            params.append(state)
        params.extend([source_device, task_id, artifact_id])
        self._exec(
            f"UPDATE artifacts SET {', '.join(sets)} WHERE source_device=? AND task_id=? AND artifact_id=?",
            tuple(params),
        )

    def list_artifacts(self, source_device: str, task_id: str) -> list[dict[str, Any]]:
        rows = self._query_all(
            "SELECT * FROM artifacts WHERE source_device=? AND task_id=? ORDER BY created_at, artifact_id",
            (source_device, task_id),
        )
        arts = []
        for row in rows:
            art = dict(row)
            art["files"] = json.loads(art["files"])
            art["received"] = json.loads(art["received"])
            arts.append(art)
        return arts

    # -- notes --------------------------------------------------------------

    def add_note(self, source_device: str, task_id: str, from_role: str, note_id: str, text: str) -> bool:
        """Insert a note; returns False when the note_id already exists (idempotent)."""
        dup = self._query_one(
            "SELECT 1 FROM notes WHERE source_device=? AND task_id=? AND from_role=? AND note_id=?",
            (source_device, task_id, from_role, note_id),
        )
        if dup:
            return False
        self._exec(
            "INSERT INTO notes(source_device, task_id, from_role, note_id, text, created_at, delivered)"
            " VALUES(?,?,?,?,?,?,0)",
            (source_device, task_id, from_role, note_id, text, now_ms()),
        )
        return True

    def pending_notes(self, source_device: str, task_id: str, from_role: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query_all(
            "SELECT * FROM notes WHERE source_device=? AND task_id=? AND from_role=? AND delivered=0"
            " ORDER BY created_at, note_id LIMIT ?",
            (source_device, task_id, from_role, limit),
        )
        return [dict(r) for r in rows]

    def mark_notes_delivered(self, source_device: str, task_id: str, from_role: str, note_ids: list[str]) -> None:
        for nid in note_ids:
            self._exec(
                "UPDATE notes SET delivered=1 WHERE source_device=? AND task_id=? AND from_role=? AND note_id=?",
                (source_device, task_id, from_role, nid),
            )

    def notes_for_worker(self, source_device: str, task_id: str, mark_delivered: bool = True) -> list[dict[str, Any]]:
        rows = self.pending_notes(source_device, task_id, "coordinator")
        if mark_delivered and rows:
            self.mark_notes_delivered(source_device, task_id, "coordinator", [r["note_id"] for r in rows])
        return rows

    # -- invocations ----------------------------------------------------------

    def insert_invocation(self, *, invocation_id: str, source_device: str, task_id: str, adapter: str) -> None:
        self._exec(
            "INSERT INTO invocations(id, source_device, task_id, adapter, handle, status, started_at)"
            " VALUES(?,?,?,?,'','starting',?)",
            (invocation_id, source_device, task_id, adapter, now_ms()),
        )

    def set_invocation_handle(self, invocation_id: str, handle: str, status: str = "submitted") -> None:
        self._exec("UPDATE invocations SET handle=?, status=? WHERE id=?", (handle, status, invocation_id))

    def set_invocation_status(self, invocation_id: str, status: str, finished: bool = False) -> None:
        if finished:
            self._exec("UPDATE invocations SET status=?, finished_at=? WHERE id=?", (status, now_ms(), invocation_id))
        else:
            self._exec("UPDATE invocations SET status=? WHERE id=?", (status, invocation_id))

    def get_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT * FROM invocations WHERE id=?", (invocation_id,))
        return dict(row) if row else None

    def latest_invocation(self, source_device: str, task_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT * FROM invocations WHERE source_device=? AND task_id=? ORDER BY started_at DESC LIMIT 1",
            (source_device, task_id),
        )
        return dict(row) if row else None

    # -- inputs ---------------------------------------------------------------

    def request_input(self, source_device: str, task_id: str, input_id: str, question: str) -> None:
        self._exec(
            "INSERT OR REPLACE INTO inputs(source_device, task_id, input_id, question, created_at)"
            " VALUES(?,?,?,?,?)",
            (source_device, task_id, input_id, question, now_ms()),
        )
        self.set_task_fields(source_device, task_id, {"input_request": json.dumps({"input_id": input_id, "question": question})})

    def answer_input(self, source_device: str, task_id: str, input_id: str, answer: str) -> bool:
        row = self._query_one(
            "SELECT 1 FROM inputs WHERE source_device=? AND task_id=? AND input_id=?",
            (source_device, task_id, input_id),
        )
        if not row:
            return False
        self._exec(
            "UPDATE inputs SET answer=?, answered_at=? WHERE source_device=? AND task_id=? AND input_id=?",
            (answer, now_ms(), source_device, task_id, input_id),
        )
        self.set_task_fields(source_device, task_id, {"input_request": None})
        return True

    def pending_inputs(self, source_device: str, task_id: str) -> list[dict[str, Any]]:
        rows = self._query_all(
            "SELECT * FROM inputs WHERE source_device=? AND task_id=? AND answer IS NULL",
            (source_device, task_id),
        )
        return [dict(r) for r in rows]

    # -- kv --------------------------------------------------------------------

    def kv_set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value)),
        )

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self._query_one("SELECT v FROM kv WHERE k=?", (key,))
        if not row:
            return default
        return json.loads(row["v"])
