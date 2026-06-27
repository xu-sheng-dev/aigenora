"""v016 M1: mental_poker engine — wires the primitive layer into a runnable engine.

Engine-owned control plane (ADR §4 / implplan v016-m1). All ``mp_*`` wire messages
are constructed and sent here; hooks only supply business material (deck universe,
deal plan, per-turn action, winner) via the ``proto_mp_*`` callbacks (hooks.py).

Design (see plan structured-juggling-tiger.md, decisions 1-5):
  - Two sealed tables, each in its own id space, each same-order as its blobs:
      k_H sealed (Host Sender, ROLE_HOST/K_INNER): label ``h-<id-A>`` (mp_setup_a)
      k_G sealed (Guest Sender, ROLE_GUEST/K_OUTER): label ``g-<id-B>`` (mp_setup_b)
  - DeckState is in id-B space (both hands are id-B sets).
  - Disclosure label derivation is symmetric:
      Guest reveals id-B (取 k_H): open_outer(self k_G) -> blob_A -> match transcript -> id-A -> h-<id-A>
      Host reveals id-B (取 k_G): label g-<id-B> directly -> OT k_G -> open blob_B -> blob_A -> match -> id-A -> self k_H
  - Transcript is ping-pong mirrored (engine fully controls send/recv order); both
    sides append the same events in the same order -> byte-for-byte identical root_hash.
  - OT event records (ot_id, z, y) only — label stays private until mp_witness.
  - audit gate: result["audit_passed"] is True only after local audit passes AND the
    peer terminal-receipt signature verifies on the same transcript hash.

Crash recovery is disabled (ADR §10/§12 #10): sensitive material (RSA priv, k_H/k_G,
tokens) lives only in memory; a daemon crash => session failed, no /result.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .hooks import HookResult
from .loader import load_hooks
from .sdk import EventBus
from . import mental_poker
from ..engine import aead_deck, keys, ot
from ..engine.p2p import AsyncJsonLineChannel, ChannelClosed, JsonLineChannel

# Lazy imports from .engine to avoid a top-level circular import (engine.py registers
# this module via entry helpers at runtime). These helpers are defined at the top of
# engine.py, so they are available once engine is importable.
from .engine import (
    _emit,
    _validate,
    _state_dir,
    _snapshot_init,
    _snapshot_phase,
    _handle_peer_disconnect,
    _resolve_decision_config,
    _maybe_wrap_heartbeat,
)


FAULT_ENV = "AIGENORA_MP_TEST_FAULT"


def _fault() -> str | None:
    """Active fault-injection mode (empty/absent => none). Hot path is just this read."""
    import os

    return os.environ.get(FAULT_ENV) or None


def _i2h(x: int) -> str:
    """Big-endian hex of a 2048-bit integer, zero-padded to 512 chars (ADR §13.3)."""
    return format(x, "x").zfill(512)


def _gen_temp_keypair() -> Any:
    """Ephemeral Ed25519 keypair for in-memory test loops (``protocol test`` CLI and
    ``test_builtin_protocols_complete``) that don't supply one — needed because the
    terminal-receipt dual-sign calls ``keypair.private_key``. Real P2P callers
    (host.py / join.py) pass a persistent keypair, which takes precedence.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from ..engine import keys

    priv = Ed25519PrivateKey.generate()
    priv_b = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub_b = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return keys.KeyPair(public_key=pub_b.hex(), private_key=priv_b.hex())


# ───────────────────────── per-side state machine ─────────────────────────


