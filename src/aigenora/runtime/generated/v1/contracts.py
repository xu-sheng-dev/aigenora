"""Generated from the Harness Runtime API v1 machine truth.

Do not add methods here without updating ``aigenora-harness/contracts/runtime-api``
and regenerating the TypeScript bindings first.
"""

from __future__ import annotations


RUNTIME_SCHEMA_DIGEST = (
    "sha256:5a8a5b604cdad87c38d22260aab222a12035111ddfd6142fe30194589d006a38"
)


def _contract(
    params: str,
    result: str,
    security_level: str,
    idempotency: str,
    cancellation: str,
    timeout_ms: int,
    max_params_bytes: int,
) -> dict[str, object]:
    return {
        "params": params,
        "result": result,
        "security_level": security_level,
        "idempotency": idempotency,
        "cancellation": cancellation,
        "timeout_ms": timeout_ms,
        "max_params_bytes": max_params_bytes,
    }


METHOD_CONTRACTS: dict[str, dict[str, object]] = {
    "runtime.hello": _contract("runtime-hello.v1#RuntimeHelloParams", "runtime-hello.v1#RuntimeHelloResult", "read_local", "not_applicable", "not_applicable", 10000, 16384),
    "runtime.health": _contract("runtime-health.v1#RuntimeHealthParams", "runtime-health.v1#RuntimeHealthResult", "read_local", "not_applicable", "best_effort", 5000, 2),
    "runtime.describe": _contract("runtime-describe.v1#RuntimeDescribeParams", "runtime-describe.v1#RuntimeDescribeResult", "read_local", "not_applicable", "best_effort", 5000, 2),
    "runtime.shutdown": _contract("runtime-shutdown.v1#RuntimeShutdownParams", "runtime-shutdown.v1#RuntimeShutdownResult", "session_lifecycle", "optional", "before_side_effect", 30000, 1024),
    "identity.describe": _contract("protocol-session.v1#IdentityDescribeParams", "protocol-session.v1#IdentityDescribeResult", "identity_sensitive", "not_applicable", "best_effort", 5000, 2),
    "registry.browse": _contract("protocol-session.v1#RegistryBrowseParams", "protocol-session.v1#RegistryBrowseResult", "network_read", "not_applicable", "best_effort", 30000, 2048),
    "invitation.inspect": _contract("protocol-session.v1#InvitationInspectParams", "protocol-session.v1#InvitationInspectResult", "network_read", "not_applicable", "best_effort", 30000, 1024),
    "protocol.catalog": _contract("protocol-session.v1#ProtocolCatalogParams", "protocol-session.v1#ProtocolCatalogResult", "read_local", "not_applicable", "best_effort", 5000, 2),
    "protocol.inspect": _contract("protocol-session.v1#ProtocolInspectParams", "protocol-session.v1#ProtocolInspectResult", "read_local", "not_applicable", "best_effort", 5000, 1024),
    "navigator.browse": _contract("protocol-session.v1#NavigatorBrowseParams", "protocol-session.v1#NavigatorBrowseResult", "read_local", "not_applicable", "best_effort", 5000, 2048),
    "navigator.select": _contract("protocol-session.v1#NavigatorSelectParams", "protocol-session.v1#NavigatorSelectResult", "read_local", "not_applicable", "best_effort", 5000, 2048),
    "session.snapshot": _contract("protocol-session.v1#SessionSnapshotParams", "protocol-session.v1#SessionSnapshotResult", "read_local", "not_applicable", "best_effort", 5000, 1024),
    "session.details": _contract("protocol-session.v1#SessionDetailsParams", "protocol-session.v1#SessionDetailsResult", "read_local", "not_applicable", "best_effort", 5000, 2048),
    "session.rating.read": _contract("protocol-session.v1#SessionRatingReadParams", "protocol-session.v1#SessionRatingReadResult", "network_read", "not_applicable", "best_effort", 30000, 1024),
    "protocol.decision.submit": _contract("protocol-session.v1#ProtocolDecisionSubmitParams", "protocol-session.v1#ProtocolDecisionSubmitResult", "write_local", "required", "before_side_effect", 5000, 2048),
    "protocol.strategy.get": _contract("protocol-session.v1#ProtocolStrategyGetParams", "protocol-session.v1#ProtocolStrategyGetResult", "read_local", "not_applicable", "best_effort", 5000, 1024),
    "protocol.strategy.patch": _contract("protocol-session.v1#ProtocolStrategyPatchParams", "protocol-session.v1#ProtocolStrategyPatchResult", "write_local", "required", "before_side_effect", 5000, 4096),
    "protocol.worker.open": _contract("protocol-session.v1#ProtocolWorkerOpenParams", "protocol-session.v1#ProtocolWorkerOpenResult", "session_lifecycle", "required", "before_side_effect", 10000, 8192),
    "protocol.worker.step": _contract("protocol-session.v1#ProtocolWorkerStepParams", "protocol-session.v1#ProtocolWorkerStepResult", "write_local", "required", "before_side_effect", 10000, 8192),
    "protocol.worker.close": _contract("protocol-session.v1#ProtocolWorkerCloseParams", "protocol-session.v1#ProtocolWorkerCloseResult", "session_lifecycle", "optional", "before_side_effect", 10000, 4096),
}

IDENTITY_METHODS = (
    "runtime.hello",
    "runtime.health",
    "runtime.describe",
    "runtime.shutdown",
    "identity.describe",
    "registry.browse",
    "invitation.inspect",
    "protocol.catalog",
    "protocol.inspect",
    "navigator.browse",
    "navigator.select",
    "session.snapshot",
    "session.details",
    "session.rating.read",
    "protocol.decision.submit",
    "protocol.strategy.get",
    "protocol.strategy.patch",
)

WORKER_METHODS = (
    "runtime.hello",
    "runtime.health",
    "runtime.describe",
    "runtime.shutdown",
    "protocol.worker.open",
    "protocol.worker.step",
    "protocol.worker.close",
)

ERROR_CODES = frozenset(
    {
        "runtime.protocol_version_incompatible",
        "runtime.schema_mismatch",
        "runtime.catalog_mismatch",
        "runtime.security_profile_insufficient",
        "runtime.capability_missing",
        "runtime.upgrade_required",
        "harness.upgrade_required",
        "runtime.method_not_supported",
        "runtime.method_not_allowed",
        "runtime.instance_stale",
        "runtime.not_ready",
        "validation.schema_invalid",
        "validation.frame_too_large",
        "transport.invalid_json",
        "transport.stdout_pollution",
        "transport.closed",
        "rate_limit.requests",
        "rate_limit.events",
        "runtime.request_timeout",
        "runtime.cancelled_before_side_effect",
        "runtime.too_late_to_cancel",
        "runtime.unknown_request",
        "runtime.idempotency_conflict",
        "runtime.event_sequence_gap",
        "runtime.event_offset_expired",
        "identity.not_configured",
        "registry.unavailable",
        "invitation.not_found",
        "protocol.untrusted",
        "protocol.bundle_mismatch",
        "protocol.decision_rejected",
        "protocol.strategy_rejected",
        "protocol.worker_output_rejected",
        "protocol.worker_role_mismatch",
        "session.not_found",
        "session.scope_mismatch",
        "session.version_conflict",
        "internal.runtime_failure",
    }
)
