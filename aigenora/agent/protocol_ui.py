"""v006 P4: Client-side UI manifest + path validation.

The server's protocol_ui_manifests.protocol_id is invariant; manifest_hash is the
content-addressed key for UI immutability. This module handles client-side:
- path validation (same semantics as server ProtocolUiController.validatePath)
- manifest normalization (consistent with server ProtocolUiController.computeManifestHash)
- ui bundle download + hash validation + materialization
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
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


class UiPathError(ValueError):
    """UI path validation failed."""


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
    for f in files:
        p = f["path"]
        ch = f["content_hash"]
        sb = int(f["size_bytes"])
        # client re-validates path to guard against malicious input
        validate_ui_path(p)
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


def materialize_bundle(out_dir: Path, ui_files: list[dict[str, Any]]) -> Path:
    """Materialize ui_files from the bundle to <out_dir>/ui/.

    Uses a temporary directory + atomic rename; on mid-flight failure no partial
    output is left behind. Each file is validated against content_hash; mismatch
    raises RuntimeError.
    """
    out_dir = Path(out_dir).resolve()
    ui_dir = out_dir / "ui"
    if not ui_files:
        return ui_dir

    tmp_dir = out_dir / ".ui-staging"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    try:
        for f in ui_files:
            path = f["path"]
            validate_ui_path(path)  # second validation to guard against server-side omissions
            content = base64.b64decode(f["content_base64"])
            actual_hash = hashlib.sha256(content).hexdigest()
            declared = f["content_hash"]
            if actual_hash != declared:
                raise RuntimeError(f"ui content_hash mismatch for {path}: declared={declared} actual={actual_hash}")
            declared_size = int(f.get("size_bytes", 0))
            if declared_size and declared_size != len(content):
                raise RuntimeError(f"ui size_bytes mismatch for {path}: declared={declared_size} actual={len(content)}")
            target = (tmp_dir / path).resolve()
            # double path-traversal defense: after resolve it must stay within tmp_dir
            try:
                target.relative_to(tmp_dir.resolve())
            except ValueError as e:
                raise RuntimeError(f"resolved path escapes ui dir: {path}") from e
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        # atomic replacement
        if ui_dir.exists():
            shutil.rmtree(ui_dir)
        tmp_dir.rename(ui_dir)
    except Exception:
        # clean up tmp on failure
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return ui_dir


def write_ui_sidecar(out_dir: Path, *, protocol_id: str, manifest_hash: str,
                     files: list[dict[str, Any]], source_server: str) -> None:
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
        "fetched_at": _utc_now_iso(),
    }
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