class _MpSession:
    """Per-side mental poker state. Pure logic + wire-message construction.

    Channel I/O is driven by the ``_run_mp_*_{host,guest}`` functions (sync/async);
    this class never touches a channel, so sync and async share the same logic.
    """

    def __init__(
        self,
        spec: dict[str, Any],
        hooks: Any,
        role: str,
        state: dict[str, Any],
        state_dir: Path,
        event_bus: EventBus | None,
        validate: bool,
        session_id: str | None,
        keypair: Any,
        peer_public_key: str | None = None,
    ) -> None:
        self.spec = spec
        self.hooks = hooks
        self.role = role
        self.peer = "guest" if role == "host" else "host"
        self.state = state
        self.state_dir = state_dir
        self.bus = event_bus
        self.validate = validate
        self.session_id = session_id or "mp-session"
        self.keypair = keypair if keypair is not None else _gen_temp_keypair()
        # expected peer agent public_key (bound at the P2P session handshake). When set,
        # the terminal-receipt verify rejects a receipt signed by any other key (codex
        # High fix). None in in-memory test loops (no real peer identity) — skipped.
        self.peer_public_key = peer_public_key
        self.protocol_id = spec.get("protocol_id") or spec.get("name") or "mental-poker"

        self.transcript = mental_poker.Transcript()
        self.deck = mental_poker.DeckState()
        self.deck_size = 0

        # inner (Host) layer
        self.ida_list: list[str] = []
        self.k_inner_by_ida: dict[str, bytes] = {}        # Host-only (self-gen)
        self.blob_a_by_ida: dict[str, bytes] = {}         # both (transcript public)
        self._rsa_inner: Any = None                        # Host's RSAPrivateKey
        self.sender_inner: ot.OTSender | None = None      # Host's k_H sealed table
        self.pub_inner: ot.Rsapub | None = None           # Host rsa_pub (Guest OTs to it)
        self.sealed_inner: dict[str, str] = {}             # h-label -> sealed hex (transcript)

        # outer (Guest) layer
        self.idb_list: list[str] = []
        self.k_outer_by_idb: dict[str, bytes] = {}        # Guest-only (self-gen)
        self.blob_b_by_idb: dict[str, bytes] = {}         # both
        self._rsa_outer: Any = None                        # Guest's RSAPrivateKey
        self.sender_outer: ot.OTSender | None = None      # Guest's k_G sealed table
        self.pub_outer: ot.Rsapub | None = None           # Guest rsa_pub
        self.sealed_outer: dict[str, str] = {}             # g-label -> sealed hex

        self.ida_to_idb: dict[str, str] = {}               # Guest knows (built at setup_b)
        self.idb_to_ida: dict[str, str] = {}               # learned locally on each disclosure

        # OT bookkeeping
        self._ot_seq = {"host": 0, "guest": 0}
        self.ot_witnesses: list[mental_poker.Witness] = []  # self-initiated OTs (Receiver side)
        self.ot_order: list[str] = []                       # transcript OT order
        self.ot_z_by_otid: dict[str, int] = {}

        # hand (id-B space)
        self.my_hand: set[str] = set()
        self.my_hand_cards: dict[str, tuple[int, int]] = {}
        self.my_hand_keys: dict[str, tuple[bytes, bytes]] = {}  # id-B -> (k_inner, k_outer)

        # deal plan (lawful hand sets)
        self.guest_deal_ids: list[str] = []
        self.host_deal_ids: list[str] = []
        # per-OT snapshot: ot_id -> the single id-B the Receiver was supposed to take
        # in that OT (the dealt card or the deterministic draw card). The peek-audit
        # checks each OT's witness label maps to THIS id_b, not a global "ever acquired"
        # set — the latter lets a malicious Receiver peek a future card's label in one
        # OT and pass audit once that card is lawfully acquired later. (codex Critical.)
        self.ot_id_to_idb: dict[str, str] = {}

        # audit material from peer
        self.peer_opening_entries: list[dict] = []
        self.peer_witnesses: list[mental_poker.Witness] = []

    # ── setup ──

    def setup_host(self) -> dict[str, Any]:
        """Host builds the inner layer (blob_A + k_H sealed table) -> mp_setup_a msg."""
        universe = self.hooks.proto_mp_deck_universe()
        self.deck_size = len(universe)
        self.ida_list = [str(i) for i in range(self.deck_size)]
        self._rsa_inner = ot.gen_session_rsa()
        payloads: dict[str, bytes] = {}
        blobs_hex: list[str] = []
        for i, (rank, suit) in enumerate(universe):
            id_a = self.ida_list[i]
            k = aead_deck.random_key()
            self.k_inner_by_ida[id_a] = k
            plaintext = aead_deck.pack_inner(aead_deck.encode_card(rank, suit))
            blob_a = aead_deck.seal_inner(k, plaintext)
            self.blob_a_by_ida[id_a] = blob_a
            blobs_hex.append(blob_a.hex())
            payloads[f"h-{i}"] = k
        self.sender_inner = ot.OTSender(
            payloads, self._rsa_inner, self.protocol_id, self.session_id,
            ot.ROLE_HOST, ot.PAYLOAD_K_INNER,
        )
        self.pub_inner = self.sender_inner.pub
        self.sealed_inner = dict(self.sender_inner.sealed)
        # fault: corrupt_sealed_payload:<label>
        fault = _fault() or ""
        if fault.startswith("corrupt_sealed_payload:"):
            lbl = fault.split(":", 1)[1]
            if lbl in self.sealed_inner:
                self.sealed_inner[lbl] = secrets_token_hex(len(self.sealed_inner[lbl]) // 2)
                _emit(self.bus, "mp_test_fault_injected", {"fault": fault})
        sealed_keys = [json.dumps({"label": f"h-{i}", "c": self.sealed_inner[f"h-{i}"]},
                                  separators=(",", ":")) for i in range(self.deck_size)]
        rsa_pub_blob = json.dumps(ot.rsa_pub_encode(self.pub_inner), separators=(",", ":"))
        msg = {"action": "mp_setup_a", "rsa_pub": rsa_pub_blob, "blobs": blobs_hex,
               "sealed_keys": sealed_keys}
        self.transcript.append("mp_setup_a", {
            "rsa_pub": ot.rsa_pub_encode(self.pub_inner),
            "blobs": blobs_hex, "sealed": dict(self.sealed_inner),
        })
        return msg

    def apply_setup_a(self, msg: dict[str, Any]) -> None:
        rsa_pub_d = json.loads(msg["rsa_pub"])
        self.pub_inner = ot.rsa_pub_decode(rsa_pub_d)
        blobs_hex = msg["blobs"]
        sealed_keys = msg["sealed_keys"]
        if len(sealed_keys) != len(blobs_hex):
            raise ValueError("mp_setup_a: sealed_keys length != blobs length")
        self.deck_size = len(blobs_hex)
        self.ida_list = [str(i) for i in range(self.deck_size)]
        sealed: dict[str, str] = {}
        for i, sk in enumerate(sealed_keys):
            o = json.loads(sk)
            if o.get("label") != f"h-{i}":
                raise ValueError(f"mp_setup_a: sealed_keys[{i}] label mismatch")
            sealed[o["label"]] = o["c"]
            self.blob_a_by_ida[self.ida_list[i]] = bytes.fromhex(blobs_hex[i])
        self.sealed_inner = sealed
        self.transcript.append("mp_setup_a", {
            "rsa_pub": rsa_pub_d, "blobs": blobs_hex, "sealed": dict(sealed),
        })

    def setup_guest(self) -> dict[str, Any]:
        """Guest builds the outer layer (blob_B + k_G sealed table) -> mp_setup_b msg."""
        self.idb_list = [str(i) for i in range(self.deck_size)]
        self._rsa_outer = ot.gen_session_rsa()
        items: list[tuple[str, bytes, bytes]] = []  # (id_a, blob_b, k_outer)
        for id_a in self.ida_list:
            k = aead_deck.random_key()
            blob_b = aead_deck.seal_outer(k, self.blob_a_by_ida[id_a])
            self.k_outer_by_idb  # touch (no-op, kept for clarity)
            items.append((id_a, blob_b, k))
        rng = random.SystemRandom()
        rng.shuffle(items)
        payloads: dict[str, bytes] = {}
        blobs_hex: list[str] = []
        for i, (id_a, blob_b, k) in enumerate(items):
            id_b = self.idb_list[i]
            self.k_outer_by_idb[id_b] = k
            self.blob_b_by_idb[id_b] = blob_b
            self.ida_to_idb[id_a] = id_b
            payloads[f"g-{i}"] = k
            blobs_hex.append(blob_b.hex())
        self.sender_outer = ot.OTSender(
            payloads, self._rsa_outer, self.protocol_id, self.session_id,
            ot.ROLE_GUEST, ot.PAYLOAD_K_OUTER,
        )
        self.pub_outer = self.sender_outer.pub
        self.sealed_outer = dict(self.sender_outer.sealed)
        fault = _fault() or ""
        if fault.startswith("corrupt_sealed_payload:"):
            lbl = fault.split(":", 1)[1]
            if lbl in self.sealed_outer:
                self.sealed_outer[lbl] = secrets_token_hex(len(self.sealed_outer[lbl]) // 2)
                _emit(self.bus, "mp_test_fault_injected", {"fault": fault})
        sealed_keys = [json.dumps({"label": f"g-{i}", "c": self.sealed_outer[f"g-{i}"]},
                                  separators=(",", ":")) for i in range(self.deck_size)]
        rsa_pub_blob = json.dumps(ot.rsa_pub_encode(self.pub_outer), separators=(",", ":"))
        msg = {"action": "mp_setup_b", "rsa_pub": rsa_pub_blob, "blobs": blobs_hex,
               "sealed_keys": sealed_keys}
        self.transcript.append("mp_setup_b", {
            "rsa_pub": ot.rsa_pub_encode(self.pub_outer),
            "blobs": blobs_hex, "sealed": dict(self.sealed_outer),
        })
        return msg

    def apply_setup_b(self, msg: dict[str, Any]) -> None:
        rsa_pub_d = json.loads(msg["rsa_pub"])
        self.pub_outer = ot.rsa_pub_decode(rsa_pub_d)
        blobs_hex = msg["blobs"]
        sealed_keys = msg["sealed_keys"]
        if len(sealed_keys) != len(blobs_hex) or len(blobs_hex) != self.deck_size:
            raise ValueError("mp_setup_b: length mismatch")
        self.idb_list = [str(i) for i in range(self.deck_size)]
        sealed: dict[str, str] = {}
        for i, sk in enumerate(sealed_keys):
            o = json.loads(sk)
            if o.get("label") != f"g-{i}":
                raise ValueError(f"mp_setup_b: sealed_keys[{i}] label mismatch")
            sealed[o["label"]] = o["c"]
            self.blob_b_by_idb[self.idb_list[i]] = bytes.fromhex(blobs_hex[i])
        self.sealed_outer = sealed
        self.transcript.append("mp_setup_b", {
            "rsa_pub": rsa_pub_d, "blobs": blobs_hex, "sealed": dict(sealed),
        })

    # ── deal planning ──

    def plan_deal(self, deal_plan: dict[str, Any]) -> None:
        """Allocate id-B to both hands (lawful deal). M1: sequential from stock."""
        stock_ids = list(self.idb_list)
        self.deck.stock.update(stock_ids)
        g = int(deal_plan.get("guest", 0))
        h = int(deal_plan.get("host", 0))
        if g + h > self.deck_size:
            raise ValueError("deal plan asks for more cards than the deck")
        self.guest_deal_ids = stock_ids[:g]
        self.host_deal_ids = stock_ids[g:g + h]

    def inject_views(self) -> None:
        """Push read-only views into the shared state dict for hooks."""
        self.state["_mp_role"] = self.role
        self.state["_mp_hand"] = self.my_hand
        self.state["_mp_hand_cards"] = self.my_hand_cards
        self.state["_mp_deck_view"] = self.deck

    # ── OT primitives ──

    def next_ot_id(self, who: str) -> str:
        n = self._ot_seq[who]
        self._ot_seq[who] = n + 1
        return f"{who}-{n}"

    def record_ot(self, ot_id: str, z: int, y: int) -> None:
        self.ot_order.append(ot_id)
        self.ot_z_by_otid[ot_id] = z
        self.transcript.append("mp_ot", {"ot_id": ot_id, "z": z, "y": y})

    def build_ot_request(self, ot_id: str, z: int) -> dict[str, Any]:
        return {"action": "mp_ot_request", "id": ot_id, "z": _i2h(z)}

    def build_ot_response(self, ot_id: str, y: int) -> dict[str, Any] | None:
        fault = _fault() or ""
        if fault == f"drop_ot_response:{ot_id}":
            _emit(self.bus, "mp_test_fault_injected", {"fault": fault})
            return None
        return {"action": "mp_ot_response", "id": ot_id, "y": _i2h(y)}

    # Guest side: OT to Host's inner table (取 k_H). The same OTReceiver instance must be
    # used for request() and recover() — recover() reads the blinded factor stored on it.
    def guest_request_inner(self, id_b: str) -> tuple[str, int, int, ot.OTReceiver, str]:
        id_a = self._idb_to_ida_local(id_b)
        label = f"h-{id_a}"
        ot_id = self.next_ot_id("guest")
        self.ot_id_to_idb[ot_id] = id_b  # per-OT snapshot for the peek-audit
        rcv = ot.OTReceiver(label, self.pub_inner, self.protocol_id, self.session_id,
                            ot.ROLE_HOST, ot.PAYLOAD_K_INNER)
        z, r = rcv.request()
        return ot_id, z, r, rcv, label

    def guest_recover_inner(self, rcv: ot.OTReceiver, id_b: str, y: int) -> tuple[int, int]:
        k_inner = rcv.recover(y, self.sealed_inner)
        k_outer = self.k_outer_by_idb[id_b]
        self.my_hand_keys[id_b] = (k_inner, k_outer)
        blob_a = aead_deck.open_outer(k_outer, self.blob_b_by_idb[id_b])
        plaintext = aead_deck.open_inner(k_inner, blob_a)
        return aead_deck.decode_card(plaintext[:aead_deck.CARD_BYTES])

    # Host side: OT to Guest's outer table (取 k_G). Same OTReceiver for request+recover.
    def host_request_outer(self, id_b: str) -> tuple[str, int, int, ot.OTReceiver, str]:
        label = f"g-{id_b}"
        ot_id = self.next_ot_id("host")
        self.ot_id_to_idb[ot_id] = id_b  # per-OT snapshot for the peek-audit
        rcv = ot.OTReceiver(label, self.pub_outer, self.protocol_id, self.session_id,
                            ot.ROLE_GUEST, ot.PAYLOAD_K_OUTER)
        z, r = rcv.request()
        return ot_id, z, r, rcv, label

    def host_recover_outer(self, rcv: ot.OTReceiver, id_b: str, y: int) -> tuple[int, int]:
        k_outer = rcv.recover(y, self.sealed_outer)
        blob_b = self.blob_b_by_idb[id_b]
        blob_a = aead_deck.open_outer(k_outer, blob_b)
        id_a = self._find_ida_by_blob_a(blob_a)
        self.idb_to_ida[id_b] = id_a
        k_inner = self.k_inner_by_ida[id_a]
        plaintext = aead_deck.open_inner(k_inner, blob_a)
        rank, suit = aead_deck.decode_card(plaintext[:aead_deck.CARD_BYTES])
        self.my_hand_keys[id_b] = (k_inner, k_outer)
        return rank, suit

    # Sender side: respond to a blind request
    def respond_inner(self, z: int) -> int:
        return self.sender_inner.respond(z)

    def respond_outer(self, z: int) -> int:
        return self.sender_outer.respond(z)

    def _idb_to_ida_local(self, id_b: str) -> str:
        if id_b in self.idb_to_ida:
            return self.idb_to_ida[id_b]
        for id_a, ib in self.ida_to_idb.items():
            if ib == id_b:
                self.idb_to_ida[id_b] = id_a
                return id_a
        raise KeyError(f"no id-A for id-B {id_b!r}")

    def _find_ida_by_blob_a(self, blob_a: bytes) -> str:
        for id_a, ba in self.blob_a_by_ida.items():
            if ba == blob_a:
                return id_a
        raise ValueError("blob_A not found in Host transcript")

    def remember_witness(self, ot_id: str, label: str, r: int) -> None:
        self.ot_witnesses.append(mental_poker.Witness(ot_id=ot_id, label=label, r=r))

    # ── play ──

    def build_play(self, id_b: str, call_suit: int | None = None) -> dict[str, Any]:
        """Build mp_play for a card in hand. Raises ValueError on local validation failure.

        ``call_suit`` (0-3) is forwarded into the message when the game declares a new
        suit on a wild card (e.g. Crazy Eights' 8); omit it for plain plays.
        """
        rank, suit = self.my_hand_cards[id_b]
        declared = aead_deck.encode_card(rank, suit)
        k_inner, k_outer = self.my_hand_keys[id_b]
        blob_b = self.blob_b_by_idb[id_b]
        vr = mental_poker.validate_play(self.my_hand, self.deck.played, id_b, declared,
                                        k_inner, k_outer, blob_b)
        if not vr.ok:
            raise ValueError(
                f"local play validation failed for {id_b}: {vr.reason} "
                f"(in_hand={id_b in self.my_hand}, has_key={id_b in self.my_hand_keys})"
            )
        msg: dict[str, Any] = {"action": "mp_play", "id_b": id_b, "k_inner": k_inner.hex(),
                               "k_outer": k_outer.hex(), "rank": rank, "suit": suit}
        if call_suit is not None:
            msg["call_suit"] = call_suit
        return msg

    def next_stock_id(self) -> str:
        """Deterministic next id-B to draw from stock (both sides compute the same)."""
        for idb in self.idb_list:
            if idb in self.deck.stock:
                return idb
        raise ValueError("draw requested but stock is empty")

    def do_draw_sync(self, channel: JsonLineChannel, spec: dict[str, Any], validate: bool) -> None:
        """Actor draws one card: initiate OT, recover the face, update local hand + ledger.

        Reuses the deal-phase OT path (Guest takes k_H / Host takes k_G). The drawn id-B
        is chosen deterministically by ``next_stock_id`` so the peer's ledger update
        (in ``handle_peer_draw_sync``) lands on the same card without an extra message.
        """
        id_b = self.next_stock_id()
        _emit(self.bus, "draw_started", {"who": self.role, "id_b": id_b})
        if self.role == "guest":
            ot_id, z, r, rcv, label = self.guest_request_inner(id_b)
        else:
            ot_id, z, r, rcv, label = self.host_request_outer(id_b)
        _emit(self.bus, "mp_ot_started", {"ot_id": ot_id, "direction": "sent", "kind": "draw"})
        req = self.build_ot_request(ot_id, z)
        if validate:
            _validate(spec, req, "both")
        channel.send(req)
        _emit(self.bus, "protocol_message", {"direction": "sent", "msg": req})
        resp = channel.recv()
        if validate:
            _validate(spec, resp, "both")
        y = int(resp["y"], 16)
        if self.role == "guest":
            rank, suit = self.guest_recover_inner(rcv, id_b, y)
        else:
            rank, suit = self.host_recover_outer(rcv, id_b, y)
        self.record_ot(ot_id, z, y)
        self.transcript.append("mp_draw", {"who": self.role, "ot_id": ot_id})
        self.remember_witness(ot_id, label, r)
        self.deck.deal_to(self.role, id_b)
        self.my_hand.add(id_b)
        self.my_hand_cards[id_b] = (rank, suit)
        _emit(self.bus, "mp_ot_completed", {"ot_id": ot_id, "label": label, "kind": "draw"})

    def handle_peer_draw_sync(self, channel: JsonLineChannel, msg: dict[str, Any],
                              spec: dict[str, Any], validate: bool) -> None:
        """Peer responds to the actor's draw OT and mirrors the ledger update.

        The peer does NOT learn the drawn card face (OT protects it); it only learns
        which id-B moved from stock to the actor's hand (same as the deal phase).
        """
        ot_id = msg["id"]
        _emit(self.bus, "mp_ot_started", {"ot_id": ot_id, "direction": "received", "kind": "draw"})
        if self.role == "host":
            # actor == guest is taking k_H from the Host's inner table
            y = self.respond_inner(int(msg["z"], 16))
        else:
            # actor == host is taking k_G from the Guest's outer table
            y = self.respond_outer(int(msg["z"], 16))
        resp = self.build_ot_response(ot_id, y)
        if resp is not None:
            if validate:
                _validate(spec, resp, "both")
            channel.send(resp)
            _emit(self.bus, "protocol_message", {"direction": "sent", "msg": resp})
        self.record_ot(ot_id, int(msg["z"], 16), y)
        self.transcript.append("mp_draw", {"who": self.peer, "ot_id": ot_id})
        id_b = self.next_stock_id()
        self.deck.deal_to(self.peer, id_b)
        self.ot_id_to_idb[ot_id] = id_b
        _emit(self.bus, "draw_completed", {"who": self.peer, "id_b": id_b})

    async def do_draw_async(self, channel: AsyncJsonLineChannel, spec: dict[str, Any],
                            validate: bool) -> None:
        """Async variant of do_draw_sync (memory_duplex is sync, but the P2P channel is async)."""
        id_b = self.next_stock_id()
        _emit(self.bus, "draw_started", {"who": self.role, "id_b": id_b})
        if self.role == "guest":
            ot_id, z, r, rcv, label = self.guest_request_inner(id_b)
        else:
            ot_id, z, r, rcv, label = self.host_request_outer(id_b)
        _emit(self.bus, "mp_ot_started", {"ot_id": ot_id, "direction": "sent", "kind": "draw"})
        req = self.build_ot_request(ot_id, z)
        if validate:
            _validate(spec, req, "both")
        await channel.send(req)
        resp = await channel.recv()
        if validate:
            _validate(spec, resp, "both")
        y = int(resp["y"], 16)
        if self.role == "guest":
            rank, suit = self.guest_recover_inner(rcv, id_b, y)
        else:
            rank, suit = self.host_recover_outer(rcv, id_b, y)
        self.record_ot(ot_id, z, y)
        self.transcript.append("mp_draw", {"who": self.role, "ot_id": ot_id})
        self.remember_witness(ot_id, label, r)
        self.deck.deal_to(self.role, id_b)
        self.my_hand.add(id_b)
        self.my_hand_cards[id_b] = (rank, suit)
        _emit(self.bus, "mp_ot_completed", {"ot_id": ot_id, "label": label, "kind": "draw"})

    async def handle_peer_draw_async(self, channel: AsyncJsonLineChannel, msg: dict[str, Any],
                                     spec: dict[str, Any], validate: bool) -> None:
        """Async variant of handle_peer_draw_sync."""
        ot_id = msg["id"]
        _emit(self.bus, "mp_ot_started", {"ot_id": ot_id, "direction": "received", "kind": "draw"})
        if self.role == "host":
            y = self.respond_inner(int(msg["z"], 16))
        else:
            y = self.respond_outer(int(msg["z"], 16))
        resp = self.build_ot_response(ot_id, y)
        if resp is not None:
            if validate:
                _validate(spec, resp, "both")
            await channel.send(resp)
        self.record_ot(ot_id, int(msg["z"], 16), y)
        self.transcript.append("mp_draw", {"who": self.peer, "ot_id": ot_id})
        id_b = self.next_stock_id()
        self.deck.deal_to(self.peer, id_b)
        self.ot_id_to_idb[ot_id] = id_b
        _emit(self.bus, "draw_completed", {"who": self.peer, "id_b": id_b})

    def verify_peer_play(self, msg: dict[str, Any]) -> mental_poker.ValidationResult:
        id_b = msg["id_b"]
        rank, suit = int(msg["rank"]), int(msg["suit"])
        declared = aead_deck.encode_card(rank, suit)
        k_inner = bytes.fromhex(msg["k_inner"])
        k_outer = bytes.fromhex(msg["k_outer"])
        blob_b = self.blob_b_by_idb[id_b]
        peer_hand = self.deck.hand_of(self.peer)
        return mental_poker.validate_play(peer_hand, self.deck.played, id_b, declared,
                                          k_inner, k_outer, blob_b)

    def apply_play(self, who: str, msg: dict[str, Any]) -> None:
        id_b = msg["id_b"]
        ok = self.deck.play(who, id_b)
        if not ok:
            raise ValueError(f"deck.play failed for {who} {id_b}")
        if who == self.role:
            self.my_hand.discard(id_b)
            self.my_hand_cards.pop(id_b, None)
        rank, suit = int(msg["rank"]), int(msg["suit"])
        entry: dict[str, Any] = {"who": who, "id_b": id_b, "rank": rank, "suit": suit}
        call_suit = msg.get("call_suit")
        if call_suit is not None:
            entry["call_suit"] = int(call_suit)
        self.transcript.append("mp_play", entry)

    # ── opening / witness ──

    def build_opening(self) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        if self.role == "host" and self.sender_inner is not None:
            tokens = self.sender_inner.export_tokens()
            for label in sorted(tokens):
                tok = tokens[label]
                if (_fault() or "") == f"forge_token:{label}":
                    tok = (tok + 1) % self.pub_inner.n
                    _emit(self.bus, "mp_test_fault_injected", {"fault": _fault()})
                entries.append({"kind": "token", "label": label, "token": _i2h(tok)})
            for id_a in self.ida_list:
                entries.append({"kind": "k_inner", "label": f"h-{id_a}",
                                "k": self.k_inner_by_ida[id_a].hex()})
        elif self.role == "guest" and self.sender_outer is not None:
            tokens = self.sender_outer.export_tokens()
            for label in sorted(tokens):
                tok = tokens[label]
                if (_fault() or "") == f"forge_token:{label}":
                    tok = (tok + 1) % self.pub_outer.n
                    _emit(self.bus, "mp_test_fault_injected", {"fault": _fault()})
                entries.append({"kind": "token", "label": label, "token": _i2h(tok)})
            for id_b in self.idb_list:
                entries.append({"kind": "k_outer", "label": f"g-{id_b}",
                                "k": self.k_outer_by_idb[id_b].hex()})
        self._last_opening_entries = entries
        return {"action": "mp_opening",
                "openings": [json.dumps(e, separators=(",", ":")) for e in entries]}

    def apply_opening(self, msg: dict[str, Any]) -> None:
        self.peer_opening_entries = [json.loads(s) for s in msg["openings"]]

    def build_witness(self) -> dict[str, Any] | None:
        if (_fault() or "") == "skip_witness":
            # fault: publish an empty witness set. We still send mp_witness (so the peer
            # does not block on recv); audit_witnesses then flags witness_transcript_mismatch.
            _emit(self.bus, "mp_test_fault_injected", {"fault": "skip_witness"})
            return {"action": "mp_witness", "witnesses": []}
        ws = sorted(self.ot_witnesses, key=lambda w: w.ot_id)
        witnesses = [json.dumps({"ot_id": w.ot_id, "label": w.label,
                                 "r": _i2h(w.r)}, separators=(",", ":")) for w in ws]
        if (_fault() or "") == "bogus_witness":
            # fault: inject a witness for an ot_id that never happened. The peer's audit
            # must reject it structurally (ot_snapshot_missing / witness_transcript_mismatch)
            # rather than KeyError. (codex r2 Medium)
            witnesses.append(json.dumps({"ot_id": "guest-999", "label": "h-999",
                                         "r": _i2h(1)}, separators=(",", ":")))
            _emit(self.bus, "mp_test_fault_injected", {"fault": "bogus_witness"})
        return {"action": "mp_witness", "witnesses": witnesses}

    def apply_witness(self, msg: dict[str, Any]) -> None:
        self.peer_witnesses = [
            mental_poker.Witness(ot_id=o["ot_id"], label=o["label"], r=int(o["r"], 16))
            for o in (json.loads(s) for s in msg["witnesses"])
        ]

    # ── audit ──

    def record_opening_to_transcript(self) -> None:
        """Append both sides' openings (ADR §12 #2: transcript covers openings + tokens)."""
        if self.role == "host":
            host_entries, guest_entries = self._last_opening_entries, self.peer_opening_entries
        else:
            host_entries, guest_entries = self.peer_opening_entries, self._last_opening_entries
        self.transcript.append("mp_opening", {"role": "host", "entries": list(host_entries)})
        self.transcript.append("mp_opening", {"role": "guest", "entries": list(guest_entries)})

    def record_witness_to_transcript(self) -> None:
        """Append both sides' witnesses (ADR §12 #2: transcript covers witnesses).

        Both lists are sorted by ``ot_id`` so the two sides agree on a canonical order.
        ``self.ot_witnesses`` is append-ordered (numeric OT sequence) while
        ``peer_witnesses`` arrives already sorted (``build_witness`` sorts by ot_id);
        without sorting here the two lists diverge once a side reaches >= 10 OTs
        (string sort puts "host-10" before "host-2"), making the transcript roots
        disagree and the terminal-receipt dual-sign fail.
        """
        all_w = list(self.ot_witnesses) + list(self.peer_witnesses)
        host_w = sorted(
            ({"ot_id": w.ot_id, "label": w.label, "r": w.r}
             for w in all_w if w.ot_id.startswith("host-")),
            key=lambda x: x["ot_id"],
        )
        guest_w = sorted(
            ({"ot_id": w.ot_id, "label": w.label, "r": w.r}
             for w in all_w if w.ot_id.startswith("guest-")),
            key=lambda x: x["ot_id"],
        )
        self.transcript.append("mp_witness", {"role": "host", "witnesses": host_w})
        self.transcript.append("mp_witness", {"role": "guest", "witnesses": guest_w})

    def audit(self) -> mental_poker.AuditResult:
        all_entries = list(getattr(self, "_last_opening_entries", [])) + self.peer_opening_entries
        tokens_h: dict[str, int] = {}
        tokens_g: dict[str, int] = {}
        k_inner_by_label: dict[str, bytes] = {}
        k_outer_by_label: dict[str, bytes] = {}
        for e in all_entries:
            kind = e["kind"]
            if kind == "token":
                if e["label"].startswith("h-"):
                    tokens_h[e["label"]] = int(e["token"], 16)
                else:
                    tokens_g[e["label"]] = int(e["token"], 16)
            elif kind == "k_inner":
                k_inner_by_label[e["label"]] = bytes.fromhex(e["k"])
            elif kind == "k_outer":
                k_outer_by_label[e["label"]] = bytes.fromhex(e["k"])

        k_outer_by_idb = {lbl[2:]: k for lbl, k in k_outer_by_label.items()}

        # derive id-B -> id-A from data (ADR §4.3, never trust disclosed map)
        try:
            id_map = mental_poker.derive_id_map(
                k_outer_by_idb, self.blob_b_by_idb, self.blob_a_by_ida
            )
        except mental_poker.AuditError as exc:
            return mental_poker.AuditResult(False, "derive_id_map_failed", {"detail": str(exc)})
        ida_to_idb = {v: k for k, v in id_map.items()}

        # sealed_payload audit (attack #15) for both tables
        expected_h = {f"h-{id_a}": k_inner_by_label[f"h-{id_a}"] for id_a in self.ida_list}
        r1 = mental_poker.audit_sealed_payload(
            tokens_h, self.sealed_inner, expected_h, self.pub_inner,
            self.protocol_id, self.session_id, ot.ROLE_HOST, ot.PAYLOAD_K_INNER,
        )
        if not r1.ok:
            return r1
        expected_g = {f"g-{id_b}": k_outer_by_label[f"g-{id_b}"] for id_b in self.idb_list}
        r2 = mental_poker.audit_sealed_payload(
            tokens_g, self.sealed_outer, expected_g, self.pub_outer,
            self.protocol_id, self.session_id, ot.ROLE_GUEST, ot.PAYLOAD_K_OUTER,
        )
        if not r2.ok:
            return r2

        # full-deck opening audit (codex #5): reconstruct every card face, compare to universe
        cards: list[bytes] = []
        for id_b in self.idb_list:
            k_outer = k_outer_by_label[f"g-{id_b}"]
            blob_a = aead_deck.open_outer(k_outer, self.blob_b_by_idb[id_b])
            id_a = id_map[id_b]
            k_inner = k_inner_by_label[f"h-{id_a}"]
            plaintext = aead_deck.open_inner(k_inner, blob_a)
            cards.append(plaintext[:aead_deck.CARD_BYTES])
        expected = {aead_deck.encode_card(r, s) for (r, s) in self.hooks.proto_mp_deck_universe()}
        r3 = mental_poker.audit_full_deck(cards, expected)
        if not r3.ok:
            return r3

        # witness audit (peek detection) — each direction audited against its own OT subset
        guest_pairs = [(oid, self.ot_z_by_otid[oid]) for oid in self.ot_order if oid.startswith("guest-")]
        host_pairs = [(oid, self.ot_z_by_otid[oid]) for oid in self.ot_order if oid.startswith("host-")]
        all_witnesses = list(self.ot_witnesses) + list(self.peer_witnesses)
        guest_ws = [w for w in all_witnesses if w.ot_id.startswith("guest-")]
        host_ws = [w for w in all_witnesses if w.ot_id.startswith("host-")]
        label_to_idb_h = {f"h-{id_a}": ida_to_idb[id_a] for id_a in self.ida_list}
        label_to_idb_g = {f"g-{id_b}": id_b for id_b in self.idb_list}
        # per-OT snapshot: each OT's witness label must map to the single id-B the
        # Receiver was supposed to take in THAT OT (dealt card or deterministic draw
        # card). A global "ever acquired" set would let a malicious Receiver peek a
        # future card's label in one OT and pass audit later (codex Critical fix).
        # Build per-OT snapshots; a witness whose ot_id has no local snapshot (e.g. a
        # malicious peer fabricating "guest-999" in mp_witness) returns a structured
        # audit failure instead of KeyError-ing the whole audit path. (codex r2 Medium)
        hand_at_ot_g: dict[str, set[str]] = {}
        for w in guest_ws:
            idb = self.ot_id_to_idb.get(w.ot_id)
            if idb is None:
                return mental_poker.AuditResult(False, "ot_snapshot_missing", {"ot_id": w.ot_id})
            hand_at_ot_g[w.ot_id] = {idb}
        hand_at_ot_h: dict[str, set[str]] = {}
        for w in host_ws:
            idb = self.ot_id_to_idb.get(w.ot_id)
            if idb is None:
                return mental_poker.AuditResult(False, "ot_snapshot_missing", {"ot_id": w.ot_id})
            hand_at_ot_h[w.ot_id] = {idb}
        r4 = mental_poker.audit_witnesses(
            guest_ws, guest_pairs, self.pub_inner, self.protocol_id,
            self.session_id, ot.ROLE_HOST, ot.PAYLOAD_K_INNER, label_to_idb_h, hand_at_ot_g,
        )
        if not r4.ok:
            return r4
        r5 = mental_poker.audit_witnesses(
            host_ws, host_pairs, self.pub_outer, self.protocol_id,
            self.session_id, ot.ROLE_GUEST, ot.PAYLOAD_K_OUTER, label_to_idb_g, hand_at_ot_h,
        )
        if not r5.ok:
            return r5
        return mental_poker.AuditResult(True)

    def transcript_hash(self) -> str:
        return self.transcript.root_hash()

    def sign_receipt(self) -> str:
        return self.transcript.sign(self.keypair)

    def verify_peer_receipt(self, peer_pub_hex: str, sig_hex: str) -> bool:
        # identity binding: when the caller supplied the peer's agent public_key (real
        # P2P handshake), the receipt MUST be signed by exactly that key — not a key
        # the peer fabricated for this receipt. None (in-memory test loops with no real
        # peer identity) skips this check. (codex High fix.)
        if self.peer_public_key and peer_pub_hex != self.peer_public_key:
            return False
        return self.transcript.verify_signature(peer_pub_hex, sig_hex)


def secrets_token_hex(nbytes: int) -> str:
    import secrets

    return secrets.token_hex(nbytes)


# ───────────────────────────── sync drivers ─────────────────────────────


def _run_mp_sync_host(
    spec: dict[str, Any], proto_dir: Path, channel: JsonLineChannel, opts: dict[str, Any],
    args: list[str] | None, state_base: str | Path | None, validate: bool, *,
    event_bus: EventBus | None, coach: bool, pace: float,
    session_id: str | None = None, keypair: Any = None, peer_public_key: str | None = None,
) -> dict[str, Any]:
    import time

    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"host-{int(time.time() * 1000)}")
    hooks.proto_init(opts, "host", args or [], state_dir, _resolve_decision_config(spec, coach))
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "host", spec, state_dir)
    metadata = hooks.proto_host_metadata()
    state: dict[str, Any] = {}
    s = _MpSession(spec, hooks, "host", state, state_dir, event_bus, validate, session_id, keypair, peer_public_key)

    # handshake (join/ready)
    join_msg = channel.recv()
    if validate:
        _validate(spec, join_msg, "guest_to_host")
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": join_msg})
    ready = {"action": "ready"}
    if validate:
        _validate(spec, ready, "host_to_guest")
    channel.send(ready)
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": ready})

    # setup_a (host -> guest)
    _emit(event_bus, "mp_setup_started", {"role": "host"})
    setup_a = s.setup_host()
    if validate:
        _validate(spec, setup_a, "host_to_guest")
    channel.send(setup_a)
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": setup_a})
    _emit(event_bus, "mp_setup_completed", {"role": "host", "deck_size": s.deck_size})

    # setup_b (guest -> host)
    setup_b = channel.recv()
    if validate:
        _validate(spec, setup_b, "guest_to_host")
    s.apply_setup_b(setup_b)
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": setup_b})

    # deal plan
    deal_plan = hooks.proto_mp_initial_deal(state)
    s.plan_deal(deal_plan)

    # deal phase 1: respond to Guest's OT requests (Guest 取 k_H)
    for id_b in s.guest_deal_ids:
        _emit(event_bus, "deal_requested", {"owner": "guest"})
        req = channel.recv()
        if validate:
            _validate(spec, req, "guest_to_host")
        ot_id = req["id"]
        _emit(event_bus, "mp_ot_started", {"ot_id": ot_id, "direction": "received"})
        y = s.respond_inner(int(req["z"], 16))
        resp = s.build_ot_response(ot_id, y)
        if resp is not None:
            if validate:
                _validate(spec, resp, "host_to_guest")
            channel.send(resp)
            _emit(event_bus, "protocol_message", {"direction": "sent", "msg": resp})
        s.record_ot(ot_id, int(req["z"], 16), y)
        s.ot_id_to_idb[ot_id] = id_b
        s.deck.deal_to("guest", id_b)

    # deal phase 2: Host OT requests (Host 取 k_G)
    for id_b in s.host_deal_ids:
        _emit(event_bus, "deal_requested", {"owner": "host"})
        ot_id, z, r, rcv, label = s.host_request_outer(id_b)
        _emit(event_bus, "mp_ot_started", {"ot_id": ot_id, "direction": "sent"})
        req = s.build_ot_request(ot_id, z)
        if validate:
            _validate(spec, req, "host_to_guest")
        channel.send(req)
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": req})
        resp = channel.recv()
        if validate:
            _validate(spec, resp, "guest_to_host")
        y = int(resp["y"], 16)
        rank, suit = s.host_recover_outer(rcv, id_b, y)
        s.record_ot(ot_id, z, y)
        s.remember_witness(ot_id, label, r)
        s.deck.deal_to("host", id_b)
        s.my_hand.add(id_b)
        s.my_hand_cards[id_b] = (rank, suit)
        _emit(event_bus, "mp_ot_completed", {"ot_id": ot_id, "label": label})

    s.inject_views()

    # play loop (host acts on even turns)
    winner = _mp_play_loop_sync(s, hooks, channel, spec, validate, host_first=True)
    return _mp_finalize_sync(s, hooks, channel, spec, validate, event_bus, state_dir,
                             metadata, winner)


