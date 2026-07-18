"""Crazy Eights (v016 M2) — shedding card game over the mental_poker engine.

Business rules only: the engine owns all ``mp_*`` wire messages (setup / OT / play /
opening / witness / receipt). Hidden cards (drawn from stock) are NEVER sent in
cleartext — the engine privately recovers each face via OT and injects it into
``_mp_hand_cards``. Hooks only supply the deck universe, deal plan, per-turn intent
(play/draw/pass), rule validation, and winner detection.

Simplifications (see spec.description):
  - No starting discard: the very first play is unconstrained; ``current_suit`` /
    ``top_rank`` are seeded by that first play.
  - Drawing ends the turn (ADR-10): a player who draws does not also play that turn.

Cheat-detectable model (not for high-stakes use): the engine's post-game audit
(opening + witnesses + transcript) catches dealt-card peeking and forged keys.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from aigenora.proto.hooks import ProtocolHooks

# rank_index 0..12 <-> ["2".."A"]; suit_index 0..3 <-> ["C","D","H","S"].
# The primitive layer (aead_deck) is index-agnostic; this mapping lives here.
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
SUITS = ["C", "D", "H", "S"]
EIGHT_IDX = RANKS.index("8")  # 6 — the wild card


def make_deck() -> list[tuple[int, int]]:
    """Full 52-card deck as (rank_index, suit_index) pairs (no duplicates)."""
    return [(r, s) for r in range(len(RANKS)) for s in range(len(SUITS))]


def can_play(rank: int, suit: int, top_rank: int | None, current_suit: int | None,
             first_turn: bool) -> bool:
    """Crazy Eights legality. A wild 8 always plays; otherwise match the current suit
    or the top rank. On the first turn (empty discard) anything plays."""
    if first_turn:
        return True
    if rank == EIGHT_IDX:
        return True
    if current_suit is not None and suit == current_suit:
        return True
    if top_rank is not None and rank == top_rank:
        return True
    return False


def pick_call_suit(hand_cards: dict[str, tuple[int, int]]) -> int:
    """When playing an 8, pick the suit we hold the most of (excluding 8s) so we keep
    follow-on options open. Falls back to suit 0 if the hand is all 8s."""
    counts = Counter(s for (r, s) in hand_cards.values() if r != EIGHT_IDX)
    if not counts:
        return 0
    return counts.most_common(1)[0][0]


def pick_play_basic(hand_cards: dict[str, tuple[int, int]], top_rank: int | None,
                    current_suit: int | None, first_turn: bool) -> tuple[str, int | None] | None:
    """Heuristic: prefer a non-8 playable card (save wilds); fall back to an 8 with a
    ``call_suit``. Returns ``(id_b, call_suit_or_None)`` or ``None`` if nothing plays."""
    playable_non8: list[str] = []
    playable_8: list[str] = []
    for id_b, (rank, suit) in hand_cards.items():
        if can_play(rank, suit, top_rank, current_suit, first_turn):
            (playable_8 if rank == EIGHT_IDX else playable_non8).append(id_b)
    if playable_non8:
        return (sorted(playable_non8)[0], None)
    if playable_8:
        id_b = sorted(playable_8)[0]
        return (id_b, pick_call_suit(hand_cards))
    return None


def winner_by_hand_count(guest_n: int, host_n: int) -> str | None:
    """Tie-break when the deck is exhausted and both sides pass: fewer cards wins;
    equal card counts => no winner (None)."""
    if host_n < guest_n:
        return "host"
    if guest_n < host_n:
        return "guest"
    return None


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")
    DECISION_SCHEMA = {
        "match_key": "action_seq",
        "value_field": "kind",
        "choices": {"play": "play", "draw": "draw", "pass": "pass"},
        "card_field": "id_b",
        "optional_fields": ["call_suit"],
    }

    def proto_host_metadata(self) -> tuple[str, str, str, dict[str, Any]]:
        return ("Crazy Eights", "game,crazy-eights,cards", "supply",
                {"hand_size": self.hand_size})

    def proto_init(self, options: dict[str, Any], role: str, args: list[str],
                   state_dir: Any, decision_config: dict[str, Any] | None = None) -> None:
        super().proto_init(options, role, args, state_dir, decision_config)
        self.hand_size = int(options.get("hand_size") or 5)
        self.current_suit: int | None = None  # suit to follow (first play / 8's call_suit)
        self.top_rank: int | None = None      # rank of the discard top

    # ── mental_poker callbacks ──

    def proto_mp_deck_universe(self) -> list[tuple[int, int]]:
        return make_deck()

    def proto_mp_initial_deal(self, state: dict) -> dict:
        return {"host": self.hand_size, "guest": self.hand_size}

    def _first_turn(self, state: dict) -> bool:
        deck = state.get("_mp_deck_view")
        return deck is None or not deck.played

    def _apply_play_locally(self, rank: int, suit: int, call_suit: int | None) -> None:
        """Update the local follow state after a (self or peer) play commits."""
        self.top_rank = rank
        self.current_suit = call_suit if rank == EIGHT_IDX else suit

    def proto_mp_choose_action(self, state: dict) -> dict:
        hand_cards = state.get("_mp_hand_cards") or {}
        deck = state.get("_mp_deck_view")
        stock_empty = deck is None or not deck.stock
        first = self._first_turn(state)
        pick = pick_play_basic(hand_cards, self.top_rank, self.current_suit, first)
        if pick is not None:
            id_b, call_suit = pick
            rank, suit = hand_cards[id_b]
            self._apply_play_locally(rank, suit, call_suit)
            return {"kind": "play", "id_b": id_b, "call_suit": call_suit}
        # nothing playable
        if not stock_empty:
            return {"kind": "draw"}
        return {"kind": "pass"}

    def proto_mp_legal_actions(self, state: dict) -> list[dict[str, Any]]:
        """Expose only this participant's legal actions and known card faces."""
        hand_cards = state.get("_mp_hand_cards") or {}
        deck = state.get("_mp_deck_view")
        self.snapshot.update(
            game="crazy_eights",
            top_rank=self.top_rank,
            current_suit=self.current_suit,
            ranks=RANKS,
            suits=SUITS,
        )
        stock_empty = deck is None or not deck.stock
        first = self._first_turn(state)
        playable: list[dict[str, Any]] = []
        for id_b, (rank, suit) in sorted(hand_cards.items()):
            if can_play(rank, suit, self.top_rank, self.current_suit, first):
                playable.append({
                    "kind": "play",
                    "id_b": id_b,
                    "rank": rank,
                    "suit": suit,
                    "call_suit_required": rank == EIGHT_IDX,
                })
        if playable:
            return playable
        return [{"kind": "pass" if stock_empty else "draw"}]

    def proto_mp_coerce_action(self, state: dict, decision: dict[str, Any]) -> dict[str, Any]:
        action = super().proto_mp_coerce_action(state, decision)
        legal = self.proto_mp_legal_actions(state)
        kind = action["kind"]
        if kind != "play":
            if not any(item["kind"] == kind for item in legal):
                raise ValueError(f"{kind} is not legal in the current state")
            return action

        legal_play = next(
            (item for item in legal
             if item["kind"] == "play" and item["id_b"] == action["id_b"]),
            None,
        )
        if legal_play is None:
            raise ValueError("selected card is not playable")
        if legal_play["call_suit_required"]:
            if "call_suit" not in action or action["call_suit"] not in range(len(SUITS)):
                raise ValueError("playing an 8 requires call_suit between 0 and 3")
        elif "call_suit" in action:
            raise ValueError("call_suit is only allowed when playing an 8")
        return action

    def proto_mp_apply_local_action(self, state: dict, action: dict[str, Any]) -> None:
        if action["kind"] != "play":
            return
        rank, suit = (state.get("_mp_hand_cards") or {})[action["id_b"]]
        self._apply_play_locally(rank, suit, action.get("call_suit"))

    def proto_mp_validate_play(self, state: dict, who: str, play_msg: dict):
        from aigenora.proto import mental_poker

        rank = int(play_msg["rank"])
        suit = int(play_msg["suit"])
        call_suit = play_msg.get("call_suit")
        if call_suit is not None:
            call_suit = int(call_suit)
        first = self._first_turn(state)
        if not can_play(rank, suit, self.top_rank, self.current_suit, first):
            return mental_poker.ValidationResult(False, "illegal_play")
        if rank == EIGHT_IDX and call_suit is None:
            return mental_poker.ValidationResult(False, "missing_call_suit")
        if rank != EIGHT_IDX and call_suit is not None:
            return mental_poker.ValidationResult(False, "unexpected_call_suit")
        self._apply_play_locally(rank, suit, call_suit)
        return mental_poker.ValidationResult(True)

    def proto_mp_check_winner(self, state: dict) -> str | None:
        deck = state.get("_mp_deck_view")
        if deck is None:
            return None
        host_n = len(deck.hand_of("host"))
        guest_n = len(deck.hand_of("guest"))
        if host_n == 0:
            return "host"
        if guest_n == 0:
            return "guest"
        if state.get("_mp_stalled"):
            return winner_by_hand_count(guest_n, host_n)
        return None

    def proto_display(self, msg: dict[str, Any], direction: str) -> str | None:
        action = msg.get("action")
        if action == "mp_play":
            rank = RANKS[int(msg["rank"])]
            suit = SUITS[int(msg["suit"])]
            extra = f" (call {SUITS[int(msg['call_suit'])]})" if msg.get("call_suit") is not None else ""
            return f"play {rank}{suit}{extra}"
        if action == "mp_pass":
            return "PASS"
        if action == "error":
            return f"illegal: {msg.get('reason')}"
        return None
