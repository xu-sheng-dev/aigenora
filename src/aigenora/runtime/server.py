from __future__ import annotations

import hashlib
import hmac
import json
import platform
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO, TextIO

from aigenora.runtime.catalog.loader import PinnedCatalog
from .errors import RuntimeMethodError
from .generated.v1.contracts import (
    ERROR_CODES,
    IDENTITY_METHODS,
    METHOD_CONTRACTS,
    RUNTIME_SCHEMA_DIGEST,
    WORKER_METHODS,
)
from .registry import MethodRegistry, RuntimeHandler
from .security import assert_no_hooks_loaded, install_no_hooks_import_gate
from .stdio import JsonLineFrameReader, JsonLineFrameWriter, RuntimeDiagnosticLogger
from .validation import validate_request_envelope, validate_safe_result

if TYPE_CHECKING:
    from aigenora.services.context import ServiceContext


@dataclass(frozen=True)
class IdempotencyRecord:
    digest: str
    result: dict[str, Any]


@dataclass
class PendingRequest:
    method: str
    future: Future[None] | None = None
    cancelled: bool = False


class RuntimeServer:
    def __init__(
        self,
        *,
        context: "ServiceContext | None",
        catalog: PinnedCatalog,
        process_role: str,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
        stderr: TextIO | None = None,
        max_frame_bytes: int = 1_048_576,
        worker_handlers: dict[str, RuntimeHandler] | None = None,
    ):
        if process_role not in {"identity_sidecar", "protocol_worker"}:
            raise ValueError("Runtime process role is invalid")
        self._context = context
        self._catalog = catalog
        self._process_role = process_role
        self._security_profile = (
            "identity_sidecar_v1" if process_role == "identity_sidecar" else "protocol_worker_v1"
        )
        self._allowed_methods = IDENTITY_METHODS if process_role == "identity_sidecar" else WORKER_METHODS
        self._registry = MethodRegistry(self._allowed_methods)
        if process_role == "identity_sidecar":
            if context is None:
                raise ValueError("identity Sidecar requires a fixed ServiceContext")
            from .methods import build_identity_handlers

            handlers = build_identity_handlers(context, catalog)
        else:
            handlers = worker_handlers or {}
        for name, handler in handlers.items():
            self._registry.register(name, handler)
        self._reader = JsonLineFrameReader(stdin or sys.stdin.buffer, max_frame_bytes)
        self._writer = JsonLineFrameWriter(stdout or sys.stdout.buffer)
        self._logger = RuntimeDiagnosticLogger(stderr or sys.stderr)
        self._max_frame_bytes = max_frame_bytes
        self._instance_id = "runtime_" + secrets.token_hex(16)
        self._started_at = time.monotonic()
        self._handshaken = False
        self._stopping = False
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="aigenora-runtime")
        self._pending: dict[str, PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._idempotency: dict[tuple[str, str, str, str], IdempotencyRecord] = {}
        self._idempotency_lock = threading.Lock()
        self._event_sequences: dict[str, int] = {}
        self._event_resume_tokens: dict[str, dict[int, str]] = {}
        self._resume_secret = secrets.token_bytes(32)
        self._event_lock = threading.Lock()

    def serve(self) -> int:
        if self._process_role == "identity_sidecar":
            install_no_hooks_import_gate()
        try:
            while not self._stopping:
                try:
                    frame = self._reader.read()
                except RuntimeMethodError as exc:
                    self._write_error("req_invalid", exc)
                    break
                if frame is None:
                    break
                kind = frame.get("kind")
                if kind == "request":
                    self._accept_request(frame)
                elif kind == "cancel":
                    self._accept_cancel(frame)
                elif kind == "ack":
                    self._accept_ack(frame)
                else:
                    self._write_error(
                        str(frame.get("request_id", "req_invalid")),
                        RuntimeMethodError(
                            "transport.stdout_pollution", "Runtime received a server-only envelope"
                        ),
                    )
                    break
        finally:
            self._stopping = True
            self._executor.shutdown(wait=True, cancel_futures=True)
            if self._process_role == "identity_sidecar":
                assert_no_hooks_loaded()
        return 0

    def _accept_request(self, frame: dict[str, Any]) -> None:
        started = time.monotonic()
        request_id = str(frame.get("request_id", "req_invalid"))
        method = str(frame.get("method", "runtime.invalid"))
        try:
            request = validate_request_envelope(frame)
            if method not in self._registry.allowed_methods:
                raise RuntimeMethodError("runtime.method_not_allowed", "Runtime method is not allowed")
            if not self._handshaken and method != "runtime.hello":
                raise RuntimeMethodError("runtime.not_ready", "Runtime handshake is required", retryable=True)
            if self._handshaken and method == "runtime.hello":
                raise RuntimeMethodError("runtime.method_not_allowed", "Runtime handshake is already complete")
            if method.startswith("runtime."):
                result = self._dispatch_runtime(method, request["params"])
                self._write_response(request_id, result)
                self._log(method, request_id, started, "ok")
                if method == "runtime.shutdown":
                    self._stopping = True
                return
            with self._pending_lock:
                if request_id in self._pending:
                    raise RuntimeMethodError("validation.schema_invalid", "Duplicate Runtime request id")
                if len(self._pending) >= 8:
                    raise RuntimeMethodError(
                        "rate_limit.requests", "Runtime request limit reached", retryable=True
                    )
                pending = PendingRequest(method=method)
                self._pending[request_id] = pending
                future = self._executor.submit(self._execute_domain, request, started)
                pending.future = future
        except RuntimeMethodError as exc:
            self._write_error(request_id, exc)
            self._log(method, request_id, started, "error", exc.code)

    def _execute_domain(self, request: dict[str, Any], started: float) -> None:
        request_id = request["request_id"]
        method = request["method"]
        try:
            result = self._dispatch_idempotent(method, request["params"], request["meta"])
            with self._pending_lock:
                pending = self._pending.get(request_id)
                cancelled = pending.cancelled if pending is not None else True
            if not cancelled:
                self._write_response(request_id, validate_safe_result(result))
                self._emit_domain_event(request, result)
                self._log(method, request_id, started, "ok")
        except Exception as exc:  # domain exceptions are mapped to stable public codes below
            mapped = self._map_exception(method, exc)
            with self._pending_lock:
                pending = self._pending.get(request_id)
                cancelled = pending.cancelled if pending is not None else True
            if not cancelled:
                self._write_error(request_id, mapped)
                self._log(method, request_id, started, "error", mapped.code)
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _dispatch_idempotent(
        self, method: str, params: dict[str, Any], meta: dict[str, Any]
    ) -> dict[str, Any]:
        contract = METHOD_CONTRACTS[method]
        idempotency = str(contract["idempotency"])
        key = meta.get("idempotency_key")
        if idempotency == "required" and not isinstance(key, str):
            raise RuntimeMethodError(
                "validation.schema_invalid", "Runtime idempotency key is required"
            )
        if not isinstance(key, str):
            return self._registry.dispatch(method, params, meta)
        cache_key = (
            str(meta.get("origin_id", "")),
            str(meta.get("session_id", "")),
            method,
            key,
        )
        digest = hashlib.sha256(
            json.dumps(
                params, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        ).hexdigest()
        with self._idempotency_lock:
            existing = self._idempotency.get(cache_key)
            if existing is not None:
                if existing.digest != digest:
                    raise RuntimeMethodError(
                        "runtime.idempotency_conflict", "Runtime idempotency key conflicts"
                    )
                return dict(existing.result)
        result = self._registry.dispatch(method, params, meta)
        with self._idempotency_lock:
            if len(self._idempotency) >= 1024:
                self._idempotency.pop(next(iter(self._idempotency)))
            self._idempotency[cache_key] = IdempotencyRecord(digest, dict(result))
        return result

    def _dispatch_runtime(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "runtime.hello":
            return self._hello(params)
        if method == "runtime.health":
            with self._pending_lock:
                queue_depth = len(self._pending)
            return {
                "instance_id": self._instance_id,
                "status": "ok",
                "uptime_ms": int((time.monotonic() - self._started_at) * 1000),
                "queue_depth": queue_depth,
            }
        if method == "runtime.describe":
            return {
                "schema_digest": RUNTIME_SCHEMA_DIGEST,
                "errors_version": "1",
                "methods": self._registry.descriptors(),
            }
        if method == "runtime.shutdown":
            return {"accepted": True}
        raise RuntimeMethodError("runtime.method_not_allowed", "Runtime method is not allowed")

    def _hello(self, params: dict[str, Any]) -> dict[str, Any]:
        if params["expected_schema_digest"] != RUNTIME_SCHEMA_DIGEST:
            raise RuntimeMethodError("runtime.schema_mismatch", "Runtime schema digest differs")
        if params["expected_catalog_digest"] != self._catalog.catalog_digest:
            raise RuntimeMethodError("runtime.catalog_mismatch", "Runtime catalog digest differs")
        if params["expected_process_role"] != self._process_role:
            raise RuntimeMethodError(
                "protocol.worker_role_mismatch", "Runtime process role differs"
            )
        if params["minimum_security_profile"] != self._security_profile:
            raise RuntimeMethodError(
                "runtime.security_profile_insufficient", "Runtime security profile differs"
            )
        required = params["required_methods"]
        if not set(required).issubset(self._registry.allowed_methods):
            raise RuntimeMethodError("runtime.capability_missing", "Runtime method is unavailable")
        self._handshaken = True
        os_name = {"Windows": "windows", "Linux": "linux", "Darwin": "darwin"}.get(
            platform.system(), "linux"
        )
        return {
            "instance_id": self._instance_id,
            "runtime_version": "0.1.0",
            "protocol_version": "1",
            "schema_digest": RUNTIME_SCHEMA_DIGEST,
            "security_profile": self._security_profile,
            "process_role": self._process_role,
            "catalog_digest": self._catalog.catalog_digest,
            "execution_trust": "builtin_pinned",
            "isolation_status": "pre_isolation",
            "provisional_tcb": True,
            "methods": self._registry.descriptors(),
            "limits": {
                "max_frame_bytes": self._max_frame_bytes,
                "max_concurrent_requests": 8,
                "max_event_buffer": 256,
                "max_subscriptions": 16,
            },
            "platform": {"os": os_name, "transport": "stdio", "framing": "jsonl"},
        }

    def _accept_cancel(self, frame: dict[str, Any]) -> None:
        if set(frame) != {"kind", "protocol_version", "instance_id", "request_id"}:
            return
        if frame.get("protocol_version") != "1" or frame.get("instance_id") != self._instance_id:
            return
        request_id = frame.get("request_id")
        if not isinstance(request_id, str):
            return
        with self._pending_lock:
            pending = self._pending.get(request_id)
            if pending is None:
                self._write_error(
                    request_id,
                    RuntimeMethodError("runtime.unknown_request", "Runtime request is unknown"),
                )
                return
            if pending.future is None or pending.future.cancel():
                pending.cancelled = True
                self._write_error(
                    request_id,
                    RuntimeMethodError(
                        "runtime.cancelled_before_side_effect",
                        "Runtime request was cancelled before its side effect",
                    ),
                )
                self._pending.pop(request_id, None)
            else:
                self._write_error(
                    request_id,
                    RuntimeMethodError(
                        "runtime.too_late_to_cancel", "Runtime request is already executing"
                    ),
                )

    def _accept_ack(self, frame: dict[str, Any]) -> None:
        if set(frame) != {
            "kind",
            "protocol_version",
            "instance_id",
            "subscription_id",
            "sequence",
            "resume_token",
        }:
            return
        if (
            frame.get("protocol_version") != "1"
            or frame.get("instance_id") != self._instance_id
            or not isinstance(frame.get("subscription_id"), str)
            or not isinstance(frame.get("sequence"), int)
            or not isinstance(frame.get("resume_token"), str)
        ):
            return
        subscription_id = frame["subscription_id"]
        sequence = frame["sequence"]
        with self._event_lock:
            tokens = self._event_resume_tokens.get(subscription_id)
            expected = tokens.get(sequence) if tokens is not None else None
            if expected is None or not hmac.compare_digest(expected, frame["resume_token"]):
                return
            for acknowledged in [item for item in tokens if item <= sequence]:
                del tokens[acknowledged]

    def _write_response(self, request_id: str, result: dict[str, Any]) -> None:
        self._writer.write(
            {
                "kind": "response",
                "protocol_version": "1",
                "instance_id": self._instance_id,
                "request_id": request_id,
                "result": result,
            }
        )

    def _write_error(self, request_id: str, error: RuntimeMethodError) -> None:
        code = error.code if error.code in ERROR_CODES else "internal.runtime_failure"
        self._writer.write(
            {
                "kind": "error",
                "protocol_version": "1",
                "instance_id": self._instance_id,
                "request_id": request_id,
                "error": {
                    "code": code,
                    "message": error.public_message[:512],
                    "retryable": bool(error.retryable),
                },
            }
        )

    def _emit_domain_event(
        self, request: dict[str, Any], result: dict[str, Any]
    ) -> None:
        method = request["method"]
        event_map = {
            "protocol.decision.submit": ("protocol.decision.receipt", "high"),
            "protocol.strategy.patch": ("protocol.strategy.receipt", "high"),
            "protocol.worker.open": ("protocol.worker.opened", "critical"),
            "protocol.worker.step": (
                "protocol.worker.completed"
                if result.get("status") == "completed"
                else "protocol.worker.transition",
                "critical" if result.get("status") == "completed" else "high",
            ),
            "protocol.worker.close": ("protocol.worker.closed", "critical"),
        }
        mapped = event_map.get(method)
        meta = request.get("meta", {})
        origin_id = meta.get("origin_id")
        session_id = meta.get("session_id")
        if mapped is None or not isinstance(origin_id, str) or not isinstance(session_id, str):
            return
        subscription_id = "sub_" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        with self._event_lock:
            sequence = self._event_sequences.get(subscription_id, 0)
            self._event_sequences[subscription_id] = sequence + 1
            resume_token = "resume_" + hmac.new(
                self._resume_secret,
                f"{self._instance_id}\0{subscription_id}\0{sequence}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            tokens = self._event_resume_tokens.setdefault(subscription_id, {})
            tokens[sequence] = resume_token
            while len(tokens) > 256:
                del tokens[next(iter(tokens))]
        allowed_keys = {
            "status",
            "reason",
            "sequence",
            "generation",
            "safe_point_sequence",
            "receipt_id",
            "outcome",
            "summary",
            "proposal_digest",
            "post_state_digest",
            "next_sequence",
            "worker_id",
            "closed",
        }
        data = {
            key: value
            for key, value in result.items()
            if key in allowed_keys and isinstance(value, (bool, int, float, str, type(None)))
        }
        event_name, importance = mapped
        self._writer.write(
            {
                "kind": "event",
                "protocol_version": "1",
                "instance_id": self._instance_id,
                "subscription_id": subscription_id,
                "sequence": sequence,
                "resume_token": resume_token,
                "event": event_name,
                "importance": importance,
                "data": data,
                "meta": {
                    "origin_id": origin_id,
                    "session_id": session_id,
                    "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "generation": int(result.get("generation", 0)),
                },
            }
        )

    @staticmethod
    def _map_exception(method: str, error: Exception) -> RuntimeMethodError:
        if isinstance(error, RuntimeMethodError):
            return error
        if isinstance(error, FileNotFoundError):
            return RuntimeMethodError("session.not_found", "Managed Runtime session was not found")
        if isinstance(error, KeyError):
            return RuntimeMethodError("protocol.untrusted", "Protocol is not in the pinned catalog")
        if isinstance(error, ValueError):
            return RuntimeMethodError("validation.schema_invalid", "Runtime method input is invalid")
        if method in {"registry.browse", "invitation.inspect", "session.rating.read"}:
            return RuntimeMethodError(
                "registry.unavailable", "Aigenora registry is unavailable", retryable=True
            )
        return RuntimeMethodError("internal.runtime_failure", "Runtime method failed")

    def _log(
        self,
        method: str,
        request_id: str,
        started: float,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        self._logger.record(
            method=method,
            request_id=request_id,
            outcome=outcome,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_code=error_code,
        )