def _run_mp_sync_guest(
    spec: dict[str, Any], proto_dir: Path, channel: JsonLineChannel, opts: dict[str, Any],
    args: list[str] | None, state_base: str | Path | None, validate: bool, *,
    event_bus: EventBus | None, coach: bool, pace: float,
    session_id: str | None = None, keypair: Any = None, peer_public_key: str | None = None,
) -> dict[str, Any]:
    import time

    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"guest-{int(time.time() * 1000)}")
    hooks.proto_init(opts, "guest", args or [], state_dir, _resolve_decision_config(spec, coach))
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "guest", spec, state_dir)
    state: dict[str, Any] = {}
    s = _MpSession(spec, hooks, "guest", state, state_dir, event_bus, validate, session_id, keypair, peer_public_key)

    # handshake
    join_msg = {"action": "join"}
    if validate:
        _validate(spec, join_msg, "guest_to_host")
    channel.send(join_msg)
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": join_msg})
    ready = channel.recv()
    if validate:
        _validate(spec, ready, "host_to_guest")
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": ready})

    # setup_a (host -> guest)
    _emit(event_bus, "mp_setup_started", {"role": "guest"})
    setup_a = channel.recv()
    if validate:
        _validate(spec, setup_a, "host_to_guest")
    s.apply_setup_a(setup_a)
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": setup_a})

    # setup_b (guest -> host)
    setup_b = s.setup_guest()
    if validate:
        _validate(spec, setup_b, "guest_to_host")
    channel.send(setup_b)
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": setup_b})
    _emit(event_bus, "mp_setup_completed", {"role": "guest", "deck_size": s.deck_size})

    # deal plan (guest computes the same deterministic plan from idb_list)
    deal_plan = hooks.proto_mp_initial_deal(state)
    s.plan_deal(deal_plan)

    # deal phase 1: Guest OT requests (Guest 取 k_H)
    for id_b in s.guest_deal_ids:
        _emit(event_bus, "deal_requested", {"owner": "guest"})
        ot_id, z, r, rcv, label = s.guest_request_inner(id_b)
        _emit(event_bus, "mp_ot_started", {"ot_id": ot_id, "direction": "sent"})
        req = s.build_ot_request(ot_id, z)
        if validate:
            _validate(spec, req, "guest_to_host")
        channel.send(req)
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": req})
        resp = channel.recv()
        if validate:
            _validate(spec, resp, "host_to_guest")
        y = int(resp["y"], 16)
        rank, suit = s.guest_recover_inner(rcv, id_b, y)
        s.record_ot(ot_id, z, y)
        s.remember_witness(ot_id, label, r)
        s.deck.deal_to("guest", id_b)
        s.my_hand.add(id_b)
        s.my_hand_cards[id_b] = (rank, suit)
        _emit(event_bus, "mp_ot_completed", {"ot_id": ot_id, "label": label})

    # deal phase 2: respond to Host's OT requests (Host 取 k_G)
    for id_b in s.host_deal_ids:
        _emit(event_bus, "deal_requested", {"owner": "host"})
        req = channel.recv()
        if validate:
            _validate(spec, req, "host_to_guest")
        ot_id = req["id"]
        _emit(event_bus, "mp_ot_started", {"ot_id": ot_id, "direction": "received"})
        y = s.respond_outer(int(req["z"], 16))
        resp = s.build_ot_response(ot_id, y)
        if resp is not None:
            if validate:
                _validate(spec, resp, "guest_to_host")
            channel.send(resp)
            _emit(event_bus, "protocol_message", {"direction": "sent", "msg": resp})
        s.record_ot(ot_id, int(req["z"], 16), y)
        s.ot_id_to_idb[ot_id] = id_b
        s.deck.deal_to("host", id_b)

    s.inject_views()

    winner = _mp_play_loop_sync(s, hooks, channel, spec, validate, host_first=True)
    return _mp_finalize_sync(s, hooks, channel, spec, validate, event_bus, state_dir,
                             hooks.proto_host_metadata(), winner)


