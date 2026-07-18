"""Briscola (v016 M2) — trick-taking card game over the mental_poker engine.

Business rules only: the engine owns all ``mp_*`` wire messages (setup / OT / play /
opening / witness / receipt). Hidden cards (drawn from stock) are NEVER sent in
cleartext — the engine privately recovers each face via OT and injects it into
``_mp_hand_cards``. Hooks only supply the deck universe, deal plan, per-turn intent
(play/draw), rule validation, and winner detection.

The engine's strictly-alternating play loop (host on even turns, guest on odd) maps
naturally onto ADR-9's fixed-leader simplification: the host is always the trick
leader, the guest always the follower. Each trick = host-lead + guest-follow (two
turns). Trick state (current trick, accumulated points) lives entirely in hooks and
is kept in sync across sides via choose_action (records own card) + validate_play
(records peer's card) + check_winner (resolves the trick when full).

Simplifications (see spec.description, ADR-8/9):
  - Trump suit (briscola) is derived deterministically from make_deck() — same every
    game, no per-game random trump, no separate briscola indicator card.
  - No briscola indicator card → stock is 34 (even) so post-trick refills stay
    symmetric (host draws on its even turn, guest on its odd turn) and never deadlock
    the engine's turn alternation.
  - Fixed leader/follower: host always leads, guest always follows. The follower has
    a slight information advantage (sees the lead before choosing); accepted under
    ADR-9's cheat-detectable, non-high-stakes model.
  - Refill order is fixed (host first) — irrelevant to fairness since OT draws are
    private.

Cheat-detectable model (not for high-stakes use): the engine's post-game audit
(opening + witnesses + transcript) catches dealt-card peeking and forged keys.
"""
from __future__ import annotations

from typing import Any

from aigenora.proto.hooks import ProtocolHooks

# rank_index 0..9 <-> ["A","2","3","4","5","6","7","J","Q","K"] (Italian 40-card deck,
# standard poker ranks 8/9/10 removed); suit_index 0..3 <-> ["C","D","H","S"].
# The primitive layer (aead_deck) is index-agnostic; this mapping lives here.
RANKS = ["A", "2", "3", "4", "5", "6", "7", "J", "Q", "K"]
SUITS = ["C", "D", "H", "S"]

# Trick strength (NOT card points): A > 3 > K > Q > J > 7 > 6 > 5 > 4 > 2.
RANK_STRENGTH = {"A": 10, "3": 9, "K": 8, "Q": 7, "J": 6, "7": 5, "6": 4, "5": 3, "4": 2, "2": 1}

# Card points (for scoring): A=11, 3=10, K=4, Q=3, J=2, others 0. Deck total = 120.
CARD_POINTS = {"A": 11, "3": 10, "K": 4, "Q": 3, "J": 2}
TOTAL_POINTS = 4 * sum(CARD_POINTS.values())  # 120

HAND_SIZE = 3


def make_deck() -> list[tuple[int, int]]:
    """Full 40-card Italian deck as (rank_index, suit_index) pairs (no duplicates)."""
    return [(r, s) for r in range(len(RANKS)) for s in range(len(SUITS))]


def briscola_suit() -> int:
    """Trump suit, deterministically derived from the canonical deck order. Both sides
    compute the same value with zero disclosure and zero info asymmetry (no OT, no
    public reveal). Simplification: same trump every game, no indicator card (ADR-8)."""
    return make_deck()[0][1]


def rank_strength(rank_idx: int) -> int:
    return RANK_STRENGTH[RANKS[rank_idx]]


def card_points(rank_idx: int) -> int:
    return CARD_POINTS.get(RANKS[rank_idx], 0)


def trick_winner(lead: tuple[int, int], follow: tuple[int, int], briscola: int) -> str:
    """2-player Briscola trick winner. ``lead``/``follow`` = (rank_idx, suit_idx). The
    host is always the leader, the guest always the follower. Returns ``'host'`` when
    the leader wins, ``'guest'`` when the follower wins.

    Same suit (both trump or both non-trump) → higher rank strength wins. Different
    suits → the follower wins only by trumping; otherwise the leader wins."""
    lead_r, lead_s = lead
    foll_r, foll_s = follow
    if lead_s == foll_s:
        return "host" if rank_strength(lead_r) > rank_strength(foll_r) else "guest"
    if foll_s == briscola:
        return "guest"  # follower trumps the leader
    return "host"      # follower neither followed suit nor trumped


def pick_lead(hand_cards: dict[str, tuple[int, int]], briscola: int) -> str:
    """Leader leads. Heuristic: lead the lowest-point non-trump card to preserve trumps
    and high-point cards. Deterministic (ties → lower rank strength → id_b)."""
    cards = [(id_b, r, s) for id_b, (r, s) in hand_cards.items()]
    cards.sort(key=lambda t: (t[2] == briscola, card_points(t[1]), rank_strength(t[1]), t[0]))
    return cards[0][0]


