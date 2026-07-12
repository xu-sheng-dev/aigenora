from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import RuntimeMethodError
from .generated.v1.contracts import METHOD_CONTRACTS


RuntimeHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class RegisteredMethod:
    name: str
    handler: RuntimeHandler


class MethodRegistry:
    """Closed Runtime method table; no reflection or attribute lookup is used."""

    def __init__(self, allowed_methods: tuple[str, ...]):
        self._ordered_allowed = allowed_methods
        self._allowed = frozenset(allowed_methods)
        self._handlers: dict[str, RegisteredMethod] = {}

    def register(self, name: str, handler: RuntimeHandler) -> None:
        if name not in self._allowed or name not in METHOD_CONTRACTS:
            raise ValueError("method is not in the generated allowlist")
        if name.startswith("runtime."):
            raise ValueError("runtime lifecycle methods are owned by RuntimeServer")
        if name in self._handlers:
            raise ValueError("method is already registered")
        self._handlers[name] = RegisteredMethod(name, handler)

    def dispatch(
        self, name: str, params: dict[str, Any], meta: dict[str, Any]
    ) -> dict[str, Any]:
        registered = self._handlers.get(name)
        if registered is None:
            raise RuntimeMethodError("runtime.method_not_allowed", "Runtime method is not allowed")
        return registered.handler(params, meta)

    def descriptors(self) -> list[dict[str, object]]:
        descriptors: list[dict[str, object]] = []
        for name in self._ordered_allowed:
            contract = METHOD_CONTRACTS[name]
            descriptors.append(
                {
                    "name": name,
                    "security_level": contract["security_level"],
                    "idempotency": contract["idempotency"],
                    "cancellation": contract["cancellation"],
                    "max_params_bytes": contract["max_params_bytes"],
                }
            )
        return descriptors

    @property
    def allowed_methods(self) -> frozenset[str]:
        return self._allowed
