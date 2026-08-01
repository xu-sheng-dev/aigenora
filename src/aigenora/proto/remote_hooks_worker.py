"""Restricted JSON-RPC worker for one received P2P hooks bundle.

This file is launched by absolute path with ``python -I -S -B``. It installs
filesystem/network/process audit gates before compiling any peer-provided bytes.
"""
from __future__ import annotations

import base64
import binascii
import builtins
import hashlib
import importlib
import json
import os
import sys
import sysconfig
import types
from pathlib import Path
from typing import Any


_AIGENORA_ROOT = Path(__file__).resolve().parents[1]
_SITE_ROOT = _AIGENORA_ROOT.parent
if str(_SITE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SITE_ROOT))

from aigenora.proto.hooks import HookResult, ProtocolHooks  # noqa: E402
from aigenora.proto.remote_hooks_contract import inspect_hooks_source  # noqa: E402


MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_LOG_BYTES = 64 * 1024
_TYPE_KEY = "__aigenora_rpc_type__"
_CONTROL_IN = sys.stdin.buffer
_CONTROL_OUT = sys.stdout.buffer
_ORIGINAL_IMPORT = builtins.__import__
_SAFE_SOURCE_IMPORTS = frozenset(
    {
        "__future__",
        "aigenora.proto",
        "aigenora.proto.hooks",
        "aigenora.proto.hidden_role",
        "aigenora.proto.mental_poker",
        "aigenora.proto.sdk",
        "bisect",
        "collections",
        "copy",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "hashlib",
        "heapq",
        "hmac",
        "itertools",
        "json",
        "math",
        "operator",
        "pathlib",
        "random",
        "re",
        "secrets",
        "statistics",
        "string",
        "time",
        "typing",
    }
)
_TRUSTED_RUNTIME_IMPORTS = frozenset(
    {
        "aigenora.control",
        "aigenora.engine.crypto",
        "aigenora.proto.decide_gateway",
        "aigenora.proto.hooks",
        "aigenora.proto.hidden_role",
        "aigenora.proto.intervention_intent",
        "aigenora.proto.mental_poker",
        "aigenora.proto.script_runner",
        "aigenora.proto.sdk",
        "aigenora.proto.whisper_bridge",
    }
)
_ALLOWED_CALLS = frozenset(
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


def _trusted_dependency_roots() -> tuple[Path, ...]:
    raw_value = os.environ.get("AIGENORA_WORKER_IMPORT_ROOTS", "")
    if not raw_value:
        return ()
    values = raw_value.split(os.pathsep)
    if len(values) > 4:
        raise RuntimeError("too many worker import roots")
    roots: list[Path] = []
    for value in values:
        if not value:
            continue
        root = Path(value)
        if not root.is_absolute():
            raise RuntimeError("worker import roots must be absolute")
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise RuntimeError("worker import root must be a directory")
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


_DEPENDENCY_ROOTS = _trusted_dependency_roots()
for _dependency_root in reversed(_DEPENDENCY_ROOTS):
    if str(_dependency_root) not in sys.path:
        sys.path.insert(0, str(_dependency_root))


def _preload_trusted_runtime() -> None:
    modules = set(_SAFE_SOURCE_IMPORTS) | set(_TRUSTED_RUNTIME_IMPORTS)
    for module_name in sorted(modules):
        try:
            importlib.import_module(module_name)
        except ImportError:
            if module_name.startswith("aigenora."):
                raise


_preload_trusted_runtime()
for _dependency_root in _DEPENDENCY_ROOTS:
    try:
        sys.path.remove(str(_dependency_root))
    except ValueError:
        pass

_STDLIB_ROOTS = tuple(
    dict.fromkeys(
        Path(value).resolve()
        for key, value in sysconfig.get_paths().items()
        if key in {"stdlib", "platstdlib"} and value
    )
)


class _BoundedLog:
    def __init__(self, path: Path):
        self._handle = path.open("a", encoding="utf-8", buffering=1)
        try:
            self._written = path.stat().st_size
        except OSError:
            self._written = 0

    def write(self, value: Any) -> int:
        text = str(value)
        encoded = text.encode("utf-8", errors="replace")
        remaining = max(0, MAX_LOG_BYTES - self._written)
        if remaining:
            chunk = encoded[:remaining].decode("utf-8", errors="ignore")
            self._handle.write(chunk)
            self._written += len(chunk.encode("utf-8"))
        return len(text)

    def flush(self) -> None:
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class _AuditGate:
    def __init__(self, state_root: Path):
        self.state_root = state_root.resolve()
        self.read_roots = (
            self.state_root,
            _AIGENORA_ROOT.resolve(),
            *_STDLIB_ROOTS,
            (Path(sys.base_prefix) / "DLLs").resolve(),
        )
        self.synthetic_filename = ""

    @staticmethod
    def _path(value: Any) -> Path:
        if isinstance(value, int):
            raise PermissionError("file-descriptor access is not allowed")
        try:
            decoded = os.fsdecode(value)
        except TypeError as exc:
            raise PermissionError("filesystem path is invalid") from exc
        return Path(decoded).resolve()

    def _allow_read(self, value: Any) -> None:
        path = self._path(value)
        if not any(_within(path, root) for root in self.read_roots):
            raise PermissionError(f"read outside worker roots is not allowed: {path}")

    def _allow_write(self, value: Any) -> None:
        path = self._path(value)
        if not _within(path, self.state_root):
            raise PermissionError(f"write outside hooks state is not allowed: {path}")

    def __call__(self, event: str, args: tuple[Any, ...]) -> None:
        if (
            event.startswith("socket.")
            or event.startswith("subprocess.")
            or event.startswith("ctypes.")
            or event.startswith("winreg.")
            or event
            in {
                "os.system",
                "os.exec",
                "os.posix_spawn",
                "os.spawn",
                "pty.spawn",
                "builtins.breakpoint",
                "builtins.input",
                "sys.settrace",
                "sys.setprofile",
            }
        ):
            raise PermissionError(f"worker operation is not allowed: {event}")
        if event in {"os.putenv", "os.unsetenv", "os.chdir", "os.fchdir"}:
            raise PermissionError(f"worker environment mutation is not allowed: {event}")
        if event in {"os.link", "os.symlink"}:
            raise PermissionError("worker links are not allowed")
        if event == "compile":
            filename = str(args[1]) if len(args) > 1 else ""
            if filename == self.synthetic_filename:
                return
            try:
                self._allow_read(filename)
            except PermissionError as exc:
                raise PermissionError("dynamic code compilation is not allowed") from exc
            return
        if event == "open":
            if not args:
                raise PermissionError("open without a path is not allowed")
            mode = args[1] if len(args) > 1 else "r"
            flags = args[2] if len(args) > 2 else 0
            write = False
            if isinstance(mode, str):
                write = any(marker in mode for marker in ("w", "a", "x", "+"))
            if isinstance(flags, int):
                write = write or bool(
                    flags
                    & (
                        os.O_WRONLY
                        | os.O_RDWR
                        | os.O_CREAT
                        | os.O_TRUNC
                        | os.O_APPEND
                    )
                )
            if write:
                self._allow_write(args[0])
            else:
                self._allow_read(args[0])
            return
        if event in {"os.listdir", "os.scandir"}:
            value = args[0] if args and args[0] is not None else Path.cwd()
            self._allow_read(value)
            return
        if event in {
            "os.remove",
            "os.rmdir",
            "os.mkdir",
            "os.chmod",
            "os.chown",
            "os.truncate",
            "os.utime",
        }:
            if args:
                self._allow_write(args[0])
            return
        if event in {"os.rename", "os.replace"}:
            if len(args) >= 2:
                self._allow_write(args[0])
                self._allow_write(args[1])


def _import_allowed(module_name: str) -> bool:
    allowed = _SAFE_SOURCE_IMPORTS | _TRUSTED_RUNTIME_IMPORTS
    if module_name in allowed:
        return True
    return any(module_name.startswith(prefix + ".") for prefix in allowed)


def _safe_import(
    name: str,
    globals_value: dict[str, Any] | None = None,
    locals_value: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] | list[str] = (),
    level: int = 0,
) -> Any:
    if level or not _import_allowed(name):
        raise ImportError(f"remote hooks import is not allowed: {name}")
    return _ORIGINAL_IMPORT(name, globals_value, locals_value, fromlist, level)