def _mp_play_loop_sync(s: _MpSession, hooks: Any, channel: JsonLineChannel,
                       spec: dict[str, Any], validate: bool, *, host_first: bool) -> str | None:
    """Strictly alternating turns. The actor plays / draws / passes; the peer verifies.

    Returns the winner ('host'/'guest') once ``proto_mp_check_winner`` reports one, or
    when both sides stall (two consecutive ``mp_pass`` → ``state["_mp_stalled"]``).
    """
    turn = 0
    consecutive_passes = 0
    while True:
        actor = "host" if turn % 2 == 0 else "guest"
        if actor == s.role:
            action = hooks.proto_mp_choose_action(s.state)
            kind = action.get("kind")
            if kind == "play":
                consecutive_passes = 0
                msg = s.build_play(action["id_b"], action.get("call_suit"))
                if validate:
                    _validate(spec, msg, "both")
                channel.send(msg)
                _emit(s.bus, "protocol_message", {"direction": "sent", "msg": msg})
                s.apply_play(s.role, msg)
                _emit(s.bus, "play_verified", {"who": s.role, "id_b": action["id_b"]})
            elif kind == "draw":
                consecutive_passes = 0
                s.do_draw_sync(channel, spec, validate)
            else:  # "pass"
                consecutive_passes += 1
                pass_msg = {"action": "mp_pass"}
                if validate:
                    _validate(spec, pass_msg, "both")
                channel.send(pass_msg)
                _emit(s.bus, "protocol_message", {"direction": "sent", "msg": pass_msg})
                _emit(s.bus, "pass_exchanged", {"who": s.role})
                s.transcript.append("mp_pass", {"who": s.role})
        else:
            msg = channel.recv()
            if validate:
                _validate(spec, msg, "both")
            _emit(s.bus, "protocol_message", {"direction": "received", "msg": msg})
            paction = msg.get("action")
            if paction == "mp_play":
                consecutive_passes = 0
                vr = s.verify_peer_play(msg)
                if not vr.ok:
                    _emit(s.bus, "play_rejected", {"who": s.peer, "reason": vr.reason})
                    abort = {"action": "error", "reason": "play_rejected"}
                    channel.send(abort)
                    raise ValueError(f"peer play rejected: {vr.reason}")
                rule_vr = hooks.proto_mp_validate_play(s.state, s.peer, msg)
                if not rule_vr.ok:
                    _emit(s.bus, "play_rejected", {"who": s.peer, "reason": rule_vr.reason})
                    abort = {"action": "error", "reason": "rule_rejected"}
                    if validate:
                        _validate(spec, abort, "both")
                    channel.send(abort)
                    raise ValueError(f"peer play rule-rejected: {rule_vr.reason}")
                s.apply_play(s.peer, msg)
                _emit(s.bus, "play_verified", {"who": s.peer, "id_b": msg["id_b"]})
            elif paction == "mp_ot_request":
                # peer is drawing (draw reuses the OT request/response, no separate msg)
                consecutive_passes = 0
                s.handle_peer_draw_sync(channel, msg, spec, validate)
            elif paction == "mp_pass":
                consecutive_passes += 1
                _emit(s.bus, "pass_exchanged", {"who": s.peer})
                s.transcript.append("mp_pass", {"who": s.peer})
            else:
                raise ValueError(f"unexpected play-loop message: {paction!r}")
        if consecutive_passes >= 2:
            s.state["_mp_stalled"] = True
        winner = hooks.proto_mp_check_winner(s.state)
        if winner in ("host", "guest"):
            return winner
        if consecutive_passes >= 2:
            return winner
        turn += 1
        # safety valve: if nobody can win and no playable cards remain on either side
        if not s.my_hand and not s.deck.hand_of(s.peer):
            return winner


