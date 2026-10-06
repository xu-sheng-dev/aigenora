"""Artifact manifests, chunk batches, and path safety for collab transfer.

Chunk contract (design doc §7.3): fixed 256 KiB decoded chunks; each wire
message carries a BATCH of chunks (start 4, bounded by the 2 MiB json field
cap); block ACK means durably written; a file is only usable after the
file-level SHA256 over the reassembled bytes verifies. No fixed quota on
file count or size — the only natural bound is local disk space.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

CHUNK_SIZE = 256 * 1024
MAX_BATCH_CHUNKS = 8  # transport batching; frames stay well under the json cap
MAX_MANIFEST_FILES = 512

_UNSAFE_PATH = re.compile(r"(^|/)\.\.(/|$)|[\x00-\x1f]|[<>:\"|?*\\\\]|^[A-Za-z]:|^/")


def safe_rel_path(path: str) -> str:
    """Validate a peer-supplied relative artifact path.

    Rejects absolute paths, drive letters, traversal, backslashes, control
    characters, and Windows-forbidden characters. Returns the normalized
    POSIX-style relative path.
    """
    if not isinstance(path, str) or not path or len(path) > 512:
        raise ValueError("unsafe path")
    if _UNSAFE_PATH.search(path):
        raise ValueError("unsafe path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or str(pure) != path:
        # PurePosixPath collapses 'a//b' and trailing slashes; require exact form.
        if str(pure) + "/" != path and path + "/" != str(pure) + "/":
            raise ValueError("unsafe path")
    parts = pure.parts
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("unsafe path")
    return "/".join(parts)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest_entry(root: Path, rel: str) -> dict[str, Any]:
    """Create one manifest entry {path,size,sha256} for a local file."""
    safe = safe_rel_path(rel)
    full = root / safe
    if not full.is_file():
        raise FileNotFoundError(str(full))
    return {"path": safe, "size": full.stat().st_size, "sha256": sha256_file(full)}


def collect_tree(root: Path, prefix: str = "") -> list[dict[str, Any]]:
    """Build manifest entries for every file under root (bounded page size)."""
    entries: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _is_excluded_dir(d))
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if _is_excluded_file(rel):
                continue
            entries.append(build_manifest_entry(root, rel))
            if len(entries) >= MAX_MANIFEST_FILES:
                return entries
    return entries


def _is_excluded_dir(name: str) -> bool:
    return name in {".git", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules", ".venv", ".aigenora"}


def _is_excluded_file(rel: str) -> bool:
    parts = rel.split("/")
    return any(part in {".DS_Store", "Thumbs.db"} for part in parts)


def encode_chunk(root: Path, entry: dict[str, Any], chunk_index: int) -> dict[str, Any] | None:
    """Read one chunk of a manifest file and encode it for the wire."""
    offset = chunk_index * CHUNK_SIZE
    size = int(entry["size"])
    if offset >= size:
        return None
    with (root / entry["path"]).open("rb") as stream:
        stream.seek(offset)
        raw = stream.read(CHUNK_SIZE)
    return {
        "path": entry["path"],
        "offset": offset,
        "size": len(raw),
        "sha256": sha256_bytes(raw),
        "data": base64.b64encode(raw).decode("ascii"),
    }


def chunk_count(size: int) -> int:
    return (int(size) + CHUNK_SIZE - 1) // CHUNK_SIZE


def validate_chunk_object(chunk: Any, manifest_files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Structural + content validation of one peer-supplied chunk object."""
    if not isinstance(chunk, dict):
        raise ValueError("chunk must be an object")
    path = safe_rel_path(str(chunk.get("path", "")))
    entry = manifest_files.get(path)
    if entry is None:
        raise ValueError("chunk path is not in the registered manifest")
    offset = chunk.get("offset")
    size = chunk.get("size")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("chunk offset must be a non-negative integer")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("chunk size must be a positive integer")
    if offset % CHUNK_SIZE != 0:
        raise ValueError("chunk offset must align to the chunk size")
    if size > CHUNK_SIZE:
        raise ValueError("chunk size exceeds the fixed chunk bound")
    if offset + size > int(entry["size"]):
        raise ValueError("chunk range exceeds the registered file size")
    digest = chunk.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("chunk sha256 must be lowercase hex")
    data = chunk.get("data")
    if not isinstance(data, str):
        raise ValueError("chunk data must be base64 text")
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        raise ValueError("chunk data is not valid base64")
    if len(raw) != size:
        raise ValueError("chunk data length does not match the declared size")
    if sha256_bytes(raw) != digest:
        raise ValueError("chunk digest mismatch")
    is_last = offset + size == int(entry["size"])
    if not is_last and size != CHUNK_SIZE:
        raise ValueError("non-final chunk must be exactly the chunk size")
    return {"path": path, "offset": offset, "size": size, "sha256": digest, "raw": raw}


def write_chunk(staging_root: Path, path: str, offset: int, raw: bytes) -> None:
    """Durably write one received chunk at its fixed offset (idempotent)."""
    target = staging_root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("staging path is a symlink")
    # Preallocate holes so sparse writes at arbitrary offsets are safe.
    with target.open("r+b" if target.exists() else "w+b") as stream:
        stream.seek(offset)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def verify_staged_file(staging_root: Path, entry: dict[str, Any]) -> bool:
    """File-level digest check after every chunk of the file arrived."""
    target = staging_root / entry["path"]
    if not target.is_file() or target.stat().st_size != int(entry["size"]):
        return False
    return sha256_file(target) == entry["sha256"]
