from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from .card_games import SUITS


class TractorRuleError(ValueError):
    """A Tractor/Upgrade card group or follow obligation was invalid."""


def effective_suit(
    card: dict[str, Any],
    *,
    level_rank: int,
    trump_suit: str | None,
) -> str:
    suit, rank = _face(card)
    _validate_context(level_rank, trump_suit)
    if suit == "joker" or rank == level_rank or suit == trump_suit:
        return "trump"
    return suit


def tractor_card_strength(
    card: dict[str, Any],
    *,
    level_rank: int,
    trump_suit: str | None,
) -> tuple[int, int]:
    """Return an effective-suit class and within-class strength."""
    suit, rank = _face(card)
    category = effective_suit(
        card,
        level_rank=level_rank,
        trump_suit=trump_suit,
    )
    if category != "trump":
        return (0, rank)
    if rank == 17:
        return (1, 1000)
    if rank == 16:
        return (1, 990)
    if rank == level_rank and trump_suit is not None and suit == trump_suit:
        return (1, 980)
    if rank == level_rank:
        return (1, 970)
    return (1, rank)


def classify_tractor_play(
    cards: Iterable[dict[str, Any]],
    *,
    level_rank: int,
    trump_suit: str | None,
) -> dict[str, Any]:
    """Classify a same-category play as single, pair, tractor, or throw."""
    selected = list(cards)
    if not selected:
        raise TractorRuleError("a play must contain at least one card")
    _validate_context(level_rank, trump_suit)
    categories = {
        effective_suit(
            card,
            level_rank=level_rank,
            trump_suit=trump_suit,
        )
        for card in selected
    }
    if len(categories) != 1:
        raise TractorRuleError("all cards in a play must share an effective suit")
    category = next(iter(categories))
    signatures = Counter(_face(card) for card in selected)
    result: dict[str, Any] = {
        "kind": "throw",
        "count": len(selected),
        "suit": category,
        "pair_count": sum(count // 2 for count in signatures.values()),
        "signatures": [
            {"suit": suit, "rank": rank, "count": count}
            for (suit, rank), count in sorted(signatures.items())
        ],
    }
    if len(selected) == 1:
        result["kind"] = "single"
        return result
    if len(selected) == 2 and len(signatures) == 1:
        result["kind"] = "pair"
        return result
    if (
        len(selected) >= 4
        and len(selected) % 2 == 0
        and all(count == 2 for count in signatures.values())
    ):
        positions = sorted(
            _pair_position(
                face,
                level_rank=level_rank,
                trump_suit=trump_suit,
                category=category,
            )
            for face in signatures
        )
        if all(right == left + 1 for left, right in zip(positions, positions[1:])):
            result["kind"] = "tractor"
            result["tractor_length"] = len(positions)
    return result


def validate_tractor_follow(
    hand_cards: Iterable[dict[str, Any]],
    selected_cards: Iterable[dict[str, Any]],
    lead: dict[str, Any],
    *,
    level_rank: int,
    trump_suit: str | None,
) -> None:
    """Enforce card count, effective suit, pairs, and available tractors."""
    hand = list(hand_cards)
    selected = list(selected_cards)
    count = lead.get("count")
    category = lead.get("suit")
    kind = lead.get("kind")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
        or category not in (*SUITS, "trump")
        or kind not in {"single", "pair", "tractor", "throw"}
    ):
        raise TractorRuleError("lead classification is invalid")
    if len(selected) != count:
        raise TractorRuleError("a follower must play the same number of cards")
    category_hand = [
        card
        for card in hand
        if effective_suit(
            card,
            level_rank=level_rank,
            trump_suit=trump_suit,
        )
        == category
    ]
    category_selected = [
        card
        for card in selected
        if effective_suit(
            card,
            level_rank=level_rank,
            trump_suit=trump_suit,
        )
        == category
    ]
    required_suit_cards = min(count, len(category_hand))
    if len(category_selected) != required_suit_cards:
        raise TractorRuleError("the follower must exhaust the led effective suit")
    if len(category_hand) < count:
        return

    selected_shape = classify_tractor_play(
        selected,
        level_rank=level_rank,
        trump_suit=trump_suit,
    )
    available_pairs = _pair_count(category_hand)
    required_pairs = min(int(lead.get("pair_count", 0)), available_pairs)
    if selected_shape["pair_count"] < required_pairs:
        raise TractorRuleError("the follower must preserve available pairs")
    if kind == "pair" and available_pairs > 0 and selected_shape["kind"] != "pair":
        raise TractorRuleError("an available pair must follow a pair")
    if kind == "tractor":
        tractor_length = int(lead.get("tractor_length", 0))
        if tractor_length < 2:
            raise TractorRuleError("tractor lead length is invalid")
        if _has_tractor(
            category_hand,
            tractor_length,
            category=category,
            level_rank=level_rank,
            trump_suit=trump_suit,
        ) and not (
            selected_shape["kind"] == "tractor"
            and selected_shape.get("tractor_length") == tractor_length
        ):
            raise TractorRuleError("an available tractor must follow a tractor")


