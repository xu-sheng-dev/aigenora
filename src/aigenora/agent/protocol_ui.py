"""v006 P4: Client-side UI manifest + path validation.

The server's protocol_ui_manifests.protocol_id is invariant; manifest_hash is the
content-addressed key for UI immutability. This module handles client-side:
- path validation (same semantics as server ProtocolUiController.validatePath)
- manifest normalization (consistent with server ProtocolUiController.computeManifestHash)
- ui bundle download + hash validation + materialization
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

from aigenora.engine.rest import RestClient


# Identical to server ProtocolUiController.ALLOWED_EXTENSIONS
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".html", ".htm", ".js", ".mjs", ".css", ".svg", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".woff", ".woff2", ".json", ".txt",
})

# Consistent with server ProtocolUiController.WINDOWS_RESERVED
_WINDOWS_RESERVED: frozenset[str] = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
})

MAX_FILE_SIZE = 512 * 1024  # 512 KB
MAX_TOTAL_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_FILE_COUNT = 100
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UiPathError(ValueError):
    """UI path validation failed."""


def _portable_path_key(path: str) -> str:
    """Return the cross-platform collision key used for UI artifact paths."""
    return unicodedata.normalize("NFC", path).lower()


def _rename_dir(source: Path, target: Path) -> None:
    """Atomically rename a directory within one parent filesystem.

    Windows can transiently return ``WinError 5`` while Defender/indexing closes
    a handle on a newly materialized directory.  Retry only that permission
    failure for a short, bounded interval; every other error remains immediate so
    concurrent-target conflicts and rollback tests keep their original meaning.
    """
    attempts = 6 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            source.rename(target)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.02 * (2 ** attempt))


def validate_ui_path(path: str) -> None:
    """Path validation identical to the server side; raises UiPathError on violation.

    The server validates again; the client validates upfront to: (1) avoid polluting
    the local filesystem; (2) present error messages in the client's language;
    (3) prevent a maliciously crafted hash from bypassing validation.
    """
    if not path or not isinstance(path, str):
        raise UiPathError("path is empty")
    if len(path) > 255:
        raise UiPathError(f"path too long (>255 chars): {path}")
    if path.startswith("/") or path.startswith("\\"):
        raise UiPathError(f"path must be relative: {path}")
    if len(path) >= 2 and path[1] == ":":
        raise UiPathError(f"path must not contain drive letter: {path}")
    if "\\" in path:
        raise UiPathError(f"path must use forward slashes only: {path}")
    segments = path.split("/")
    for seg in segments:
        if seg == "..":
            raise UiPathError(f"path must not contain '..': {path}")
        if not seg:
            raise UiPathError(f"path must not contain empty segments: {path}")
        for c in seg:
            o = ord(c)
            if o < 0x20 or o == 0x7f:
                raise UiPathError(f"path must not contain control chars: {path}")
        if seg.endswith(".") or seg.endswith(" "):
            raise UiPathError(f"path segment must not end with '.' or ' ': {path}")
        stem = seg.lower().split(".", 1)[0]
        if stem in _WINDOWS_RESERVED:
            raise UiPathError(f"path segment uses reserved name: {path}")
    dot_idx = path.rfind(".")
    if dot_idx < 0:
        raise UiPathError(f"path must have an extension: {path}")
    ext = path[dot_idx:].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UiPathError(f"extension not allowed: {ext} (allowed: {sorted(ALLOWED_EXTENSIONS)})")


def compute_manifest_hash(files: list[dict[str, Any]]) -> str:
    """Compute the ui manifest hash, consistent with server ProtocolUiController.computeManifestHash.

    files: list of {"path": str, "content_hash": str (sha256 hex), "size_bytes": int}
    """
    normalized = []
    seen_portable: set[str] = set()
    for f in files:
        p = f["path"]
        ch = f["content_hash"]
        sb = int(f["size_bytes"])
        # client re-validates path to guard against malicious input
        validate_ui_path(p)
        portable_key = _portable_path_key(p)
        if portable_key in seen_portable:
            raise UiPathError(f"portable path collision in manifest: {p}")
        seen_portable.add(portable_key)
        normalized.append({"path": p, "content_hash": ch, "size_bytes": sb})
    normalized.sort(key=lambda x: x["path"])
    manifest = {"files": normalized}
    canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_manifest_from_dir(ui_dir: Path) -> tuple[list[dict[str, Any]], str]:
    """Scan the local ui/ directory and build manifest + manifest_hash.

    Returns (manifest_files, manifest_hash).
    manifest_files is shaped as [{"path", "content_base64", "content_hash", "size_bytes"}].
    """
    ui_dir = Path(ui_dir).resolve()
    if not ui_dir.is_dir():
        raise FileNotFoundError(f"ui directory not found: {ui_dir}")

    collected: list[dict[str, Any]] = []
    total_size = 0
    for entry in sorted(ui_dir.rglob("*")):
        if not entry.is_file():
            continue
        try:
            entry.resolve(strict=True).relative_to(ui_dir)
        except (OSError, ValueError) as exc:
            raise UiPathError(f"ui file resolves outside ui directory: {entry}") from exc
        rel = entry.relative_to(ui_dir).as_posix()  # force forward slashes
        validate_ui_path(rel)
        content = entry.read_bytes()
        if len(content) > MAX_FILE_SIZE:
            raise ValueError(f"file too large ({len(content)}>{MAX_FILE_SIZE}): {rel}")
        total_size += len(content)
        if total_size > MAX_TOTAL_SIZE:
            raise ValueError(f"protocol ui total size exceeds {MAX_TOTAL_SIZE} bytes")
        collected.append({
            "path": rel,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "content_hash": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        })
    if len(collected) > MAX_FILE_COUNT:
        raise ValueError(f"protocol ui file count exceeds {MAX_FILE_COUNT}")
    if not collected:
        raise ValueError(f"ui directory is empty: {ui_dir}")

    manifest_hash = compute_manifest_hash(collected)
    return collected, manifest_hash


def _validated_bundle_files(
    ui_files: list[dict[str, Any]], *, require_index: bool = False,
) -> list[tuple[str, bytes, str, int]]:
    """Validate an untrusted UI bundle and return decoded file records.

    This is deliberately shared by platform and P2P ingestion. A malicious or
    buggy transport must not bypass the limits enforced during author upload.
    """
    if not isinstance(ui_files, list):
        raise RuntimeError("ui_files must be an array")
    if not ui_files:
        raise RuntimeError("ui bundle is empty")
    if len(ui_files) > MAX_FILE_COUNT:
        raise RuntimeError(f"protocol ui file count exceeds {MAX_FILE_COUNT}")

    decoded: list[tuple[str, bytes, str, int]] = []
    seen: set[str] = set()
    seen_portable: set[str] = set()
    total_size = 0
    for index, item in enumerate(ui_files):
        path, content, declared_hash, declared_size = _validate_ui_file_record(item, index=index)
        if path in seen:
            raise RuntimeError(f"duplicate ui path: {path}")
        seen.add(path)
        portable_key = _portable_path_key(path)
        if portable_key in seen_portable:
            raise RuntimeError(f"portable ui path collision: {path}")
        seen_portable.add(portable_key)
        total_size += len(content)
        if total_size > MAX_TOTAL_SIZE:
            raise RuntimeError(f"protocol ui total size exceeds {MAX_TOTAL_SIZE} bytes")
        decoded.append((path, content, declared_hash, declared_size))

    if require_index and "index.html" not in seen:
        raise RuntimeError("ui bundle must contain index.html")
    return decoded


def _validate_ui_file_record(
    item: Any, *, index: int = 0,
) -> tuple[str, bytes, str, int]:
    """Validate and decode one untrusted UI file record.

    P2P uses this per frame so a peer cannot accumulate many oversized Base64
    strings in memory before bundle-level validation runs.
    """
    if not isinstance(item, dict):
        raise RuntimeError(f"ui file #{index} must be an object")
    path = item.get("path")
    if not isinstance(path, str):
        raise RuntimeError(f"ui file #{index} path must be a string")
    validate_ui_path(path)

    encoded = item.get("content_base64")
    if not isinstance(encoded, str):
        raise RuntimeError(f"ui content_base64 must be a string: {path}")
    # Exact Base64 for MAX_FILE_SIZE is at most ceil(n / 3) * 4 bytes.
    max_encoded_size = ((MAX_FILE_SIZE + 2) // 3) * 4
    if len(encoded) > max_encoded_size:
        raise RuntimeError(f"encoded ui file exceeds size limit: {path}")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(f"invalid ui content_base64 for {path}") from exc
    if len(content) > MAX_FILE_SIZE:
        raise RuntimeError(f"ui file too large ({len(content)}>{MAX_FILE_SIZE}): {path}")

    declared_size = item.get("size_bytes")
    if isinstance(declared_size, bool) or not isinstance(declared_size, int):
        raise RuntimeError(f"invalid ui size_bytes for {path}")
    if declared_size < 0 or declared_size != len(content):
        raise RuntimeError(
            f"ui size_bytes mismatch for {path}: declared={declared_size} actual={len(content)}"
        )

    declared_hash = item.get("content_hash")
    if not isinstance(declared_hash, str) or not _SHA256_RE.fullmatch(declared_hash):
        raise RuntimeError(f"invalid ui content_hash for {path}")
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != declared_hash:
        raise RuntimeError(
            f"ui content_hash mismatch for {path}: declared={declared_hash} actual={actual_hash}"
        )
    return path, content, declared_hash, declared_size


def validate_ui_bundle(
    ui_files: list[dict[str, Any]], *, expected_manifest_hash: str | None = None,
    require_index: bool = False,
) -> str:
    """Validate files and return their content-addressed manifest hash."""
    decoded = _validated_bundle_files(ui_files, require_index=require_index)
    manifest_files = [
        {"path": path, "content_hash": content_hash, "size_bytes": size_bytes}
        for path, _content, content_hash, size_bytes in decoded
    ]
    actual_manifest_hash = compute_manifest_hash(manifest_files)
    if expected_manifest_hash is not None:
        if not isinstance(expected_manifest_hash, str) or not _SHA256_RE.fullmatch(expected_manifest_hash):
            raise RuntimeError("invalid ui manifest hash")
        if actual_manifest_hash != expected_manifest_hash:
            raise RuntimeError(
                "ui manifest hash mismatch: "
                f"declared {expected_manifest_hash}, got {actual_manifest_hash}"
            )
    return actual_manifest_hash


def materialize_bundle(
    out_dir: Path, ui_files: list[dict[str, Any]], *, require_index: bool = False,
) -> Path:
    """Materialize ui_files from the bundle to <out_dir>/ui/.

    Uses a temporary directory + atomic rename; on mid-flight failure no partial
    output is left behind. Each file is validated against content_hash; mismatch
    raises RuntimeError.
    """
    out_dir = Path(out_dir).resolve()
    ui_dir = out_dir / "ui"
    if not ui_files:
        return ui_dir

    decoded = _validated_bundle_files(ui_files, require_index=require_index)

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=".ui-staging-", dir=out_dir))
    backup_dir: Path | None = None

    try:
        for path, content, _declared_hash, _declared_size in decoded:
            target = (tmp_dir / path).resolve()
            # double path-traversal defense: after resolve it must stay within tmp_dir
            try:
                target.relative_to(tmp_dir.resolve())
            except ValueError as e:
                raise RuntimeError(f"resolved path escapes ui dir: {path}") from e
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        # Directory replacement with rollback.  A unique staging/backup pair
        # also prevents concurrent fetches from deleting each other's work.
        if ui_dir.exists():
            backup_dir = Path(tempfile.mkdtemp(prefix=".ui-backup-", dir=out_dir))
            backup_dir.rmdir()
            _rename_dir(ui_dir, backup_dir)
        try:
            _rename_dir(tmp_dir, ui_dir)
        except Exception:
            if backup_dir is not None and backup_dir.exists() and not ui_dir.exists():
                _rename_dir(backup_dir, ui_dir)
                backup_dir = None
            raise
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
            backup_dir = None
    except Exception:
        # clean up tmp on failure
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if backup_dir is not None and backup_dir.exists():
            if not ui_dir.exists():
                _rename_dir(backup_dir, ui_dir)
            else:
                shutil.rmtree(backup_dir, ignore_errors=True)
        raise

    return ui_dir


def write_ui_sidecar(out_dir: Path, *, protocol_id: str, manifest_hash: str,
                     files: list[dict[str, Any]], source_server: str,
                     source_kind: str = "platform",
                     source_peer: str | None = None) -> None:
    """Write the <out_dir>/.aigenora-ui.json sidecar recording UI metadata."""
    sidecar = {
        "protocol_id": protocol_id,
        "ui_manifest_hash": manifest_hash,
        "files": [
            {
                "path": f["path"],
                "content_hash": f["content_hash"],
                "size_bytes": f["size_bytes"],
            }
            for f in files
        ],
        "source_server": source_server,
        "source_kind": source_kind,
        "fetched_at": _utc_now_iso(),
    }
    if source_peer:
        sidecar["source_peer"] = source_peer
    (Path(out_dir) / ".aigenora-ui.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def read_ui_sidecar(out_dir: Path) -> dict[str, Any] | None:
    p = Path(out_dir) / ".aigenora-ui.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def has_usable_ui(out_dir: Path) -> bool:
    return (Path(out_dir) / "ui" / "index.html").is_file()


def install_ui_bundle(
    out_dir: Path, *, protocol_id: str, manifest_hash: str,
    ui_files: list[dict[str, Any]], source_server: str,
    source_kind: str = "platform", source_peer: str | None = None,
) -> dict[str, Any]:
    """Validate, atomically install, and record provenance for an untrusted UI bundle."""
    actual_manifest_hash = validate_ui_bundle(
        ui_files,
        expected_manifest_hash=manifest_hash,
        require_index=True,
    )
    materialize_bundle(out_dir, ui_files, require_index=True)
    write_ui_sidecar(
        out_dir,
        protocol_id=protocol_id,
        manifest_hash=actual_manifest_hash,
        files=ui_files,
        source_server=source_server,
        source_kind=source_kind,
        source_peer=source_peer,
    )
    return read_ui_sidecar(out_dir) or {}


def fetch_platform_ui(client: RestClient, protocol_id: str, out_dir: Path) -> dict[str, Any] | None:
    """Fetch and install the currently published platform UI for an existing protocol."""
    resp = client.request("GET", f"/api/v1/protocols/{protocol_id}/bundle", None)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"GET /bundle failed: HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    manifest_hash = data.get("ui_manifest_hash")
    ui_files = data.get("ui_files") or []
    if not manifest_hash:
        return None
    return install_ui_bundle(
        out_dir,
        protocol_id=protocol_id,
        manifest_hash=manifest_hash,
        ui_files=ui_files,
        source_server=client.server,
        source_kind="platform",
    )


def fetch_ui_bundle(client: RestClient, protocol_id: str, out_dir: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """GET /api/v1/protocols/{id}/bundle, returns (ui_manifest, ui_files).

    Older servers without the bundle endpoint → 404 fallback → returns (None, []).
    Newer servers with no published manifest → ui_manifest=None, ui_files=[].
    """
    resp = client.request("GET", f"/api/v1/protocols/{protocol_id}/bundle", None)
    if resp.status_code == 404:
        # legacy server or protocol does not exist
        return None, []
    if resp.status_code != 200:
        raise RuntimeError(f"GET /bundle failed: HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return data.get("ui_manifest"), data.get("ui_files") or []


def _utc_now_iso() -> str:
    """ISO 8601 UTC, consistent in style with server created_at."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
