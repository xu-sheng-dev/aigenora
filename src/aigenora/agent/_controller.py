"""Small filesystem gate used to start a human Web controller before turn one."""
from __future__ import annotations

import time
from pathlib import Path


READY_FILE = "controller.ready"


def mark_controller_ready(state_dir: str | Path) -> Path:
    path = Path(state_dir) / READY_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(time.time()), encoding="utf-8")
    tmp.replace(path)
    return path


def wait_for_controller_ready(state_dir: str | Path, timeout: float = 30.0) -> None:
    path = Path(state_dir) / READY_FILE
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise RuntimeError(
        f"human controller did not become ready within {timeout:g}s: {path}"
    )


__all__ = ["READY_FILE", "mark_controller_ready", "wait_for_controller_ready"]

