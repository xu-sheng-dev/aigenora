from __future__ import annotations

from typing import Any

from aigenora.services.invitation import InvitationService


class InvitationMethods:
    def __init__(self, service: InvitationService):
        self._service = service

    def inspect(self, params: dict[str, Any], _meta: dict[str, Any]) -> dict[str, Any]:
        return self._service.inspect_projection(params["post_id"])
