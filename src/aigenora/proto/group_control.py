from __future__ import annotations

from typing import Any

from aigenora.engine.crypto import transport_binding_canonical
from aigenora.engine.keys import KeyPair, sign_raw
from aigenora.engine.rest import RestClient


EMPTY_CHECKPOINT_HASH = "0" * 64


def admission_canonical(
    group_id: str,
    leader_public_key: str,
    member_public_key: str,
    protocol_id: str,
    leader_epoch: int,
    join_nonce: str,
) -> str:
    return (
        "aigenora-group-admission-v1:"
        f"{group_id}:{leader_public_key}:{member_public_key}:"
        f"{protocol_id}:{leader_epoch}:{join_nonce}"
    )


def create_group(
    client: RestClient,
    *,
    post_id: str,
    min_participants: int,
    max_participants: int,
    allow_late_join: bool,
    recovery_mode: str,
) -> dict[str, Any]:
    value = client.json(
        "POST",
        "/api/v1/groups",
        {
            "post_id": post_id,
            "min_participants": min_participants,
            "max_participants": max_participants,
            "allow_late_join": allow_late_join,
            "recovery_mode": recovery_mode,
        },
        expected={200, 201},
    )
    return _object(value, "group create response")


def get_group_by_post(client: RestClient, post_id: str) -> dict[str, Any]:
    value = client.json(
        "GET", f"/api/v1/groups/by-post/{post_id}", expected={200}
    )
    return _object(value, "group response")


def get_group(client: RestClient, group_id: str) -> dict[str, Any]:
    value = client.json("GET", f"/api/v1/groups/{group_id}", expected={200})
    return _object(value, "group response")


def admit_member(
    client: RestClient,
    keypair: KeyPair,
    *,
    group: dict[str, Any],
    join_nonce: str,
    leader_signature: str,
) -> dict[str, Any]:
    group_id = _string(group, "group_id")
    leader_public_key = _string(group, "leader_public_key")
    protocol_id = _string(group, "protocol_id")
    leader_epoch = _integer(group, "leader_epoch")
    canonical = admission_canonical(
        group_id,
        leader_public_key,
        keypair.public_key,
        protocol_id,
        leader_epoch,
        join_nonce,
    )
    member_signature = sign_raw(
        keypair.private_key, canonical.encode("utf-8")
    )
    value = client.json(
        "POST",
        f"/api/v1/groups/{group_id}/members",
        {
            "join_nonce": join_nonce,
            "leader_epoch": leader_epoch,
            "leader_signature": leader_signature,
            "member_signature": member_signature,
        },
        expected={200, 201},
    )
    return _object(value, "group admission response")


def renew_leader(
    client: RestClient,
    keypair: KeyPair,
    *,
    group_id: str,
    protocol_id: str,
    leader_epoch: int,
    checkpoint_seq: int,
    checkpoint_hash: str,
    iroh_ticket: str,
) -> dict[str, Any]:
    signature = _transport_signature(
        keypair, protocol_id=protocol_id, iroh_ticket=iroh_ticket
    )
    value = client.json(
        "POST",
        f"/api/v1/groups/{group_id}/heartbeat",
        {
            "leader_epoch": leader_epoch,
            "checkpoint_seq": checkpoint_seq,
            "checkpoint_hash": checkpoint_hash,
            "iroh_ticket": iroh_ticket,
            "transport_binding_signature": signature,
        },
        expected={200},
    )
    return _object(value, "group heartbeat response")


def heartbeat_member(client: RestClient, group_id: str) -> dict[str, Any]:
    value = client.json(
        "POST",
        f"/api/v1/groups/{group_id}/members/heartbeat",
        {},
        expected={200},
    )
    return _object(value, "member heartbeat response")


def claim_leader(
    client: RestClient,
    keypair: KeyPair,
    *,
    group_id: str,
    protocol_id: str,
    expected_epoch: int,
    checkpoint: dict[str, Any],
    iroh_ticket: str,
) -> dict[str, Any]:
    signature = _transport_signature(
        keypair, protocol_id=protocol_id, iroh_ticket=iroh_ticket
    )
    value = client.json(
        "POST",
        f"/api/v1/groups/{group_id}/claim-leader",
        {
            "expected_epoch": expected_epoch,
            "checkpoint_seq": _integer(checkpoint, "seq"),
            "checkpoint_hash": _string(checkpoint, "checkpoint_hash"),
            "checkpoint_frame_hash": _string(checkpoint, "frame_hash"),
            "checkpoint_membership_version": _integer(
                checkpoint, "membership_version"
            ),
            "checkpoint_signature": _string(
                checkpoint, "checkpoint_signature"
            ),
            "iroh_ticket": iroh_ticket,
            "transport_binding_signature": signature,
        },
        expected={200},
    )
    return _object(value, "leader claim response")


def leave_group(client: RestClient, group_id: str) -> dict[str, Any]:
    value = client.json(
        "POST", f"/api/v1/groups/{group_id}/leave", {}, expected={200}
    )
    return _object(value, "group leave response")


def update_group_status(
    client: RestClient,
    *,
    group_id: str,
    leader_epoch: int,
    status: str,
) -> dict[str, Any]:
    value = client.json(
        "POST",
        f"/api/v1/groups/{group_id}/status",
        {"leader_epoch": leader_epoch, "status": status},
        expected={200},
    )
    return _object(value, "group status response")


def _transport_signature(
    keypair: KeyPair, *, protocol_id: str, iroh_ticket: str
) -> str:
    canonical = transport_binding_canonical(
        keypair.public_key, "iroh", iroh_ticket, protocol_id
    )
    return sign_raw(keypair.private_key, canonical.encode("utf-8"))


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RuntimeError(f"group field {key} must be a non-empty string")
    return item


def _integer(value: dict[str, Any], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise RuntimeError(f"group field {key} must be a non-negative integer")
    return item
