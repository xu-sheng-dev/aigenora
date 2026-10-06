"""In-stream ``admission_psk_v1`` handshake (design doc §4.3).

Runs on the raw iroh JSON-line channel BEFORE ``_session_init``. Both sides
must already hold the shared 32-byte PSK; the handshake additionally binds:

- the pinned community Ed25519 keys of both devices (signatures over the
  transcript, domain-separated from the legacy ``session_canonical``),
- the REAL iroh node IDs read from the live connection objects (not the
  self-reported ``transport_info.endpoint_id``),
- the collaboration protocol id and fresh per-connection nonces.

MAC confirmation keys are derived per-direction with HKDF-SHA256 so PSK
possession is proven without ever transmitting it. All comparisons are
constant-time. A failed handshake drops only that candidate connection; the
host keeps serving.

This is the M1a sub-phase admission: it authenticates before any business
frame but after the TCP/QUIC dial (the strict pre-dial private-invitation
path is the M1 server-side work per §4.2).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from aigenora.collab.errors import AdmissionError
from aigenora.collab.psk import PeerRegistry, PreSharedKey
from aigenora.engine.keys import sign_raw, verify_raw

ADMISSION_DOMAIN = "aigenora/admission-psk/v1"
ADMISSION_SIGN_PREFIX = "aigenora/admission-psk/v1/sign"
ADMISSION_VERSION = 1
MAX_ADMISSION_FRAME_BYTES = 4096

# §4.3: per-handshake monotonic budget covering only the admission exchange.
DEFAULT_HANDSHAKE_TIMEOUT_SECONDS = 10.0


def _canonical(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _derive_keys(psk: PreSharedKey, transcript: bytes) -> tuple[bytes, bytes, bytes]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    salt = hashlib.sha256(transcript).digest()

    def derive(info: bytes) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=info,
        ).derive(psk.material())

    host_key = derive(f"{ADMISSION_DOMAIN}/host-confirm".encode())
    guest_key = derive(f"{ADMISSION_DOMAIN}/guest-confirm".encode())
    final_key = derive(f"{ADMISSION_DOMAIN}/final".encode())
    return host_key, guest_key, final_key


def _mac(key: bytes, transcript: bytes, label: str) -> str:
    return hmac.new(key, label.encode() + transcript, hashlib.sha256).hexdigest()


def _verify_mac(key: bytes, transcript: bytes, label: str, claimed: str) -> bool:
    return hmac.compare_digest(_mac(key, transcript, label), claimed or "")


def build_transcript(
    *,
    key_id: str,
    key_epoch: int,
    protocol_id: str,
    host_public_key: str,
    guest_public_key: str,
    host_node_id: str,
    guest_node_id: str,
    guest_nonce: str,
    host_nonce: str,
    session_nonce: str,
) -> bytes:
    return _canonical(
        {
            "domain": ADMISSION_DOMAIN,
            "v": ADMISSION_VERSION,
            "key_id": key_id,
            "key_epoch": key_epoch,
            "protocol_id": protocol_id,
            "host_public_key": host_public_key,
            "guest_public_key": guest_public_key,
            "host_node_id": host_node_id,
            "guest_node_id": guest_node_id,
            "guest_nonce": guest_nonce,
            "host_nonce": host_nonce,
            "session_nonce": session_nonce,
        }
    )


@dataclass
class AdmissionResult:
    peer_public_key: str
    peer_node_id: str
    session_nonce: str


def _sign_transcript(private_key_hex: str, transcript: bytes) -> str:
    return sign_raw(private_key_hex, ADMISSION_SIGN_PREFIX.encode() + b"\n" + transcript)


def _verify_transcript_signature(public_key: str, transcript: bytes, signature: str) -> None:
    verify_raw(public_key, ADMISSION_SIGN_PREFIX.encode() + b"\n" + transcript, signature)


def _check_frame(frame: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(frame, dict):
        raise AdmissionError("bad_frame", "admission frame must be an object")
    for key in keys:
        if key not in frame:
            raise AdmissionError("bad_frame", f"missing field {key}")
    return frame


async def _node_id_of(node: Any) -> str:
    """Read the local iroh node id (async in the real FFI; sync in test fakes)."""
    value = node.net().node_id()
    if hasattr(value, "__await__"):
        value = await value
    return str(value)


def _remote_node_id_of(conn: Any) -> str:
    return str(conn.remote_node_id())


async def run_guest_admission(
    channel: Any,
    *,
    psk: PreSharedKey,
    expected_host_public_key: str,
    protocol_id: str,
    local_node: Any,
    conn: Any,
    session_nonce: str,
    guest_private_key_hex: str,
    guest_public_key: str,
    host_node_id_hint: str | None = None,
) -> AdmissionResult:
    """Guest side of the admission handshake. Raises AdmissionError on failure."""
    guest_nonce = hashlib.sha256(
        f"{time.time_ns()}:{session_nonce}".encode() + secrets.token_bytes(16)
    ).hexdigest()
    init = {
        "_admission_init": True,
        "v": ADMISSION_VERSION,
        "key_id": psk.key_id,
        "key_epoch": psk.key_epoch,
        "guest_public_key": guest_public_key,
        "guest_nonce": guest_nonce,
        "session_nonce": session_nonce,
        "protocol_id": protocol_id,
    }
    await channel.send(init)
    challenge = await channel.recv()
    _check_frame(
        challenge,
        (
            "_admission_challenge",
            "host_public_key",
            "host_nonce",
            "host_signature",
            "host_mac",
        ),
    )
    host_public_key = str(challenge["host_public_key"])
    if host_public_key != expected_host_public_key:
        raise AdmissionError("host_key_mismatch", "challenge host key differs from pinned peer")
    host_nonce = str(challenge["host_nonce"])
    remote_node = _remote_node_id_of(conn)
    local_node_id = await _node_id_of(local_node)
    if host_node_id_hint is not None and remote_node != host_node_id_hint:
        raise AdmissionError("node_id_mismatch", "connected node differs from ticket address")
    transcript = build_transcript(
        key_id=psk.key_id,
        key_epoch=psk.key_epoch,
        protocol_id=protocol_id,
        host_public_key=host_public_key,
        guest_public_key=guest_public_key,
        host_node_id=remote_node,
        guest_node_id=local_node_id,
        guest_nonce=guest_nonce,
        host_nonce=host_nonce,
        session_nonce=session_nonce,
    )
    host_key_material, guest_key_material, _ = _derive_keys(psk, transcript)
    _verify_transcript_signature(host_public_key, transcript, str(challenge["host_signature"]))
    if not _verify_mac(host_key_material, transcript, "host", str(challenge["host_mac"])):
        raise AdmissionError("host_mac_invalid", "host PSK confirmation failed")
    finish = {
        "_admission_finish": True,
        "guest_signature": _sign_transcript(guest_private_key_hex, transcript),
        "guest_mac": _mac(guest_key_material, transcript, "guest"),
    }
    await channel.send(finish)
    confirm = await channel.recv()
    if not isinstance(confirm, dict) or confirm.get("_admission_reject"):
        code = confirm.get("_admission_reject", "rejected") if isinstance(confirm, dict) else "bad_frame"
        raise AdmissionError(str(code), "host rejected admission")
    _check_frame(confirm, ("_admission_ok", "final_mac"))
    _, _, final_key = _derive_keys(psk, transcript)
    if not _verify_mac(final_key, transcript + b"|ok", "final", str(confirm["final_mac"])):
        raise AdmissionError("final_mac_invalid", "final confirmation failed")
    return AdmissionResult(
        peer_public_key=host_public_key,
        peer_node_id=remote_node,
        session_nonce=session_nonce,
    )


async def run_host_admission(
    channel: Any,
    *,
    psk: PreSharedKey,
    peers: PeerRegistry,
    protocol_id: str,
    local_node: Any,
    conn: Any,
    host_private_key_hex: str,
    host_public_key: str,
    timeout_seconds: float = DEFAULT_HANDSHAKE_TIMEOUT_SECONDS,
    challenge_ttl_seconds: float = 30.0,
    seen_guest_nonces: dict[str, float] | None = None,
) -> AdmissionResult:
    """Host side of the admission handshake for one candidate connection.

    ``seen_guest_nonces`` (when provided and persisted by the caller) rejects
    replayed admission_init guest nonces within the challenge TTL.
    """
    deadline = time.monotonic() + timeout_seconds

    async def timed_recv() -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdmissionError("handshake_timeout", "admission exchange exceeded budget")
        return await asyncio.wait_for(channel.recv(), timeout=remaining)

    init = await timed_recv()
    _check_frame(init, ("_admission_init", "key_id", "key_epoch", "guest_public_key", "guest_nonce", "session_nonce", "protocol_id"))
    if int(init.get("v", 0)) != ADMISSION_VERSION:
        raise AdmissionError("version_mismatch", "unsupported admission version")
    if str(init.get("protocol_id", "")) != protocol_id:
        raise AdmissionError("protocol_mismatch", "admission is scoped to a different protocol")
    key_id = str(init["key_id"])
    key_epoch = int(init["key_epoch"])
    guest_public_key = str(init["guest_public_key"])
    guest_nonce = str(init["guest_nonce"])
    session_nonce = str(init["session_nonce"])
    if key_id != psk.key_id or key_epoch != psk.key_epoch:
        raise AdmissionError("psk_mismatch", "peer presented a different key id or epoch")
    if not peers.is_allowed(guest_public_key, key_id, key_epoch):
        raise AdmissionError("peer_not_trusted", "guest identity is not in the trusted registry")
    if seen_guest_nonces is not None:
        now = time.monotonic()
        stale = [n for n, t in seen_guest_nonces.items() if now - t > challenge_ttl_seconds]
        for n in stale:
            seen_guest_nonces.pop(n, None)
        if guest_nonce in seen_guest_nonces:
            raise AdmissionError("nonce_replay", "guest nonce was already used")
        seen_guest_nonces[guest_nonce] = now

    host_nonce = secrets.token_hex(32)
    remote_node = _remote_node_id_of(conn)
    local_node_id = await _node_id_of(local_node)
    transcript = build_transcript(
        key_id=psk.key_id,
        key_epoch=psk.key_epoch,
        protocol_id=protocol_id,
        host_public_key=host_public_key,
        guest_public_key=guest_public_key,
        host_node_id=local_node_id,
        guest_node_id=remote_node,
        guest_nonce=guest_nonce,
        host_nonce=host_nonce,
        session_nonce=session_nonce,
    )
    host_key_material, guest_key_material, final_key = _derive_keys(psk, transcript)
    challenge = {
        "_admission_challenge": True,
        "host_public_key": host_public_key,
        "host_nonce": host_nonce,
        "host_signature": _sign_transcript(host_private_key_hex, transcript),
        "host_mac": _mac(host_key_material, transcript, "host"),
    }
    await channel.send(challenge)
    finish = await timed_recv()
    _check_frame(finish, ("_admission_finish", "guest_signature", "guest_mac"))
    try:
        _verify_transcript_signature(guest_public_key, transcript, str(finish["guest_signature"]))
    except Exception:
        raise AdmissionError("guest_signature_invalid", "guest transcript signature failed")
    if not _verify_mac(guest_key_material, transcript, "guest", str(finish["guest_mac"])):
        raise AdmissionError("guest_mac_invalid", "guest PSK confirmation failed")
    # Challenge is atomically consumed: after this point a replayed finish on a
    # new connection carries a different host_nonce transcript and cannot pass.
    await channel.send(
        {
            "_admission_ok": True,
            "final_mac": _mac(final_key, transcript + b"|ok", "final"),
        }
    )
    return AdmissionResult(
        peer_public_key=guest_public_key,
        peer_node_id=remote_node,
        session_nonce=session_nonce,
    )


async def reject_admission(channel: Any, code: str) -> None:
    """Best-effort rejection frame; the caller then closes the connection."""
    try:
        await channel.send({"_admission_reject": code})
    except Exception:
        pass
