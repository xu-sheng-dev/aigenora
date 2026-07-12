from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from .context import ServiceContext


class RegistryService:
    def __init__(self, context: ServiceContext):
        self._context = context

    def browse(
        self,
        *,
        protocol_id: str | None = None,
        invitation_type: str | None = None,
        tags: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        query = {
            key: value
            for key, value in {
                "protocol_id": protocol_id,
                "type": invitation_type,
                "tags": tags,
                "limit": limit,
            }.items()
            if value is not None
        }
        path = "/api/v1/invitations"
        if query:
            path += "?" + urlencode(query)
        data = self._context.rest().json("GET", path, expected={200})
        if isinstance(data, list):
            items = data
            total = len(items)
        elif isinstance(data, dict):
            raw_items = data.get("results", [])
            items = raw_items if isinstance(raw_items, list) else []
            raw_total = data.get("total", len(items))
            total = int(raw_total) if isinstance(raw_total, int) and raw_total >= 0 else len(items)
        else:
            raise ValueError("registry response must be an object or array")
        if len(items) > 100:
            raise ValueError("registry response exceeds the service limit")
        if not all(isinstance(item, dict) for item in items):
            raise ValueError("registry response contains a non-object invitation")
        return [dict(item) for item in items], total

    @staticmethod
    def project_invitation(item: dict[str, Any]) -> dict[str, object]:
        protocol_id = item.get("protocol_id")
        public_key = item.get("public_key") or item.get("participant_public_key")
        invitation_type = item.get("type", "chat")
        if not isinstance(protocol_id, str) or len(protocol_id) != 64:
            raise ValueError("invitation protocol_id is invalid")
        if not isinstance(public_key, str) or len(public_key) != 64:
            raise ValueError("invitation public_key is invalid")
        if invitation_type not in {"supply", "demand", "chat"}:
            raise ValueError("invitation type is invalid")
        return {
            "post_id": str(item.get("post_id", ""))[:128],
            "protocol_id": protocol_id,
            "invitation_type": invitation_type,
            "status": str(item.get("status", "active"))[:32],
            "participant_public_key": public_key,
            "message": str(item.get("message", ""))[:512],
        }
