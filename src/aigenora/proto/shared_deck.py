from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable


class SharedDeckError(ValueError):
    """The shared draw pile or a private hand violated deck conservation."""


def create_shared_deck(
    card_faces: Iterable[dict[str, Any]],
    members: list[dict[str, Any]],
    *,
    hand_size: int,
    seed: str,
    copies: int = 1,
) -> dict[str, Any]:
    """Create a deterministic shared draw pile with per-member private hands.

    The returned structure is JSON-serializable. Card identities are unique even
    when several physical copies share the same face. Only Host-authoritative
    hooks should mutate the structure; Member views must go through
    ``private_deck_view``.
    """
    if not isinstance(seed, str) or not seed or len(seed.encode("utf-8")) > 256:
        raise SharedDeckError("seed must be 1-256 UTF-8 bytes")
    if not isinstance(hand_size, int) or isinstance(hand_size, bool) or hand_size < 0:
        raise SharedDeckError("hand_size must be a non-negative integer")
    if not isinstance(copies, int) or isinstance(copies, bool) or copies < 1 or copies > 8:
        raise SharedDeckError("copies must be between 1 and 8")
    faces = [copy.deepcopy(face) for face in card_faces]
    if not faces:
        raise SharedDeckError("card_faces must not be empty")
    for face in faces:
        if not isinstance(face, dict):
            raise SharedDeckError("every card face must be an object")
        _canonical_json(face)

    catalog: dict[str, dict[str, Any]] = {}
    card_ids: list[str] = []
    for copy_index in range(copies):
        for face_index, face in enumerate(faces):
            identity = hashlib.sha256(
                _canonical_json(
                    {
                        "seed": seed,
                        "copy": copy_index,
                        "face_index": face_index,
                        "face": face,
                    }
                )
            ).hexdigest()[:24]
            if identity in catalog:
                raise SharedDeckError("card identity collision")
            catalog[identity] = {
                **copy.deepcopy(face),
                "card_id": identity,
                "copy": copy_index,
            }
            card_ids.append(identity)
    draw_pile = deterministic_shuffle(card_ids, seed=seed)
    hands = {
        str(member["public_key"]): []
        for member in sorted(members, key=lambda item: int(item["seat"]))
        if member.get("status") == "active"
    }
    if hand_size * len(hands) > len(draw_pile):
        raise SharedDeckError("initial deal exceeds the shared deck")
    for _ in range(hand_size):
        for public_key in hands:
            hands[public_key].append(draw_pile.pop())
    state = {
        "catalog": catalog,
        "draw_pile": draw_pile,
        "hands": hands,
        "discard_pile": [],
        "zones": {},
        "total_cards": len(catalog),
    }
    validate_conservation(state)
    return state


def deterministic_shuffle(values: Iterable[str], *, seed: str) -> list[str]:
    """Fisher-Yates shuffle driven by SHA-256 instead of runtime RNG details."""
    shuffled = list(values)
    counter = 0
    for index in range(len(shuffled) - 1, 0, -1):
        digest = hashlib.sha256(
            f"aigenora-shared-deck-v1:{seed}:{counter}".encode("utf-8")
        ).digest()
        target = int.from_bytes(digest, "big") % (index + 1)
        shuffled[index], shuffled[target] = shuffled[target], shuffled[index]
        counter += 1
    return shuffled


def draw_cards(
    deck: dict[str, Any], public_key: str, count: int = 1
) -> list[dict[str, Any]]:
    if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > 32:
        raise SharedDeckError("draw count must be between 1 and 32")
    hands = deck.get("hands")
    draw_pile = deck.get("draw_pile")
    catalog = deck.get("catalog")
    if not isinstance(hands, dict) or public_key not in hands:
        raise SharedDeckError("unknown hand owner")
    if not isinstance(draw_pile, list) or len(draw_pile) < count:
        raise SharedDeckError("shared draw pile does not have enough cards")
    if not isinstance(catalog, dict):
        raise SharedDeckError("deck catalog is invalid")
    drawn: list[dict[str, Any]] = []
    for _ in range(count):
        card_id = draw_pile.pop()
        hands[public_key].append(card_id)
        drawn.append(copy.deepcopy(catalog[card_id]))
    validate_conservation(deck)
    return drawn


