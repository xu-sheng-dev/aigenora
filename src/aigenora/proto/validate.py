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
    _validate_shadow_judge(spec)
    return spec


def _validate_shadow_judge(spec: dict[str, Any]) -> None:
    """v015-M2: shadow_judge 若声明则必须为 bool（协议级开关，声明该协议启用 Guest 影子裁决）。

    shadow_judge 不进 protocol_id（protocol_contract 白名单不含它），属行为配置开关，
    故此处只做类型校验，不参与内容寻址 hash。
    """
    sj = spec.get("shadow_judge")
    if sj is not None and not isinstance(sj, bool):
        raise ValidationError("shadow_judge must be a boolean when present")


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
        errors.extend(_validate_field_value(field, msg[key], key))
    if errors:
        raise ValidationError("; ".join(errors))


def _validate_field_value(field: dict[str, Any], value: Any, key: str) -> list[str]:
    """Validate a single field value (scalar or array).

    Factored out of validate_message_obj in v016 so the new ``array`` machine type
    can recurse over its elements. v016 adds ciphertext / key / ot_blob / array
    alongside the existing scalar types; ``array.items`` must itself be a scalar
    type (no array-of-array), and elements are re-validated through this function.
    """
    errors: list[str] = []
    ftype = field.get("type")
    if ftype == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{key}: expected integer")
            return errors
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
    elif ftype == "ciphertext":
        # v016: AEAD ciphertext (blob_A / blob_B). Hex, even length, capped.
        max_len = field.get("max_length", 512)
        if (
            not isinstance(value, str)
            or len(value) > max_len
            or len(value) % 2 != 0
            or not re.fullmatch(r"[0-9a-f]+", value)
        ):
            errors.append(f"{key}: expected hex ciphertext <= {max_len} chars (even length)")
    elif ftype == "key":
        # v016: symmetric key disclosed at play time. Fixed 32 bytes (64 hex).
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            errors.append(f"{key}: expected 32-byte key (64 hex chars)")
    elif ftype == "ot_blob":
        # v016: OT payload (z / y / sealed payload / witness). Hex/JSON text, large cap.
        max_len = field.get("max_length", 16384)
        if not isinstance(value, str) or len(value.encode("utf-8")) > max_len:
            errors.append(f"{key}: expected ot_blob <= {max_len} UTF-8 bytes")
    elif ftype == "array":
        errors.extend(_validate_array_field(field, value, key))
    elif ftype == "json":
        errors.extend(_validate_json_field(field, value, key))
    else:
        errors.append(f"{key}: unsupported field type {ftype!r}")
    return errors


def _validate_array_field(field: dict[str, Any], value: Any, key: str) -> list[str]:
    """v016 structured machine-field list (e.g. 52 ciphertexts / sealed payloads).

    - ``items`` must be a scalar-type schema; array-of-array is rejected
      (recursion depth capped at 1, preventing DoS and implementation blowup).
    - ``min_items`` / ``max_items`` bound element count; ``max_total_bytes``
      (default 256 KiB) bounds aggregate size so a spec declaring huge
      ``max_items`` cannot manufacture a DoS payload.
    """
    errors: list[str] = []
    if not isinstance(value, list):
        errors.append(f"{key}: expected array")
        return errors
    items_schema = field.get("items")
    if not isinstance(items_schema, dict):
        errors.append(f"{key}: array requires an 'items' schema")
        return errors
    if items_schema.get("type") == "array":
        errors.append(f"{key}: nested array not allowed (items must be scalar)")
        return errors
    min_items = field.get("min_items")
    if isinstance(min_items, int) and not isinstance(min_items, bool) and len(value) < min_items:
        errors.append(f"{key}: below min_items {min_items}")
    max_items = field.get("max_items")
    if isinstance(max_items, int) and not isinstance(max_items, bool) and len(value) > max_items:
        errors.append(f"{key}: above max_items {max_items}")
    max_total = field.get("max_total_bytes", 262144)
    total = sum(len(str(v).encode("utf-8")) for v in value)
    if total > max_total:
        errors.append(f"{key}: array total {total} bytes exceeds max_total_bytes {max_total}")
    for i, v in enumerate(value):
        errors.extend(_validate_field_value(items_schema, v, f"{key}[{i}]"))
    return errors