def _mp_finalize_sync(s: _MpSession, hooks: Any, channel: JsonLineChannel,
                      spec: dict[str, Any], validate: bool, event_bus: EventBus | None,
                      state_dir: Path, metadata: Any, winner: str | None) -> dict[str, Any]:
    base = {"state_dir": str(state_dir), "game_over": True, "winner": winner,
            "metadata": metadata}

    # opening exchange
    if (_fault() or "") == "skip_opening":
        _emit(event_bus, "mp_test_fault_injected", {"fault": "skip_opening"})
        my_opening = None
    else:
        my_opening = s.build_opening()
    if my_opening is not None:
        if validate:
            _validate(spec, my_opening, "both")
        channel.send(my_opening)
        _emit(event_bus, "mp_opening_sent", {"entry_count": len(my_opening["openings"])})
    peer_opening = channel.recv()
    if peer_opening.get("action") != "mp_opening":
        _emit(event_bus, "audit_refused", {"peer_role": s.peer})
        return {**base, "audit_passed": False}
    s.apply_opening(peer_opening)
    s.record_opening_to_transcript()
    _emit(event_bus, "mp_opening_received", {"entry_count": len(peer_opening["openings"])})

    # witness exchange
    my_witness = s.build_witness()
    if my_witness is not None:
        if validate:
            _validate(spec, my_witness, "both")
        channel.send(my_witness)
        _emit(event_bus, "mp_witness_sent", {"witness_count": len(my_witness["witnesses"])})
    peer_witness = channel.recv()
    if peer_witness.get("action") != "mp_witness":
        _emit(event_bus, "audit_refused", {"peer_role": s.peer})
        return {**base, "audit_passed": False}
    s.apply_witness(peer_witness)
    s.record_witness_to_transcript()
    _emit(event_bus, "mp_witness_received", {"witness_count": len(peer_witness["witnesses"])})

    _emit(event_bus, "audit_started", {})
    result = s.audit()
    if not result.ok:
        _emit(event_bus, "audit_failed", {"reason": result.reason, "evidence": result.evidence})
        return {**base, "audit_passed": False}

    _emit(event_bus, "audit_passed", {})
    # terminal receipt dual-sign
    thash = s.transcript_hash()
    sig = s.sign_receipt()
    receipt = {"action": "mp_terminal_receipt", "transcript_hash": thash, "signature": sig,
               "audit_status": "passed",
               "public_key": s.keypair.public_key if s.keypair else ""}
    if validate:
        _validate(spec, receipt, "both")
    channel.send(receipt)
    _emit(event_bus, "mp_terminal_receipt_signed", {"transcript_hash": thash})
    peer_receipt = channel.recv()
    if peer_receipt.get("action") != "mp_terminal_receipt":
        _emit(event_bus, "audit_refused", {"peer_role": s.peer})
        return {**base, "audit_passed": False}
    peer_ok = peer_receipt.get("transcript_hash", "") == thash and s.verify_peer_receipt(
        peer_receipt.get("public_key", ""), peer_receipt.get("signature", ""))
    _emit(event_bus, "mp_terminal_receipt_verified",
          {"transcript_hash": thash, "peer_signature_ok": peer_ok})
    if not peer_ok:
        _emit(event_bus, "audit_failed", {"reason": "peer_receipt_signature_invalid"})
        return {**base, "audit_passed": False}

    # persist receipt locally
    try:
        (Path(state_dir) / "transcript_receipt.json").write_text(
            json.dumps({"transcript_hash": thash, "signature": sig,
                        "peer_signature": peer_receipt.get("signature", ""),
                        "audit_status": "passed"}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    _emit(event_bus, "game_over", {"winner": winner, "audit_status": "passed"})
    _snapshot_phase(hooks, "game_over", "Mental poker session completed", winner=winner)
    return {**base, "audit_passed": True}


# ──────────────────────────── async drivers ────────────────────────────


async def _run_mp_async_host(
    spec: dict[str, Any], proto_dir: Path, channel: AsyncJsonLineChannel, opts: dict[str, Any],
    args: list[str] | None, state_base: str | Path | None, validate: bool, *,
    event_bus: EventBus | None, coach: bool, pace: float,
    heartbeat_interval: float = 0, heartbeat_timeout: float = 0,
    session_id: str | None = None, keypair: Any = None, peer_public_key: str | None = None,
) -> dict[str, Any]:
    import asyncio
    import time

    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"host-{int(time.time() * 1000)}")
    hooks.proto_init(opts, "host", args or [], state_dir, _resolve_decision_config(spec, coach))
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "host", spec, state_dir)
    metadata = hooks.proto_host_metadata()
    state: dict[str, Any] = {}
    s = _MpSession(spec, hooks, "host", state, state_dir, event_bus, validate, session_id, keypair, peer_public_key)
    channel = await _maybe_wrap_heartbeat(channel, heartbeat_interval, heartbeat_timeout, event_bus, hooks)

    try:
        join_msg = await channel.recv()
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": join_msg})
        ready = {"action": "ready"}
        if validate:
            _validate(spec, ready, "host_to_guest")
        await channel.send(ready)
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": ready})

        _emit(event_bus, "mp_setup_started", {"role": "host"})
        setup_a = s.setup_host()
        if validate:
            _validate(spec, setup_a, "host_to_guest")
        await channel.send(setup_a)
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": setup_a})
        _emit(event_bus, "mp_setup_completed", {"role": "host", "deck_size": s.deck_size})

        setup_b = await channel.recv()
        if validate:
            _validate(spec, setup_b, "guest_to_host")
        s.apply_setup_b(setup_b)
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": setup_b})

        deal_plan = hooks.proto_mp_initial_deal(state)
        s.plan_deal(deal_plan)

        for id_b in s.guest_deal_ids:
            _emit(event_bus, "deal_requested", {"owner": "guest"})
            req = await channel.recv()
            if validate:
                _validate(spec, req, "guest_to_host")
            ot_id = req["id"]
            _emit(event_bus, "mp_ot_started", {"ot_id": ot_id, "direction": "received"})
            y = s.respond_inner(int(req["z"], 16))
            resp = s.build_ot_response(ot_id, y)
            if resp is not None:
                if validate:
                    _validate(spec, resp, "host_to_guest")
                await channel.send(resp)
                _emit(event_bus, "protocol_message", {"direction": "sent", "msg": resp})
            s.record_ot(ot_id, int(req["z"], 16), y)
            s.ot_id_to_idb[ot_id] = id_b
            s.deck.deal_to("guest", id_b)

        for id_b in s.host_deal_ids:
            _emit(event_bus, "deal_requested", {"owner": "host"})
            ot_id, z, r, rcv, label = s.host_request_outer(id_b)
            _emit(event_bus, "mp_ot_started", {"ot_id": ot_id, "direction": "sent"})
            req = s.build_ot_request(ot_id, z)
            if validate:
                _validate(spec, req, "host_to_guest")
            await channel.send(req)
            resp = await channel.recv()
            if validate:
                _validate(spec, resp, "guest_to_host")
            y = int(resp["y"], 16)
            rank, suit = s.host_recover_outer(rcv, id_b, y)
            s.record_ot(ot_id, z, y)
            s.remember_witness(ot_id, label, r)
            s.deck.deal_to("host", id_b)
            s.my_hand.add(id_b)
            s.my_hand_cards[id_b] = (rank, suit)
            _emit(event_bus, "mp_ot_completed", {"ot_id": ot_id, "label": label})

        s.inject_views()
        winner = await _mp_play_loop_async(s, hooks, channel, spec, validate)
        return await _mp_finalize_async(s, hooks, channel, spec, validate, event_bus, state_dir,
                                        metadata, winner)
    except ChannelClosed:
        return _handle_peer_disconnect(hooks, event_bus, state_dir=str(state_dir))


