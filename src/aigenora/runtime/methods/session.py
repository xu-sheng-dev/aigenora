from __future__ import annotations

from typing import Any

from aigenora.services.session import SessionService

from aigenora.runtime.errors import RuntimeMethodError


def _assert_session_binding(params: dict[str, Any], meta: dict[str, Any]) -> None:
    session_id = params.get("session_id")
    if meta.get("session_id") != session_id:
        raise RuntimeMethodError("session.scope_mismatch", "Runtime session binding is invalid")
    if meta.get("origin_id") is None:
        raise RuntimeMethodError("session.scope_mismatch", "Runtime origin binding is required")


class SessionMethods:
    def __init__(self, service: SessionService):
        self._service = service

    def snapshot(self, params: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        _assert_session_binding(params, meta)
        return self._service.snapshot(params["session_id"])

    def details(self, params: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        _assert_session_binding(params, meta)
        return self._service.details(
            params["session_id"],
            after_sequence=int(params.get("after_sequence", -1)),
            limit=int(params.get("limit", 64)),
        )

    def rating_read(self, params: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        _assert_session_binding(params, meta)
        return self._service.rating_read(params["session_id"])

    def decision_submit(self, params: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        _assert_session_binding(params, meta)
        return self._service.submit_decision(
            params["session_id"],
            decision_kind=params["decision_kind"],
            expected_sequence=int(params["expected_sequence"]),
            choice=params.get("choice"),
            number=params.get("number"),
        )

    def strategy_get(self, params: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        _assert_session_binding(params, meta)
        return self._service.strategy_get(params["session_id"])

    def strategy_patch(self, params: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        _assert_session_binding(params, meta)
        return self._service.strategy_patch(
            params["session_id"],
            expected_generation=int(params["expected_generation"]),
            mode=params["mode"],
            preferred_choice=params.get("preferred_choice"),
            preferred_number=params.get("preferred_number"),
            policy=params.get("policy"),
            supersedes=params.get("supersedes"),
        )
