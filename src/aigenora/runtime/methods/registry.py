from __future__ import annotations

from typing import Any

from aigenora.services.registry import RegistryService


class RegistryMethods:
    def __init__(self, service: RegistryService):
        self._service = service

    def browse(self, params: dict[str, Any], _meta: dict[str, Any]) -> dict[str, Any]:
        items, total = self._service.browse(
            protocol_id=params.get("protocol_id"),
            invitation_type=params.get("invitation_type"),
            limit=params.get("limit"),
        )
        projected = []
        for item in items:
            try:
                projected.append(self._service.project_invitation(item))
            except ValueError:
                continue
            if len(projected) >= int(params.get("limit", 50)):
                break
        return {"invitations": projected, "total": total}