async def _run_mp_async_guest(
    spec: dict[str, Any], proto_dir: Path, channel: AsyncJsonLineChannel, opts: dict[str, Any],
    args: list[str] | None, state_base: str | Path | None, validate: bool, *,
    event_bus: EventBus | None, coach: bool, pace: float,
    heartbeat_interval: float = 0, heartbeat_timeout: float = 0,
    session_id: str | None = None, keypair: Any = None, peer_public_key: str | None = None,
) -> dict[str, Any]:
    import time

    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"guest-{int(time.time() * 1000)}")
    hooks.proto_init(opts, "guest", args or [], state_dir, _resolve_decision_config(spec, coach))
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "guest", spec, state_dir)
    state: dict[str, Any] = {}
    s = _MpSession(spec, hooks, "guest", state, state_dir, event_bus, validate, session_id, keypair, peer_public_key)
    channel = await _maybe_wrap_heartbeat(channel, heartbeat_interval, heartbeat_timeout, event_bus, hooks)

    try:
        join_msg = {"action": "join"}
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        await channel.send(join_msg)
        ready = await channel.recv()
        if validate:
            _validate(spec, ready, "host_to_guest")

        _emit(event_bus, "mp_setup_started", {"role": "guest"})
        setup_a = await channel.recv()
        if validate:
            _validate(spec, setup_a, "host_to_guest")
        s.apply_setup_a(setup_a)

        setup_b = s.setup_guest()
        if validate:
            _validate(spec, setup_b, "guest_to_host")
        await channel.send(setup_b)
        _emit(event_bus, "mp_setup_completed", {"role": "guest", "deck_size": s.deck_size})

        deal_plan = hooks.proto_mp_initial_deal(state)
        s.plan_deal(deal_plan)

        for id_b in s.guest_deal_ids:
            _emit(event_bus, "deal_requested", {"owner": "guest"})
            ot_id, z, r, rcv, label = s.guest_request_inner(id_b)
            req = s.build_ot_request(ot_id, z)
            if validate:
                _validate(spec, req, "guest_to_host")
            await channel.send(req)
            resp = await channel.recv()
            if validate:
                _validate(spec, resp, "host_to_guest")
            y = int(resp["y"], 16)
            rank, suit = s.guest_recover_inner(rcv, id_b, y)
            s.record_ot(ot_id, z, y)
            s.remember_witness(ot_id, label, r)
            s.deck.deal_to("guest", id_b)
            s.my_hand.add(id_b)
            s.my_hand_cards[id_b] = (rank, suit)
            _emit(event_bus, "mp_ot_completed", {"ot_id": ot_id, "label": label})

        for id_b in s.host_deal_ids:
            _emit(event_bus, "deal_requested", {"owner": "host"})
            req = await channel.recv()
            if validate:
                _validate(spec, req, "host_to_guest")
            ot_id = req["id"]
            y = s.respond_outer(int(req["z"], 16))
            resp = s.build_ot_response(ot_id, y)
            if resp is not None:
                if validate:
                    _validate(spec, resp, "guest_to_host")
                await channel.send(resp)
            s.record_ot(ot_id, int(req["z"], 16), y)
            s.ot_id_to_idb[ot_id] = id_b
            s.deck.deal_to("host", id_b)

        s.inject_views()
        winner = await _mp_play_loop_async(s, hooks, channel, spec, validate)
        return await _mp_finalize_async(s, hooks, channel, spec, validate, event_bus, state_dir,
                                        hooks.proto_host_metadata(), winner)
    except ChannelClosed:
        return _handle_peer_disconnect(hooks, event_bus, state_dir=str(state_dir))


