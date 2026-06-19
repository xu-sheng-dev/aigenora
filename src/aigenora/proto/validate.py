from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class ValidationError(ValueError):
    pass


def load_spec(spec_file: str | Path) -> dict[str, Any]:
    with Path(spec_file).open("r", encoding="utf-8") as f:
        spec = json.load(f)
    validate_timing(spec)
    validate_flow(spec)
    return spec


def validate_extra_args(spec: dict[str, Any], extra_args: list[str] | None) -> None:
    """Reject passing extra_args under non-manual protocols to avoid crashing after the P2P handshake.

    The CLI entries `aigenora join <post_id> [extra_args...]` / `aigenora host ... [extra_args...]`
    historically forward extra_args to hooks.proto_init. But only the manual decision mode consumes these args;
    passing extra_args in other modes causes hooks to return an invalid message on the first move,
    crashing only after the P2P handshake.
    """
    if not extra_args:
        return
    decision = spec.get("decision") or {}
    mode = decision.get("mode", "auto") if isinstance(decision, dict) else "auto"
    if mode == "manual":
        return
    raise ValidationError(
        f"protocol decision mode is '{mode}'; extra_args {extra_args!r} not accepted. "
        "Hint: omit positional args after post_id; for interactive decisions use --coach."
    )


def _direction_matches(schema_dir: str | None, expected: str | None) -> bool:
    return not expected or schema_dir == "both" or schema_dir == expected


def find_message_schema(
    spec: dict[str, Any], msg: dict[str, Any], direction: str | None = None, message_name: str | None = None
) -> dict[str, Any]:
    messages = spec.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValidationError("spec.messages must be a non-empty array")
    action = msg.get("action")
    candidates = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields") or {}
        if message_name:
            if item.get("name") == message_name and _direction_matches(item.get("direction"), direction):
                candidates.append(item)
            continue
        action_schema = fields.get("action") if isinstance(fields, dict) else None
        values = action_schema.get("values", []) if isinstance(action_schema, dict) else []
        if action in values and _direction_matches(item.get("direction"), direction):
            candidates.append(item)
    if not candidates:
        suffix = f" direction={direction}" if direction else ""
        raise ValidationError(f"message schema not found for action={action!r}{suffix}")
    if len(candidates) > 1:
        raise ValidationError("ambiguous message schema; pass message_name")
    return candidates[0]


def validate_message_obj(
    spec: dict[str, Any], msg: dict[str, Any], direction: str | None = None, message_name: str | None = None
) -> None:
    if not isinstance(msg, dict):
        raise ValidationError("message must be a JSON object")
    schema = find_message_schema(spec, msg, direction, message_name)
    fields = schema.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValidationError(f"message {schema.get('name')} has no fields schema")
    errors: list[str] = []
    for key in msg:
        if key not in fields:
            errors.append(f"unknown field: {key}")
    for key, field in fields.items():
        if not isinstance(field, dict):
            errors.append(f"field schema must be object: {key}")
            continue
        if field.get("required") is True and key not in msg:
            errors.append(f"missing required field: {key}")
            continue
        if key not in msg:
            continue
        value = msg[key]
        ftype = field.get("type")
        if ftype == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{key}: expected integer")
                continue
            if "min" in field and value < field["min"]:
                errors.append(f"{key}: below min {field['min']}")
            if "max" in field and value > field["max"]:
                errors.append(f"{key}: above max {field['max']}")
        elif ftype == "enum":
            values = field.get("values", [])
            if not isinstance(value, str):
                errors.append(f"{key}: expected enum string")
            elif value not in values:
                errors.append(f"{key}: value {value!r} not in enum")
        elif ftype == "boolean":
            if not isinstance(value, bool):
                errors.append(f"{key}: expected boolean")
        elif ftype == "hash":
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                errors.append(f"{key}: expected lowercase SHA256 hex")
        elif ftype == "nonce":
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{16,64}", value):
                errors.append(f"{key}: expected 16-64 lowercase hex chars")
        elif ftype == "id":
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                errors.append(f"{key}: expected safe identifier")
        elif ftype == "signature":
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{128}", value):
                errors.append(f"{key}: expected lowercase Ed25519 signature hex")
        elif ftype == "ticket":
            if not isinstance(value, str) or not value or len(value) > 2048:
                errors.append(f"{key}: expected non-empty ticket <= 2048 chars")
        elif ftype == "text":
            max_len = field.get("max_length", 2000)
            if not isinstance(value, str) or len(value.encode("utf-8")) > max_len:
                errors.append(f"{key}: expected text <= {max_len} UTF-8 bytes")
        else:
            errors.append(f"{key}: unsupported field type {ftype!r}")
    if errors:
        raise ValidationError("; ".join(errors))