def take_from_hand(
    deck: dict[str, Any], public_key: str, card_ids: list[str]
) -> list[dict[str, Any]]:
    if not card_ids or len(card_ids) > 64 or len(set(card_ids)) != len(card_ids):
        raise SharedDeckError("card_ids must be a non-empty unique array")
    hands = deck.get("hands")
    catalog = deck.get("catalog")
    if not isinstance(hands, dict) or public_key not in hands:
        raise SharedDeckError("unknown hand owner")
    hand = hands[public_key]
    if not isinstance(hand, list) or any(card_id not in hand for card_id in card_ids):
        raise SharedDeckError("card is not in the player's hand")
    result = []
    for card_id in card_ids:
        hand.remove(card_id)
        result.append(copy.deepcopy(catalog[card_id]))
    return result


def discard_cards(deck: dict[str, Any], card_ids: list[str]) -> None:
    discard = deck.get("discard_pile")
    if not isinstance(discard, list):
        raise SharedDeckError("discard_pile is invalid")
    discard.extend(card_ids)
    validate_conservation(deck)


def put_in_zone(
    deck: dict[str, Any], zone: str, owner: str, card_ids: list[str]
) -> None:
    owner_cards = _zone_cards(deck, zone, owner, create=True)
    owner_cards.extend(card_ids)
    validate_conservation(deck)


def move_hand_to_zone(
    deck: dict[str, Any],
    public_key: str,
    zone: str,
    owner: str,
    card_ids: list[str],
) -> list[dict[str, Any]]:
    """Atomically move unique cards from a private hand into a named zone."""
    _validate_card_ids(card_ids)
    hands = deck.get("hands")
    catalog = deck.get("catalog")
    if not isinstance(hands, dict) or public_key not in hands:
        raise SharedDeckError("unknown hand owner")
    if not isinstance(catalog, dict):
        raise SharedDeckError("deck catalog is invalid")
    hand = hands[public_key]
    if not isinstance(hand, list) or any(card_id not in hand for card_id in card_ids):
        raise SharedDeckError("card is not in the player's hand")
    owner_cards = _zone_cards(deck, zone, owner, create=True)
    for card_id in card_ids:
        hand.remove(card_id)
        owner_cards.append(card_id)
    validate_conservation(deck)
    return [copy.deepcopy(catalog[card_id]) for card_id in card_ids]


def move_draw_to_zone(
    deck: dict[str, Any],
    zone: str,
    owner: str,
    count: int,
) -> list[dict[str, Any]]:
    """Atomically move cards from the shared draw pile into a named zone."""
    if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > 64:
        raise SharedDeckError("zone draw count must be between 1 and 64")
    draw_pile = deck.get("draw_pile")
    catalog = deck.get("catalog")
    if not isinstance(draw_pile, list) or len(draw_pile) < count:
        raise SharedDeckError("shared draw pile does not have enough cards")
    if not isinstance(catalog, dict):
        raise SharedDeckError("deck catalog is invalid")
    owner_cards = _zone_cards(deck, zone, owner, create=True)
    card_ids = [draw_pile.pop() for _ in range(count)]
    owner_cards.extend(card_ids)
    validate_conservation(deck)
    return [copy.deepcopy(catalog[card_id]) for card_id in card_ids]


def move_discard_to_zone(
    deck: dict[str, Any],
    zone: str,
    owner: str,
    card_id: str,
) -> dict[str, Any]:
    """Atomically claim one public discard into a named zone."""
    discard = deck.get("discard_pile")
    catalog = deck.get("catalog")
    if not isinstance(discard, list) or card_id not in discard:
        raise SharedDeckError("discard card does not exist")
    if not isinstance(catalog, dict) or card_id not in catalog:
        raise SharedDeckError("deck catalog is invalid")
    owner_cards = _zone_cards(deck, zone, owner, create=True)
    discard.remove(card_id)
    owner_cards.append(card_id)
    validate_conservation(deck)
    return copy.deepcopy(catalog[card_id])


def move_zone_to_discard(
    deck: dict[str, Any], zone: str, owner: str, card_id: str
) -> None:
    owner_cards = _zone_cards(deck, zone, owner, create=False)
    if card_id not in owner_cards:
        raise SharedDeckError("zone card does not exist")
    owner_cards.remove(card_id)
    deck["discard_pile"].append(card_id)
    validate_conservation(deck)


