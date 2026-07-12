from __future__ import annotations

from typing import Any

from aigenora.runtime.catalog.loader import PinnedCatalog
from aigenora.runtime.registry import RuntimeHandler
from aigenora.services.context import ServiceContext
from aigenora.services.identity import IdentityService
from aigenora.services.invitation import InvitationService
from aigenora.services.protocol import ProtocolService
from aigenora.services.registry import RegistryService
from aigenora.services.session import SessionService

from .invitation import InvitationMethods
from .protocol import ProtocolMethods
from .registry import RegistryMethods
from .session import SessionMethods


def build_identity_handlers(
    context: ServiceContext, catalog: PinnedCatalog
) -> dict[str, RuntimeHandler]:
    identity = IdentityService(context)
    registry = RegistryMethods(RegistryService(context))
    invitation = InvitationMethods(InvitationService(context))
    protocol = ProtocolMethods(ProtocolService(catalog))
    session = SessionMethods(SessionService(context))

    def identity_describe(
        _params: dict[str, Any], _meta: dict[str, Any]
    ) -> dict[str, Any]:
        return identity.describe()

    # Explicit table: no getattr, import path, command string, URL, or reflection.
    return {
        "identity.describe": identity_describe,
        "registry.browse": registry.browse,
        "invitation.inspect": invitation.inspect,
        "protocol.catalog": protocol.catalog,
        "protocol.inspect": protocol.inspect,
        "navigator.browse": protocol.browse,
        "navigator.select": protocol.select,
        "session.snapshot": session.snapshot,
        "session.details": session.details,
        "session.rating.read": session.rating_read,
        "protocol.decision.submit": session.decision_submit,
        "protocol.strategy.get": session.strategy_get,
        "protocol.strategy.patch": session.strategy_patch,
    }