def _safe_builtins() -> dict[str, Any]:
    values = dict(vars(builtins))
    for name in ("breakpoint", "compile", "eval", "exec", "input", "open"):
        values.pop(name, None)
    values["__import__"] = _safe_import
    return values


def _decode(value: Any, *, depth: int = 0) -> Any:
    if depth > 64:
        raise ValueError("RPC value nesting exceeds limit")
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not (float("-inf") < value < float("inf")):
            raise ValueError("RPC value must not contain NaN or infinity")
        return value
    if isinstance(value, list):
        return [_decode(item, depth=depth + 1) for item in value]
    if not isinstance(value, dict):
        raise ValueError("RPC value type is invalid")
    value_type = value.get(_TYPE_KEY)
    if value_type is None:
        if any(not isinstance(key, str) for key in value):
            raise ValueError("RPC object key is invalid")
        return {key: _decode(item, depth=depth + 1) for key, item in value.items()}
    if value_type == "bytes" and set(value) == {_TYPE_KEY, "base64"}:
        try:
            return base64.b64decode(value["base64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("RPC bytes are invalid") from exc
    if value_type == "path" and set(value) == {_TYPE_KEY, "value"}:
        if not isinstance(value["value"], str):
            raise ValueError("RPC path is invalid")
        return Path(value["value"])
    if value_type in {"tuple", "set"} and set(value) == {_TYPE_KEY, "items"}:
        if not isinstance(value["items"], list):
            raise ValueError("RPC collection is invalid")
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
            raise ValueError("RPC DeckState fields are invalid")
        return DeckState(**fields)
    raise ValueError("RPC tagged value is invalid")


def _encode(value: Any, *, depth: int = 0) -> Any:
    if depth > 64:
        raise ValueError("RPC response nesting exceeds limit")
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not (float("-inf") < value < float("inf")):
            raise ValueError("RPC response must not contain NaN or infinity")
        return value
    if isinstance(value, HookResult):
        return {
            _TYPE_KEY: "hook_result",
            "response": _encode(value.response, depth=depth + 1),
            "completed": value.completed,
            "abort": value.abort,
        }
    try:
        from aigenora.proto.mental_poker import ValidationResult
    except ImportError:
        ValidationResult = None  # type: ignore[assignment,misc]
    if ValidationResult is not None and isinstance(value, ValidationResult):
        return {
            _TYPE_KEY: "validation_result",
            "ok": bool(value.ok),
            "reason": value.reason,
        }
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
        items = [_encode(item, depth=depth + 1) for item in value]
        items.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        return {_TYPE_KEY: "set", "items": items}
    if isinstance(value, list):
        return [_encode(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("RPC response object key is invalid")
        return {key: _encode(item, depth=depth + 1) for key, item in value.items()}
    raise ValueError(f"RPC response type is not allowed: {type(value).__name__}")


class _Worker:
    def __init__(self) -> None:
        self.hooks: ProtocolHooks | None = None
        self.gate: _AuditGate | None = None
        self.log: _BoundedLog | None = None
        self.allowed_methods: set[str] = set()

    def initialize(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.hooks is not None:
            raise RuntimeError("worker is already initialized")
        expected_keys = {
            "source_base64",
            "source_hash",
            "manifest_hash",
            "hook_methods",
            "supported_control_modes",
            "options",
            "role",
            "args",
            "state_dir",
            "decision_config",
        }
        if set(payload) != expected_keys:
            raise ValueError("worker initialization fields are invalid")
        source_base64 = payload["source_base64"]
        if not isinstance(source_base64, str):
            raise ValueError("worker hooks source is invalid")
        try:
            source = base64.b64decode(source_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("worker hooks source Base64 is invalid") from exc
        if hashlib.sha256(source).hexdigest() != payload["source_hash"]:
            raise ValueError("worker hooks source hash mismatch")
        inspection = inspect_hooks_source(source)
        if (
            list(inspection.methods) != payload["hook_methods"]
            or list(inspection.supported_control_modes)
            != payload["supported_control_modes"]
        ):
            raise ValueError("worker hooks inspection differs from manifest")
        state_dir_value = payload["state_dir"]
        if not isinstance(state_dir_value, str) or not Path(state_dir_value).is_absolute():
            raise ValueError("worker state_dir must be absolute")
        state_dir = Path(state_dir_value).resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
        cwd = state_dir / "remote-hooks-worker" / "cwd"
        cwd.mkdir(parents=True, exist_ok=True)
        os.chdir(cwd)
        for name in list(os.environ):
            if name not in {"PYTHONIOENCODING", "PYTHONUTF8", "TEMP", "TMP", "SYSTEMROOT", "WINDIR"}:
                os.environ.pop(name, None)
        self.log = _BoundedLog(state_dir / "remote-hooks-worker" / "hooks.log")
        sys.stdout = self.log  # type: ignore[assignment]
        sys.stderr = self.log  # type: ignore[assignment]
        gate = _AuditGate(state_dir)
        synthetic_filename = (
            f"<aigenora-host-bundle:{str(payload['manifest_hash'])[:16]}>"
        )
        gate.synthetic_filename = synthetic_filename
        self.gate = gate
        sys.addaudithook(gate)
        sys.dont_write_bytecode = True

        code = compile(source, synthetic_filename, "exec", dont_inherit=True)
        module_name = "aigenora_host_bundle_" + str(payload["manifest_hash"])[:16]
        module = types.ModuleType(module_name)
        module.__dict__.update(
            {
                "__builtins__": _safe_builtins(),
                "__file__": synthetic_filename,
                "__package__": None,
            }
        )
        exec(code, module.__dict__)
        hooks_class = module.__dict__.get("Hooks")
        if not isinstance(hooks_class, type):
            raise TypeError("remote hooks module does not define class Hooks")
        hooks = hooks_class()
        if not isinstance(hooks, ProtocolHooks):
            raise TypeError("remote Hooks must inherit ProtocolHooks")
        actual_modes = tuple(sorted(hooks.supported_control_modes()))
        if list(actual_modes) != payload["supported_control_modes"]:
            raise TypeError("remote Hooks control modes differ from manifest")
        hooks.proto_init(
            payload["options"],
            payload["role"],
            payload["args"],
            state_dir,
            payload["decision_config"],
        )
        self.hooks = hooks
        self.allowed_methods = set(_ALLOWED_CALLS)
        decision_schema = hooks.get_decision_schema()
        if not isinstance(decision_schema, dict):
            raise TypeError("remote Hooks decision schema must be an object")
        choice_keywords = getattr(hooks, "CHOICE_KEYWORDS", None)
        if choice_keywords is not None and not isinstance(choice_keywords, dict):
            raise TypeError("remote Hooks choice keywords must be an object")
        return {
            "status": "ready",
            "control_mode": hooks.control_mode,
            "supported_control_modes": list(actual_modes),
            "decision_schema": decision_schema or None,
            "choice_keywords": choice_keywords,
        }

    def call(self, payload: dict[str, Any]) -> Any:
        if self.hooks is None:
            raise RuntimeError("worker is not initialized")
        if set(payload) != {"method", "args", "kwargs"}:
            raise ValueError("worker call fields are invalid")
        method = payload["method"]
        args = payload["args"]
        kwargs = payload["kwargs"]
        if (
            not isinstance(method, str)
            or method not in self.allowed_methods
            or not isinstance(args, list)
            or not isinstance(kwargs, dict)
        ):
            raise ValueError("worker call is invalid")
        target = getattr(self.hooks, method, None)
        if not callable(target):
            raise AttributeError(f"remote Hooks does not implement {method}")
        return target(*args, **kwargs)

    def set_attribute(self, payload: dict[str, Any]) -> None:
        if self.hooks is None:
            raise RuntimeError("worker is not initialized")
        if set(payload) != {"name", "value"} or payload["name"] != "timing":
            raise ValueError("worker attribute update is invalid")
        self.hooks.timing = payload["value"]

    def snapshot_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.hooks is None:
            raise RuntimeError("worker is not initialized")
        if set(payload) != {"values"} or not isinstance(payload["values"], dict):
            raise ValueError("worker snapshot update is invalid")
        return self.hooks.snapshot.update(**payload["values"])

    def snapshot_set_phase(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.hooks is None:
            raise RuntimeError("worker is not initialized")
        if set(payload) != {"phase", "summary", "structured"}:
            raise ValueError("worker snapshot phase fields are invalid")
        phase = payload["phase"]
        summary = payload["summary"]
        structured = payload["structured"]
        if (
            not isinstance(phase, str)
            or (summary is not None and not isinstance(summary, str))
            or not isinstance(structured, dict)
        ):
            raise ValueError("worker snapshot phase values are invalid")
        return self.hooks.snapshot.set_phase(
            phase,
            summary=summary,
            **structured,
        )

    def shutdown(self, payload: dict[str, Any]) -> dict[str, bool]:
        if payload:
            raise ValueError("worker shutdown payload must be empty")
        return {"stopped": True}

    def close(self) -> None:
        if self.log is not None:
            self.log.flush()
            self.log.close()


def _loads_line(line: bytes) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    value = json.loads(line, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("request frame must be an object")
    return value


def _send(value: dict[str, Any]) -> None:
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
    if len(encoded) > MAX_FRAME_BYTES:
        raise RuntimeError("response frame exceeds limit")
    _CONTROL_OUT.write(encoded)
    _CONTROL_OUT.flush()


def main() -> int:
    worker = _Worker()
    try:
        while True:
            line = _CONTROL_IN.readline(MAX_FRAME_BYTES + 1)
            if not line:
                return 0
            if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                return 2
            request_id: Any = None
            try:
                request = _loads_line(line)
                if set(request) != {"id", "action", "payload"}:
                    raise ValueError("request fields are invalid")
                request_id = request["id"]
                if isinstance(request_id, bool) or not isinstance(request_id, int):
                    raise ValueError("request id is invalid")
                action = request["action"]
                payload = _decode(request["payload"])
                if not isinstance(action, str) or not isinstance(payload, dict):
                    raise ValueError("request action or payload is invalid")
                if action == "initialize":
                    result = worker.initialize(payload)
                elif action == "call":
                    result = worker.call(payload)
                elif action == "set_attribute":
                    result = worker.set_attribute(payload)
                elif action == "snapshot_update":
                    result = worker.snapshot_update(payload)
                elif action == "snapshot_set_phase":
                    result = worker.snapshot_set_phase(payload)
                elif action == "shutdown":
                    result = worker.shutdown(payload)
                else:
                    raise ValueError("unknown worker action")
                _send({"id": request_id, "ok": True, "result": _encode(result)})
                if action == "shutdown":
                    return 0
            except BaseException as exc:
                try:
                    _send(
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {
                                "type": type(exc).__name__,
                                "message": str(exc)[:500],
                            },
                        }
                    )
                except Exception:
                    return 3
    finally:
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