def private_deck_view(
    deck: dict[str, Any],
    viewer_public_key: str,
    *,
    hidden_zones: Iterable[str] = (),
) -> dict[str, Any]:
    validate_conservation(deck)
    catalog = deck["catalog"]
    hands = deck["hands"]
    if viewer_public_key not in hands:
        raise SharedDeckError("viewer does not own a hand")
    hidden = set(hidden_zones)
    if any(not isinstance(zone, str) or not zone for zone in hidden):
        raise SharedDeckError("hidden_zones must contain non-empty strings")
    return {
        "draw_count": len(deck["draw_pile"]),
        "discard": [copy.deepcopy(catalog[card_id]) for card_id in deck["discard_pile"]],
        "my_hand": [copy.deepcopy(catalog[card_id]) for card_id in hands[viewer_public_key]],
        "hand_counts": {
            public_key: len(card_ids)
            for public_key, card_ids in hands.items()
        },
        "zones": {
            zone: {
                owner: [copy.deepcopy(catalog[card_id]) for card_id in card_ids]
                for owner, card_ids in owners.items()
            }
            for zone, owners in deck["zones"].items()
            if zone not in hidden
        },
        "zone_counts": {
            zone: {
                owner: len(card_ids)
                for owner, card_ids in owners.items()
            }
            for zone, owners in deck["zones"].items()
        },
        "total_cards": deck["total_cards"],
    }


def validate_conservation(deck: dict[str, Any]) -> None:
    catalog = deck.get("catalog")
    if not isinstance(catalog, dict) or not catalog:
        raise SharedDeckError("catalog must be a non-empty object")
    expected = set(catalog)
    observed: list[str] = []
    for key in ("draw_pile", "discard_pile"):
        value = deck.get(key)
        if not isinstance(value, list):
            raise SharedDeckError(f"{key} must be an array")
        observed.extend(value)
    hands = deck.get("hands")
    if not isinstance(hands, dict):
        raise SharedDeckError("hands must be an object")
    for cards in hands.values():
        if not isinstance(cards, list):
            raise SharedDeckError("hand must be an array")
        observed.extend(cards)
    zones = deck.get("zones")
    if not isinstance(zones, dict):
        raise SharedDeckError("zones must be an object")
    for owners in zones.values():
        if not isinstance(owners, dict):
            raise SharedDeckError("zone owners must be an object")
        for cards in owners.values():
            if not isinstance(cards, list):
                raise SharedDeckError("zone cards must be an array")
            observed.extend(cards)
    if len(observed) != len(set(observed)):
        raise SharedDeckError("a card appears in more than one deck zone")
    if set(observed) != expected:
        missing = sorted(expected - set(observed))
        unknown = sorted(set(observed) - expected)
        raise SharedDeckError(
            f"deck conservation failed (missing={missing[:3]}, unknown={unknown[:3]})"
        )
    if deck.get("total_cards") != len(expected):
        raise SharedDeckError("total_cards does not match catalog size")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SharedDeckError(f"card face is not canonical JSON: {exc}") from exc


def _validate_card_ids(card_ids: list[str]) -> None:
    if (
        not isinstance(card_ids, list)
        or not card_ids
        or len(card_ids) > 64
        or any(not isinstance(card_id, str) or not card_id for card_id in card_ids)
        or len(set(card_ids)) != len(card_ids)
    ):
        raise SharedDeckError("card_ids must be a non-empty unique array")


def _zone_cards(
    deck: dict[str, Any],
    zone: str,
    owner: str,
    *,
    create: bool,
) -> list[str]:
    if not isinstance(zone, str) or not zone or len(zone) > 64:
        raise SharedDeckError("zone is invalid")
    if not isinstance(owner, str) or not owner or len(owner) > 256:
        raise SharedDeckError("zone owner is invalid")
    zones = deck.get("zones")
    if not isinstance(zones, dict):
        raise SharedDeckError("zones is invalid")
    if create:
        zone_map = zones.setdefault(zone, {})
        if not isinstance(zone_map, dict):
            raise SharedDeckError("zone map is invalid")
        owner_cards = zone_map.setdefault(owner, [])
    else:
        try:
            owner_cards = zones[zone][owner]
        except (KeyError, TypeError):
            raise SharedDeckError("zone card does not exist")
    if not isinstance(owner_cards, list):
        raise SharedDeckError("owner zone is invalid")
    return owner_cards
