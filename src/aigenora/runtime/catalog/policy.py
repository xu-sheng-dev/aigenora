from __future__ import annotations

from .loader import CatalogEntry, PinnedCatalog


def require_pinned_bundle(
    catalog: PinnedCatalog,
    *,
    protocol_id: str,
    bundle_digest: str,
    profile: str,
) -> CatalogEntry:
    entry = catalog.resolve(protocol_id=protocol_id)
    if entry.execution_trust != "builtin_pinned":
        raise ValueError("protocol execution trust is not builtin_pinned")
    if entry.bundle_digest != bundle_digest:
        raise ValueError("protocol bundle digest mismatch")
    if profile not in entry.profiles:
        raise ValueError("protocol profile is not pinned")
    return entry
