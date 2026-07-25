"""Main-process proxy for session-scoped Host P2P hooks workers.

Remote source bytes are never compiled or imported in this module. Every hook
call crosses a bounded JSON line protocol to one isolated child process.
"""
from __future__ import annotations

import atexit
import base64
import importlib.util
import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
import weakref
from functools import partial
from pathlib import Path
from typing import Any

from aigenora.agent.protocol_bundle import (
    WORKER_ISOLATION_PROFILE,
    VerifiedInstalledBundle,
    verify_installed_bundle,
)
from aigenora.proto.hooks import (
    HookResult,
    InvalidHumanDecisionError,
    ProtocolHooks,
)


MAX_WORKER_FRAME_BYTES = 2 * 1024 * 1024
DEFAULT_CALL_TIMEOUT_SECONDS = 10.0
HUMAN_CALL_TIMEOUT_SECONDS = 600.0
INITIALIZE_TIMEOUT_SECONDS = 10.0
_TYPE_KEY = "__aigenora_rpc_type__"
_REMOTE_METHODS = frozenset(
    {
        "_await_human_decision",
        "_consume_hybrid",
        "_reject_human_decision",
        "build_decision_context",
        "get_decision_schema",
        "proto_display",
        "proto_guest_first_action",
        "proto_guest_handle",
        "proto_guest_handle_ready",
        "proto_guest_join_message",
        "proto_host_handle",
        "proto_host_handle_join",
        "proto_host_metadata",
        "proto_mp_apply_local_action",
        "proto_mp_check_winner",
        "proto_mp_choose_action",
        "proto_mp_coerce_action",
        "proto_mp_deck_universe",
        "proto_mp_initial_deal",
        "proto_mp_legal_actions",
        "proto_mp_validate_play",
        "proto_on_end",
        "proto_on_message",
        "proto_on_send",
        "proto_parse_whisper_intent",
        "proto_realtime_audit_outcome",
        "proto_realtime_commands",
        "proto_realtime_initial_state",
        "proto_realtime_snapshot",
        "proto_realtime_step",
        "proto_realtime_transport_update",
        "proto_realtime_validate_commands",
        "proto_round_judge",
        "proto_round_judge_pure",
        "proto_round_value",
        "run_policy",
    }
)
_OPTIONAL_METHODS = frozenset({"proto_round_judge_pure"})
_ACTIVE_PROXIES: "weakref.WeakSet[RemoteHooksProxy]" = weakref.WeakSet()
_ACTIVE_SESSION_WORKERS: dict[Path, "weakref.ReferenceType[RemoteHooksProxy]"] = {}
_ACTIVE_SESSION_WORKERS_LOCK = threading.Lock()


class RemoteHooksError(RuntimeError):
    """The restricted hooks worker failed, timed out, or violated RPC."""


def _worker_failure_status(exc: BaseException) -> str:
    message = str(exc)
    if "timed out" in message:
        return "timeout"
    if "exited with code" in message:
        return "crashed"
    return "failed"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _trusted_worker_import_roots() -> tuple[str, ...]:
    """Locate only the installed roots needed by trusted runtime dependencies."""
    roots: list[str] = []
    for module_name in ("cryptography",):
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            raise RemoteHooksError(
                f"trusted worker dependency is unavailable: {module_name}"
            )
        candidates: list[Path] = []
        if spec.submodule_search_locations:
            candidates.extend(Path(value).resolve().parent for value in spec.submodule_search_locations)
        elif spec.origin:
            candidates.append(Path(spec.origin).resolve().parent)
        for candidate in candidates:
            value = str(candidate)
            if value not in roots:
                roots.append(value)
    return tuple(roots)


