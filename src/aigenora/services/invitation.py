from __future__ import annotations

from typing import Any

from .context import ServiceContext
from .registry import RegistryService


class InvitationService:
    def __init__(self, context: ServiceContext):
        self._context = context

    def inspect(self, post_id: str) -> dict[str, Any]:
        if not post_id or len(post_id) > 128:
            raise ValueError("post_id is invalid")
        data = self._context.rest().json(
            "GET", f"/api/v1/invitations/{post_id}", expected={200}
        )
        if not isinstance(data, dict):
            raise ValueError("invitation response must be an object")
        return dict(data)

    def inspect_projection(self, post_id: str) -> dict[str, object]:
        return RegistryService.project_invitation(self.inspect(post_id))
