from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aigenora.engine.crypto import random_nonce, session_canonical, session_id
from aigenora.engine.keys import KeyPair, sign_raw
from aigenora.engine.rest import RestClient

if TYPE_CHECKING:
    from aigenora.proto.sdk import EventBus


@dataclass
class SessionProof:
    post_id: str
    host_public_key: str
    guest_public_key: str
    protocol_id: str
    session_nonce: str
    host_signature: str
    guest_signature: str

    @property
    def session_id(self) -> str:
        return session_id(
            self.post_id,
            self.host_public_key,
            self.guest_public_key,
            self.protocol_id,
            self.session_nonce,
        )

    def to_json(self) -> dict[str, str]:
        return {
            "post_id": self.post_id,
            "host_public_key": self.host_public_key,
            "guest_public_key": self.guest_public_key,
            "protocol_id": self.protocol_id,
            "session_nonce": self.session_nonce,
            "host_signature": self.host_signature,
            "guest_signature": self.guest_signature,
        }


def sign_session(kp: KeyPair, post_id: str, host_pub: str, guest_pub: str, protocol_id: str, nonce: str) -> str:
    return sign_raw(kp.private_key, session_canonical(post_id, host_pub, guest_pub, protocol_id, nonce).encode("utf-8"))


def new_session_nonce() -> str:
    return random_nonce()


def submit_session(client: RestClient, proof: SessionProof) -> str:
    data = client.json("POST", "/api/v1/sessions", proof.to_json(), expected={201})
    if not isinstance(data, dict):
        raise RuntimeError("POST /api/v1/sessions returned no JSON body")
    returned = data.get("session_id")
    if returned != proof.session_id:
        raise RuntimeError("POST /api/v1/sessions returned mismatched session_id")
    return returned


def close_session(client: RestClient, session_id: str, status: str = "closed",
                  winner: str | None = None,
                  event_bus: EventBus | None = None) -> None:
    """Write the session status back to the server after a match ends, and best-effort close the related invitation.

    The server-side updateSessionStatus transitions status==matched to closed/failed/cancelled
    and closes the related invitation (invitations.closed_at). Both host and guest call this;
    the second call hitting a 409 (already closed) is expected, swallowed via expected={200,409}.
    Any failure does not block the local match result — the session status write-back is auxiliary
    cleanup; the match outcome has already been recorded in events.jsonl.

    v010 M5 ELO: winner (host/guest/draw) declared by the closer triggers server-side EloService
    for game:* protocols. None for non-game sessions or unknown winner — best-effort, server no-ops
    when family is not game:* or winner is absent.
    """
    if not session_id:
        return
    body = {"status": status}
    if winner:
        body["winner"] = winner
    try:
        client.json(
            "POST",
            f"/api/v1/sessions/{session_id}/status",
            body,
            expected={200, 409},
        )
    except Exception as e:
        # 关键状态写回失败（网络/5xx 等；预期的 409 已被 expected 吸收不再视为失败）：不再静默吞。
        # 写 stderr 让用户/agent 可见；若提供 event_bus 再记一条事件便于事后排查（批次1-c）。
        msg = str(e)[:200]
        print(f"[aigenora] warning: failed to close session {session_id}: {msg}", file=sys.stderr)
        if event_bus is not None:
            try:
                event_bus.emit("session_close_failed", {"session_id": session_id, "error": msg})
            except Exception:
                pass
