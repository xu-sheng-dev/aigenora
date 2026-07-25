"""Bounded, content-addressed protocol UI transfer during the P2P handshake.

The platform remains the canonical persistent bundle store. This module is only
the explicitly enabled Host -> Guest fallback transport. Received code is
installed into the Guest's local protocol directory and later served by the
existing isolated localhost UI origin; it is never loaded from a Host URL.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aigenora.agent.protocol_ui import (
    MAX_FILE_COUNT,
    MAX_FILE_SIZE,
    MAX_TOTAL_SIZE,
    _validate_ui_file_record,
    _portable_path_key,
    build_manifest_from_dir,
    has_usable_ui,
    install_ui_bundle,
    read_ui_sidecar,
    validate_ui_bundle,
)


UI_P2P_VERSION = 1
UI_TRANSFER_TIMEOUT_SECONDS = 30.0


class UiArtifactProtocolError(RuntimeError):
    """The peer violated the bounded UI artifact handshake."""


@dataclass(frozen=True)
class HostUiArtifact:
    offer: dict[str, Any]
    files: tuple[dict[str, Any], ...]


def build_host_ui_artifact(protocol_dir: str | Path) -> HostUiArtifact | None:
    """Build a stable offer from Host's local protocol ui/, or None when absent."""
    protocol_dir = Path(protocol_dir)
    if not has_usable_ui(protocol_dir):
        return None
    sidecar = read_ui_sidecar(protocol_dir) or {}
    if sidecar.get("source_kind") == "host_p2p":
        raise UiArtifactProtocolError(
            "refusing to re-share a UI cached from another Host; install it as trusted local "
            "content or publish an author bundle first"
        )
    files, manifest_hash = build_manifest_from_dir(protocol_dir / "ui")
    validate_ui_bundle(files, expected_manifest_hash=manifest_hash, require_index=True)
    offer = {
        "version": UI_P2P_VERSION,
        "manifest_hash": manifest_hash,
        "file_count": len(files),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in files),
        "max_file_size_bytes": MAX_FILE_SIZE,
    }
    return HostUiArtifact(offer=offer, files=tuple(files))


def describe_local_ui(protocol_dir: str | Path) -> dict[str, Any] | None:
    """Return non-secret provenance metadata for the currently usable local UI."""
    protocol_dir = Path(protocol_dir)
    if not has_usable_ui(protocol_dir):
        return None
    sidecar = read_ui_sidecar(protocol_dir) or {}
    manifest_hash = sidecar.get("ui_manifest_hash")
    files = sidecar.get("files") if isinstance(sidecar.get("files"), list) else []
    if not isinstance(manifest_hash, str) or len(manifest_hash) != 64 or any(
        ch not in "0123456789abcdef" for ch in manifest_hash
    ):
        built_files, manifest_hash = build_manifest_from_dir(protocol_dir / "ui")
        files = built_files
    source_kind = sidecar.get("source_kind")
    if source_kind not in {"local", "platform", "host_p2p"}:
        source_kind = "platform" if sidecar.get("source_server") else "local"
    return {
        "status": "available",
        "source_kind": source_kind,
        "manifest_hash": manifest_hash,
        "file_count": len(files),
        "total_size_bytes": sum(int(item.get("size_bytes", 0)) for item in files),
        **({"source_peer": sidecar["source_peer"]} if sidecar.get("source_peer") else {}),
    }