def pick_follow(hand_cards: dict[str, tuple[int, int]], lead: tuple[int, int],
                briscola: int) -> str:
    """Follower follows (Briscola does not force following suit). Heuristic: if we can
    win the trick, win with the cheapest winner; otherwise dump the lowest-point card.
    Deterministic."""
    winners = [(id_b, r, s) for id_b, (r, s) in hand_cards.items()
               if trick_winner(lead, (r, s), briscola) == "guest"]
    if winners:
        winners.sort(key=lambda t: (card_points(t[1]), rank_strength(t[1]), t[0]))
        return winners[0][0]
    rest = [(id_b, r, s) for id_b, (r, s) in hand_cards.items()]
    rest.sort(key=lambda t: (card_points(t[1]), rank_strength(t[1]), t[0]))
    return rest[0][0]


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")
    DECISION_SCHEMA = {
        "match_key": "action_seq",
        "value_field": "kind",
        "choices": {"play": "play", "draw": "draw"},
        "card_field": "id_b",
    }

    def proto_host_metadata(self) -> tuple[str, str, str, dict[str, Any]]:
        return ("Briscola", "game,briscola,cards,trick-taking", "supply", {})

    def proto_init(self, options: dict[str, Any], role: str, args: list[str],
                   state_dir: Any, decision_config: dict[str, Any] | None = None) -> None:
        super().proto_init(options, role, args, state_dir, decision_config)
        self.briscola: int = briscola_suit()
        # current trick: list of (who, (rank_idx, suit_idx)); cleared when resolved
        self.current_trick: list[tuple[str, tuple[int, int]]] = []
        self.points: dict[str, int] = {"host": 0, "guest": 0}
        self.completed_tricks: int = 0

    # ── mental_poker callbacks ──

    def proto_mp_deck_universe(self) -> list[tuple[int, int]]:
        return make_deck()

    def proto_mp_initial_deal(self, state: dict) -> dict:
        return {"host": HAND_SIZE, "guest": HAND_SIZE}

    def proto_mp_choose_action(self, state: dict) -> dict:
        hand_cards = state.get("_mp_hand_cards") or {}
        deck = state.get("_mp_deck_view")
        stock = deck.stock if deck is not None else set()
        role = state.get("_mp_role")

        # Phase 1 — refill after a completed trick (before the next lead). Symmetric:
        # host draws on its even lead-turn, guest on its odd follow-turn, so the two
        # refills never deadlock the engine's turn alternation.
        if not self.current_trick and self.completed_tricks > 0:
            if len(hand_cards) < HAND_SIZE and stock:
                return {"kind": "draw"}

        # Phase 2 — play. Empty trick => leader leads; otherwise follower follows.
        if not self.current_trick:
            id_b = pick_lead(hand_cards, self.briscola)
        else:
            lead = self.current_trick[0][1]
            id_b = pick_follow(hand_cards, lead, self.briscola)
        rank, suit = hand_cards[id_b]
        self.current_trick.append((role, (rank, suit)))
        return {"kind": "play", "id_b": id_b}

    def proto_mp_legal_actions(self, state: dict) -> list[dict[str, Any]]:
        hand_cards = state.get("_mp_hand_cards") or {}
        deck = state.get("_mp_deck_view")
        self.snapshot.update(
            game="briscola",
            briscola=self.briscola,
            points=self.points,
            completed_tricks=self.completed_tricks,
            current_trick=[
                {"who": who, "rank": card[0], "suit": card[1]}
                for who, card in self.current_trick
            ],
            ranks=RANKS,
            suits=SUITS,
        )
        stock = deck.stock if deck is not None else set()
        if (not self.current_trick and self.completed_tricks > 0
                and len(hand_cards) < HAND_SIZE and stock):
            return [{"kind": "draw"}]
        return [
            {"kind": "play", "id_b": id_b, "rank": rank, "suit": suit}
            for id_b, (rank, suit) in sorted(hand_cards.items())
        ]

    def proto_mp_coerce_action(self, state: dict, decision: dict[str, Any]) -> dict[str, Any]:
        action = super().proto_mp_coerce_action(state, decision)
        legal = self.proto_mp_legal_actions(state)
        if action["kind"] == "draw":
            if not any(item["kind"] == "draw" for item in legal):
                raise ValueError("draw is not legal in the current state")
            return action
        if action["kind"] != "play":
            raise ValueError("Briscola only supports play and refill draw actions")
        if not any(
            item["kind"] == "play" and item["id_b"] == action["id_b"]
            for item in legal
        ):
            raise ValueError("selected card is not legal in the current state")
        return action

    def proto_mp_apply_local_action(self, state: dict, action: dict[str, Any]) -> None:
        if action["kind"] != "play":
            return
        rank, suit = (state.get("_mp_hand_cards") or {})[action["id_b"]]
        self.current_trick.append((state.get("_mp_role"), (rank, suit)))

    def proto_mp_validate_play(self, state: dict, who: str, play_msg: dict):
        from aigenora.proto import mental_poker

        rank = int(play_msg["rank"])
        suit = int(play_msg["suit"])
        self.current_trick.append((who, (rank, suit)))
        # Briscola does not force following suit; any in-hand, unplayed card is legal.
        # Nullifier + key verification is already done by engine.verify_peer_play.
        return mental_poker.ValidationResult(True)

    def proto_mp_check_winner(self, state: dict) -> str | None:
        deck = state.get("_mp_deck_view")
        # Resolve a full trick (lead + follow) — runs on the turn that completes it.
        if len(self.current_trick) >= 2:
            lead_card = self.current_trick[0][1]
            foll_card = self.current_trick[1][1]
            winner = trick_winner(lead_card, foll_card, self.briscola)
            self.points[winner] += card_points(lead_card[0]) + card_points(foll_card[0])
            self.completed_tricks += 1
            self.current_trick = []
        # Terminal: stock exhausted and both hands empty → points decide.
        if deck is not None and not deck.stock:
            if not deck.hand_of("host") and not deck.hand_of("guest"):
                ph, pg = self.points["host"], self.points["guest"]
                if ph > pg:
                    return "host"
                if pg > ph:
                    return "guest"
                return None  # 60-60 draw
        return None

    def proto_display(self, msg: dict[str, Any], direction: str) -> str | None:
        action = msg.get("action")
        if action == "mp_play":
            rank = RANKS[int(msg["rank"])]
            suit = SUITS[int(msg["suit"])]
            return f"play {rank}{suit}"
        if action == "mp_pass":
            return "PASS"
        if action == "error":
            return f"illegal: {msg.get('reason')}"
        return None
