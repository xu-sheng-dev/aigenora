from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from aigenora.proto.sdk import EventBus


# Cold start of the business subprocess (iroh node + spec load + invitation publish) has been
# observed at ~18-30s on Windows. 15s caused false "timeout waiting for invite_created" while
# the subprocess was still alive. Override with AIGENORA_DAEMON_STARTUP_TIMEOUT env if needed.
DEFAULT_STARTUP_WAIT_SECONDS = 30.0


def startup_wait_seconds() -> float:
    raw = os.environ.get("AIGENORA_DAEMON_STARTUP_TIMEOUT")
    if raw is None or raw == "":
        return DEFAULT_STARTUP_WAIT_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_STARTUP_WAIT_SECONDS


def wait_for_event(
    state_dir: str | Path,
    event_type: str,
    *,
    timeout_seconds: float | None = None,
    required_data_keys: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Poll state_dir/events.jsonl until a matching startup event appears."""
    timeout = startup_wait_seconds() if timeout_seconds is None else max(0.0, timeout_seconds)
    deadline = time.monotonic() + timeout
    bus = EventBus(state_dir)
    while True:
        for event in bus.read_events():
            if event.get("type") != event_type:
                continue
            data = event.get("data") or {}
            if all(data.get(k) for k in required_data_keys):
                return event
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def write_session_meta(state_dir: str | Path, meta: dict[str, Any]) -> None:
    Path(state_dir, "session.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def update_session_meta(state_dir: str | Path | None, **updates: Any) -> None:
    """Read-modify-write session.json.

    The daemon parent process (host._run_daemon / join._run_daemon) writes the initial
    session.json and returns right after startup, so it never observes how the business
    subprocess ends. The business subprocess therefore calls this on its terminal path to
    record the final status (closed/aborted), ended_at, game_over and end_reason — otherwise
    console/list keeps showing a stale "running" session for a process that already exited.
    Best-effort: a missing/unreadable session.json or a None state_dir is a silent no-op.
    """
    if not state_dir:
        return
    path = Path(state_dir, "session.json")
    if not path.exists():
        return
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        # session.json 损坏/不可读：原为静默 return，导致终态丢失且无从排查。改为记录 warning。
        print(f"[aigenora] warning: failed to read session.json for update: {e}", file=sys.stderr)
        return
    meta.update(updates)
    path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def read_log_excerpt(state_dir: str | Path, name: str = "daemon.err.log", limit: int = 500) -> str:
    path = Path(state_dir) / name
    if not path.exists() or path.stat().st_size <= 0:
        return ""
    with path.open("rb") as f:
        size = path.stat().st_size
        if size > limit:
            f.seek(size - limit)
        return f.read().decode("utf-8", errors="replace")


def terminate_process(proc: Any) -> None:
    try:
        proc.terminate()
    except Exception:
        pass
