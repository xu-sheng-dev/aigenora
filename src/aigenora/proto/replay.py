from __future__ import annotations

import hashlib
import json
import os
import secrets
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from aigenora.engine.crypto import protocol_hash
from aigenora.engine.keys import KeyPair, sign_raw, verify_raw
from aigenora.proto.group import (
    GroupProtocolError,
    canonical_json,
    checkpoint_certificate_canonical,
    json_hash,
)
from aigenora.proto.group_peer import (
    PeerEvidenceLog,
    verify_peer_message,
    verify_peer_receipt,
)


REPLAY_BUNDLE_VERSION = 1
MAX_BUNDLE_FILES = 64
MAX_BUNDLE_ENTRY_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_TOTAL_BYTES = 128 * 1024 * 1024

_PARTICIPANT_FILES = (
    "session.json",
    "events.jsonl",
    "details.jsonl",
    "snapshot.json",
    "group-checkpoint.json",
    "group-actions.jsonl",
    "group-action-outbox.json",
    "group-client-seq",
    "group-peer-evidence.jsonl",
    "group-peer-actions.jsonl",
    "group-peer-outbox.json",
    "group-peer-listener.json",
    "group-peer-directory.json",
)

_PUBLIC_EVENT_TYPES = {
    "invite_created",
    "peer_joined",
    "session_ended",
    "group_started",
    "group_frame",
    "group_frame_received",
    "group_action_receipt",
    "group_action_rejected",
    "group_member_connected",
    "group_member_reconnected",
    "group_member_disconnected",
    "group_leader_disconnected",
    "group_leadership_claimed",
    "group_leader_changed",
    "group_leader_fenced",
    "group_peer_listener_started",
    "group_peer_directory_updated",
    "group_peer_message_sent",
    "group_peer_message_received",
    "group_peer_message_rejected",
    "group_peer_message_send_failed",
    "group_peer_message_expired",
}

_PUBLIC_EVENT_FIELDS = {
    "action_id",
    "authority_seq",
    "channel",
    "checkpoint_hash",
    "completed",
    "envelope_hash",
    "frame_hash",
    "frame_kind",
    "group_id",
    "leader_epoch",
    "member_id",
    "membership_version",
    "message_id",
    "outcome",
    "participant_count",
    "post_id",
    "protocol_id",
    "public_key",
    "reason",
    "receipt_status",
    "recipient_count",
    "recipient_public_key",
    "sender_public_key",
    "seq",
    "session_id",
    "status",
    "terminal",
    "ticket_hash",
    "seat",
}


