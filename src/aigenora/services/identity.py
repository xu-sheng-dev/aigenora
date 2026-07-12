from __future__ import annotations

from .context import ServiceContext


class IdentityService:
    def __init__(self, context: ServiceContext):
        self._context = context

    def describe(self) -> dict[str, object]:
        try:
            keys = self._context.keys()
        except FileNotFoundError:
            return {"configured": False}
        return {"configured": True, "public_key": keys.public_key}
