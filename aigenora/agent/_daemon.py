from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from aigenora.proto.sdk import EventBus


DEFAULT_STARTUP_WAIT_SECONDS = 15.0


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