def export_replay_bundle(
    state_dir: str | Path,
    output: str | Path,
    *,
    keypair: KeyPair,
    scope: str = "public",
    force: bool = False,
) -> dict[str, Any]:
    if scope not in {"public", "participant"}:
        raise ValueError("replay scope must be public or participant")
    source_root, effective_root = _resolve_roots(Path(state_dir))
    output_path = Path(output)
    if output_path.exists() and not force:
        raise FileExistsError(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    session_meta = _first_json(
        source_root / "session.json", effective_root / "session.json"
    )
    snapshot = _first_json(effective_root / "snapshot.json")
    checkpoint = _first_json(effective_root / "group-checkpoint.json")
    identity = _session_identity(session_meta, snapshot, checkpoint)
    identity["protocol_id"] = _resolve_protocol_id(session_meta, identity)
    if identity.get("group_id") and not _is_hash(identity.get("protocol_id")):
        raise ValueError("group replay requires a valid protocol_id")

    files: dict[str, bytes] = {}
    authority_frames = _authority_frames(effective_root / "details.jsonl")
    files["evidence/authority-frames.jsonl"] = _jsonl_bytes(authority_frames)
    files["evidence/protocol-events.jsonl"] = _jsonl_bytes(
        _protocol_events(effective_root / "details.jsonl")
    )
    files["evidence/public-events.jsonl"] = _jsonl_bytes(
        _public_events(source_root, effective_root)
    )
    files["evidence/peer-index.jsonl"] = _jsonl_bytes(
        _peer_index(effective_root / "group-peer-evidence.jsonl")
    )
    if scope == "participant":
        participant_roots = (
            (("state", effective_root),)
            if source_root == effective_root
            else (("runtime", source_root), ("state", effective_root))
        )
        for prefix, root in participant_roots:
            for name in _PARTICIPANT_FILES:
                path = root / name
                if not path.is_file():
                    continue
                logical = f"participant/{prefix}/{name}"
                if logical in files:
                    continue
                data = path.read_bytes()
                if len(data) > MAX_BUNDLE_ENTRY_BYTES:
                    raise ValueError(f"replay evidence file is too large: {path}")
                files[logical] = data

    file_manifest = [
        {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }
        for path, data in sorted(files.items())
    ]
    membership = _public_membership(checkpoint)
    if identity.get("group_id") and (
        not membership
        or keypair.public_key not in {
            member["public_key"] for member in membership
        }
    ):
        raise ValueError("replay signer is not in the recorded group membership")
    _validate_derived_evidence(files)
    terminal_evidence = _terminal_evidence(checkpoint, authority_frames)
    _validate_terminal_evidence(
        terminal_evidence,
        authority_frames,
        required=bool(identity.get("group_id")),
    )
    body = {
        "bundle_version": REPLAY_BUNDLE_VERSION,
        "bundle_kind": "aigenora-session-replay",
        "scope": scope,
        "exported_at": _now(),
        "participant_public_key": keypair.public_key,
        **identity,
        "membership": membership,
        "terminal_evidence": terminal_evidence,
        "privacy": {
            "contains_participant_view": scope == "participant",
            "contains_peer_payloads": scope == "participant",
            "excludes_private_keys": True,
            "excludes_provider_secrets": True,
            "excludes_hidden_reasoning": True,
        },
        "files": file_manifest,
    }
    signed_body = {**body, "bundle_id": json_hash(body)}
    manifest = {
        **signed_body,
        "signature": sign_raw(keypair.private_key, canonical_json(signed_body)),
    }
    files["manifest.json"] = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _write_zip(output_path, files)
    return {**manifest, "output": str(output_path.resolve())}


def verify_replay_bundle(path: str | Path) -> dict[str, Any]:
    bundle_path = Path(path)
    with zipfile.ZipFile(bundle_path, "r") as archive:
        members = archive.infolist()
        if not 1 <= len(members) <= MAX_BUNDLE_FILES:
            raise ValueError("replay bundle file count is invalid")
        seen: set[str] = set()
        total = 0
        content: dict[str, bytes] = {}
        for member in members:
            logical = _safe_zip_path(member.filename)
            if logical in seen or member.is_dir():
                raise ValueError("replay bundle contains a duplicate or directory entry")
            seen.add(logical)
            if member.file_size > MAX_BUNDLE_ENTRY_BYTES:
                raise ValueError("replay bundle entry exceeds the size limit")
            total += member.file_size
            if total > MAX_BUNDLE_TOTAL_BYTES:
                raise ValueError("replay bundle exceeds the total size limit")
            content[logical] = archive.read(member)
    raw_manifest = content.pop("manifest.json", None)
    if raw_manifest is None:
        raise ValueError("replay bundle has no manifest.json")
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("replay manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("replay manifest must be an object")
    signature = manifest.get("signature")
    signed_body = {key: value for key, value in manifest.items() if key != "signature"}
    body = {key: value for key, value in signed_body.items() if key != "bundle_id"}
    if (
        manifest.get("bundle_version") != REPLAY_BUNDLE_VERSION
        or manifest.get("bundle_kind") != "aigenora-session-replay"
        or manifest.get("scope") not in {"public", "participant"}
        or manifest.get("bundle_id") != json_hash(body)
    ):
        raise ValueError("replay manifest identity is invalid")
    public_key = manifest.get("participant_public_key")
    if not _is_public_key(public_key) or not isinstance(signature, str):
        raise ValueError("replay manifest signer is invalid")
    membership = manifest.get("membership")
    if not isinstance(membership, list):
        raise ValueError("replay membership is invalid")
    if manifest.get("group_id") and (
        not membership
        or public_key not in {
            member.get("public_key")
            for member in membership
            if isinstance(member, dict)
        }
    ):
        raise ValueError("replay signer is not in the recorded membership")
    if manifest.get("group_id") and not _is_hash(manifest.get("protocol_id")):
        raise ValueError("group replay protocol_id is invalid")
    try:
        verify_raw(public_key, canonical_json(signed_body), signature)
    except Exception as exc:
        raise ValueError("replay manifest signature is invalid") from exc
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or len(raw_files) != len(content):
        raise ValueError("replay manifest file inventory is invalid")
    expected_paths: set[str] = set()
    for entry in raw_files:
        if not isinstance(entry, dict):
            raise ValueError("replay manifest file entry must be an object")
        logical = _safe_zip_path(entry.get("path"))
        if logical in expected_paths or logical not in content:
            raise ValueError("replay manifest file path is invalid")
        expected_paths.add(logical)
        data = content[logical]
        if (
            entry.get("size_bytes") != len(data)
            or entry.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
            raise ValueError(f"replay evidence hash mismatch: {logical}")
    if expected_paths != set(content):
        raise ValueError("replay bundle contains unlisted evidence")
    _validate_derived_evidence(content)
    authority_frames = _decode_jsonl_bytes(
        content.get("evidence/authority-frames.jsonl", b"")
    )
    _validate_terminal_evidence(
        manifest.get("terminal_evidence"),
        authority_frames,
        required=bool(manifest.get("group_id")),
    )
    peer_index = _decode_jsonl_bytes(
        content.get("evidence/peer-index.jsonl", b"")
    )
    if manifest.get("scope") == "participant":
        _validate_participant_peer_evidence(content, peer_index)
    return {
        "status": "verified",
        "path": str(bundle_path.resolve()),
        "manifest": manifest,
        "authority_frames": authority_frames,
        "peer_index": peer_index,
    }


def reconcile_replay_bundles(paths: Iterable[str | Path]) -> dict[str, Any]:
    verified = [verify_replay_bundle(path) for path in paths]
    if len(verified) < 2:
        raise ValueError("replay reconciliation requires at least two bundles")
    group_ids = {
        item["manifest"].get("group_id") for item in verified
        if item["manifest"].get("group_id")
    }
    protocol_ids = {
        item["manifest"].get("protocol_id") for item in verified
        if item["manifest"].get("protocol_id")
    }
    if len(group_ids) != 1 or len(protocol_ids) != 1:
        raise ValueError("replay bundles do not describe one group and protocol")
    participants = [
        item["manifest"]["participant_public_key"] for item in verified
    ]
    duplicate_participants = sorted(
        {public_key for public_key in participants if participants.count(public_key) > 1}
    )

    frame_observations: dict[tuple[int, int], dict[str, set[str]]] = {}
    coverage: list[dict[str, Any]] = []
    for item in verified:
        public_key = item["manifest"]["participant_public_key"]
        per_epoch: dict[int, list[int]] = {}
        for frame in item["authority_frames"]:
            epoch = frame.get("leader_epoch")
            seq = frame.get("seq")
            frame_hash = frame.get("frame_hash")
            if not isinstance(epoch, int) or not isinstance(seq, int) or not _is_hash(frame_hash):
                continue
            per_epoch.setdefault(epoch, []).append(seq)
            observers = frame_observations.setdefault((epoch, seq), {})
            observers.setdefault(frame_hash, set()).add(public_key)
        gaps: list[dict[str, int]] = []
        for epoch, seqs in sorted(per_epoch.items()):
            ordered = sorted(set(seqs))
            for previous, current in zip(ordered, ordered[1:]):
                if current != previous + 1:
                    gaps.append({"leader_epoch": epoch, "after": previous, "before": current})
        coverage.append(
            {
                "participant_public_key": public_key,
                "frame_count": len(item["authority_frames"]),
                "gaps": gaps,
            }
        )
    conflicts = [
        {
            "leader_epoch": epoch,
            "seq": seq,
            "variants": [
                {"frame_hash": frame_hash, "participants": sorted(observers)}
                for frame_hash, observers in sorted(variants.items())
            ],
        }
        for (epoch, seq), variants in sorted(frame_observations.items())
        if len(variants) > 1
    ]

    peer_observations: dict[str, list[dict[str, Any]]] = {}
    for item in verified:
        public_key = item["manifest"]["participant_public_key"]
        for record in item["peer_index"]:
            message_id = record.get("message_id")
            if isinstance(message_id, str):
                peer_observations.setdefault(message_id, []).append(
                    {**record, "bundle_participant": public_key}
                )
    peer_matches: list[dict[str, Any]] = []
    peer_unmatched: list[dict[str, Any]] = []
    peer_conflicts: list[dict[str, Any]] = []
    for message_id, observations in sorted(peer_observations.items()):
        hashes = {item.get("envelope_hash") for item in observations}
        directions = {item.get("direction") for item in observations}
        summary = {"message_id": message_id, "observations": observations}
        if len(hashes) > 1:
            peer_conflicts.append(summary)
        elif {"sent", "received"} <= directions:
            sent_routes = {
                (item["bundle_participant"], item.get("peer_public_key"))
                for item in observations
                if item.get("direction") == "sent"
            }
            received_routes = {
                (item.get("peer_public_key"), item["bundle_participant"])
                for item in observations
                if item.get("direction") == "received"
            }
            if sent_routes & received_routes:
                peer_matches.append(summary)
            else:
                peer_conflicts.append(summary)
        else:
            peer_unmatched.append(summary)

    terminal_reports = [
        {
            "participant_public_key": item["manifest"]["participant_public_key"],
            "terminal_evidence": item["manifest"].get("terminal_evidence"),
        }
        for item in verified
    ]
    incomplete_terminal = [
        item["participant_public_key"]
        for item in terminal_reports
        if not isinstance(item["terminal_evidence"], dict)
        or item["terminal_evidence"].get("completed") is not True
    ]
    completed_terminal_variants = {
        (
            item["terminal_evidence"].get("leader_epoch"),
            item["terminal_evidence"].get("seq"),
            item["terminal_evidence"].get("frame_hash"),
            json.dumps(
                item["terminal_evidence"].get("outcome"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        for item in terminal_reports
        if isinstance(item["terminal_evidence"], dict)
        and item["terminal_evidence"].get("completed") is True
    }
    terminal_conflict = len(completed_terminal_variants) > 1
    status = "ok"
    if conflicts or peer_conflicts or duplicate_participants or terminal_conflict:
        status = "conflict"
    elif (
        peer_unmatched
        or incomplete_terminal
        or any(item["gaps"] for item in coverage)
    ):
        status = "incomplete"
    head = max(frame_observations, default=None)
    return {
        "reconciliation_version": 1,
        "status": status,
        "group_id": next(iter(group_ids)),
        "protocol_id": next(iter(protocol_ids)),
        "participants": participants,
        "duplicate_participants": duplicate_participants,
        "authority_head": (
            {"leader_epoch": head[0], "seq": head[1]} if head is not None else None
        ),
        "coverage": coverage,
        "authority_conflicts": conflicts,
        "peer_matches": peer_matches,
        "peer_unmatched": peer_unmatched,
        "peer_conflicts": peer_conflicts,
        "terminal_reports": terminal_reports,
        "incomplete_terminal_participants": incomplete_terminal,
        "terminal_conflict": terminal_conflict,
        "limitations": [
            "Consistency covers submitted Aigenora evidence only.",
            "Missing bundles and off-platform communication cannot be disproved.",
        ],
    }


def _resolve_roots(path: Path) -> tuple[Path, Path]:
    root = path.resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(root)
    if (root / "session.json").is_file():
        children = sorted(
            [
                child
                for child in root.iterdir()
                if child.is_dir()
                and (child.name.startswith("host-") or child.name.startswith("guest-"))
                and (
                    (child / "snapshot.json").is_file()
                    or (child / "details.jsonl").is_file()
                )
            ],
            key=lambda child: child.name,
        )
        return root, children[-1] if children else root
    parent = root.parent if (root.parent / "session.json").is_file() else root
    return parent, root


def _session_identity(
    session_meta: dict[str, Any],
    snapshot: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    group = snapshot.get("group") if isinstance(snapshot.get("group"), dict) else {}
    return {
        "session_id": _first_string(
            session_meta.get("session_id"), group.get("group_id"), checkpoint.get("group_id")
        ),
        "group_id": _first_string(
            session_meta.get("group_id"), group.get("group_id"), checkpoint.get("group_id")
        ),
        "protocol_id": _first_string(session_meta.get("protocol_id")),
        "group_role": _first_string(session_meta.get("group_role"), snapshot.get("role")),
        "seat": session_meta.get("seat"),
    }


def _resolve_protocol_id(
    session_meta: dict[str, Any], identity: dict[str, Any]
) -> str:
    """Recover legacy daemon metadata from the pinned local protocol bundle.

    Older guest daemons omitted ``protocol_id`` from ``session.json``.  The
    recorded trusted local bundle paths are sufficient to recompute the same
    content-addressed identifier without mutating the completed session.
    """
    declared = _first_string(identity.get("protocol_id"))
    derived: set[str] = set()
    for name in ("protocol_dir", "local_protocol_dir"):
        raw = session_meta.get(name)
        if not isinstance(raw, str) or not raw:
            continue
        spec_path = Path(raw).expanduser().resolve() / "spec.json"
        if spec_path.is_file():
            derived.add(protocol_hash(spec_path))
    if len(derived) > 1:
        raise ValueError("session protocol directories resolve to different protocol_ids")
    computed = next(iter(derived), "")
    if declared and computed and declared != computed:
        raise ValueError("session protocol_id does not match the trusted local bundle")
    return declared or computed


def _authority_frames(path: Path) -> list[dict[str, Any]]:
    merged: dict[tuple[int, int, str], dict[str, Any]] = {}
    for record in _read_jsonl(path):
        if record.get("type") not in {"group_frame", "group_recovery_record"}:
            continue
        epoch = record.get("leader_epoch")
        seq = record.get("seq")
        frame_hash = record.get("frame_hash")
        if not isinstance(epoch, int) or not isinstance(seq, int) or not _is_hash(frame_hash):
            continue
        key = (epoch, seq, frame_hash)
        entry = merged.setdefault(
            key,
            {
                "group_id": record.get("group_id"),
                "leader_epoch": epoch,
                "seq": seq,
                "frame_hash": frame_hash,
            },
        )
        for field in (
            "leader_public_key",
            "membership_version",
            "recovery_state_hash",
            "checkpoint_hash",
            "checkpoint_signature",
            "checkpoint_mode",
            "frame_kind",
            "wire_version",
            "previous_hash",
            "authority_state_hash",
            "events_hash",
            "completed",
            "outcome",
            "ts",
        ):
            if record.get(field) is not None:
                entry[field] = record[field]
    return [merged[key] for key in sorted(merged)]


def _protocol_events(path: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for record in _read_jsonl(path):
        if record.get("type") != "group_frame":
            continue
        events = record.get("events")
        if not isinstance(events, list):
            continue
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            output.append(
                {
                    "group_id": record.get("group_id"),
                    "leader_epoch": record.get("leader_epoch"),
                    "seq": record.get("seq"),
                    "frame_hash": record.get("frame_hash"),
                    "events_hash": record.get("events_hash"),
                    "event_index": index,
                    "event": event,
                    "ts": record.get("ts"),
                }
            )
    return output


def _public_events(source_root: Path, effective_root: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    paths = [source_root / "events.jsonl"]
    if effective_root != source_root:
        paths.append(effective_root / "events.jsonl")
    for path in paths:
        for record in _read_jsonl(path):
            event_type = record.get("type")
            if event_type not in _PUBLIC_EVENT_TYPES:
                continue
            raw_data = record.get("data")
            data = {
                key: value
                for key, value in (raw_data.items() if isinstance(raw_data, dict) else [])
                if key in _PUBLIC_EVENT_FIELDS
            }
            output.append(
                {
                    "ts": record.get("ts"),
                    "type": event_type,
                    "data": data,
                }
            )
    output.sort(key=lambda item: str(item.get("ts") or ""))
    return output


def _peer_index(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = PeerEvidenceLog(path.parent).read_all(verify=True)
    return _peer_index_from_records(records)


def _peer_index_from_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for record in records:
        receipt = record.get("receipt")
        output.append(
            {
                "record_seq": record.get("record_seq"),
                "record_hash": record.get("record_hash"),
                "recorded_at": record.get("recorded_at"),
                "direction": record.get("direction"),
                "leader_epoch": record.get("leader_epoch"),
                "authority_seq": record.get("authority_seq"),
                "message_id": record.get("message_id"),
                "envelope_hash": record.get("envelope_hash"),
                "peer_public_key": record.get("peer_public_key"),
                "channel": record.get("channel"),
                "receipt_status": (
                    receipt.get("status") if isinstance(receipt, dict) else None
                ),
                "receipt_hash": (
                    json_hash(receipt) if isinstance(receipt, dict) else None
                ),
            }
        )
    return output


def _validate_participant_peer_evidence(
    content: dict[str, bytes], peer_index: list[dict[str, Any]]
) -> None:
    paths = sorted(
        path
        for path in content
        if path.startswith("participant/")
        and path.endswith("/group-peer-evidence.jsonl")
    )
    if not paths:
        if peer_index:
            raise ValueError("participant replay omits its peer evidence log")
        return
    derived_indexes: list[list[dict[str, Any]]] = []
    for path in paths:
        records = _decode_jsonl_bytes(content[path])
        previous_hash = "0" * 64
        for expected_seq, record in enumerate(records, start=1):
            claimed_hash = record.get("record_hash")
            unsigned = dict(record)
            unsigned.pop("record_hash", None)
            if (
                record.get("record_seq") != expected_seq
                or record.get("previous_hash") != previous_hash
                or not _is_hash(claimed_hash)
                or json_hash(unsigned) != claimed_hash
            ):
                raise ValueError("participant peer evidence hash chain is invalid")
            previous_hash = claimed_hash
            message = record.get("message")
            receipt = record.get("receipt")
            if not isinstance(message, dict) or not isinstance(receipt, dict):
                raise ValueError("participant peer evidence envelope is missing")
            grant = message.get("grant")
            channels = grant.get("channels") if isinstance(grant, dict) else None
            ticket_hash = (
                grant.get("recipient_ticket_hash")
                if isinstance(grant, dict)
                else None
            )
            if (
                not isinstance(channels, list)
                or not channels
                or not _is_hash(ticket_hash)
            ):
                raise ValueError("participant peer evidence grant is invalid")
            try:
                verified_message = verify_peer_message(
                    message,
                    group_id=str(message.get("group_id") or ""),
                    protocol_id=str(message.get("protocol_id") or ""),
                    leader_public_key=str(
                        message.get("leader_public_key") or ""
                    ),
                    leader_epoch=int(message.get("leader_epoch", -1)),
                    authority_seq=int(message.get("authority_seq", -1)),
                    membership_version=int(
                        message.get("membership_version", -1)
                    ),
                    recipient_public_key=str(
                        message.get("recipient_public_key") or ""
                    ),
                    recipient_ticket_hash=ticket_hash,
                    allowed_channels=tuple(str(item) for item in channels),
                    max_message_bytes=65536,
                )
                envelope_hash = json_hash(verified_message)
                verify_peer_receipt(
                    receipt,
                    message=verified_message,
                    envelope_hash=envelope_hash,
                )
            except (TypeError, ValueError, GroupProtocolError) as exc:
                raise ValueError(
                    "participant peer evidence signature is invalid"
                ) from exc
            direction = record.get("direction")
            expected_peer = (
                message.get("recipient_public_key")
                if direction == "sent"
                else message.get("sender_public_key")
                if direction == "received"
                else None
            )
            if (
                record.get("group_id") != message.get("group_id")
                or record.get("protocol_id") != message.get("protocol_id")
                or record.get("leader_epoch") != message.get("leader_epoch")
                or record.get("authority_seq") != message.get("authority_seq")
                or record.get("message_id") != message.get("message_id")
                or record.get("envelope_hash") != envelope_hash
                or record.get("peer_public_key") != expected_peer
                or record.get("channel") != message.get("channel")
            ):
                raise ValueError("participant peer evidence context is invalid")
        derived_indexes.append(_peer_index_from_records(records))
    if peer_index not in derived_indexes:
        raise ValueError("participant peer evidence does not match the public index")


def _terminal_evidence(
    checkpoint: dict[str, Any], frames: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if checkpoint:
        return {
            key: checkpoint.get(key)
            for key in (
                "leader_public_key",
                "leader_epoch",
                "seq",
                "frame_hash",
                "checkpoint_hash",
                "checkpoint_signature",
                "completed",
                "outcome",
            )
        }
    return frames[-1] if frames else None


def _public_membership(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    members = checkpoint.get("members")
    if not isinstance(members, list):
        return []
    output: list[dict[str, Any]] = []
    for member in members:
        if not isinstance(member, dict) or not _is_public_key(member.get("public_key")):
            continue
        output.append(
            {
                "member_id": member.get("member_id"),
                "public_key": member["public_key"],
                "seat": member.get("seat"),
                "status": member.get("status"),
            }
        )
    return output


def _validate_terminal_evidence(
    terminal: Any,
    frames: list[dict[str, Any]],
    *,
    required: bool,
) -> None:
    if terminal is None and not required:
        return
    if not isinstance(terminal, dict):
        raise ValueError("replay terminal evidence is missing or invalid")
    key = (
        terminal.get("leader_epoch"),
        terminal.get("seq"),
        terminal.get("frame_hash"),
    )
    matched = next(
        (
            frame
            for frame in frames
            if (
                frame.get("leader_epoch"),
                frame.get("seq"),
                frame.get("frame_hash"),
            )
            == key
        ),
        None,
    )
    if matched is None:
        raise ValueError("replay terminal evidence has no authority frame")
    for field in (
        "leader_public_key",
        "checkpoint_hash",
        "checkpoint_signature",
        "completed",
        "outcome",
    ):
        if terminal.get(field) != matched.get(field):
            raise ValueError("replay terminal evidence does not match its frame")


def _validate_derived_evidence(content: dict[str, bytes]) -> None:
    frames = _decode_jsonl_bytes(content.get("evidence/authority-frames.jsonl", b""))
    seen: dict[tuple[int, int], str] = {}
    frame_by_key: dict[tuple[int, int, str], dict[str, Any]] = {}
    for frame in frames:
        epoch = frame.get("leader_epoch")
        seq = frame.get("seq")
        frame_hash = frame.get("frame_hash")
        if (
            not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or epoch < 0
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < 0
            or not _is_hash(frame_hash)
        ):
            raise ValueError("authority frame evidence is invalid")
        key = (epoch, seq)
        if key in seen and seen[key] != frame_hash:
            raise ValueError("one replay bundle contains authority equivocation")
        seen[key] = frame_hash
        core_fields = {
            "_group": frame.get("frame_kind"),
            "wire_version": frame.get("wire_version"),
            "group_id": frame.get("group_id"),
            "leader_public_key": frame.get("leader_public_key"),
            "leader_epoch": epoch,
            "seq": seq,
            "previous_hash": frame.get("previous_hash"),
            "membership_version": frame.get("membership_version"),
            "authority_state_hash": frame.get("authority_state_hash"),
            "recovery_state_hash": frame.get("recovery_state_hash"),
            "events_hash": frame.get("events_hash"),
            "completed": frame.get("completed"),
            "outcome": frame.get("outcome"),
        }
        required_core_fields = set(core_fields) - {"outcome"}
        if not all(core_fields[field] is not None for field in required_core_fields):
            raise ValueError("authority frame core evidence is incomplete")
        if (
            core_fields["_group"]
            not in {"snapshot", "membership", "epoch_start", "frame"}
            or core_fields["wire_version"] != 2
            or not isinstance(core_fields["group_id"], str)
            or not core_fields["group_id"]
            or not _is_public_key(core_fields["leader_public_key"])
            or not _is_hash(core_fields["previous_hash"])
            or not isinstance(core_fields["membership_version"], int)
            or isinstance(core_fields["membership_version"], bool)
            or not _is_hash(core_fields["authority_state_hash"])
            or not _is_hash(core_fields["recovery_state_hash"])
            or not _is_hash(core_fields["events_hash"])
            or not isinstance(core_fields["completed"], bool)
        ):
            raise ValueError("authority frame core evidence is invalid")
        if json_hash(core_fields) != frame_hash:
            raise ValueError("authority frame core hash is invalid")
        checkpoint_hash = frame.get("checkpoint_hash")
        checkpoint_signature = frame.get("checkpoint_signature")
        if not _is_hash(checkpoint_hash) or not isinstance(
            checkpoint_signature, str
        ):
            raise ValueError("authority checkpoint certificate is incomplete")
        certificate = {
            "group_id": core_fields["group_id"],
            "leader_public_key": core_fields["leader_public_key"],
            "leader_epoch": epoch,
            "seq": seq,
            "frame_hash": frame_hash,
            "membership_version": core_fields["membership_version"],
            "checkpoint_hash": checkpoint_hash,
        }
        try:
            verify_raw(
                core_fields["leader_public_key"],
                checkpoint_certificate_canonical(certificate).encode("utf-8"),
                checkpoint_signature,
            )
        except Exception as exc:
            raise ValueError(
                "authority checkpoint certificate signature is invalid"
            ) from exc
        frame_by_key[(epoch, seq, frame_hash)] = frame

    ordered_frames = sorted(
        frame_by_key.values(),
        key=lambda item: (int(item["seq"]), int(item["leader_epoch"])),
    )
    if ordered_frames and ordered_frames[0]["seq"] == 0:
        if ordered_frames[0].get("previous_hash") != "0" * 64:
            raise ValueError("authority frame chain does not start at zero")
    for previous, current in zip(ordered_frames, ordered_frames[1:]):
        if current["seq"] <= previous["seq"]:
            raise ValueError("authority frame sequence is not monotonic")
        if current["seq"] == previous["seq"] + 1 and (
            current.get("previous_hash") != previous.get("frame_hash")
        ):
            raise ValueError("authority frame hash chain is invalid")

    protocol_events = _decode_jsonl_bytes(
        content.get("evidence/protocol-events.jsonl", b"")
    )
    by_frame: dict[tuple[int, int, str], list[dict[str, Any]]] = {}
    for record in protocol_events:
        key = (
            record.get("leader_epoch"),
            record.get("seq"),
            record.get("frame_hash"),
        )
        if not isinstance(key[0], int) or not isinstance(key[1], int) or not _is_hash(key[2]):
            raise ValueError("protocol event evidence is invalid")
        by_frame.setdefault(key, []).append(record)
    for key, records in by_frame.items():
        frame = frame_by_key.get(key)
        if frame is None:
            raise ValueError("protocol events reference an unknown authority frame")
        ordered = sorted(records, key=lambda item: int(item.get("event_index", -1)))
        if [item.get("event_index") for item in ordered] != list(
            range(len(ordered))
        ):
            raise ValueError("protocol event indexes are invalid")
        events = [item.get("event") for item in ordered]
        expected_hashes = {item.get("events_hash") for item in ordered}
        if (
            len(expected_hashes) != 1
            or frame.get("events_hash") not in expected_hashes
            or json_hash(events) != frame.get("events_hash")
        ):
            raise ValueError("protocol event hash is invalid")
    for key, frame in frame_by_key.items():
        if key not in by_frame and frame.get("events_hash") != json_hash([]):
            raise ValueError("authority frame protocol events are missing")
    for record in _decode_jsonl_bytes(content.get("evidence/peer-index.jsonl", b"")):
        if (
            not _is_message_id(record.get("message_id"))
            or record.get("direction") not in {"sent", "received"}
            or not _is_hash(record.get("envelope_hash"))
            or not _is_public_key(record.get("peer_public_key"))
            or not isinstance(record.get("channel"), str)
            or not record.get("channel")
            or not _is_hash(record.get("record_hash"))
            or not _is_hash(record.get("receipt_hash"))
        ):
            raise ValueError("peer index evidence is invalid")


def _write_zip(path: Path, files: dict[str, bytes]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for logical, data in sorted(files.items()):
                info = zipfile.ZipInfo(logical, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _safe_zip_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("replay bundle path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("replay bundle path is unsafe")
    return path.as_posix()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return _decode_jsonl_bytes(path.read_bytes())


def _decode_jsonl_bytes(data: bytes) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in data.splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("replay JSONL evidence is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("replay JSONL evidence must contain objects")
        records.append(value)
    return records


def _jsonl_bytes(records: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
        for record in records
    )


def _first_json(*paths: Path) -> dict[str, Any]:
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _is_public_key(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_hash(value: Any) -> bool:
    return _is_public_key(value)


def _is_message_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "REPLAY_BUNDLE_VERSION",
    "export_replay_bundle",
    "reconcile_replay_bundles",
    "verify_replay_bundle",
]
