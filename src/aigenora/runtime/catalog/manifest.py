from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class CatalogEntry:
    protocol_id: str
    alias: str
    family: str
    name: str
    path: str
    default_profile: str
    profiles: tuple[str, ...]
    profile_options_map: Mapping[str, Mapping[str, object]]
    spec_sha256: str
    hooks_sha256: str
    bundle_digest: str
    execution_trust: str = "builtin_pinned"

    def as_catalog_result(self) -> dict[str, object]:
        return {
            "protocol_id": self.protocol_id,
            "alias": self.alias,
            "family": self.family,
            "name": self.name,
            "default_profile": self.default_profile,
            "bundle_digest": self.bundle_digest,
            "execution_trust": self.execution_trust,
        }

    def as_inspect_result(self) -> dict[str, object]:
        return {
            **self.as_catalog_result(),
            "spec_digest": "sha256:" + self.spec_sha256,
            "hooks_digest": "sha256:" + self.hooks_sha256,
        }

    def profile_options(self, profile: str) -> dict[str, object]:
        raw = self.profile_options_map.get(profile, {})
        return {key: value for key, value in raw.items() if isinstance(key, str)}


@dataclass(frozen=True)
class CatalogManifest:
    catalog_digest: str
    entries: tuple[CatalogEntry, ...]
