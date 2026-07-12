from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from aigenora.engine.config import builtin_protocols_root
from aigenora.engine.crypto import protocol_hash

from .manifest import CatalogEntry, CatalogManifest


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TOP_LEVEL_FIELDS = {
    "catalog_version",
    "catalog_digest",
    "execution_trust",
    "index_sha256",
    "wheel_version",
    "protocols",
}
_ENTRY_FIELDS = {
    "protocol_id",
    "alias",
    "family",
    "name",
    "path",
    "default_profile",
    "profiles",
    "spec_sha256",
    "hooks_sha256",
    "bundle_digest",
    "execution_trust",
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_exact_fields(value: dict[str, Any], allowed: set[str], label: str) -> None:
    if set(value) != allowed:
        raise ValueError(f"{label} contains missing or unknown fields")


def _safe_bundle_path(root: Path, relative: str) -> Path:
    logical = PurePosixPath(relative)
    if logical.is_absolute() or ".." in logical.parts or not logical.parts:
        raise ValueError("catalog bundle path is invalid")
    candidate = root.joinpath(*logical.parts)
    resolved = candidate.resolve()
    if resolved.parent == root or root not in resolved.parents:
        raise ValueError("catalog bundle path escapes the package root")
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("catalog bundle path is not a trusted directory")
    return candidate


def _profile_options(index_entry: dict[str, Any], names: tuple[str, ...]) -> dict[str, dict[str, object]]:
    raw_profiles = index_entry.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ValueError("protocol index profiles are invalid")
    result: dict[str, dict[str, object]] = {}
    for name in names:
        raw_profile = raw_profiles.get(name)
        if not isinstance(raw_profile, dict):
            raise ValueError("catalog profile is missing from the protocol index")
        raw_options = raw_profile.get("options", {})
        if not isinstance(raw_options, dict):
            raise ValueError("catalog profile options are invalid")
        options: dict[str, object] = {}
        for key, item in raw_options.items():
            if not isinstance(key, str) or not isinstance(item, (bool, int, float, str, dict)):
                raise ValueError("catalog profile contains an unsupported option")
            options[key] = item
        result[name] = options
    return result


class PinnedCatalog:
    def __init__(self, manifest: CatalogManifest):
        self.catalog_digest = manifest.catalog_digest
        self.entries = manifest.entries

    def resolve(
        self,
        *,
        protocol_id: str | None = None,
        alias: str | None = None,
    ) -> CatalogEntry:
        if (protocol_id is None) == (alias is None):
            raise ValueError("resolve exactly one catalog selector")
        for entry in self.entries:
            if protocol_id is not None and entry.protocol_id == protocol_id:
                return entry
            if alias is not None and entry.alias == alias:
                return entry
        raise KeyError("protocol is absent from the pinned catalog")

    def as_runtime_result(self) -> dict[str, object]:
        return {
            "catalog_digest": self.catalog_digest,
            "execution_trust": "builtin_pinned",
            "protocols": [entry.as_catalog_result() for entry in self.entries],
        }


def load_pinned_catalog() -> PinnedCatalog:
    manifest_path = Path(__file__).with_name("catalog.v1.json")
    manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest_raw, dict):
        raise ValueError("catalog manifest must be an object")
    _require_exact_fields(manifest_raw, _TOP_LEVEL_FIELDS, "catalog manifest")
    if manifest_raw.get("catalog_version") != "1":
        raise ValueError("catalog manifest version is unsupported")
    if manifest_raw.get("execution_trust") != "builtin_pinned":
        raise ValueError("catalog execution trust is not pinned")
    declared_digest = manifest_raw.get("catalog_digest")
    if not isinstance(declared_digest, str) or _DIGEST_RE.fullmatch(declared_digest) is None:
        raise ValueError("catalog digest is invalid")
    digest_source = dict(manifest_raw)
    del digest_source["catalog_digest"]
    actual_catalog_digest = "sha256:" + hashlib.sha256(_canonical_bytes(digest_source)).hexdigest()
    if actual_catalog_digest != declared_digest:
        raise ValueError("catalog manifest digest mismatch")

    protocols_root = builtin_protocols_root().resolve()
    index_path = protocols_root / "index.json"
    index_hash = manifest_raw.get("index_sha256")
    if not isinstance(index_hash, str) or _HEX_DIGEST_RE.fullmatch(index_hash) is None:
        raise ValueError("catalog index digest is invalid")
    if _sha256_file(index_path) != index_hash:
        raise ValueError("protocol index digest mismatch")
    index_raw = json.loads(index_path.read_text(encoding="utf-8"))
    index_entries = index_raw.get("protocols", []) if isinstance(index_raw, dict) else []
    if not isinstance(index_entries, list):
        raise ValueError("protocol index is invalid")
    by_id = {
        item.get("protocol_id"): item
        for item in index_entries
        if isinstance(item, dict) and isinstance(item.get("protocol_id"), str)
    }

    raw_entries = manifest_raw.get("protocols")
    if not isinstance(raw_entries, list) or len(raw_entries) < 2 or len(raw_entries) > 32:
        raise ValueError("catalog must contain a bounded protocol list")
    entries: list[CatalogEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError("catalog protocol entry must be an object")
        _require_exact_fields(raw, _ENTRY_FIELDS, "catalog protocol entry")
        protocol_id = raw.get("protocol_id")
        spec_hash = raw.get("spec_sha256")
        hooks_hash = raw.get("hooks_sha256")
        bundle_digest = raw.get("bundle_digest")
        if not isinstance(protocol_id, str) or _HEX_DIGEST_RE.fullmatch(protocol_id) is None:
            raise ValueError("catalog protocol_id is invalid")
        if not isinstance(spec_hash, str) or _HEX_DIGEST_RE.fullmatch(spec_hash) is None:
            raise ValueError("catalog spec digest is invalid")
        if not isinstance(hooks_hash, str) or _HEX_DIGEST_RE.fullmatch(hooks_hash) is None:
            raise ValueError("catalog hooks digest is invalid")
        if not isinstance(bundle_digest, str) or _DIGEST_RE.fullmatch(bundle_digest) is None:
            raise ValueError("catalog bundle digest is invalid")
        if raw.get("execution_trust") != "builtin_pinned":
            raise ValueError("catalog entry is not builtin_pinned")
        relative_path = raw.get("path")
        if not isinstance(relative_path, str):
            raise ValueError("catalog entry path is invalid")
        bundle_root = _safe_bundle_path(protocols_root, relative_path)
        spec_path = bundle_root / "spec.json"
        hooks_path = bundle_root / "hooks.py"
        if spec_path.is_symlink() or hooks_path.is_symlink():
            raise ValueError("catalog bundle contains a symlink")
        if _sha256_file(spec_path) != spec_hash or _sha256_file(hooks_path) != hooks_hash:
            raise ValueError("catalog bundle file digest mismatch")
        if protocol_hash(spec_path) != protocol_id:
            raise ValueError("catalog protocol_id does not match the protocol contract")
        bundle_source = {
            "hooks_sha256": hooks_hash,
            "protocol_id": protocol_id,
            "spec_sha256": spec_hash,
        }
        actual_bundle_digest = "sha256:" + hashlib.sha256(_canonical_bytes(bundle_source)).hexdigest()
        if actual_bundle_digest != bundle_digest:
            raise ValueError("catalog bundle digest mismatch")
        index_entry = by_id.get(protocol_id)
        if not isinstance(index_entry, dict):
            raise ValueError("catalog protocol is missing from the package index")
        for field in ("alias", "family", "name", "default_profile", "path"):
            if index_entry.get(field) != raw.get(field):
                raise ValueError(f"catalog/index {field} mismatch")
        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, list) or not raw_profiles:
            raise ValueError("catalog profiles are invalid")
        profiles = tuple(raw_profiles)
        if not all(isinstance(item, str) and item for item in profiles) or len(set(profiles)) != len(profiles):
            raise ValueError("catalog profiles must be unique strings")
        entries.append(
            CatalogEntry(
                protocol_id=protocol_id,
                alias=str(raw["alias"]),
                family=str(raw["family"]),
                name=str(raw["name"]),
                path=relative_path,
                default_profile=str(raw["default_profile"]),
                profiles=profiles,
                profile_options_map=_profile_options(index_entry, profiles),
                spec_sha256=spec_hash,
                hooks_sha256=hooks_hash,
                bundle_digest=bundle_digest,
            )
        )
    if [entry.protocol_id for entry in entries] != sorted(entry.protocol_id for entry in entries):
        raise ValueError("catalog protocols must use deterministic protocol_id order")
    if len({entry.protocol_id for entry in entries}) != len(entries):
        raise ValueError("catalog contains duplicate protocol ids")
    return PinnedCatalog(CatalogManifest(declared_digest, tuple(entries)))
