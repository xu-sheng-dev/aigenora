from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import Any, Iterable

from .card_games import SUITS


HAND_CATEGORY_NAMES = {
    8: "straight_flush",
    7: "four_of_a_kind",
    6: "full_house",
    5: "flush",
    4: "straight",
    3: "three_of_a_kind",
    2: "two_pair",
    1: "one_pair",
    0: "high_card",
}


class PokerRuleError(ValueError):
    """A poker hand or contribution set was invalid."""


def evaluate_five(cards: Iterable[dict[str, Any]]) -> tuple[int, ...]:
    """Return a lexicographically comparable rank for exactly five cards."""
    hand = list(cards)
    if len(hand) != 5:
        raise PokerRuleError("a five-card hand must contain exactly five cards")
    ranks, suits = _validated_ranks_and_suits(hand)
    counts = Counter(ranks)
    groups = sorted(
        ((count, rank) for rank, count in counts.items()),
        reverse=True,
    )
    flush = len(set(suits)) == 1
    straight_high = _straight_high(ranks)

    if flush and straight_high:
        return (8, straight_high)
    if groups[0][0] == 4:
        four_rank = groups[0][1]
        kicker = max(rank for rank in ranks if rank != four_rank)
        return (7, four_rank, kicker)
    if sorted(counts.values()) == [2, 3]:
        trip = max(rank for rank, count in counts.items() if count == 3)
        pair = max(rank for rank, count in counts.items() if count == 2)
        return (6, trip, pair)
    if flush:
        return (5, *sorted(ranks, reverse=True))
    if straight_high:
        return (4, straight_high)
    if groups[0][0] == 3:
        trip = groups[0][1]
        kickers = sorted(
            (rank for rank in ranks if rank != trip),
            reverse=True,
        )
        return (3, trip, *kickers)
    pair_ranks = sorted(
        (rank for rank, count in counts.items() if count == 2),
        reverse=True,
    )
    if len(pair_ranks) == 2:
        kicker = max(rank for rank in ranks if rank not in pair_ranks)
        return (2, pair_ranks[0], pair_ranks[1], kicker)
    if len(pair_ranks) == 1:
        pair = pair_ranks[0]
        kickers = sorted(
            (rank for rank in ranks if rank != pair),
            reverse=True,
        )
        return (1, pair, *kickers)
    return (0, *sorted(ranks, reverse=True))


def best_poker_hand(cards: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate the best five-card selection from five, six, or seven cards."""
    available = list(cards)
    if len(available) < 5 or len(available) > 7:
        raise PokerRuleError("poker evaluation requires five to seven cards")
    _validated_ranks_and_suits(available)
    best_cards: tuple[dict[str, Any], ...] | None = None
    best_rank: tuple[int, ...] | None = None
    for candidate in combinations(available, 5):
        rank = evaluate_five(candidate)
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_cards = candidate
    assert best_rank is not None and best_cards is not None
    return {
        "category": HAND_CATEGORY_NAMES[best_rank[0]],
        "rank": list(best_rank),
        "card_ids": [
            str(card.get("card_id", card.get("code", "")))
            for card in best_cards
        ],
    }


def build_side_pots(
    contributions: dict[str, int],
    folded: Iterable[str],
) -> list[dict[str, Any]]:
    """Build deterministic main and side pots from total hand contributions."""
    if not isinstance(contributions, dict) or not contributions:
        raise PokerRuleError("contributions must be a non-empty object")
    folded_set = set(folded)
    if any(public_key not in contributions for public_key in folded_set):
        raise PokerRuleError("folded player is missing from contributions")
    for public_key, amount in contributions.items():
        if not isinstance(public_key, str) or not public_key:
            raise PokerRuleError("contribution owner is invalid")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise PokerRuleError("contributions must be non-negative integers")
    levels = sorted({amount for amount in contributions.values() if amount > 0})
    previous = 0
    pots: list[dict[str, Any]] = []
    for level in levels:
        participants = sorted(
            public_key
            for public_key, amount in contributions.items()
            if amount >= level
        )
        amount = (level - previous) * len(participants)
        eligible = [
            public_key
            for public_key in participants
            if public_key not in folded_set
        ]
        if amount > 0:
            if not eligible:
                raise PokerRuleError("a side pot has no eligible winner")
            pots.append(
                {
                    "amount": amount,
                    "cap": level,
                    "contributors": participants,
                    "eligible": eligible,
                }
            )
        previous = level
    return pots


def normalize_uncalled_contributions(
    contributions: dict[str, int],
) -> dict[str, dict[str, int]]:
    """Return a copied ledger with a unique unmatched excess refunded."""
    if not isinstance(contributions, dict) or not contributions:
        raise PokerRuleError("contributions must be a non-empty object")
    normalized: dict[str, int] = {}
    for public_key, amount in contributions.items():
        if not isinstance(public_key, str) or not public_key:
            raise PokerRuleError("contribution owner is invalid")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise PokerRuleError("contributions must be non-negative integers")
        normalized[public_key] = amount
    ranked = sorted(
        normalized.items(),
        key=lambda item: (-item[1], item[0]),
    )
    refunds = {public_key: 0 for public_key in normalized}
    if len(ranked) >= 2 and ranked[0][1] > ranked[1][1]:
        public_key, highest = ranked[0]
        refund = highest - ranked[1][1]
        normalized[public_key] -= refund
        refunds[public_key] = refund
    return {
        "contributions": normalized,
        "refunds": refunds,
    }


def _validated_ranks_and_suits(
    cards: list[dict[str, Any]],
) -> tuple[list[int], list[str]]:
    ranks: list[int] = []
    suits: list[str] = []
    seen: set[tuple[str, int]] = set()
    for card in cards:
        if not isinstance(card, dict):
            raise PokerRuleError("every card must be an object")
        rank = card.get("rank")
        suit = card.get("suit")
        if (
            not isinstance(rank, int)
            or isinstance(rank, bool)
            or rank < 2
            or rank > 14
            or suit not in SUITS
        ):
            raise PokerRuleError("poker card rank or suit is invalid")
        face = (str(suit), rank)
        if face in seen:
            raise PokerRuleError("a standard poker hand cannot repeat a card face")
        seen.add(face)
        ranks.append(rank)
        suits.append(str(suit))
    return ranks, suits


def _straight_high(ranks: list[int]) -> int | None:
    unique = sorted(set(ranks), reverse=True)
    if len(unique) != 5:
        return None
    if unique == [14, 5, 4, 3, 2]:
        return 5
    if unique[0] - unique[-1] == 4:
        return unique[0]
    return None
