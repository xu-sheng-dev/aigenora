from __future__ import annotations

from typing import Any

from aigenora.services.protocol import ProtocolService


class ProtocolMethods:
    def __init__(self, service: ProtocolService):
        self._service = service

    def catalog(self, _params: dict[str, Any], _meta: dict[str, Any]) -> dict[str, Any]:
        return self._service.catalog()

    def inspect(self, params: dict[str, Any], _meta: dict[str, Any]) -> dict[str, Any]:
        return self._service.inspect(params["protocol_id"])

    def browse(self, params: dict[str, Any], _meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidates": self._service.browse(
                alias=params.get("alias"),
                family=params.get("family"),
                limit=int(params.get("limit", 32)),
            )
        }

    def select(self, params: dict[str, Any], _meta: dict[str, Any]) -> dict[str, Any]:
        return self._service.select(
            protocol_id=params.get("protocol_id"),
            alias=params.get("alias"),
            family=params.get("family"),
            profile=params.get("profile"),
        )
