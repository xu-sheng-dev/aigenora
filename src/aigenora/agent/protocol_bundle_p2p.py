"""Bounded Host-to-Guest P2P transfer for signed protocol bundle artifacts."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from aigenora.agent.protocol_bundle import (
    BUNDLE_VERSION,
    BundleValidationError,
    HostBundleArtifact,
    describe_installed_bundle,
    inspect_hooks_source,
    install_received_bundle,
    validate_bundle_file_record,
    validate_bundle_manifest,
    validate_signed_bundle_offer,
)
from aigenora.agent.protocol_ui_p2p import (
    HostUiArtifact,
    serve_host_ui_and_wait_ready,
)


BUNDLE_TRANSFER_TIMEOUT_SECONDS = 30.0


class BundleArtifactProtocolError(BundleValidationError):
    """The peer violated the signed, bounded bundle handshake."""


def has_exact_bundle_capability(value: Any) -> bool:
    return (
        type(value) is dict
        and tuple(value) == ("p2p_bundle_v1",)
        and value["p2p_bundle_v1"] is True
    )


def _frame(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BundleArtifactProtocolError(f"{label} must be a JSON object")
    return value


async def _recv_before(
    channel: Any,
    *,
    deadline: float,
    label: str,
) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BundleArtifactProtocolError("bundle artifact transfer timed out")
    return _frame(
        await channel.recv(timeout=remaining),
        label,
    )


async def _send_rejected_ack(
    channel: Any,
    *,
    manifest_hash: str,
) -> None:
    try:
        await channel.send(
            {
                "_bundle_artifact_ack": True,
                "status": "rejected",
                "manifest_hash": manifest_hash,
                "reason": "verification_failed",
            }
        )
    except Exception:
        pass


async def receive_host_bundle(
    channel: Any,
    *,
    offer: dict[str, Any],
    session_binding: dict[str, str],
    protocol_id: str,
    local_spec_path: str | Path,
    install_dir: str | Path,
) -> dict[str, Any]:
    """Request, verify, atomically install, and acknowledge one signed bundle."""
    expected_offer = validate_signed_bundle_offer(offer, binding=session_binding)
    if expected_offer["protocol_id"] != protocol_id:
        raise BundleArtifactProtocolError("bundle offer protocol_id mismatch")
    await channel.send(
        {
            "_bundle_artifact_request": True,
            "version": BUNDLE_VERSION,
            "manifest_hash": expected_offer["manifest_hash"],
            "session_binding_hash": expected_offer["session_binding_hash"],
        }
    )
    deadline = time.monotonic() + BUNDLE_TRANSFER_TIMEOUT_SECONDS
    try:
        begin = await _recv_before(
            channel,
            deadline=deadline,
            label="bundle artifact begin frame",
        )
        if begin.get("_bundle_artifact_error") is True:
            raise BundleArtifactProtocolError(
                str(begin.get("reason") or "Host refused bundle artifact")
            )
        if set(begin) != {
            "_bundle_artifact_begin",
            "version",
            "manifest_hash",
            "session_binding_hash",
            "manifest",
        }:
            raise BundleArtifactProtocolError("bundle artifact begin fields are invalid")
        if (
            begin.get("_bundle_artifact_begin") is not True
            or type(begin.get("version")) is not int
            or begin.get("version") != BUNDLE_VERSION
            or begin.get("manifest_hash") != expected_offer["manifest_hash"]
            or begin.get("session_binding_hash")
            != expected_offer["session_binding_hash"]
        ):
            raise BundleArtifactProtocolError("bundle artifact begin differs from offer")
        manifest = validate_bundle_manifest(
            begin.get("manifest"),
            expected_protocol_id=protocol_id,
            expected_manifest_hash=expected_offer["manifest_hash"],
        )
        if len(manifest["files"]) != expected_offer["file_count"]:
            raise BundleArtifactProtocolError("bundle manifest file count differs from offer")
        manifest_total = sum(int(item["size_bytes"]) for item in manifest["files"])
        if manifest_total != expected_offer["total_size_bytes"]:
            raise BundleArtifactProtocolError("bundle manifest size differs from offer")

        received: list[tuple[dict[str, Any], bytes]] = []
        for index, expected_file in enumerate(manifest["files"]):
            message = await _recv_before(
                channel,
                deadline=deadline,
                label=f"bundle artifact file frame {index}",
            )
            if set(message) != {
                "_bundle_artifact_file",
                "index",
                "path",
                "kind",
                "content_base64",
                "content_hash",
                "size_bytes",
            }:
                raise BundleArtifactProtocolError(
                    f"bundle artifact file frame {index} fields are invalid"
                )
            if (
                message.get("_bundle_artifact_file") is not True
                or type(message.get("index")) is not int
                or message.get("index") != index
            ):
                raise BundleArtifactProtocolError(
                    f"bundle artifact file frame {index} is out of order"
                )
            record, content = validate_bundle_file_record(
                {
                    key: message[key]
                    for key in (
                        "path",
                        "kind",
                        "content_base64",
                        "content_hash",
                        "size_bytes",
                    )
                },
                index=index,
                expected=expected_file,
            )
            if record["kind"] == "hooks":
                inspection = inspect_hooks_source(content)
                if (
                    list(inspection.methods) != manifest["hook_methods"]
                    or list(inspection.supported_control_modes)
                    != manifest["supported_control_modes"]
                ):
                    raise BundleArtifactProtocolError(
                        "received hooks inspection differs from manifest"
                    )
            received.append((record, content))

        end = await _recv_before(
            channel,
            deadline=deadline,
            label="bundle artifact end frame",
        )
        if set(end) != {
            "_bundle_artifact_end",
            "manifest_hash",
            "session_binding_hash",
        } or (
            end.get("_bundle_artifact_end") is not True
            or end.get("manifest_hash") != expected_offer["manifest_hash"]
            or end.get("session_binding_hash")
            != expected_offer["session_binding_hash"]
        ):
            raise BundleArtifactProtocolError("bundle artifact end differs from offer")

        result = install_received_bundle(
            install_dir,
            protocol_id=protocol_id,
            local_spec_path=local_spec_path,
            manifest=manifest,
            manifest_hash=expected_offer["manifest_hash"],
            files=received,
            offer=expected_offer,
            session_binding=session_binding,
        )
    except Exception:
        await _send_rejected_ack(
            channel,
            manifest_hash=expected_offer["manifest_hash"],
        )
        raise
    await channel.send(
        {
            "_bundle_artifact_ack": True,
            "status": "installed",
            "manifest_hash": expected_offer["manifest_hash"],
            "session_binding_hash": expected_offer["session_binding_hash"],
        }
    )
    return result


async def maybe_receive_host_bundle(
    channel: Any,
    *,
    offer: Any,
    accept_host_bundle: bool,
    session_binding: dict[str, str],
    protocol_id: str,
    local_spec_path: str | Path,
    install_dir: str | Path | None,
) -> dict[str, Any] | None:
    if not accept_host_bundle:
        return None
    if offer is None:
        return None
    if not isinstance(offer, dict):
        raise BundleArtifactProtocolError("bundle offer must be an object")
    if install_dir is None:
        raise BundleArtifactProtocolError("bundle acceptance requires a Session directory")
    return await receive_host_bundle(
        channel,
        offer=offer,
        session_binding=session_binding,
        protocol_id=protocol_id,
        local_spec_path=local_spec_path,
        install_dir=install_dir,
    )


async def _serve_bundle(
    channel: Any,
    *,
    request: dict[str, Any],
    artifact: HostBundleArtifact | None,
    offer: dict[str, Any] | None,
) -> dict[str, Any]:
    if artifact is None or offer is None:
        await channel.send(
            {
                "_bundle_artifact_error": True,
                "reason": "bundle artifact is not shared",
            }
        )
        raise BundleArtifactProtocolError("Guest requested an unavailable bundle")
    if set(request) != {
        "_bundle_artifact_request",
        "version",
        "manifest_hash",
        "session_binding_hash",
    } or (
        request.get("_bundle_artifact_request") is not True
        or type(request.get("version")) is not int
        or request.get("version") != BUNDLE_VERSION
        or request.get("manifest_hash") != offer["manifest_hash"]
        or request.get("session_binding_hash") != offer["session_binding_hash"]
    ):
        await channel.send(
            {
                "_bundle_artifact_error": True,
                "reason": "bundle artifact request mismatch",
            }
        )
        raise BundleArtifactProtocolError("Guest requested a different bundle")

    await channel.send(
        {
            "_bundle_artifact_begin": True,
            "version": BUNDLE_VERSION,
            "manifest_hash": offer["manifest_hash"],
            "session_binding_hash": offer["session_binding_hash"],
            "manifest": artifact.manifest,
        }
    )
    for index, item in enumerate(artifact.files):
        await channel.send(
            {
                "_bundle_artifact_file": True,
                "index": index,
                "path": item["path"],
                "kind": item["kind"],
                "content_base64": item["content_base64"],
                "content_hash": item["content_hash"],
                "size_bytes": item["size_bytes"],
            }
        )
    await channel.send(
        {
            "_bundle_artifact_end": True,
            "manifest_hash": offer["manifest_hash"],
            "session_binding_hash": offer["session_binding_hash"],
        }
    )
    acknowledgement = await _recv_before(
        channel,
        deadline=time.monotonic() + BUNDLE_TRANSFER_TIMEOUT_SECONDS,
        label="bundle artifact acknowledgement",
    )
    if (
        set(acknowledgement)
        != {
            "_bundle_artifact_ack",
            "status",
            "manifest_hash",
            "session_binding_hash",
        }
        or acknowledgement.get("_bundle_artifact_ack") is not True
        or acknowledgement.get("status") != "installed"
        or acknowledgement.get("manifest_hash") != offer["manifest_hash"]
        or acknowledgement.get("session_binding_hash")
        != offer["session_binding_hash"]
    ):
        raise BundleArtifactProtocolError(
            "Guest did not acknowledge bundle installation"
        )
    return await _recv_before(
        channel,
        deadline=time.monotonic() + BUNDLE_TRANSFER_TIMEOUT_SECONDS,
        label="session ready frame",
    )


async def serve_host_artifacts_and_wait_ready(
    channel: Any,
    *,
    bundle_artifact: HostBundleArtifact | None,
    bundle_offer: dict[str, Any] | None,
    ui_artifact: HostUiArtifact | None,
) -> tuple[dict[str, Any], str | None]:
    """Serve a negotiated bundle or UI artifact, then return Session ready."""
    message = _frame(await channel.recv(), "post-proof frame")
    if message.get("_bundle_artifact_request") is True:
        return (
            await _serve_bundle(
                channel,
                request=message,
                artifact=bundle_artifact,
                offer=bundle_offer,
            ),
            "bundle",
        )
    artifact_kind = "ui" if message.get("_ui_artifact_request") is True else None
    return (
        await serve_host_ui_and_wait_ready(
            channel,
            artifact=ui_artifact,
            initial_message=message,
        ),
        artifact_kind,
    )


def describe_pinned_bundle(protocol_dir: str | Path) -> dict[str, Any]:
    return describe_installed_bundle(Path(protocol_dir))


__all__ = [
    "BUNDLE_TRANSFER_TIMEOUT_SECONDS",
    "BundleArtifactProtocolError",
    "describe_pinned_bundle",
    "has_exact_bundle_capability",
    "maybe_receive_host_bundle",
    "receive_host_bundle",
    "serve_host_artifacts_and_wait_ready",
]
