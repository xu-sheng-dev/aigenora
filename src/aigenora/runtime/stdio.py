from __future__ import annotations

import json
import threading
from typing import Any, BinaryIO, TextIO

from .errors import RuntimeMethodError


class JsonLineFrameReader:
    def __init__(self, stream: BinaryIO, max_frame_bytes: int):
        if max_frame_bytes < 1024 or max_frame_bytes > 16 * 1024 * 1024:
            raise ValueError("Runtime frame limit is invalid")
        self._stream = stream
        self._max_frame_bytes = max_frame_bytes

    def read(self) -> dict[str, Any] | None:
        raw = self._stream.readline(self._max_frame_bytes + 1)
        if raw == b"":
            return None
        if len(raw) > self._max_frame_bytes:
            raise RuntimeMethodError("validation.frame_too_large", "Runtime frame is too large")
        if not raw.endswith(b"\n"):
            raise RuntimeMethodError("transport.closed", "Runtime frame is truncated", retryable=True)
        raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if not raw or raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
            raise RuntimeMethodError("transport.stdout_pollution", "Runtime frame is polluted")
        try:
            text = raw.decode("utf-8", errors="strict")
            value = json.loads(
                text,
                parse_constant=lambda _constant: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeMethodError("transport.invalid_json", "Runtime frame is not valid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeMethodError("transport.invalid_json", "Runtime frame must be an object")
        return value


class JsonLineFrameWriter:
    def __init__(self, stream: BinaryIO):
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, value: dict[str, Any]) -> None:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        with self._lock:
            self._stream.write(encoded)
            self._stream.flush()


class RuntimeDiagnosticLogger:
    def __init__(self, stream: TextIO):
        self._stream = stream
        self._lock = threading.Lock()

    def record(
        self,
        *,
        method: str,
        request_id: str,
        outcome: str,
        duration_ms: int,
        error_code: str | None = None,
    ) -> None:
        event = {
            "event": "runtime.request",
            "method": method[:128],
            "request_id": request_id[:128],
            "outcome": outcome[:32],
            "duration_ms": max(0, min(duration_ms, 300000)),
        }
        if error_code is not None:
            event["error_code"] = error_code[:128]
        line = json.dumps(event, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()