def _validate_json_field(field: dict[str, Any], value: Any, key: str) -> list[str]:
    """Validate a bounded structured JSON field.

    Real-time protocols exchange world snapshots and command objects whose shape is
    owned by the protocol hook.  The generic message validator still enforces the
    outer container type, JSON serializability, finite numbers and a byte limit so a
    peer cannot bypass the transport envelope with an unbounded payload.
    """
    errors: list[str] = []
    container = field.get("container", "any")
    if container == "object" and not isinstance(value, dict):
        return [f"{key}: expected JSON object"]
    if container == "array" and not isinstance(value, list):
        return [f"{key}: expected JSON array"]
    if container not in ("any", "object", "array"):
        return [f"{key}: json container must be any, object, or array"]
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return [f"{key}: expected finite JSON value"]
    max_total = field.get("max_total_bytes", 1048576)
    if not isinstance(max_total, int) or isinstance(max_total, bool) or max_total < 1:
        errors.append(f"{key}: json max_total_bytes must be a positive integer")
    elif len(encoded) > max_total:
        errors.append(f"{key}: JSON payload {len(encoded)} bytes exceeds max_total_bytes {max_total}")
    return errors


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
        elif ptype == "text":
            max_len = schema.get("max_length", 2000)
            if not isinstance(value, str) or len(value.encode("utf-8")) > max_len:
                errors.append(f"{key}: expected text <= {max_len} UTF-8 bytes")
        elif ptype == "table":
            _validate_table(value, schema, key, errors)
    if errors:
        raise ValidationError("; ".join(errors))