def validate_options(spec: dict[str, Any], options: dict[str, Any]) -> None:
    params = spec.get("parameters") or {}
    if not isinstance(params, dict):
        return
    errors: list[str] = []
    for key, schema in params.items():
        if key not in options or not isinstance(schema, dict):
            continue
        value = options[key]
        ptype = schema.get("type")
        if ptype == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{key}: expected integer")
            elif ("min" in schema and value < schema["min"]) or ("max" in schema and value > schema["max"]):
                errors.append(f"{key}: value out of range")
        elif ptype == "boolean" and not isinstance(value, bool):
            errors.append(f"{key}: expected boolean")
        elif ptype == "enum":
            if not isinstance(value, str) or value not in schema.get("values", []):
                errors.append(f"{key}: invalid enum")
    if errors:
        raise ValidationError("; ".join(errors))


# flow.mode set implemented by the engine at the P0 stage (excluding placeholders other than the session_loop default).
# Subsequent stages extend this: P2 adds "request_response", P3 adds "simultaneous_round".
IMPLEMENTED_FLOW_MODES: tuple[str, ...] = ("session_loop", "free", "request_response", "simultaneous_round")

# repeat field values allowed at the P1 stage (see docs/design/flow-modes.md §3 and the P1 design).
# Unlisted values (e.g. forever / five / while true) fail early as spec errors,
# with no silent fallback.
ALLOWED_REPEAT_VALUES: tuple[str, ...] = ("best_of", "total_rounds", "until game_over")


def resolve_flow_mode(spec: dict[str, Any]) -> str:
    """Unified rules for resolving flow.mode.

    - flow field missing or not an object -> "session_loop"
    - flow.mode missing or empty -> "session_loop"
    - flow.mode not in the set implemented at the current stage -> ValidationError
    """
    flow = spec.get("flow")
    if not isinstance(flow, dict):
        return "session_loop"
    mode = flow.get("mode")
    if mode is None or mode == "":
        return "session_loop"
    if not isinstance(mode, str):
        raise ValidationError("flow.mode must be a string")
    if mode not in IMPLEMENTED_FLOW_MODES:
        raise ValidationError(
            f"unsupported flow.mode: {mode!r}; "
            f"current stage allows {sorted(IMPLEMENTED_FLOW_MODES)!r}"
        )
    return mode


def validate_flow(spec: dict[str, Any]) -> None:
    """P0 minimal validation:

    - flow, if present, must be an object.
    - flow.mode, if present, must be a string and belong to the implemented set.
    - flow.phases, if present, must be an array.
    P1 additions:
    - flow.phases[].repeat, if present, must fall within ALLOWED_REPEAT_VALUES;
      unknown values (forever / five / while true, etc.) raise ValidationError,
      with no silent fallback.
    Other fields (round, exchange, etc.) will receive stricter rules in later stages.
    """
    flow = spec.get("flow")
    if flow is None:
        return
    if not isinstance(flow, dict):
        raise ValidationError("flow must be an object")
    if "mode" in flow:
        resolve_flow_mode(spec)
    # P3 addition: simultaneous_round must have flow.round.value_field / value_type_ref
    mode = flow.get("mode") if isinstance(flow.get("mode"), str) else None
    if mode == "simultaneous_round":
        _validate_simultaneous_round(flow)
    phases = flow.get("phases")
    if phases is None:
        return
    if not isinstance(phases, list):
        raise ValidationError("flow.phases must be an array")
    for idx, phase in enumerate(phases):
        if not isinstance(phase, dict):
            continue
        if "repeat" not in phase:
            continue
        repeat = phase["repeat"]
        if not isinstance(repeat, str) or repeat not in ALLOWED_REPEAT_VALUES:
            raise ValidationError(
                f"flow.phases[{idx}].repeat must be one of "
                f"{list(ALLOWED_REPEAT_VALUES)!r}, got {repeat!r}"
            )


def _validate_simultaneous_round(flow: dict[str, Any]) -> None:
    round_spec = flow.get("round")
    if not isinstance(round_spec, dict):
        raise ValidationError("flow.round must be an object when mode=simultaneous_round")
    value_field = round_spec.get("value_field")
    if not isinstance(value_field, str) or not value_field:
        raise ValidationError("flow.round.value_field must be a non-empty string")
    value_type_ref = round_spec.get("value_type_ref")
    if not isinstance(value_type_ref, str) or not value_type_ref:
        raise ValidationError("flow.round.value_type_ref must be a non-empty string")


def validate_timing(spec: dict[str, Any]) -> None:
    """Validate the spec.timing field (v004 round timing mechanism)."""
    timing = spec.get("timing")
    if timing is None:
        return
    if not isinstance(timing, dict):
        raise ValidationError("timing must be an object")
    errors: list[str] = []
    mode = timing.get("mode")
    if mode not in ("simultaneous", "sequential", "none"):
        errors.append("timing.mode must be simultaneous, sequential, or none")
    for key in ("min_think_seconds", "max_think_seconds"):
        val = timing.get(key)
        if val is not None:
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                errors.append(f"timing.{key} must be a non-negative integer")
    min_s = timing.get("min_think_seconds")
    max_s = timing.get("max_think_seconds")
    if isinstance(min_s, int) and isinstance(max_s, int) and max_s < min_s:
        errors.append("timing.max_think_seconds must be >= timing.min_think_seconds")
    if errors:
        raise ValidationError("; ".join(errors))

