"""Local participant control-mode contract.

Control mode describes how *this participant* produces game actions.  It is a
runtime choice and deliberately does not belong to ``spec.json`` or the protocol
hash.  Host and Guest resolve it independently.
"""
from __future__ import annotations

from typing import Any, Iterable


AUTONOMOUS = "autonomous"
HYBRID = "hybrid"
HUMAN = "human"

CONTROL_MODES: tuple[str, ...] = (AUTONOMOUS, HYBRID, HUMAN)
DEFAULT_CONTROL_MODE = HYBRID


class ControlModeError(ValueError):
    """Raised when a local control-mode selection is invalid or unsupported."""


def normalize_control_mode(value: Any) -> str | None:
    """Return a canonical control mode, or ``None`` for an omitted value."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ControlModeError("control mode must be a string")
    normalized = value.strip().lower()
    if normalized not in CONTROL_MODES:
        allowed = ", ".join(CONTROL_MODES)
        raise ControlModeError(f"control mode must be one of: {allowed}")
    return normalized


def resolve_control_mode(control_mode: Any = None, *, coach: bool = False) -> str:
    """Resolve the final local mode.

    ``--coach`` is retained only as a deprecated compatibility alias for
    ``--control-mode human``.  It may be combined with an explicit ``human`` but
    conflicts with either non-human mode.
    """
    explicit = normalize_control_mode(control_mode)
    if coach:
        if explicit is not None and explicit != HUMAN:
            raise ControlModeError(
                "--coach is an alias for --control-mode human and conflicts with "
                f"--control-mode {explicit}"
            )
        return HUMAN
    return explicit or DEFAULT_CONTROL_MODE


def control_mode_from_args(args: Any) -> str:
    return resolve_control_mode(
        getattr(args, "control_mode", None),
        coach=bool(getattr(args, "coach", False)),
    )


def declared_control_modes(hooks: Any) -> tuple[str, ...]:
    """Read and normalize a hooks object's explicit capability declaration."""
    provider = getattr(hooks, "supported_control_modes", None)
    raw: Iterable[Any]
    if callable(provider):
        raw = provider()
    else:
        raw = getattr(hooks, "SUPPORTED_CONTROL_MODES", (AUTONOMOUS, HYBRID))
    result: list[str] = []
    for item in raw or ():
        mode = normalize_control_mode(item)
        if mode is not None and mode not in result:
            result.append(mode)
    return tuple(result)


def ensure_control_mode_supported(hooks: Any, control_mode: str) -> None:
    supported = declared_control_modes(hooks)
    if control_mode not in supported:
        name = hooks.__class__.__name__
        modes = ", ".join(supported) if supported else "none"
        raise ControlModeError(
            f"protocol hooks {name} do not support control mode {control_mode!r}; "
            f"supported modes: {modes}"
        )


__all__ = [
    "AUTONOMOUS",
    "HYBRID",
    "HUMAN",
    "CONTROL_MODES",
    "DEFAULT_CONTROL_MODE",
    "ControlModeError",
    "normalize_control_mode",
    "resolve_control_mode",
    "control_mode_from_args",
    "declared_control_modes",
    "ensure_control_mode_supported",
]