def _validate_table(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate a ``table`` parameter (v015): a constrained nested numeric table.

    A balance table is declarative data (not code); its structure is pinned by the
    ``schema.columns`` field-tree whitelist. Leaf nodes are integer/boolean/enum
    (with optional min/max/values); interior nodes use ``object`` + ``fields``
    recursion. Extra fields, missing fields, wrong types, and out-of-range values
    are appended to ``errors``. See v015 ADR-2/5.
    """
    columns = schema.get("columns")
    if not isinstance(columns, dict) or not columns:
        errors.append(f"{path}: table schema requires a non-empty 'columns' field-tree")
        return
    if not isinstance(value, dict):
        errors.append(f"{path}: expected a keyed table object")
        return
    # Each keyed row is validated against the shared columns field-tree.
    for row_key, row_val in value.items():
        _validate_table_fields(row_val, columns, f"{path}.{row_key}", errors)


def _validate_table_fields(value: Any, fields: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate an object/table-row against a field-tree.

    Shared by table rows (against ``columns``) and ``object`` nodes (against their
    ``fields``). Table fields default to required — a balance table declares every
    value completely; set ``required: false`` to allow omission (differs from message
    fields, which default to optional, because a balance table emphasizes
    completeness). Unknown keys are rejected (whitelist).
    """
    if not isinstance(value, dict):
        errors.append(f"{path}: expected object")
        return
    for k in value:
        if k not in fields:
            errors.append(f"{path}: unknown field {k!r}")
    for k, fs in fields.items():
        required = fs.get("required", True) if isinstance(fs, dict) else True
        if k not in value:
            if required:
                errors.append(f"{path}: missing field {k!r}")
            continue
        _validate_table_node(value[k], fs, f"{path}.{k}", errors)


def _validate_table_node(value: Any, node_schema: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate a single field value against its leaf/``object`` node schema."""
    if not isinstance(node_schema, dict):
        errors.append(f"{path}: invalid node schema")
        return
    ntype = node_schema.get("type")
    if ntype == "object":
        fields = node_schema.get("fields")
        if not isinstance(fields, dict):
            errors.append(f"{path}: object node requires a 'fields' field-tree")
            return
        _validate_table_fields(value, fields, path, errors)
    elif ntype == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{path}: expected integer")
        else:
            if "min" in node_schema and value < node_schema["min"]:
                errors.append(f"{path}: below min {node_schema['min']}")
            if "max" in node_schema and value > node_schema["max"]:
                errors.append(f"{path}: above max {node_schema['max']}")
    elif ntype == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{path}: expected boolean")
    elif ntype == "enum":
        if not isinstance(value, str) or value not in node_schema.get("values", []):
            errors.append(f"{path}: invalid enum")
    else:
        errors.append(f"{path}: unsupported node type {ntype!r}")


# flow.mode set implemented by the engine at the P0 stage (excluding placeholders other than the session_loop default).
# Subsequent stages extend this: P2 adds "request_response", P3 adds "simultaneous_round".
# v016 adds "mental_poker" (layered AEAD + Blind-RSA token OT fair-dealing engine).
IMPLEMENTED_FLOW_MODES: tuple[str, ...] = (
    "session_loop", "free", "request_response", "simultaneous_round", "mental_poker",
    "authoritative_realtime", "authoritative_group",
)

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
    elif mode == "authoritative_realtime":
        _validate_authoritative_realtime(flow)
    elif mode == "authoritative_group":
        _validate_authoritative_group(flow)
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


def _validate_authoritative_realtime(flow: dict[str, Any]) -> None:
    """Validate Host-authoritative real-time transport settings.

    These values affect wire timing and command acceptance, so they are part of the
    protocol contract rather than local preferences.
    """
    realtime = flow.get("realtime")
    if not isinstance(realtime, dict):
        raise ValidationError(
            "flow.realtime must be an object when mode=authoritative_realtime"
        )
    allowed = {
        "tick_rate_hz",
        "input_delay_ticks",
        "snapshot_every_ticks",
        "max_command_lead_ticks",
        "max_commands_per_frame",
        "disconnect_policy",
    }
    unknown = sorted(set(realtime) - allowed)
    if unknown:
        raise ValidationError(f"flow.realtime has unknown fields: {unknown!r}")
    integer_ranges = {
        "tick_rate_hz": (1, 60),
        "input_delay_ticks": (1, 120),
        "snapshot_every_ticks": (1, 60),
        "max_command_lead_ticks": (1, 600),
        "max_commands_per_frame": (1, 1024),
    }
    for key, (minimum, maximum) in integer_ranges.items():
        value = realtime.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"flow.realtime.{key} must be an integer")
        if value < minimum or value > maximum:
            raise ValidationError(
                f"flow.realtime.{key} must be between {minimum} and {maximum}"
            )
    if realtime["max_command_lead_ticks"] < max(1, realtime["input_delay_ticks"]):
        raise ValidationError(
            "flow.realtime.max_command_lead_ticks must be >= input_delay_ticks"
        )
    if realtime.get("disconnect_policy") not in ("abort", "continue"):
        raise ValidationError(
            "flow.realtime.disconnect_policy must be 'abort' or 'continue'"
        )


def _validate_authoritative_group(flow: dict[str, Any]) -> None:
    """Validate Host-authoritative multiplayer membership and recovery policy."""
    group = flow.get("group")
    if not isinstance(group, dict):
        raise ValidationError(
            "flow.group must be an object when mode=authoritative_group"
        )
    allowed = {
        "min_participants",
        "max_participants",
        "allow_late_join",
        "recovery_mode",
        "start_policy",
        "checkpoint_every_events",
        "max_action_bytes",
        "max_events_per_action",
        "peer_channels",
    }
    unknown = sorted(set(group) - allowed)
    if unknown:
        raise ValidationError(f"flow.group has unknown fields: {unknown!r}")
    minimum = group.get("min_participants")
    maximum = group.get("max_participants")
    if not isinstance(minimum, int) or isinstance(minimum, bool):
        raise ValidationError("flow.group.min_participants must be an integer")
    if not isinstance(maximum, int) or isinstance(maximum, bool):
        raise ValidationError("flow.group.max_participants must be an integer")
    if minimum < 2 or minimum > maximum or maximum > 32:
        raise ValidationError(
            "flow.group participant bounds must satisfy 2 <= min <= max <= 32"
        )
    if not isinstance(group.get("allow_late_join"), bool):
        raise ValidationError("flow.group.allow_late_join must be boolean")
    if group.get("recovery_mode") not in ("exact", "restart_round", "abort"):
        raise ValidationError(
            "flow.group.recovery_mode must be exact, restart_round, or abort"
        )
    if group.get("start_policy") not in ("min_ready", "full", "fixed_full"):
        raise ValidationError(
            "flow.group.start_policy must be min_ready, full, or fixed_full"
        )
    checkpoint_every_events = group.get("checkpoint_every_events")
    if checkpoint_every_events is not None:
        if (
            not isinstance(checkpoint_every_events, int)
            or isinstance(checkpoint_every_events, bool)
        ):
            raise ValidationError(
                "flow.group.checkpoint_every_events must be an integer"
            )
        if not 1 <= checkpoint_every_events <= 256:
            raise ValidationError(
                "flow.group.checkpoint_every_events must be between 1 and 256"
            )
    optional_integer_ranges = {
        "max_action_bytes": (256, 65536),
        "max_events_per_action": (1, 256),
    }
    for key, (lower, upper) in optional_integer_ranges.items():
        value = group.get(key)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"flow.group.{key} must be an integer")
        if value < lower or value > upper:
            raise ValidationError(
                f"flow.group.{key} must be between {lower} and {upper}"
            )
    _validate_group_peer_channels(group.get("peer_channels"))


def _validate_group_peer_channels(value: Any) -> None:
    """Validate the optional Member-to-Member side-channel contract."""
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValidationError("flow.group.peer_channels must be an object")
    allowed = {"enabled", "routing", "channels", "max_message_bytes"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(
            f"flow.group.peer_channels has unknown fields: {unknown!r}"
        )
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise ValidationError("flow.group.peer_channels.enabled must be boolean")
    if not enabled:
        if set(value) != {"enabled"}:
            raise ValidationError(
                "disabled flow.group.peer_channels must contain only enabled"
            )
        return
    if value.get("routing") not in ("all_members", "hook"):
        raise ValidationError(
            "flow.group.peer_channels.routing must be all_members or hook"
        )
    channels = value.get("channels")
    if not isinstance(channels, list) or not 1 <= len(channels) <= 16:
        raise ValidationError(
            "flow.group.peer_channels.channels must contain 1 to 16 names"
        )
    normalized: set[str] = set()
    slug_characters = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
    for channel in channels:
        if (
            not isinstance(channel, str)
            or not 1 <= len(channel) <= 32
            or channel[0] not in "abcdefghijklmnopqrstuvwxyz"
            or any(character not in slug_characters for character in channel)
        ):
            raise ValidationError(
                "flow.group.peer_channels channel names must be lowercase slugs"
            )
        if channel in normalized:
            raise ValidationError(
                "flow.group.peer_channels channel names must be unique"
            )
        normalized.add(channel)
    maximum = value.get("max_message_bytes", 16384)
    if not isinstance(maximum, int) or isinstance(maximum, bool):
        raise ValidationError(
            "flow.group.peer_channels.max_message_bytes must be an integer"
        )
    if not 256 <= maximum <= 65536:
        raise ValidationError(
            "flow.group.peer_channels.max_message_bytes must be between 256 and 65536"
        )


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
