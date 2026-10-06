"""Safe error codes for the trusted cross-device collaboration plane.

Wire-visible error payloads carry only a stable code from ERROR_TEXT plus a
bounded, locally-composed detail string that never echoes raw peer input
values (design doc §6.2 projection boundary). Full diagnostics (exception
types, phases, raw excerpts) go to the local operator-only event log.
"""
from __future__ import annotations

# Stable wire error codes. Values are part of the protocol contract; append
# only, never rename or reuse.
ERROR_INVALID_MESSAGE = "invalid_message"
ERROR_UNKNOWN_TASK = "unknown_task"
ERROR_STATE_CONFLICT = "task_state_conflict"
ERROR_IDEMPOTENCY_CONFLICT = "idempotency_conflict"
ERROR_STORAGE_FULL = "storage_full"
ERROR_PATH_UNSAFE = "path_unsafe"
ERROR_ARTIFACT_INCOMPLETE = "artifact_incomplete"
ERROR_ARTIFACT_UNKNOWN = "artifact_unknown"
ERROR_UNAUTHORIZED = "unauthorized"
ERROR_EXPIRED_MESSAGE = "message_expired"
ERROR_CLOCK_SKEW = "clock_skew_detected"
ERROR_QUEUE_BUSY = "queue_busy"
ERROR_ADAPTER = "adapter_error"
ERROR_INTERNAL = "internal_error"

# Fixed static descriptions safe to show on the wire. Peer-supplied values are
# never interpolated into these strings.
ERROR_TEXT: dict[str, str] = {
    ERROR_INVALID_MESSAGE: "message failed protocol spec validation",
    ERROR_UNKNOWN_TASK: "task id is unknown on this host",
    ERROR_STATE_CONFLICT: "message conflicts with the task state machine",
    ERROR_IDEMPOTENCY_CONFLICT: "task id exists with a different request digest",
    ERROR_STORAGE_FULL: "local storage is full; artifact refused",
    ERROR_PATH_UNSAFE: "artifact path failed local path safety checks",
    ERROR_ARTIFACT_INCOMPLETE: "artifact is not fully received and verified yet",
    ERROR_ARTIFACT_UNKNOWN: "artifact id is not registered for this task",
    ERROR_UNAUTHORIZED: "peer or grant is not authorized for this operation",
    ERROR_EXPIRED_MESSAGE: "message acceptance window has expired",
    ERROR_CLOCK_SKEW: "clock skew beyond tolerance; verify device time",
    ERROR_QUEUE_BUSY: "host queue is busy; retry later",
    ERROR_ADAPTER: "local execution adapter reported an error",
    ERROR_INTERNAL: "internal error; see host operator diagnostics",
}


class CollabError(Exception):
    """Protocol-level error carrying a safe wire code and local-only detail."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def wire(self) -> dict:
        payload: dict = {
            "action": "error",
            "error_code": self.code,
            "detail": ERROR_TEXT.get(self.code, ERROR_TEXT[ERROR_INTERNAL])[:500],
        }
        return payload


class AdmissionError(Exception):
    """In-stream admission handshake failure (connection is dropped)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"admission rejected: {code}: {detail}")
        self.code = code
        self.detail = detail