def _validated_offer(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UiArtifactProtocolError("ui offer must be an object")
    if value.get("version") != UI_P2P_VERSION:
        raise UiArtifactProtocolError("unsupported ui artifact version")
    manifest_hash = value.get("manifest_hash")
    if not isinstance(manifest_hash, str) or len(manifest_hash) != 64 or any(
        ch not in "0123456789abcdef" for ch in manifest_hash
    ):
        raise UiArtifactProtocolError("invalid ui offer manifest_hash")
    file_count = value.get("file_count")
    total_size = value.get("total_size_bytes")
    if (
        isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or isinstance(total_size, bool)
        or not isinstance(total_size, int)
    ):
        raise UiArtifactProtocolError("invalid ui offer limits")
    if file_count < 1 or file_count > MAX_FILE_COUNT:
        raise UiArtifactProtocolError("ui offer file_count exceeds limit")
    if total_size < 0 or total_size > MAX_TOTAL_SIZE:
        raise UiArtifactProtocolError("ui offer total_size_bytes exceeds limit")
    return {
        "version": UI_P2P_VERSION,
        "manifest_hash": manifest_hash,
        "file_count": file_count,
        "total_size_bytes": total_size,
    }


def _frame(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UiArtifactProtocolError(f"{label} must be a JSON object")
    return value


async def receive_host_ui(
    channel: Any,
    *,
    offer: dict[str, Any],
    protocol_dir: str | Path,
    protocol_id: str,
    host_public_key: str,
) -> dict[str, Any]:
    """Request, verify and install an offered Host P2P UI bundle."""
    expected = _validated_offer(offer)
    await channel.send({
        "_ui_artifact_request": True,
        "version": UI_P2P_VERSION,
        "manifest_hash": expected["manifest_hash"],
    })
    begin = _frame(
        await channel.recv(timeout=UI_TRANSFER_TIMEOUT_SECONDS),
        "ui artifact begin frame",
    )
    if begin.get("_ui_artifact_error") is True:
        raise UiArtifactProtocolError(str(begin.get("reason") or "host refused ui artifact"))
    if begin.get("_ui_artifact_begin") is not True:
        raise UiArtifactProtocolError("host did not begin ui artifact transfer")
    announced = _validated_offer(begin)
    if announced != expected:
        raise UiArtifactProtocolError("ui artifact begin does not match offer")

    files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_portable_paths: set[str] = set()
    received_size = 0
    for index in range(expected["file_count"]):
        message = _frame(
            await channel.recv(timeout=UI_TRANSFER_TIMEOUT_SECONDS),
            f"ui artifact file frame {index}",
        )
        if message.get("_ui_artifact_file") is not True or message.get("index") != index:
            raise UiArtifactProtocolError(f"invalid ui artifact file frame at index {index}")
        item = {
            "path": message.get("path"),
            "content_base64": message.get("content_base64"),
            "content_hash": message.get("content_hash"),
            "size_bytes": message.get("size_bytes"),
        }
        try:
            path, content, _content_hash, _size_bytes = _validate_ui_file_record(
                item, index=index,
            )
        except (RuntimeError, ValueError) as exc:
            raise UiArtifactProtocolError(f"invalid ui artifact file frame {index}: {exc}") from exc
        if path in seen_paths:
            raise UiArtifactProtocolError(f"duplicate ui artifact path: {path}")
        seen_paths.add(path)
        portable_key = _portable_path_key(path)
        if portable_key in seen_portable_paths:
            raise UiArtifactProtocolError(f"portable ui artifact path collision: {path}")
        seen_portable_paths.add(portable_key)
        received_size += len(content)
        if received_size > expected["total_size_bytes"] or received_size > MAX_TOTAL_SIZE:
            raise UiArtifactProtocolError("ui artifact received size exceeds offer")
        files.append(item)

    end = _frame(
        await channel.recv(timeout=UI_TRANSFER_TIMEOUT_SECONDS),
        "ui artifact end frame",
    )
    if end.get("_ui_artifact_end") is not True:
        raise UiArtifactProtocolError("host did not end ui artifact transfer")
    if end.get("manifest_hash") != expected["manifest_hash"]:
        raise UiArtifactProtocolError("ui artifact end hash does not match offer")

    if received_size != expected["total_size_bytes"]:
        raise UiArtifactProtocolError("ui artifact total size does not match offer")
    install_ui_bundle(
        Path(protocol_dir),
        protocol_id=protocol_id,
        manifest_hash=expected["manifest_hash"],
        ui_files=files,
        source_server="",
        source_kind="host_p2p",
        source_peer=host_public_key,
    )
    result = describe_local_ui(protocol_dir) or {}
    result["status"] = "installed"
    await channel.send({
        "_ui_artifact_ack": True,
        "status": "installed",
        "manifest_hash": expected["manifest_hash"],
    })
    return result


async def maybe_receive_host_ui(
    channel: Any,
    *,
    offer: Any,
    protocol_dir: str | Path,
    protocol_id: str,
    host_public_key: str,
    accept_host_ui: bool,
    install_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Prefer trusted local/platform UI; use Host P2P only as an explicit fallback.

    A previously received ``host_p2p`` sidecar is not treated as trusted local UI:
    consent applies to the current Host/session.  Callers should pass a session-scoped
    ``install_dir`` so the received snapshot cannot silently become a future local UI.
    """
    local = describe_local_ui(protocol_dir)
    if local is not None and local.get("source_kind") == "host_p2p":
        local = None
    if local is not None:
        if accept_host_ui and isinstance(offer, dict):
            try:
                offered = _validated_offer(offer)
            except UiArtifactProtocolError:
                local["status"] = "local_preferred"
                local["offered_ui_status"] = "invalid_ignored"
            else:
                local["status"] = (
                    "cached" if local.get("manifest_hash") == offered["manifest_hash"]
                    else "local_preferred"
                )
                local["offered_manifest_hash"] = offered["manifest_hash"]
        return local
    if not accept_host_ui or not isinstance(offer, dict):
        return None
    return await receive_host_ui(
        channel,
        offer=offer,
        protocol_dir=install_dir or protocol_dir,
        protocol_id=protocol_id,
        host_public_key=host_public_key,
    )


async def serve_host_ui_and_wait_ready(
    channel: Any,
    *,
    artifact: HostUiArtifact | None,
    initial_message: Any | None = None,
) -> dict[str, Any]:
    """Serve at most one requested artifact, then return the Guest session-ready frame."""
    message = _frame(
        await channel.recv() if initial_message is None else initial_message,
        "post-proof frame",
    )
    if message.get("_ui_artifact_request") is not True:
        return message
    if artifact is None:
        await channel.send({"_ui_artifact_error": True, "reason": "ui artifact is not shared"})
        raise UiArtifactProtocolError("guest requested an unavailable ui artifact")
    expected_hash = artifact.offer["manifest_hash"]
    if message.get("version") != UI_P2P_VERSION or message.get("manifest_hash") != expected_hash:
        await channel.send({"_ui_artifact_error": True, "reason": "ui artifact request mismatch"})
        raise UiArtifactProtocolError("guest requested a different ui artifact")

    await channel.send({"_ui_artifact_begin": True, **artifact.offer})
    for index, item in enumerate(artifact.files):
        await channel.send({
            "_ui_artifact_file": True,
            "index": index,
            "path": item["path"],
            "content_base64": item["content_base64"],
            "content_hash": item["content_hash"],
            "size_bytes": item["size_bytes"],
        })
    await channel.send({"_ui_artifact_end": True, "manifest_hash": expected_hash})

    ack = _frame(
        await channel.recv(timeout=UI_TRANSFER_TIMEOUT_SECONDS),
        "ui artifact acknowledgement",
    )
    if (
        ack.get("_ui_artifact_ack") is not True
        or ack.get("status") != "installed"
        or ack.get("manifest_hash") != expected_hash
    ):
        raise UiArtifactProtocolError("guest did not acknowledge ui artifact installation")
    return _frame(await channel.recv(), "session ready frame")


__all__ = [
    "HostUiArtifact",
    "UI_P2P_VERSION",
    "UiArtifactProtocolError",
    "build_host_ui_artifact",
    "describe_local_ui",
    "maybe_receive_host_ui",
    "receive_host_ui",
    "serve_host_ui_and_wait_ready",
]