def winning_tractor_play_index(
    plays: Iterable[Iterable[dict[str, Any]]],
    *,
    level_rank: int,
    trump_suit: str | None,
) -> int:
    """Return the winner index for compatible single, pair, or tractor plays."""
    groups = [list(play) for play in plays]
    if not groups:
        raise TractorRuleError("a trick must contain at least one play")
    lead = classify_tractor_play(
        groups[0],
        level_rank=level_rank,
        trump_suit=trump_suit,
    )
    if lead["kind"] == "throw":
        raise TractorRuleError("throw resolution must be handled by the protocol profile")

    winner = 0
    winner_shape = lead
    winner_strength = _play_strength(
        groups[0],
        winner_shape,
        level_rank=level_rank,
        trump_suit=trump_suit,
    )
    for index, group in enumerate(groups[1:], start=1):
        shape = classify_tractor_play(
            group,
            level_rank=level_rank,
            trump_suit=trump_suit,
        )
        if shape["count"] != lead["count"] or shape["kind"] != lead["kind"]:
            continue
        if shape["suit"] not in {lead["suit"], "trump"}:
            continue
        strength = _play_strength(
            group,
            shape,
            level_rank=level_rank,
            trump_suit=trump_suit,
        )
        if (
            shape["suit"] == "trump" and winner_shape["suit"] != "trump"
        ) or (
            shape["suit"] == winner_shape["suit"] and strength > winner_strength
        ):
            winner = index
            winner_shape = shape
            winner_strength = strength
    return winner


def _play_strength(
    cards: list[dict[str, Any]],
    shape: dict[str, Any],
    *,
    level_rank: int,
    trump_suit: str | None,
) -> tuple[int, int]:
    if shape["kind"] == "single":
        return tractor_card_strength(
            cards[0],
            level_rank=level_rank,
            trump_suit=trump_suit,
        )
    faces = Counter(_face(card) for card in cards)
    strongest_face = max(
        faces,
        key=lambda face: _pair_position(
            face,
            level_rank=level_rank,
            trump_suit=trump_suit,
            category=str(shape["suit"]),
        ),
    )
    return (
        1 if shape["suit"] == "trump" else 0,
        _pair_position(
            strongest_face,
            level_rank=level_rank,
            trump_suit=trump_suit,
            category=str(shape["suit"]),
        ),
    )


def _pair_count(cards: list[dict[str, Any]]) -> int:
    return sum(count // 2 for count in Counter(_face(card) for card in cards).values())


def _has_tractor(
    cards: list[dict[str, Any]],
    length: int,
    *,
    category: str,
    level_rank: int,
    trump_suit: str | None,
) -> bool:
    positions = sorted(
        _pair_position(
            face,
            level_rank=level_rank,
            trump_suit=trump_suit,
            category=category,
        )
        for face, count in Counter(_face(card) for card in cards).items()
        if count >= 2
    )
    run = 1
    for left, right in zip(positions, positions[1:]):
        run = run + 1 if right == left + 1 else 1
        if run >= length:
            return True
    return length <= 1 and bool(positions)


def _pair_position(
    face: tuple[str, int],
    *,
    level_rank: int,
    trump_suit: str | None,
    category: str,
) -> int:
    suit, rank = face
    if category != "trump":
        if suit != category or rank == level_rank:
            raise TractorRuleError("card face is outside the effective suit")
        ordered = [value for value in range(2, 15) if value != level_rank]
        return ordered.index(rank)
    regular_ranks = (
        [value for value in range(2, 15) if value != level_rank]
        if trump_suit is not None
        else []
    )
    if trump_suit is not None and suit == trump_suit and rank in regular_ranks:
        return regular_ranks.index(rank)
    off_level_position = len(regular_ranks)
    if rank == level_rank and suit != trump_suit:
        return off_level_position
    if rank == level_rank and suit == trump_suit:
        return off_level_position + 1
    joker_offset = 2 if trump_suit is not None else 1
    if face == ("joker", 16):
        return off_level_position + joker_offset
    if face == ("joker", 17):
        return off_level_position + joker_offset + 1
    raise TractorRuleError("card face is outside the trump sequence")


def _face(card: dict[str, Any]) -> tuple[str, int]:
    if not isinstance(card, dict):
        raise TractorRuleError("every card must be an object")
    suit = card.get("suit")
    rank = card.get("rank")
    if (
        suit not in (*SUITS, "joker")
        or not isinstance(rank, int)
        or isinstance(rank, bool)
        or rank not in {*range(2, 15), 16, 17}
        or (suit == "joker") != (rank in {16, 17})
    ):
        raise TractorRuleError("card face is invalid")
    return str(suit), rank


def _validate_context(level_rank: int, trump_suit: str | None) -> None:
    if (
        not isinstance(level_rank, int)
        or isinstance(level_rank, bool)
        or level_rank < 2
        or level_rank > 14
    ):
        raise TractorRuleError("level_rank must be between 2 and 14")
    if trump_suit is not None and trump_suit not in SUITS:
        raise TractorRuleError("trump_suit must be a standard suit or null")
