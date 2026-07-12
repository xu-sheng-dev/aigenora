"""Business services shared by the legacy CLI and managed Runtime API.

Imports stay lazy so a health/catalog-only Sidecar does not import optional
network, cryptography, protocol execution, or identity modules at startup.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "ServiceContext": ("aigenora.services.context", "ServiceContext"),
    "IdentityService": ("aigenora.services.identity", "IdentityService"),
    "InvitationService": ("aigenora.services.invitation", "InvitationService"),
    "ProtocolService": ("aigenora.services.protocol", "ProtocolService"),
    "RegistryService": ("aigenora.services.registry", "RegistryService"),
    "SessionService": ("aigenora.services.session", "SessionService"),
    "SessionStateService": ("aigenora.services.session", "SessionStateService"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
