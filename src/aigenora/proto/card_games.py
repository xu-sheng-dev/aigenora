from __future__ import annotations

from typing import Any, Iterable


SUITS = ("clubs", "diamonds", "hearts", "spades")
SUIT_SYMBOLS = {
    "clubs": "C",
    "diamonds": "D",
    "hearts": "H",
    "spades": "S",
}
RANK_LABELS = {
    2: "2",
    3: "3",
    4: "4",
    5: "5",
    6: "6",
    7: "7",
    8: "8",
    9: "9",
    10: "10",
    11: "J",
    12: "Q",
    13: "K",
    14: "A",
    16: "SJ",
    17: "BJ",
}


class CardGameError(ValueError):
    """A reusable playing-card rule or card face was invalid."""


def standard_card_faces(*, include_jokers: bool = False) -> list[dict[str, Any]]:
    """Return a stable 52-card catalog, optionally followed by two jokers."""
    faces = [
        {
            "code": f"{RANK_LABELS[rank]}{SUIT_SYMBOLS[suit]}",
            "suit": suit,
            "rank": rank,
            "label": f"{RANK_LABELS[rank]}{SUIT_SYMBOLS[suit]}",
            "color": "red" if suit in {"diamonds", "hearts"} else "black",
        }
        for suit in SUITS
        for rank in range(2, 15)
    ]
    if include_jokers:
        faces.extend(
            [
                {
                    "code": "SJ",
                    "suit": "joker",
                    "rank": 16,
                    "label": "Small Joker",
                    "color": "black",
                },
                {
                    "code": "BJ",
                    "suit": "joker",
                    "rank": 17,
                    "label": "Big Joker",
                    "color": "red",
                },
            ]
        )
    return faces


def validate_follow_suit(
    hand_cards: Iterable[dict[str, Any]],
    played_card: dict[str, Any],
    led_suit: str,
) -> None:
    """Reject an off-suit play while the hand still contains the led suit."""
    if led_suit not in SUITS:
        raise CardGameError("led_suit must be a standard suit")
    played_suit = _card_suit(played_card)
    if played_suit == led_suit:
        return
    if any(_card_suit(card) == led_suit for card in hand_cards):
        raise CardGameError("the player must follow the led suit")


def winning_trick_index(
    cards: Iterable[dict[str, Any]],
    *,
    led_suit: str,
    trump_suit: str | None = None,
) -> int:
    """Return the zero-based winner of a standard single-card trick."""
    trick = list(cards)
    if not trick:
        raise CardGameError("a trick must contain at least one card")
    if led_suit not in SUITS:
        raise CardGameError("led_suit must be a standard suit")
    if trump_suit is not None and trump_suit not in SUITS:
        raise CardGameError("trump_suit must be a standard suit or null")

    def strength(card: dict[str, Any]) -> tuple[int, int]:
        suit = _card_suit(card)
        rank = _card_rank(card)
        if trump_suit is not None and suit == trump_suit:
            return (2, rank)
        if suit == led_suit:
            return (1, rank)
        return (0, rank)

    return max(range(len(trick)), key=lambda index: strength(trick[index]))


def card_by_id(deck: dict[str, Any], card_id: str) -> dict[str, Any]:
    """Return one authoritative catalog card without exposing another hand."""
    catalog = deck.get("catalog")
    if not isinstance(catalog, dict) or card_id not in catalog:
        raise CardGameError("unknown card_id")
    card = catalog[card_id]
    if not isinstance(card, dict):
        raise CardGameError("card catalog entry is invalid")
    return card


def cards_by_id(deck: dict[str, Any], card_ids: Iterable[str]) -> list[dict[str, Any]]:
    return [card_by_id(deck, card_id) for card_id in card_ids]


def _card_suit(card: dict[str, Any]) -> str:
    suit = card.get("suit")
    if suit not in (*SUITS, "joker"):
        raise CardGameError("card suit is invalid")
    return str(suit)


def _card_rank(card: dict[str, Any]) -> int:
    rank = card.get("rank")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank not in RANK_LABELS:
        raise CardGameError("card rank is invalid")
    return rank
