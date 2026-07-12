from __future__ import annotations

import json
import math
import re
from typing import Any

from .errors import RuntimeMethodError
from .generated.v1.contracts import METHOD_CONTRACTS


_REQUEST_ID_RE = re.compile(r"^req_[A-Za-z0-9][A-Za-z0-9._-]{0,123}$")
_INSTANCE_ID_RE = re.compile(r"^runtime_[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_ORIGIN_ID_RE = re.compile(r"^origin_[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_SESSION_ID_RE = re.compile(r"^sess_[A-Za-z0-9][A-Za-z0-9._-]{0,122}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROTOCOL_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_FORBIDDEN_OUTPUT_KEYS = {
    "private_key",
    "private_key_hex",
    "secret_key",
    "credential",
    "token",
    "raw_frame",
    "protocol_dir",
    "data_dir",
    "server",
    "path",
}


_PARAM_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "runtime.hello": (
        {
            "harness_version",
            "supported_protocol_versions",
            "expected_schema_digest",
            "expected_catalog_digest",
            "expected_process_role",
            "required_methods",
            "minimum_security_profile",
        },
        set(),
    ),
    "runtime.health": (set(), set()),
    "runtime.describe": (set(), set()),
    "runtime.shutdown": (set(), {"grace_ms", "reason"}),
    "identity.describe": (set(), set()),
    "registry.browse": (set(), {"protocol_id", "invitation_type", "limit"}),
    "invitation.inspect": ({"post_id"}, set()),
    "protocol.catalog": (set(), set()),
    "protocol.inspect": ({"protocol_id"}, set()),
    "navigator.browse": (set(), {"alias", "family", "limit"}),
    "navigator.select": (set(), {"protocol_id", "alias", "family", "profile"}),
    "session.snapshot": ({"session_id"}, set()),
    "session.details": ({"session_id"}, {"after_sequence", "limit"}),
    "session.rating.read": ({"session_id"}, set()),
    "protocol.decision.submit": (
        {"session_id", "decision_kind", "expected_sequence"},
        {"choice", "number"},
    ),
    "protocol.strategy.get": ({"session_id"}, set()),
    "protocol.strategy.patch": (
        {"session_id", "expected_generation", "mode"},
        {"preferred_choice", "preferred_number", "policy", "supersedes"},
    ),
    "protocol.worker.open": (
        {"session_id", "protocol_id", "bundle_digest", "role", "profile", "options_json"},
        set(),
    ),
    "protocol.worker.step": (
        {
            "worker_id",
            "session_id",
            "protocol_id",
            "bundle_digest",
            "generation",
            "sequence",
            "pre_state_digest",
            "action_kind",
        },
        {"self_choice", "peer_choice", "self_number", "peer_number"},
    ),
    "protocol.worker.close": (
        {"worker_id", "session_id", "generation", "expected_state_digest"},
        set(),
    ),
}


def _invalid(message: str = "Runtime value failed schema validation") -> RuntimeMethodError:
    return RuntimeMethodError("validation.schema_invalid", message)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _scalar(value: object) -> bool:
    if value is None or isinstance(value, (bool, str)):
        return not isinstance(value, str) or len(value) <= 4096
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.isfinite(value) and abs(value) <= 1e300
    return False


def validate_bounded_payload(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 64:
        raise _invalid()
    for key, item in value.items():
        if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
            raise _invalid()
        if _scalar(item):
            continue
        if isinstance(item, list) and len(item) <= 64:
            if all(_scalar(member) for member in item):
                continue
            if all(
                isinstance(member, dict)
                and len(member) <= 64
                and all(
                    isinstance(child_key, str)
                    and _KEY_RE.fullmatch(child_key) is not None
                    and _scalar(child_value)
                    for child_key, child_value in member.items()
                )
                for member in item
            ):
                continue
        if isinstance(item, dict) and len(item) <= 64 and all(
            isinstance(child_key, str)
            and _KEY_RE.fullmatch(child_key) is not None
            and _scalar(child_value)
            for child_key, child_value in item.items()
        ):
            continue
        raise _invalid()
    return value


def validate_request_envelope(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "protocol_version",
        "request_id",
        "method",
        "params",
        "meta",
    }:
        raise _invalid("Runtime request envelope is invalid")
    if value.get("kind") != "request" or value.get("protocol_version") != "1":
        raise _invalid("Runtime request protocol is invalid")
    request_id = value.get("request_id")
    method = value.get("method")
    if not isinstance(request_id, str) or _REQUEST_ID_RE.fullmatch(request_id) is None:
        raise _invalid("Runtime request id is invalid")
    if not isinstance(method, str) or method not in METHOD_CONTRACTS:
        raise RuntimeMethodError("runtime.method_not_allowed", "Runtime method is not allowed")
    params = validate_bounded_payload(value.get("params"))
    raw_size = len(json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if raw_size > int(METHOD_CONTRACTS[method]["max_params_bytes"]):
        raise _invalid("Runtime method params exceed their byte limit")
    meta = value.get("meta")
    if not isinstance(meta, dict) or not {"deadline_ms"}.issubset(meta):
        raise _invalid("Runtime request metadata is invalid")
    if not set(meta).issubset({"deadline_ms", "origin_id", "session_id", "idempotency_key"}):
        raise _invalid("Runtime request metadata contains unknown fields")
    deadline = meta.get("deadline_ms")
    if not _is_integer(deadline) or not 1 <= deadline <= 300000:
        raise _invalid("Runtime request deadline is invalid")
    origin_id = meta.get("origin_id")
    session_id = meta.get("session_id")
    if origin_id is not None and (
        not isinstance(origin_id, str) or _ORIGIN_ID_RE.fullmatch(origin_id) is None
    ):
        raise _invalid("Runtime request origin is invalid")
    if session_id is not None and (
        not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None
    ):
        raise _invalid("Runtime request session is invalid")
    idempotency_key = meta.get("idempotency_key")
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not 1 <= len(idempotency_key) <= 128
        or re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$", idempotency_key) is None
    ):
        raise _invalid("Runtime idempotency key is invalid")
    validate_method_params(method, params)
    return value


def validate_method_params(method: str, params: dict[str, Any]) -> None:
    required, optional = _PARAM_FIELDS[method]
    if not required.issubset(params) or not set(params).issubset(required | optional):
        raise _invalid("Runtime method params contain missing or unknown fields")
    for key in ("session_id",):
        if key in params and (
            not isinstance(params[key], str) or _SESSION_ID_RE.fullmatch(params[key]) is None
        ):
            raise _invalid()
    for key in ("protocol_id",):
        if key in params and (
            not isinstance(params[key], str) or _PROTOCOL_ID_RE.fullmatch(params[key]) is None
        ):
            raise _invalid()
    for key in (
        "bundle_digest",
        "pre_state_digest",
        "expected_state_digest",
        "expected_schema_digest",
        "expected_catalog_digest",
    ):
        if key in params and (
            not isinstance(params[key], str) or _DIGEST_RE.fullmatch(params[key]) is None
        ):
            raise _invalid()
    for key in (
        "limit",
        "after_sequence",
        "expected_sequence",
        "expected_generation",
        "generation",
        "sequence",
        "self_number",
        "peer_number",
        "number",
        "preferred_number",
        "grace_ms",
    ):
        if key in params and not _is_integer(params[key]):
            raise _invalid()
    if method == "runtime.hello":
        if params["supported_protocol_versions"] != ["1"]:
            raise _invalid()
        if not isinstance(params["required_methods"], list) or not params["required_methods"]:
            raise _invalid()
        if not all(isinstance(item, str) and item in METHOD_CONTRACTS for item in params["required_methods"]):
            raise _invalid()
        if params["expected_process_role"] not in {"identity_sidecar", "protocol_worker"}:
            raise _invalid()
    if method == "navigator.select":
        if sum(name in params for name in ("protocol_id", "alias", "family")) != 1:
            raise _invalid()
    if method == "protocol.decision.submit":
        if params["decision_kind"] == "rps_choice":
            if params.get("choice") not in {"rock", "paper", "scissors"} or "number" in params:
                raise _invalid()
        elif params["decision_kind"] == "guess_number":
            if "number" not in params or "choice" in params:
                raise _invalid()
        else:
            raise _invalid()
    if method == "protocol.strategy.patch":
        mode = params["mode"]
        required_field = {
            "fixed": "preferred_choice",
            "numeric": "preferred_number",
            "policy": "policy",
        }.get(mode)
        if mode not in {"random", "fixed", "numeric", "policy"}:
            raise _invalid()
        if required_field is not None and required_field not in params:
            raise _invalid()
    if method == "protocol.worker.step":
        if params["action_kind"] == "rps_round":
            if params.get("self_choice") not in {"rock", "paper", "scissors"} or params.get(
                "peer_choice"
            ) not in {"rock", "paper", "scissors"}:
                raise _invalid()
        elif params["action_kind"] == "guess_attempt":
            if "self_number" not in params and "peer_number" not in params:
                raise _invalid()
        else:
            raise _invalid()


def validate_instance_id(value: object) -> str:
    if not isinstance(value, str) or _INSTANCE_ID_RE.fullmatch(value) is None:
        raise _invalid("Runtime instance id is invalid")
    return value


def validate_safe_result(value: object) -> dict[str, Any]:
    result = validate_bounded_payload(value)
    stack: list[object] = [result]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if key.lower() in _FORBIDDEN_OUTPUT_KEYS:
                    raise RuntimeMethodError(
                        "internal.runtime_failure", "Runtime output violated the response policy"
                    )
                stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)
    return result
