from __future__ import annotations

import copy
from typing import Any


MAX_DELTA_PATH_DEPTH = 32
MAX_DELTA_OPS = 1024


class JsonDeltaError(ValueError):
    """A JSON delta is malformed or cannot be applied to its base value."""


def make_json_delta(previous: Any, current: Any) -> list[dict[str, Any]]:
    """Build a deterministic, replayable delta from two JSON values."""
    operations: list[dict[str, Any]] = []
    _diff(previous, current, (), operations)
    if len(operations) > MAX_DELTA_OPS or any(
        len(operation["path"]) > MAX_DELTA_PATH_DEPTH
        for operation in operations
    ):
        return [{"op": "set", "path": [], "value": copy.deepcopy(current)}]
    return operations


def apply_json_delta(previous: Any, operations: Any) -> Any:
    """Apply a bounded delta without mutating the caller's base value."""
    if not isinstance(operations, list):
        raise JsonDeltaError("JSON delta must be an array")
    if len(operations) > MAX_DELTA_OPS:
        raise JsonDeltaError("JSON delta has too many operations")
    result = copy.deepcopy(previous)
    for operation in operations:
        if not isinstance(operation, dict):
            raise JsonDeltaError("JSON delta operation must be an object")
        kind = operation.get("op")
        path = _validate_path(operation.get("path"))
        if kind == "set":
            if set(operation) != {"op", "path", "value"}:
                raise JsonDeltaError("set operation has unknown fields")
            value = copy.deepcopy(operation["value"])
            if not path:
                result = value
            else:
                parent, token = _resolve_parent(result, path)
                _set_child(parent, token, value)
        elif kind == "remove":
            if set(operation) != {"op", "path"} or not path:
                raise JsonDeltaError("remove operation is invalid")
            parent, token = _resolve_parent(result, path)
            _remove_child(parent, token)
        elif kind == "splice":
            if set(operation) != {
                "op",
                "path",
                "index",
                "delete",
                "items",
            }:
                raise JsonDeltaError("splice operation has unknown fields")
            target = _resolve_value(result, path)
            index = operation.get("index")
            delete = operation.get("delete")
            items = operation.get("items")
            if not isinstance(target, list):
                raise JsonDeltaError("splice target must be an array")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not isinstance(delete, int)
                or isinstance(delete, bool)
                or index < 0
                or index > len(target)
                or delete < 0
                or delete > len(target) - index
                or not isinstance(items, list)
            ):
                raise JsonDeltaError("splice bounds are invalid")
            target[index : index + delete] = copy.deepcopy(items)
        else:
            raise JsonDeltaError("unsupported JSON delta operation")
    return result


def _diff(
    previous: Any,
    current: Any,
    path: tuple[str | int, ...],
    operations: list[dict[str, Any]],
) -> None:
    if previous == current:
        return
    if isinstance(previous, dict) and isinstance(current, dict):
        previous_keys = set(previous)
        current_keys = set(current)
        for key in sorted(previous_keys - current_keys):
            operations.append({"op": "remove", "path": [*path, key]})
        for key in sorted(previous_keys & current_keys):
            _diff(previous[key], current[key], (*path, key), operations)
        for key in sorted(current_keys - previous_keys):
            operations.append(
                {
                    "op": "set",
                    "path": [*path, key],
                    "value": copy.deepcopy(current[key]),
                }
            )
        return
    if isinstance(previous, list) and isinstance(current, list):
        _diff_list(previous, current, path, operations)
        return
    operations.append(
        {"op": "set", "path": list(path), "value": copy.deepcopy(current)}
    )


def _diff_list(
    previous: list[Any],
    current: list[Any],
    path: tuple[str | int, ...],
    operations: list[dict[str, Any]],
) -> None:
    prefix = 0
    limit = min(len(previous), len(current))
    while prefix < limit and previous[prefix] == current[prefix]:
        prefix += 1

    suffix = 0
    while (
        suffix < len(previous) - prefix
        and suffix < len(current) - prefix
        and previous[len(previous) - suffix - 1]
        == current[len(current) - suffix - 1]
    ):
        suffix += 1

    if prefix or suffix:
        previous_end = len(previous) - suffix
        current_end = len(current) - suffix
        operations.append(
            {
                "op": "splice",
                "path": list(path),
                "index": prefix,
                "delete": previous_end - prefix,
                "items": copy.deepcopy(current[prefix:current_end]),
            }
        )
        return

    # Preserve bounded FIFO histories efficiently: remove the expired prefix,
    # then append the new suffix instead of replacing the entire array.
    for drop in range(1, len(previous)):
        overlap = len(previous) - drop
        if (
            overlap > 0
            and overlap <= len(current)
            and previous[drop:] == current[:overlap]
        ):
            operations.append(
                {
                    "op": "splice",
                    "path": list(path),
                    "index": 0,
                    "delete": drop,
                    "items": [],
                }
            )
            if overlap < len(current):
                operations.append(
                    {
                        "op": "splice",
                        "path": list(path),
                        "index": overlap,
                        "delete": 0,
                        "items": copy.deepcopy(current[overlap:]),
                    }
                )
            return

    operations.append(
        {
            "op": "splice",
            "path": list(path),
            "index": 0,
            "delete": len(previous),
            "items": copy.deepcopy(current),
        }
    )


def _validate_path(value: Any) -> list[str | int]:
    if not isinstance(value, list) or len(value) > MAX_DELTA_PATH_DEPTH:
        raise JsonDeltaError("JSON delta path is invalid")
    path: list[str | int] = []
    for token in value:
        if isinstance(token, str):
            path.append(token)
        elif isinstance(token, int) and not isinstance(token, bool) and token >= 0:
            path.append(token)
        else:
            raise JsonDeltaError("JSON delta path token is invalid")
    return path


def _resolve_value(root: Any, path: list[str | int]) -> Any:
    current = root
    for token in path:
        if isinstance(current, dict) and isinstance(token, str):
            if token not in current:
                raise JsonDeltaError("JSON delta path does not exist")
            current = current[token]
        elif isinstance(current, list) and isinstance(token, int):
            if token >= len(current):
                raise JsonDeltaError("JSON delta array path is out of bounds")
            current = current[token]
        else:
            raise JsonDeltaError("JSON delta path type mismatch")
    return current


def _resolve_parent(root: Any, path: list[str | int]) -> tuple[Any, str | int]:
    return _resolve_value(root, path[:-1]), path[-1]


def _set_child(parent: Any, token: str | int, value: Any) -> None:
    if isinstance(parent, dict) and isinstance(token, str):
        parent[token] = value
        return
    if isinstance(parent, list) and isinstance(token, int) and token < len(parent):
        parent[token] = value
        return
    raise JsonDeltaError("JSON delta set target is invalid")


def _remove_child(parent: Any, token: str | int) -> None:
    if isinstance(parent, dict) and isinstance(token, str) and token in parent:
        del parent[token]
        return
    if isinstance(parent, list) and isinstance(token, int) and token < len(parent):
        del parent[token]
        return
    raise JsonDeltaError("JSON delta remove target is invalid")
