"""Dependency-free static contract checks for received hooks source bytes."""
from __future__ import annotations

import ast
from dataclasses import dataclass


MAX_HOOKS_SIZE = 256 * 1024
_ALLOWED_CONTROL_MODES = frozenset({"autonomous", "hybrid", "human"})
_ALLOWED_HOOK_IMPORTS = frozenset(
    {
        "__future__",
        "aigenora.proto.hooks",
        "aigenora.proto.hidden_role",
        "aigenora.proto.mental_poker",
        "aigenora.proto.sdk",
        "aigenora.proto.whisper_bridge",
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
_FORBIDDEN_CALL_NAMES = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "eval",
        "exec",
        "input",
        "open",
    }
)


class HooksContractError(RuntimeError):
    """The received hooks source violates the executable bundle contract."""


@dataclass(frozen=True)
class HooksInspection:
    methods: tuple[str, ...]
    supported_control_modes: tuple[str, ...]


def _import_allowed(module_name: str) -> bool:
    if module_name in _ALLOWED_HOOK_IMPORTS:
        return True
    return any(module_name.startswith(prefix + ".") for prefix in _ALLOWED_HOOK_IMPORTS)


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def inspect_hooks_source(source: bytes) -> HooksInspection:
    if len(source) > MAX_HOOKS_SIZE:
        raise HooksContractError(f"hooks.py exceeds {MAX_HOOKS_SIZE} bytes")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HooksContractError("hooks.py must be strict UTF-8") from exc
    if text.startswith("\ufeff"):
        raise HooksContractError("hooks.py must not contain a UTF-8 BOM")
    try:
        tree = ast.parse(text, filename="hooks.py", mode="exec")
    except SyntaxError as exc:
        raise HooksContractError(f"hooks.py syntax error: {exc.msg}") from exc

    hooks_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Hooks"
    ]
    if len(hooks_classes) != 1:
        raise HooksContractError(
            "hooks.py must define exactly one top-level class Hooks"
        )
    hooks_class = hooks_classes[0]
    if not any(_base_name(base) == "ProtocolHooks" for base in hooks_class.bases):
        raise HooksContractError("Hooks must inherit ProtocolHooks")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _import_allowed(alias.name):
                    raise HooksContractError(
                        f"hooks.py import is not allowed: {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise HooksContractError("hooks.py relative imports are not allowed")
            module_name = node.module or ""
            if module_name == "aigenora.proto":
                imported = {alias.name for alias in node.names}
                if imported != {"mental_poker"}:
                    raise HooksContractError(
                        "hooks.py may import only mental_poker from aigenora.proto"
                    )
            elif not _import_allowed(module_name):
                raise HooksContractError(
                    f"hooks.py import is not allowed: {module_name}"
                )
        elif isinstance(node, ast.Call):
            call_name = _base_name(node.func)
            if call_name in _FORBIDDEN_CALL_NAMES:
                raise HooksContractError(
                    f"hooks.py call is not allowed: {call_name}"
                )

    methods = tuple(
        sorted(
            node.name
            for node in hooks_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (
                node.name.startswith("proto_")
                or node.name in {"build_decision_context", "run_policy"}
            )
        )
    )
    supported_modes = ("autonomous", "hybrid")
    for node in hooks_class.body:
        target_name = ""
        value_node: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                target_name = node.targets[0].id
                value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value_node = node.value
        if target_name != "SUPPORTED_CONTROL_MODES" or value_node is None:
            continue
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError) as exc:
            raise HooksContractError(
                "SUPPORTED_CONTROL_MODES must be a literal list or tuple"
            ) from exc
        if (
            not isinstance(value, (list, tuple))
            or not value
            or any(not isinstance(item, str) for item in value)
            or any(item not in _ALLOWED_CONTROL_MODES for item in value)
            or len(set(value)) != len(value)
        ):
            raise HooksContractError("SUPPORTED_CONTROL_MODES is invalid")
        supported_modes = tuple(value)
        break
    return HooksInspection(
        methods=methods,
        supported_control_modes=tuple(sorted(supported_modes)),
    )


__all__ = [
    "HooksContractError",
    "HooksInspection",
    "MAX_HOOKS_SIZE",
    "inspect_hooks_source",
]
