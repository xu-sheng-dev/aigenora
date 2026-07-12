from __future__ import annotations

import json
from typing import Any

from aigenora.runtime.catalog.loader import CatalogEntry, PinnedCatalog


class ProtocolService:
    def __init__(self, catalog: PinnedCatalog):
        self._catalog = catalog

    def catalog(self) -> dict[str, object]:
        return self._catalog.as_runtime_result()

    def inspect(self, protocol_id: str) -> dict[str, object]:
        return self._catalog.resolve(protocol_id=protocol_id).as_inspect_result()

    def browse(
        self,
        *,
        alias: str | None = None,
        family: str | None = None,
        limit: int = 32,
    ) -> list[dict[str, object]]:
        if limit < 1 or limit > 32:
            raise ValueError("limit must be between 1 and 32")
        entries = self._catalog.entries
        if alias is not None:
            entries = tuple(entry for entry in entries if entry.alias == alias)
        if family is not None:
            entries = tuple(entry for entry in entries if entry.family == family)
        return [entry.as_catalog_result() for entry in entries[:limit]]

    def select(
        self,
        *,
        protocol_id: str | None = None,
        alias: str | None = None,
        family: str | None = None,
        profile: str | None = None,
    ) -> dict[str, object]:
        entry, source = self._select_entry(protocol_id=protocol_id, alias=alias, family=family)
        selected_profile = profile or entry.default_profile
        if selected_profile not in entry.profiles:
            raise ValueError("profile is not pinned for the selected protocol")
        return {
            "protocol_id": entry.protocol_id,
            "alias": entry.alias,
            "family": entry.family,
            "profile": selected_profile,
            "bundle_digest": entry.bundle_digest,
            "selection_source": source,
            "options_json": json.dumps(
                entry.profile_options(selected_profile),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    def _select_entry(
        self,
        *,
        protocol_id: str | None,
        alias: str | None,
        family: str | None,
    ) -> tuple[CatalogEntry, str]:
        selectors = sum(value is not None for value in (protocol_id, alias, family))
        if selectors != 1:
            raise ValueError("select exactly one of protocol_id, alias, or family")
        if protocol_id is not None:
            return self._catalog.resolve(protocol_id=protocol_id), "explicit_protocol_id"
        if alias is not None:
            return self._catalog.resolve(alias=alias), "explicit_alias"
        matches = tuple(entry for entry in self._catalog.entries if entry.family == family)
        if len(matches) != 1:
            raise ValueError("family does not resolve to exactly one pinned protocol")
        return matches[0], "unique_family"


def safe_options(value: Any) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, (bool, int, float, str)):
            result[key] = item
    return result
