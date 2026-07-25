"""Strict, session-bound artifacts for bilateral P2P protocol bundles.

Bundle v1 deliberately supports only one ``hooks.py`` plus ``ui/`` static
assets. It is not an archive format and never installs dependencies.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aigenora.agent.protocol_ui import (
    MAX_FILE_COUNT as MAX_UI_FILE_COUNT,
    MAX_FILE_SIZE as MAX_UI_FILE_SIZE,
    MAX_TOTAL_SIZE as MAX_UI_TOTAL_SIZE,
    compute_manifest_hash,
    validate_ui_path,
)
from aigenora.engine.crypto import protocol_hash_from_obj
from aigenora.engine.keys import sign_raw, verify_raw
from aigenora.proto.remote_hooks_contract import (
    HooksContractError,
    HooksInspection,
    MAX_HOOKS_SIZE,
    inspect_hooks_source as _inspect_remote_hooks_source,
)


BUNDLE_SCHEMA = "aigenora-p2p-bundle-manifest/1"
BUNDLE_SIDECAR = ".aigenora-bundle.json"
BUNDLE_INSTALL_DIRNAME = "bundle-artifact"
BUNDLE_SOURCE_KIND = "host_p2p_bundle"
BUNDLE_VERSION = 1
HOOKS_ENTRYPOINT = "hooks.py"
UI_ENTRYPOINT = "ui/index.html"
MAX_BUNDLE_FILE_COUNT = MAX_UI_FILE_COUNT + 1
MAX_BUNDLE_TOTAL_SIZE = MAX_UI_TOTAL_SIZE + MAX_HOOKS_SIZE
WORKER_ISOLATION_PROFILE = "restricted-subprocess-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX_SIGNATURE_RE = re.compile(r"^[0-9a-f]{128}$")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_ALLOWED_CONTROL_MODES = frozenset({"autonomous", "hybrid", "human"})


class BundleValidationError(RuntimeError):
    """A local or peer bundle violated the frozen v1 contract."""


def inspect_hooks_source(source: bytes) -> HooksInspection:
    try:
        return _inspect_remote_hooks_source(source)
    except HooksContractError as exc:
        raise BundleValidationError(str(exc)) from exc


@dataclass(frozen=True)
class HostBundleArtifact:
    manifest: dict[str, Any]
    manifest_hash: str
    files: tuple[dict[str, Any], ...]

    @property
    def offer_limits(self) -> dict[str, int]:
        return {
            "file_count": len(self.files),
            "total_size_bytes": sum(int(item["size_bytes"]) for item in self.files),
        }


@dataclass(frozen=True)
class VerifiedInstalledBundle:
    manifest: dict[str, Any]
    manifest_hash: str
    hooks_source: bytes
    offer: dict[str, Any]
    session_binding: dict[str, str]
    sidecar: dict[str, Any]


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise BundleValidationError("bundle value is not canonical JSON") from exc
    return encoded.encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_json_bytes(value: bytes, *, label: str) -> Any:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BundleValidationError(f"{label} must be strict UTF-8") from exc

    def reject_constant(constant: str) -> None:
        raise ValueError(f"invalid constant: {constant}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = item
        return result

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BundleValidationError(f"{label} is not strict JSON") from exc


def _protocol_hash_from_bytes(value: bytes, *, label: str) -> str:
    spec = _strict_json_bytes(value, label=label)
    if not isinstance(spec, dict):
        raise BundleValidationError(f"{label} must contain a JSON object")
    try:
        return protocol_hash_from_obj(spec)
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleValidationError(f"{label} is not a valid protocol spec") from exc


def _portable_path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def validate_bundle_path(path: Any) -> tuple[str, str]:
    if not isinstance(path, str) or not path:
        raise BundleValidationError("bundle path must be a non-empty string")
    if path == HOOKS_ENTRYPOINT:
        return path, "hooks"
    if not path.startswith("ui/"):
        raise BundleValidationError("bundle path must be hooks.py or ui/<asset>")
    ui_path = path[3:]
    try:
        validate_ui_path(ui_path)
    except ValueError as exc:
        raise BundleValidationError(f"invalid bundle UI path {path}: {exc}") from exc
    return path, "ui"


def _is_reparse_point(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & _REPARSE_POINT)


def _assert_directory(path: Path) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise BundleValidationError(f"cannot inspect bundle directory: {path}") from exc
    if path.is_symlink() or _is_reparse_point(value) or not stat.S_ISDIR(value.st_mode):
        raise BundleValidationError(f"bundle directory is not a plain directory: {path}")
    return value


def _stable_stat_key(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        stat.S_IFMT(value.st_mode),
        int(value.st_nlink),
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
    )


def _read_regular_file_once(path: Path, *, root: Path, max_size: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise BundleValidationError(f"cannot inspect bundle file: {path}") from exc
    if (
        path.is_symlink()
        or _is_reparse_point(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise BundleValidationError(f"bundle file must be an unlinked regular file: {path}")
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise BundleValidationError(f"bundle file resolves outside protocol root: {path}") from exc
    if before.st_size > max_size:
        raise BundleValidationError(f"bundle file exceeds size limit: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BundleValidationError(f"cannot open bundle file safely: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if _stable_stat_key(opened) != _stable_stat_key(before):
            raise BundleValidationError(f"bundle file changed before read: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(max_size + 1)
        after = os.fstat(descriptor)
        if _stable_stat_key(after) != _stable_stat_key(opened):
            raise BundleValidationError(f"bundle file changed during read: {path}")
    finally:
        os.close(descriptor)
    if len(content) > max_size:
        raise BundleValidationError(f"bundle file exceeds size limit: {path}")
    return content


def _scan_ui_files(ui_root: Path, *, protocol_root: Path) -> list[tuple[str, bytes]]:
    _assert_directory(ui_root)
    collected: list[tuple[str, bytes]] = []

    def visit(directory: Path) -> None:
        _assert_directory(directory)
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(scanner, key=lambda item: item.name)
        except OSError as exc:
            raise BundleValidationError(f"cannot scan bundle UI directory: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise BundleValidationError(
                    f"cannot inspect bundle UI entry: {path}"
                ) from exc
            if entry.is_symlink() or _is_reparse_point(entry_stat):
                raise BundleValidationError(
                    f"bundle UI entry must not be a link or reparse point: {path}"
                )
            if stat.S_ISDIR(entry_stat.st_mode):
                visit(path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise BundleValidationError(
                    f"bundle UI entry must be a regular file: {path}"
                )
            relative = path.relative_to(ui_root).as_posix()
            full_path, kind = validate_bundle_path(f"ui/{relative}")
            if kind != "ui":
                raise AssertionError("UI scanner produced a non-UI path")
            content = _read_regular_file_once(
                path,
                root=protocol_root,
                max_size=MAX_UI_FILE_SIZE,
            )
            collected.append((full_path, content))

    visit(ui_root)
    if len(collected) > MAX_UI_FILE_COUNT:
        raise BundleValidationError(
            f"bundle UI file count exceeds {MAX_UI_FILE_COUNT}"
        )
    if sum(len(content) for _path, content in collected) > MAX_UI_TOTAL_SIZE:
        raise BundleValidationError(
            f"bundle UI total size exceeds {MAX_UI_TOTAL_SIZE}"
        )
    if not any(path == UI_ENTRYPOINT for path, _content in collected):
        raise BundleValidationError("bundle must contain ui/index.html")
    return collected


def _record(path: str, kind: str, content: bytes) -> dict[str, Any]:
    return {
        "path": path,
        "kind": kind,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_hash": sha256_hex(content),
        "size_bytes": len(content),
    }


def _manifest_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": item["path"],
        "kind": item["kind"],
        "content_hash": item["content_hash"],
        "size_bytes": item["size_bytes"],
    }


def build_host_bundle_artifact(
    protocol_dir: str | Path,
    *,
    protocol_id: str,
) -> HostBundleArtifact:
    protocol_root = Path(protocol_dir).resolve(strict=True)
    _assert_directory(protocol_root)
    if is_received_bundle(protocol_root):
        raise BundleValidationError("refusing to re-share a received P2P bundle")
    ui_sidecar = protocol_root / ".aigenora-ui.json"
    if ui_sidecar.is_file():
        try:
            ui_source = json.loads(ui_sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ui_source = {}
        if ui_source.get("source_kind") == "host_p2p":
            raise BundleValidationError("refusing to re-share Host P2P UI")
    spec_source = _read_regular_file_once(
        protocol_root / "spec.json",
        root=protocol_root,
        max_size=MAX_BUNDLE_TOTAL_SIZE,
    )
    if _protocol_hash_from_bytes(spec_source, label="spec.json") != protocol_id:
        raise BundleValidationError("protocol_id does not match local spec.json")

    hooks_source = _read_regular_file_once(
        protocol_root / HOOKS_ENTRYPOINT,
        root=protocol_root,
        max_size=MAX_HOOKS_SIZE,
    )
    inspection = inspect_hooks_source(hooks_source)
    file_records = [_record(HOOKS_ENTRYPOINT, "hooks", hooks_source)]
    for path, content in _scan_ui_files(
        protocol_root / "ui",
        protocol_root=protocol_root,
    ):
        file_records.append(_record(path, "ui", content))
    file_records.sort(key=lambda item: item["path"])
    if len(file_records) > MAX_BUNDLE_FILE_COUNT:
        raise BundleValidationError(
            f"bundle file count exceeds {MAX_BUNDLE_FILE_COUNT}"
        )
    total_size = sum(int(item["size_bytes"]) for item in file_records)
    if total_size > MAX_BUNDLE_TOTAL_SIZE:
        raise BundleValidationError(
            f"bundle total size exceeds {MAX_BUNDLE_TOTAL_SIZE}"
        )

    manifest = {
        "schema": BUNDLE_SCHEMA,
        "protocol_id": protocol_id,
        "bundle_kind": "hooks_ui",
        "hooks_entrypoint": HOOKS_ENTRYPOINT,
        "ui_entrypoint": UI_ENTRYPOINT,
        "supported_control_modes": list(inspection.supported_control_modes),
        "hook_methods": list(inspection.methods),
        "files": [_manifest_record(item) for item in file_records],
    }
    validated_manifest = validate_bundle_manifest(manifest)
    manifest_hash = sha256_hex(canonical_json_bytes(validated_manifest))
    return HostBundleArtifact(
        manifest=validated_manifest,
        manifest_hash=manifest_hash,
        files=tuple(file_records),
    )


def validate_bundle_manifest(
    value: Any,
    *,
    expected_protocol_id: str | None = None,
    expected_manifest_hash: str | None = None,
) -> dict[str, Any]:
    expected_keys = {
        "schema",
        "protocol_id",
        "bundle_kind",
        "hooks_entrypoint",
        "ui_entrypoint",
        "supported_control_modes",
        "hook_methods",
        "files",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise BundleValidationError("bundle manifest fields are invalid")
    protocol_id = value.get("protocol_id")
    if not isinstance(protocol_id, str) or not _SHA256_RE.fullmatch(protocol_id):
        raise BundleValidationError("bundle manifest protocol_id is invalid")
    if expected_protocol_id is not None and protocol_id != expected_protocol_id:
        raise BundleValidationError("bundle manifest protocol_id mismatch")
    if (
        value.get("schema") != BUNDLE_SCHEMA
        or value.get("bundle_kind") != "hooks_ui"
        or value.get("hooks_entrypoint") != HOOKS_ENTRYPOINT
        or value.get("ui_entrypoint") != UI_ENTRYPOINT
    ):
        raise BundleValidationError("bundle manifest version or entrypoint is invalid")

    modes = value.get("supported_control_modes")
    if (
        not isinstance(modes, list)
        or not modes
        or any(not isinstance(item, str) for item in modes)
        or modes != sorted(modes)
        or len(set(modes)) != len(modes)
        or any(item not in _ALLOWED_CONTROL_MODES for item in modes)
    ):
        raise BundleValidationError("bundle supported_control_modes is invalid")
    methods = value.get("hook_methods")
    if (
        not isinstance(methods, list)
        or any(not isinstance(item, str) for item in methods)
        or methods != sorted(methods)
        or len(set(methods)) != len(methods)
        or any(
            not item
            or (
                not item.startswith("proto_")
                and item not in {"build_decision_context", "run_policy"}
            )
            for item in methods
        )
    ):
        raise BundleValidationError("bundle hook_methods is invalid")

    files = value.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_BUNDLE_FILE_COUNT:
        raise BundleValidationError("bundle manifest file count is invalid")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    portable: set[str] = set()
    total_size = 0
    ui_total_size = 0
    hooks_count = 0
    for index, item in enumerate(files):
        if not isinstance(item, dict) or set(item) != {
            "path",
            "kind",
            "content_hash",
            "size_bytes",
        }:
            raise BundleValidationError(f"bundle manifest file #{index} is invalid")
        path, expected_kind = validate_bundle_path(item.get("path"))
        if item.get("kind") != expected_kind:
            raise BundleValidationError(f"bundle file kind mismatch: {path}")
        if path in seen:
            raise BundleValidationError(f"duplicate bundle path: {path}")
        seen.add(path)
        key = _portable_path_key(path)
        if key in portable:
            raise BundleValidationError(f"portable bundle path collision: {path}")
        portable.add(key)
        content_hash = item.get("content_hash")
        if not isinstance(content_hash, str) or not _SHA256_RE.fullmatch(content_hash):
            raise BundleValidationError(f"bundle content_hash is invalid: {path}")
        size_bytes = item.get("size_bytes")
        max_size = MAX_HOOKS_SIZE if expected_kind == "hooks" else MAX_UI_FILE_SIZE
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or size_bytes > max_size
        ):
            raise BundleValidationError(f"bundle size is invalid: {path}")
        total_size += size_bytes
        if total_size > MAX_BUNDLE_TOTAL_SIZE:
            raise BundleValidationError("bundle manifest total size exceeds limit")
        if expected_kind == "hooks":
            hooks_count += 1
        else:
            ui_total_size += size_bytes
            if ui_total_size > MAX_UI_TOTAL_SIZE:
                raise BundleValidationError("bundle UI total size exceeds limit")
        normalized.append(
            {
                "path": path,
                "kind": expected_kind,
                "content_hash": content_hash,
                "size_bytes": size_bytes,
            }
        )
    if normalized != sorted(normalized, key=lambda item: item["path"]):
        raise BundleValidationError("bundle manifest files must be sorted by path")
    if hooks_count != 1 or HOOKS_ENTRYPOINT not in seen or UI_ENTRYPOINT not in seen:
        raise BundleValidationError("bundle must contain one hooks.py and ui/index.html")

    normalized_manifest = {
        "schema": BUNDLE_SCHEMA,
        "protocol_id": protocol_id,
        "bundle_kind": "hooks_ui",
        "hooks_entrypoint": HOOKS_ENTRYPOINT,
        "ui_entrypoint": UI_ENTRYPOINT,
        "supported_control_modes": list(modes),
        "hook_methods": list(methods),
        "files": normalized,
    }
    actual_hash = sha256_hex(canonical_json_bytes(normalized_manifest))
    if expected_manifest_hash is not None:
        if (
            not isinstance(expected_manifest_hash, str)
            or not _SHA256_RE.fullmatch(expected_manifest_hash)
            or actual_hash != expected_manifest_hash
        ):
            raise BundleValidationError("bundle manifest hash mismatch")
    return normalized_manifest


def build_session_binding(
    *,
    post_id: str,
    host_public_key: str,
    guest_public_key: str,
    protocol_id: str,
    session_nonce: str,
) -> dict[str, str]:
    binding = {
        "schema": "aigenora-p2p-bundle-session/1",
        "post_id": post_id,
        "host_public_key": host_public_key,
        "guest_public_key": guest_public_key,
        "protocol_id": protocol_id,
        "session_nonce": session_nonce,
    }
    if (
        not post_id
        or not session_nonce
        or not _SHA256_RE.fullmatch(protocol_id)
        or not re.fullmatch(r"[0-9a-f]{64}", host_public_key)
        or not re.fullmatch(r"[0-9a-f]{64}", guest_public_key)
    ):
        raise BundleValidationError("bundle Session binding fields are invalid")
    return binding


def session_binding_hash(binding: dict[str, str]) -> str:
    expected = build_session_binding(
        post_id=binding.get("post_id", ""),
        host_public_key=binding.get("host_public_key", ""),
        guest_public_key=binding.get("guest_public_key", ""),
        protocol_id=binding.get("protocol_id", ""),
        session_nonce=binding.get("session_nonce", ""),
    )
    if binding != expected:
        raise BundleValidationError("bundle Session binding contains unknown fields")
    return sha256_hex(canonical_json_bytes(expected))


def _offer_signature_payload(
    *,
    binding_hash: str,
    protocol_id: str,
    manifest_hash: str,
) -> bytes:
    return (
        "aigenora-p2p-bundle-offer/v1\n"
        f"{binding_hash}\n{protocol_id}\n{manifest_hash}"
    ).encode("ascii")


def sign_bundle_offer(
    artifact: HostBundleArtifact,
    *,
    binding: dict[str, str],
    private_key: str,
) -> dict[str, Any]:
    binding_hash = session_binding_hash(binding)
    limits = artifact.offer_limits
    offer = {
        "version": BUNDLE_VERSION,
        "protocol_id": artifact.manifest["protocol_id"],
        "manifest_hash": artifact.manifest_hash,
        "file_count": limits["file_count"],
        "total_size_bytes": limits["total_size_bytes"],
        "session_binding_hash": binding_hash,
    }
    offer["signature"] = sign_raw(
        private_key,
        _offer_signature_payload(
            binding_hash=binding_hash,
            protocol_id=offer["protocol_id"],
            manifest_hash=offer["manifest_hash"],
        ),
    )
    return offer


def validate_signed_bundle_offer(
    value: Any,
    *,
    binding: dict[str, str],
) -> dict[str, Any]:
    expected_keys = {
        "version",
        "protocol_id",
        "manifest_hash",
        "file_count",
        "total_size_bytes",
        "session_binding_hash",
        "signature",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise BundleValidationError("bundle offer fields are invalid")
    protocol_id = value.get("protocol_id")
    manifest_hash = value.get("manifest_hash")
    binding_hash = value.get("session_binding_hash")
    signature = value.get("signature")
    file_count = value.get("file_count")
    total_size = value.get("total_size_bytes")
    if (
        type(value.get("version")) is not int
        or value.get("version") != BUNDLE_VERSION
    ):
        raise BundleValidationError("unsupported bundle offer version")
    if protocol_id != binding.get("protocol_id"):
        raise BundleValidationError("bundle offer protocol_id mismatch")
    if (
        not isinstance(manifest_hash, str)
        or not _SHA256_RE.fullmatch(manifest_hash)
        or binding_hash != session_binding_hash(binding)
        or not isinstance(signature, str)
        or not _HEX_SIGNATURE_RE.fullmatch(signature)
    ):
        raise BundleValidationError("bundle offer hash, binding, or signature is invalid")
    if (
        isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or file_count < 2
        or file_count > MAX_BUNDLE_FILE_COUNT
        or isinstance(total_size, bool)
        or not isinstance(total_size, int)
        or total_size < 0
        or total_size > MAX_BUNDLE_TOTAL_SIZE
    ):
        raise BundleValidationError("bundle offer limits are invalid")
    try:
        verify_raw(
            binding["host_public_key"],
            _offer_signature_payload(
                binding_hash=binding_hash,
                protocol_id=protocol_id,
                manifest_hash=manifest_hash,
            ),
            signature,
        )
    except Exception as exc:
        raise BundleValidationError("bundle offer signature verification failed") from exc
    return {
        "version": BUNDLE_VERSION,
        "protocol_id": protocol_id,
        "manifest_hash": manifest_hash,
        "file_count": file_count,
        "total_size_bytes": total_size,
        "session_binding_hash": binding_hash,
        "signature": signature,
    }


def validate_bundle_file_record(
    value: Any,
    *,
    index: int,
    expected: dict[str, Any],
) -> tuple[dict[str, Any], bytes]:
    if not isinstance(value, dict) or set(value) != {
        "path",
        "kind",
        "content_base64",
        "content_hash",
        "size_bytes",
    }:
        raise BundleValidationError(f"bundle file #{index} fields are invalid")
    path, kind = validate_bundle_path(value.get("path"))
    if (
        path != expected.get("path")
        or kind != expected.get("kind")
        or value.get("content_hash") != expected.get("content_hash")
        or value.get("size_bytes") != expected.get("size_bytes")
    ):
        raise BundleValidationError(f"bundle file #{index} differs from manifest")
    encoded = value.get("content_base64")
    if not isinstance(encoded, str):
        raise BundleValidationError(f"bundle file #{index} content must be Base64")
    max_size = MAX_HOOKS_SIZE if kind == "hooks" else MAX_UI_FILE_SIZE
    max_encoded = ((max_size + 2) // 3) * 4
    if len(encoded) > max_encoded:
        raise BundleValidationError(f"bundle file #{index} encoded content exceeds limit")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BundleValidationError(f"bundle file #{index} Base64 is invalid") from exc
    if (
        len(content) != expected["size_bytes"]
        or len(content) > max_size
        or sha256_hex(content) != expected["content_hash"]
    ):
        raise BundleValidationError(f"bundle file #{index} content verification failed")
    if kind == "hooks":
        inspect_hooks_source(content)
    normalized = {
        "path": path,
        "kind": kind,
        "content_hash": expected["content_hash"],
        "size_bytes": expected["size_bytes"],
    }
    return normalized, content


def install_received_bundle(
    install_dir: str | Path,
    *,
    protocol_id: str,
    local_spec_path: str | Path,
    manifest: dict[str, Any],
    manifest_hash: str,
    files: list[tuple[dict[str, Any], bytes]],
    offer: dict[str, Any],
    session_binding: dict[str, str],
) -> dict[str, Any]:
    install_path = Path(install_dir).resolve()
    parent = install_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if install_path.exists():
        raise BundleValidationError("bundle artifact is already pinned for this Session")
    validated_manifest = validate_bundle_manifest(
        manifest,
        expected_protocol_id=protocol_id,
        expected_manifest_hash=manifest_hash,
    )
    validated_offer = validate_signed_bundle_offer(offer, binding=session_binding)
    if validated_offer["manifest_hash"] != manifest_hash:
        raise BundleValidationError("bundle offer and manifest differ")
    manifest_size = sum(
        int(item["size_bytes"]) for item in validated_manifest["files"]
    )
    if (
        validated_offer["file_count"] != len(validated_manifest["files"])
        or validated_offer["total_size_bytes"] != manifest_size
    ):
        raise BundleValidationError("bundle offer limits differ from manifest")
    local_spec = Path(local_spec_path)
    if not local_spec.is_absolute():
        local_spec = Path.cwd() / local_spec
    local_spec_root = local_spec.parent.resolve(strict=True)
    _assert_directory(local_spec_root)
    local_spec_source = _read_regular_file_once(
        local_spec,
        root=local_spec_root,
        max_size=MAX_BUNDLE_TOTAL_SIZE,
    )
    if _protocol_hash_from_bytes(local_spec_source, label="local spec.json") != protocol_id:
        raise BundleValidationError("local spec.json does not match protocol_id")
    if len(files) != len(validated_manifest["files"]):
        raise BundleValidationError("received bundle file count differs from manifest")
    total_size = sum(len(content) for _item, content in files)
    if total_size != validated_offer["total_size_bytes"]:
        raise BundleValidationError("received bundle size differs from offer")

    stage = Path(tempfile.mkdtemp(prefix=".bundle-staging-", dir=parent))
    try:
        for index, ((record, content), expected) in enumerate(
            zip(files, validated_manifest["files"], strict=True)
        ):
            if record != expected:
                raise BundleValidationError(
                    f"received bundle file #{index} differs from manifest"
                )
            target = (stage / record["path"]).resolve()
            try:
                target.relative_to(stage.resolve())
            except ValueError as exc:
                raise BundleValidationError("bundle staging path escaped root") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (stage / "spec.json").write_bytes(local_spec_source)
        sidecar = {
            "schema": "aigenora-p2p-bundle-sidecar/1",
            "source_kind": BUNDLE_SOURCE_KIND,
            "protocol_id": protocol_id,
            "manifest_hash": manifest_hash,
            "manifest": validated_manifest,
            "offer": validated_offer,
            "session_binding": dict(session_binding),
            "source_peer": session_binding["host_public_key"],
            "accepted_at": datetime.now(timezone.utc).isoformat(),
            "worker_isolation_profile": WORKER_ISOLATION_PROFILE,
        }
        (stage / BUNDLE_SIDECAR).write_bytes(canonical_json_bytes(sidecar))
        verify_installed_bundle(stage)
        for attempt in range(4):
            try:
                stage.rename(install_path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 3:
                    raise
                time.sleep(0.025 * (attempt + 1))
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise
    return describe_installed_bundle(install_path)


def _read_sidecar(protocol_dir: Path) -> dict[str, Any]:
    sidecar_path = protocol_dir / BUNDLE_SIDECAR
    value = _strict_json_bytes(
        _read_regular_file_once(
            sidecar_path,
            root=protocol_dir,
            max_size=MAX_BUNDLE_TOTAL_SIZE,
        ),
        label="bundle provenance sidecar",
    )
    expected_keys = {
        "schema",
        "source_kind",
        "protocol_id",
        "manifest_hash",
        "manifest",
        "offer",
        "session_binding",
        "source_peer",
        "accepted_at",
        "worker_isolation_profile",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("schema") != "aigenora-p2p-bundle-sidecar/1"
        or value.get("source_kind") != BUNDLE_SOURCE_KIND
        or value.get("worker_isolation_profile") != WORKER_ISOLATION_PROFILE
    ):
        raise BundleValidationError("bundle provenance sidecar fields are invalid")
    return value


def verify_installed_bundle(protocol_dir: str | Path) -> VerifiedInstalledBundle:
    root = Path(protocol_dir).resolve(strict=True)
    _assert_directory(root)
    sidecar = _read_sidecar(root)
    protocol_id = sidecar.get("protocol_id")
    manifest_hash = sidecar.get("manifest_hash")
    manifest = validate_bundle_manifest(
        sidecar.get("manifest"),
        expected_protocol_id=protocol_id,
        expected_manifest_hash=manifest_hash,
    )
    binding = sidecar.get("session_binding")
    if not isinstance(binding, dict):
        raise BundleValidationError("bundle Session binding is invalid")
    offer = validate_signed_bundle_offer(sidecar.get("offer"), binding=binding)
    if (
        offer["manifest_hash"] != manifest_hash
        or offer["file_count"] != len(manifest["files"])
        or offer["total_size_bytes"]
        != sum(int(item["size_bytes"]) for item in manifest["files"])
        or binding.get("protocol_id") != protocol_id
        or sidecar.get("source_peer") != binding.get("host_public_key")
        or not isinstance(sidecar.get("accepted_at"), str)
        or not sidecar["accepted_at"]
    ):
        raise BundleValidationError("bundle provenance values differ")
    spec_source = _read_regular_file_once(
        root / "spec.json",
        root=root,
        max_size=MAX_BUNDLE_TOTAL_SIZE,
    )
    if _protocol_hash_from_bytes(spec_source, label="installed spec.json") != protocol_id:
        raise BundleValidationError("installed bundle spec.json mismatch")

    hooks_source = b""
    expected_paths = {BUNDLE_SIDECAR, "spec.json"}
    for item in manifest["files"]:
        path = item["path"]
        expected_paths.add(path)
        max_size = MAX_HOOKS_SIZE if item["kind"] == "hooks" else MAX_UI_FILE_SIZE
        content = _read_regular_file_once(
            root / Path(path),
            root=root,
            max_size=max_size,
        )
        if len(content) != item["size_bytes"] or sha256_hex(content) != item["content_hash"]:
            raise BundleValidationError(f"installed bundle file mismatch: {path}")
        if path == HOOKS_ENTRYPOINT:
            inspection = inspect_hooks_source(content)
            if (
                list(inspection.methods) != manifest["hook_methods"]
                or list(inspection.supported_control_modes)
                != manifest["supported_control_modes"]
            ):
                raise BundleValidationError("installed hooks inspection differs from manifest")
            hooks_source = content

    expected_directories: set[str] = set()
    for expected_path in expected_paths:
        parent = Path(expected_path).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_paths: set[str] = set()
    actual_directories: set[str] = set()
    for directory, dir_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        _assert_directory(directory_path)
        for name in dir_names:
            child = directory_path / name
            _assert_directory(child)
            actual_directories.add(child.relative_to(root).as_posix())
        for name in file_names:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            actual_paths.add(relative)
    if (
        actual_paths != expected_paths
        or actual_directories != expected_directories
    ):
        raise BundleValidationError("installed bundle contains unexpected or missing files")
    return VerifiedInstalledBundle(
        manifest=manifest,
        manifest_hash=manifest_hash,
        hooks_source=hooks_source,
        offer=offer,
        session_binding=dict(binding),
        sidecar=sidecar,
    )


def describe_installed_bundle(protocol_dir: str | Path) -> dict[str, Any]:
    verified = verify_installed_bundle(protocol_dir)
    ui_files = [
        {
            "path": item["path"][3:],
            "content_hash": item["content_hash"],
            "size_bytes": item["size_bytes"],
        }
        for item in verified.manifest["files"]
        if item["kind"] == "ui"
    ]
    return {
        "status": "installed",
        "source_kind": BUNDLE_SOURCE_KIND,
        "source_peer": verified.sidecar["source_peer"],
        "protocol_id": verified.manifest["protocol_id"],
        "manifest_hash": verified.manifest_hash,
        "session_binding_hash": verified.offer["session_binding_hash"],
        "file_count": verified.offer["file_count"],
        "total_size_bytes": verified.offer["total_size_bytes"],
        "hooks_execution": WORKER_ISOLATION_PROFILE,
        "ui_manifest_hash": compute_manifest_hash(ui_files),
        "ui_file_count": len(ui_files),
        "ui_total_size_bytes": sum(
            int(item["size_bytes"]) for item in ui_files
        ),
    }


def is_received_bundle(protocol_dir: str | Path) -> bool:
    protocol_path = Path(protocol_dir)
    return (
        protocol_path.name.casefold() == BUNDLE_INSTALL_DIRNAME
        or os.path.lexists(protocol_path / BUNDLE_SIDECAR)
    )


__all__ = [
    "BUNDLE_SCHEMA",
    "BUNDLE_INSTALL_DIRNAME",
    "BUNDLE_SIDECAR",
    "BUNDLE_SOURCE_KIND",
    "BUNDLE_VERSION",
    "BundleValidationError",
    "HostBundleArtifact",
    "HooksInspection",
    "MAX_BUNDLE_FILE_COUNT",
    "MAX_BUNDLE_TOTAL_SIZE",
    "MAX_HOOKS_SIZE",
    "VerifiedInstalledBundle",
    "WORKER_ISOLATION_PROFILE",
    "build_host_bundle_artifact",
    "build_session_binding",
    "canonical_json_bytes",
    "describe_installed_bundle",
    "inspect_hooks_source",
    "install_received_bundle",
    "is_received_bundle",
    "session_binding_hash",
    "sha256_hex",
    "sign_bundle_offer",
    "validate_bundle_file_record",
    "validate_bundle_manifest",
    "validate_bundle_path",
    "validate_signed_bundle_offer",
    "verify_installed_bundle",
]