def _encode(value: Any, *, depth: int = 0) -> Any:
    if depth > 64:
        raise RemoteHooksError("remote hooks value nesting exceeds limit")
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not (float("-inf") < value < float("inf")):
            raise RemoteHooksError("remote hooks values must not contain NaN or infinity")
        return value
    if isinstance(value, bytes):
        return {_TYPE_KEY: "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Path):
        return {_TYPE_KEY: "path", "value": str(value)}
    from aigenora.proto.mental_poker import DeckState

    if isinstance(value, DeckState):
        return {
            _TYPE_KEY: "deck_state",
            "guest_hand": _encode(value.guest_hand, depth=depth + 1),
            "host_hand": _encode(value.host_hand, depth=depth + 1),
            "stock": _encode(value.stock, depth=depth + 1),
            "played": _encode(value.played, depth=depth + 1),
        }
    if isinstance(value, tuple):
        return {_TYPE_KEY: "tuple", "items": [_encode(item, depth=depth + 1) for item in value]}
    if isinstance(value, set):
        encoded = [_encode(item, depth=depth + 1) for item in value]
        encoded.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        return {_TYPE_KEY: "set", "items": encoded}
    if isinstance(value, list):
        return [_encode(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise RemoteHooksError("remote hooks dictionaries require string keys")
        return {
            key: _encode(item, depth=depth + 1)
            for key, item in value.items()
        }
    raise RemoteHooksError(
        f"remote hooks value type is not allowed: {type(value).__name__}"
    )


def _decode(value: Any, *, depth: int = 0) -> Any:
    if depth > 64:
        raise RemoteHooksError("remote hooks response nesting exceeds limit")
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not (float("-inf") < value < float("inf")):
            raise RemoteHooksError(
                "remote hooks response must not contain NaN or infinity"
            )
        return value
    if isinstance(value, list):
        return [_decode(item, depth=depth + 1) for item in value]
    if not isinstance(value, dict):
        raise RemoteHooksError("remote hooks response contains an invalid type")
    value_type = value.get(_TYPE_KEY)
    if value_type is None:
        if any(not isinstance(key, str) for key in value):
            raise RemoteHooksError("remote hooks response contains a non-string key")
        return {
            key: _decode(item, depth=depth + 1)
            for key, item in value.items()
        }
    if value_type == "bytes" and set(value) == {_TYPE_KEY, "base64"}:
        try:
            return base64.b64decode(value["base64"], validate=True)
        except Exception as exc:
            raise RemoteHooksError("remote hooks returned invalid bytes") from exc
    if value_type == "path" and set(value) == {_TYPE_KEY, "value"}:
        if not isinstance(value["value"], str):
            raise RemoteHooksError("remote hooks returned an invalid path")
        return Path(value["value"])
    if value_type in {"tuple", "set"} and set(value) == {_TYPE_KEY, "items"}:
        if not isinstance(value["items"], list):
            raise RemoteHooksError("remote hooks returned invalid collection items")
        items = [_decode(item, depth=depth + 1) for item in value["items"]]
        return tuple(items) if value_type == "tuple" else set(items)
    if value_type == "deck_state" and set(value) == {
        _TYPE_KEY,
        "guest_hand",
        "host_hand",
        "stock",
        "played",
    }:
        from aigenora.proto.mental_poker import DeckState

        fields = {
            name: _decode(value[name], depth=depth + 1)
            for name in ("guest_hand", "host_hand", "stock", "played")
        }
        if any(
            not isinstance(items, set)
            or any(not isinstance(item, str) for item in items)
            for items in fields.values()
        ):
            raise RemoteHooksError("remote DeckState fields are invalid")
        return DeckState(**fields)
    if value_type == "hook_result" and set(value) == {
        _TYPE_KEY,
        "response",
        "completed",
        "abort",
    }:
        response = _decode(value["response"], depth=depth + 1)
        if response is not None and not isinstance(response, dict):
            raise RemoteHooksError("remote HookResult.response must be an object")
        if not isinstance(value["completed"], bool) or not isinstance(value["abort"], bool):
            raise RemoteHooksError("remote HookResult flags must be booleans")
        return HookResult(
            response=response,
            completed=value["completed"],
            abort=value["abort"],
        )
    if value_type == "validation_result" and set(value) == {
        _TYPE_KEY,
        "ok",
        "reason",
    }:
        from aigenora.proto.mental_poker import ValidationResult

        valid = value["ok"]
        reason = value["reason"]
        if not isinstance(valid, bool) or (
            reason is not None and not isinstance(reason, str)
        ):
            raise RemoteHooksError("remote validation result is invalid")
        return ValidationResult(valid, reason)
    raise RemoteHooksError("remote hooks response contains an unknown tagged type")


class _RemoteSnapshot:
    def __init__(self, owner: "RemoteHooksProxy"):
        self._owner = owner

    def update(
        self,
        patch: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        merged = dict(patch or {})
        merged.update(kwargs)
        return self._owner._control_call(
            "snapshot_update",
            {"values": merged},
        )

    def set_phase(
        self,
        phase: str,
        summary: str | None = None,
        **structured: Any,
    ) -> dict[str, Any]:
        return self._owner._control_call(
            "snapshot_set_phase",
            {
                "phase": phase,
                "summary": summary,
                "structured": structured,
            },
        )


class _WorkerClient:
    def __init__(self, process: subprocess.Popen[bytes], stderr_handle: Any):
        self.process = process
        self._stderr_handle = stderr_handle
        self._responses: "queue.Queue[dict[str, Any] | BaseException]" = queue.Queue()
        self._lock = threading.Lock()
        self._next_id = 1
        self._reader = threading.Thread(
            target=self._read_responses,
            name=f"aigenora-remote-hooks-{process.pid}",
            daemon=True,
        )
        self._reader.start()

    def _read_responses(self) -> None:
        stdout = self.process.stdout
        if stdout is None:
            self._responses.put(RemoteHooksError("worker stdout is unavailable"))
            return
        try:
            while True:
                line = stdout.readline(MAX_WORKER_FRAME_BYTES + 1)
                if not line:
                    self._responses.put(
                        RemoteHooksError(
                            f"remote hooks worker exited with code {self.process.poll()}"
                        )
                    )
                    return
                if len(line) > MAX_WORKER_FRAME_BYTES or not line.endswith(b"\n"):
                    self._responses.put(RemoteHooksError("remote hooks worker frame exceeds limit"))
                    return
                try:
                    value = json.loads(
                        line,
                        parse_constant=_reject_json_constant,
                    )
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                    self._responses.put(
                        RemoteHooksError(f"remote hooks worker returned invalid JSON: {exc}")
                    )
                    return
                if not isinstance(value, dict):
                    self._responses.put(RemoteHooksError("remote hooks worker frame is not an object"))
                    return
                self._responses.put(value)
        except BaseException as exc:
            self._responses.put(exc)

    def call(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> Any:
        with self._lock:
            if self.process.poll() is not None:
                raise RemoteHooksError(
                    f"remote hooks worker is not running: {self.process.returncode}"
                )
            request_id = self._next_id
            self._next_id += 1
            request = {
                "id": request_id,
                "action": action,
                "payload": _encode(payload),
            }
            encoded = (
                json.dumps(
                    request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            if len(encoded) > MAX_WORKER_FRAME_BYTES:
                raise RemoteHooksError("remote hooks request frame exceeds limit")
            stdin = self.process.stdin
            if stdin is None:
                raise RemoteHooksError("worker stdin is unavailable")
            try:
                stdin.write(encoded)
                stdin.flush()
            except OSError as exc:
                self.terminate()
                raise RemoteHooksError("failed to write to remote hooks worker") from exc
            try:
                response = self._responses.get(timeout=timeout)
            except queue.Empty as exc:
                self.terminate()
                raise RemoteHooksError(
                    f"remote hooks worker timed out during {action}"
                ) from exc
            if isinstance(response, BaseException):
                self.terminate()
                raise RemoteHooksError(str(response)) from response
            if response.get("id") != request_id:
                self.terminate()
                raise RemoteHooksError("remote hooks worker response id mismatch")
            if response.get("ok") is True and set(response) == {"id", "ok", "result"}:
                return _decode(response["result"])
            error = response.get("error")
            if not isinstance(error, dict):
                self.terminate()
                raise RemoteHooksError("remote hooks worker error frame is invalid")
            error_type = str(error.get("type") or "RuntimeError")
            message = str(error.get("message") or "remote hooks call failed")[:500]
            if error_type == "InvalidHumanDecisionError":
                raise InvalidHumanDecisionError(message)
            if error_type == "NotImplementedError":
                raise NotImplementedError(message)
            if error_type == "KeyError":
                raise KeyError(message)
            if error_type == "TypeError":
                raise TypeError(message)
            if error_type == "ValueError":
                raise ValueError(message)
            self.terminate()
            raise RemoteHooksError(f"{error_type}: {message}")

    def terminate(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.process.kill()
                    self.process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for stream in (self.process.stdin, self.process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            self._stderr_handle.close()
        except OSError:
            pass


class RemoteHooksProxy(ProtocolHooks):
    """ProtocolHooks-compatible proxy backed by one restricted child process."""

    def __init__(self, protocol_dir: str | Path):
        verified = verify_installed_bundle(protocol_dir)
        object.__setattr__(self, "_protocol_dir", Path(protocol_dir).resolve())
        object.__setattr__(self, "_verified", verified)
        object.__setattr__(self, "_client", None)
        object.__setattr__(self, "_closed", False)
        object.__setattr__(self, "_timing", None)
        object.__setattr__(self, "_snapshot_proxy", _RemoteSnapshot(self))
        object.__setattr__(self, "_state_dir", None)
        object.__setattr__(self, "_event_dir", None)
        object.__setattr__(self, "_worker_session_key", None)
        object.__setattr__(self, "control_mode", "autonomous")
        object.__setattr__(self, "DECISION_SCHEMA", None)
        object.__setattr__(self, "CHOICE_KEYWORDS", None)
        _ACTIVE_PROXIES.add(self)

    def __getattribute__(self, name: str) -> Any:
        if name in _REMOTE_METHODS:
            verified: VerifiedInstalledBundle = object.__getattribute__(self, "_verified")
            if name in _OPTIONAL_METHODS and name not in verified.manifest["hook_methods"]:
                raise AttributeError(name)
            return partial(object.__getattribute__(self, "_remote_call"), name)
        return object.__getattribute__(self, name)

    @property
    def snapshot(self) -> _RemoteSnapshot:
        return self._snapshot_proxy

    @property
    def timing(self) -> Any:
        return self._timing

    @timing.setter
    def timing(self, value: Any) -> None:
        object.__setattr__(self, "_timing", value)
        if self._client is not None:
            self._control_call("set_attribute", {"name": "timing", "value": value})

    def supported_control_modes(self) -> tuple[str, ...]:
        return tuple(self._verified.manifest["supported_control_modes"])

    def proto_init(
        self,
        options: dict[str, Any],
        role: str,
        args: list[str],
        state_dir: Path,
        decision_config: dict[str, Any] | None = None,
    ) -> None:
        if self._client is not None:
            raise RemoteHooksError("remote hooks worker is already initialized")
        if self._closed:
            raise RemoteHooksError("remote hooks proxy is already closed")
        state_path = Path(state_dir).resolve()
        state_path.mkdir(parents=True, exist_ok=True)
        if self._protocol_dir.parent != state_path.parent:
            raise RemoteHooksError(
                "received bundle and hooks state are not pinned to the same Session"
            )
        session_key = state_path.parent
        with _ACTIVE_SESSION_WORKERS_LOCK:
            existing_ref = _ACTIVE_SESSION_WORKERS.get(session_key)
            existing = existing_ref() if existing_ref is not None else None
            if existing is not None and existing is not self:
                raise RemoteHooksError(
                    "only one remote hooks worker is allowed per Session"
                )
            _ACTIVE_SESSION_WORKERS[session_key] = weakref.ref(self)
        object.__setattr__(self, "_event_dir", session_key)
        object.__setattr__(self, "_worker_session_key", session_key)
        worker_root = state_path / "remote-hooks-worker"
        worker_root.mkdir(parents=True, exist_ok=True)
        cwd = worker_root / "cwd"
        temporary = worker_root / "tmp"
        cwd.mkdir(parents=True, exist_ok=True)
        temporary.mkdir(parents=True, exist_ok=True)
        stderr_path = worker_root / "stderr.log"
        stderr_handle = stderr_path.open("ab", buffering=0)
        worker_script = Path(__file__).with_name("remote_hooks_worker.py").resolve()
        environment: dict[str, str] = {
            "AIGENORA_WORKER_IMPORT_ROOTS": os.pathsep.join(
                _trusted_worker_import_roots()
            ),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "TEMP": str(temporary),
            "TMP": str(temporary),
        }
        for name in ("SYSTEMROOT", "WINDIR"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        command = [sys.executable, "-I", "-S", "-B", str(worker_script)]
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform == "win32"
            else 0
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_handle,
                shell=False,
                creationflags=creationflags,
            )
        except Exception:
            stderr_handle.close()
            self._release_session_worker()
            raise
        client = _WorkerClient(process, stderr_handle)
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_state_dir", state_path)
        self._record_worker_state("started")
        try:
            hooks_file = next(
                item
                for item in self._verified.manifest["files"]
                if item["path"] == "hooks.py"
            )
            result = client.call(
                "initialize",
                {
                    "source_base64": base64.b64encode(
                        self._verified.hooks_source
                    ).decode("ascii"),
                    "source_hash": hooks_file["content_hash"],
                    "manifest_hash": self._verified.manifest_hash,
                    "hook_methods": self._verified.manifest["hook_methods"],
                    "supported_control_modes": self._verified.manifest[
                        "supported_control_modes"
                    ],
                    "options": options,
                    "role": role,
                    "args": args,
                    "state_dir": str(state_path),
                    "decision_config": decision_config,
                },
                timeout=INITIALIZE_TIMEOUT_SECONDS,
            )
            if (
                not isinstance(result, dict)
                or set(result)
                != {
                    "status",
                    "control_mode",
                    "supported_control_modes",
                    "decision_schema",
                    "choice_keywords",
                }
                or result.get("status") != "ready"
            ):
                raise RemoteHooksError("remote hooks worker initialization result is invalid")
            actual_mode = result.get("control_mode")
            if actual_mode not in {"autonomous", "hybrid", "human"}:
                raise RemoteHooksError("remote hooks worker control mode is invalid")
            if result.get("supported_control_modes") != list(
                self._verified.manifest["supported_control_modes"]
            ):
                raise RemoteHooksError(
                    "remote hooks worker control modes differ from manifest"
                )
            decision_schema = result.get("decision_schema")
            choice_keywords = result.get("choice_keywords")
            if decision_schema is not None and not isinstance(decision_schema, dict):
                raise RemoteHooksError("remote hooks decision schema is invalid")
            if choice_keywords is not None and not isinstance(choice_keywords, dict):
                raise RemoteHooksError("remote hooks choice keywords are invalid")
            object.__setattr__(self, "control_mode", actual_mode)
            object.__setattr__(self, "DECISION_SCHEMA", decision_schema)
            object.__setattr__(self, "CHOICE_KEYWORDS", choice_keywords)
            self._record_hook_metadata()
            if self._timing is not None:
                self._control_call(
                    "set_attribute",
                    {"name": "timing", "value": self._timing},
                )
        except Exception as exc:
            self._record_worker_state(
                _worker_failure_status(exc),
                reason=str(exc)[:500],
            )
            self.close(reason="initialization_failed")
            raise

    def _remote_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        timeout = (
            HUMAN_CALL_TIMEOUT_SECONDS
            if method == "_await_human_decision"
            else DEFAULT_CALL_TIMEOUT_SECONDS
        )
        return self._control_call(
            "call",
            {"method": method, "args": list(args), "kwargs": kwargs},
            timeout=timeout,
        )

    def _control_call(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> Any:
        client = self._client
        if client is None:
            raise RemoteHooksError("remote hooks worker is not initialized")
        try:
            return client.call(action, payload, timeout=timeout)
        except (
            InvalidHumanDecisionError,
            KeyError,
            NotImplementedError,
            TypeError,
            ValueError,
        ):
            raise
        except Exception as exc:
            message = str(exc)[:500]
            self._record_worker_state(
                _worker_failure_status(exc),
                reason=message,
            )
            raise

    def _record_worker_state(
        self,
        status: str,
        *,
        reason: str | None = None,
        temporary_dirs_cleaned: bool | None = None,
    ) -> None:
        state_dir = self._state_dir
        event_dir = self._event_dir
        client = self._client
        if state_dir is None:
            return
        payload = {
            "schema": "aigenora-remote-hooks-worker/1",
            "status": status,
            "pid": client.process.pid if client is not None else None,
            "manifest_hash": self._verified.manifest_hash,
            "source_peer": self._verified.sidecar["source_peer"],
            "isolation_profile": WORKER_ISOLATION_PROFILE,
            "updated_at": time.time(),
        }
        if reason:
            payload["reason"] = reason
        if temporary_dirs_cleaned is not None:
            payload["temporary_dirs_cleaned"] = temporary_dirs_cleaned
        record_dir = event_dir if event_dir is not None else state_dir
        target = record_dir / "remote-hooks-worker.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        try:
            from aigenora.proto.sdk import EventBus

            EventBus(record_dir).emit(
                f"bundle_worker_{status}",
                {
                    "pid": payload["pid"],
                    "manifest_hash": payload["manifest_hash"],
                    "source_peer": payload["source_peer"],
                    "isolation_profile": payload["isolation_profile"],
                    **({"reason": reason} if reason else {}),
                    **(
                        {"temporary_dirs_cleaned": temporary_dirs_cleaned}
                        if temporary_dirs_cleaned is not None
                        else {}
                    ),
                },
            )
        except Exception:
            pass
        try:
            from aigenora.agent._daemon import update_session_meta

            update_session_meta(record_dir, bundle_worker=payload)
        except Exception:
            pass

    def _record_hook_metadata(self) -> None:
        state_dir = self._state_dir
        event_dir = self._event_dir
        if state_dir is None:
            return
        payload = {
            "schema": "aigenora-remote-hooks-metadata/1",
            "supported_control_modes": list(
                self._verified.manifest["supported_control_modes"]
            ),
            "decision_schema": self.DECISION_SCHEMA,
            "choice_keywords": self.CHOICE_KEYWORDS,
            "manifest_hash": self._verified.manifest_hash,
            "source_peer": self._verified.sidecar["source_peer"],
        }
        target = state_dir / "remote-hooks-metadata.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        if event_dir is not None:
            try:
                from aigenora.agent._daemon import update_session_meta

                update_session_meta(
                    event_dir,
                    supported_control_modes=payload[
                        "supported_control_modes"
                    ],
                    decision_schema=payload["decision_schema"],
                )
            except Exception:
                pass

    def _cleanup_worker_temporary_dirs(self) -> bool:
        state_dir = self._state_dir
        if state_dir is None:
            return True
        worker_root = state_dir / "remote-hooks-worker"
        cleaned = True
        for name in ("cwd", "tmp"):
            path = worker_root / name
            try:
                value = path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                cleaned = False
                continue
            try:
                is_reparse = bool(
                    getattr(value, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
                if path.is_symlink() or is_reparse:
                    try:
                        path.unlink()
                    except IsADirectoryError:
                        path.rmdir()
                elif stat.S_ISDIR(value.st_mode):
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError:
                cleaned = False
        return cleaned

    def _release_session_worker(self) -> None:
        session_key = self._worker_session_key
        if session_key is None:
            return
        with _ACTIVE_SESSION_WORKERS_LOCK:
            existing_ref = _ACTIVE_SESSION_WORKERS.get(session_key)
            existing = existing_ref() if existing_ref is not None else None
            if existing is self or existing is None:
                _ACTIVE_SESSION_WORKERS.pop(session_key, None)
        object.__setattr__(self, "_worker_session_key", None)

    def close(self, *, reason: str = "session_ended") -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        client = self._client
        if client is not None:
            try:
                client.call("shutdown", {}, timeout=3.0)
            except Exception:
                pass
            client.terminate()
            temporary_dirs_cleaned = self._cleanup_worker_temporary_dirs()
            self._record_worker_state(
                "stopped",
                reason=reason,
                temporary_dirs_cleaned=temporary_dirs_cleaned,
            )
        object.__setattr__(self, "_client", None)
        self._release_session_worker()

    @property
    def worker_pid(self) -> int | None:
        client = self._client
        return client.process.pid if client is not None else None

    def __del__(self) -> None:
        try:
            self.close(reason="proxy_finalized")
        except Exception:
            pass


def close_remote_hook_workers(*, reason: str = "session_ended") -> None:
    for proxy in list(_ACTIVE_PROXIES):
        try:
            proxy.close(reason=reason)
        except Exception:
            pass


atexit.register(close_remote_hook_workers, reason="process_exit")


__all__ = [
    "DEFAULT_CALL_TIMEOUT_SECONDS",
    "HUMAN_CALL_TIMEOUT_SECONDS",
    "INITIALIZE_TIMEOUT_SECONDS",
    "MAX_WORKER_FRAME_BYTES",
    "RemoteHooksError",
    "RemoteHooksProxy",
    "close_remote_hook_workers",
]