async def _mp_play_loop_async(s: _MpSession, hooks: Any, channel: AsyncJsonLineChannel,
                              spec: dict[str, Any], validate: bool) -> str | None:
    turn = 0
    consecutive_passes = 0
    while True:
        actor = "host" if turn % 2 == 0 else "guest"
        if actor == s.role:
            action = hooks.proto_mp_choose_action(s.state)
            kind = action.get("kind")
            if kind == "play":
                consecutive_passes = 0
                msg = s.build_play(action["id_b"], action.get("call_suit"))
                if validate:
                    _validate(spec, msg, "both")
                await channel.send(msg)
                s.apply_play(s.role, msg)
                _emit(s.bus, "play_verified", {"who": s.role, "id_b": action["id_b"]})
            elif kind == "draw":
                consecutive_passes = 0
                await s.do_draw_async(channel, spec, validate)
            else:  # "pass"
                consecutive_passes += 1
                pass_msg = {"action": "mp_pass"}
                if validate:
                    _validate(spec, pass_msg, "both")
                await channel.send(pass_msg)
                _emit(s.bus, "pass_exchanged", {"who": s.role})
                s.transcript.append("mp_pass", {"who": s.role})
        else:
            msg = await channel.recv()
            if validate:
                _validate(spec, msg, "both")
            paction = msg.get("action")
            if paction == "mp_play":
                consecutive_passes = 0
                vr = s.verify_peer_play(msg)
                if not vr.ok:
                    _emit(s.bus, "play_rejected", {"who": s.peer, "reason": vr.reason})
                    abort = {"action": "error", "reason": "play_rejected"}
                    await channel.send(abort)
                    raise ValueError(f"peer play rejected: {vr.reason}")
                rule_vr = hooks.proto_mp_validate_play(s.state, s.peer, msg)
                if not rule_vr.ok:
                    _emit(s.bus, "play_rejected", {"who": s.peer, "reason": rule_vr.reason})
                    abort = {"action": "error", "reason": "rule_rejected"}
                    if validate:
                        _validate(spec, abort, "both")
                    await channel.send(abort)
                    raise ValueError(f"peer play rule-rejected: {rule_vr.reason}")
                s.apply_play(s.peer, msg)
                _emit(s.bus, "play_verified", {"who": s.peer, "id_b": msg["id_b"]})
            elif paction == "mp_ot_request":
                consecutive_passes = 0
                await s.handle_peer_draw_async(channel, msg, spec, validate)
            elif paction == "mp_pass":
                consecutive_passes += 1
                _emit(s.bus, "pass_exchanged", {"who": s.peer})
                s.transcript.append("mp_pass", {"who": s.peer})
            else:
                raise ValueError(f"unexpected play-loop message: {paction!r}")
        if consecutive_passes >= 2:
            s.state["_mp_stalled"] = True
        winner = hooks.proto_mp_check_winner(s.state)
        if winner in ("host", "guest"):
            return winner
        if consecutive_passes >= 2:
            return winner
        turn += 1
        if not s.my_hand and not s.deck.hand_of(s.peer):
            return winner


async def _mp_finalize_async(s: _MpSession, hooks: Any, channel: AsyncJsonLineChannel,
                             spec: dict[str, Any], validate: bool, event_bus: EventBus | None,
                             state_dir: Path, metadata: Any, winner: str | None) -> dict[str, Any]:
    base = {"state_dir": str(state_dir), "game_over": True, "winner": winner, "metadata": metadata}

    if (_fault() or "") == "skip_opening":
        _emit(event_bus, "mp_test_fault_injected", {"fault": "skip_opening"})
        my_opening = None
    else:
        my_opening = s.build_opening()
    if my_opening is not None:
        if validate:
            _validate(spec, my_opening, "both")
        await channel.send(my_opening)
        _emit(event_bus, "mp_opening_sent", {"entry_count": len(my_opening["openings"])})
    peer_opening = await channel.recv()
    if peer_opening.get("action") != "mp_opening":
        _emit(event_bus, "audit_refused", {"peer_role": s.peer})
        return {**base, "audit_passed": False}
    s.apply_opening(peer_opening)
    s.record_opening_to_transcript()

    my_witness = s.build_witness()
    if my_witness is not None:
        if validate:
            _validate(spec, my_witness, "both")
        await channel.send(my_witness)
    peer_witness = await channel.recv()
    if peer_witness.get("action") != "mp_witness":
        _emit(event_bus, "audit_refused", {"peer_role": s.peer})
        return {**base, "audit_passed": False}
    s.apply_witness(peer_witness)
    s.record_witness_to_transcript()

    _emit(event_bus, "audit_started", {})
    result = s.audit()
    if not result.ok:
        _emit(event_bus, "audit_failed", {"reason": result.reason, "evidence": result.evidence})
        return {**base, "audit_passed": False}
    _emit(event_bus, "audit_passed", {})

    thash = s.transcript_hash()
    sig = s.sign_receipt()
    receipt = {"action": "mp_terminal_receipt", "transcript_hash": thash, "signature": sig,
               "audit_status": "passed",
               "public_key": s.keypair.public_key if s.keypair else ""}
    if validate:
        _validate(spec, receipt, "both")
    await channel.send(receipt)
    peer_receipt = await channel.recv()
    if peer_receipt.get("action") != "mp_terminal_receipt":
        _emit(event_bus, "audit_refused", {"peer_role": s.peer})
        return {**base, "audit_passed": False}
    peer_ok = peer_receipt.get("transcript_hash", "") == thash and s.verify_peer_receipt(
        peer_receipt.get("public_key", ""), peer_receipt.get("signature", ""))
    _emit(event_bus, "mp_terminal_receipt_verified",
          {"transcript_hash": thash, "peer_signature_ok": peer_ok})
    if not peer_ok:
        _emit(event_bus, "audit_failed", {"reason": "peer_receipt_signature_invalid"})
        return {**base, "audit_passed": False}
    _emit(event_bus, "game_over", {"winner": winner, "audit_status": "passed"})
    _snapshot_phase(hooks, "game_over", "Mental poker session completed", winner=winner)
    return {**base, "audit_passed": True}


# ──────────────────────────── dispatchers ────────────────────────────


def run_mental_poker_sync(
    spec: dict[str, Any], proto_dir: Path, channel: JsonLineChannel, options: dict[str, Any],
    args: list[str] | None, state_base: str | Path | None, validate: bool, role: str, *,
    event_bus: EventBus | None = None, coach: bool = False, pace: float = 0,
    session_id: str | None = None, keypair: Any = None, peer_public_key: str | None = None,
) -> dict[str, Any]:
    if role == "host":
        return _run_mp_sync_host(spec, proto_dir, channel, options, args, state_base, validate,
                                 event_bus=event_bus, coach=coach, pace=pace,
                                 session_id=session_id, keypair=keypair, peer_public_key=peer_public_key)
    return _run_mp_sync_guest(spec, proto_dir, channel, options, args, state_base, validate,
                              event_bus=event_bus, coach=coach, pace=pace,
                              session_id=session_id, keypair=keypair, peer_public_key=peer_public_key)


async def run_mental_poker_async(
    spec: dict[str, Any], proto_dir: Path, channel: AsyncJsonLineChannel, options: dict[str, Any],
    args: list[str] | None, state_base: str | Path | None, validate: bool, role: str, *,
    event_bus: EventBus | None = None, coach: bool = False, pace: float = 0,
    heartbeat_interval: float = 0, heartbeat_timeout: float = 0,
    session_id: str | None = None, keypair: Any = None, peer_public_key: str | None = None,
) -> dict[str, Any]:
    if role == "host":
        return await _run_mp_async_host(
            spec, proto_dir, channel, options, args, state_base, validate,
            event_bus=event_bus, coach=coach, pace=pace,
            heartbeat_interval=heartbeat_interval, heartbeat_timeout=heartbeat_timeout,
            session_id=session_id, keypair=keypair, peer_public_key=peer_public_key)
    return await _run_mp_async_guest(
        spec, proto_dir, channel, options, args, state_base, validate,
        event_bus=event_bus, coach=coach, pace=pace,
        heartbeat_interval=heartbeat_interval, heartbeat_timeout=heartbeat_timeout,
        session_id=session_id, keypair=keypair, peer_public_key=peer_public_key)
