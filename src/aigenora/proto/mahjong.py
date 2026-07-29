from __future__ import annotations

from collections import Counter
from functools import lru_cache
from typing import Any, Iterable


NUMBERED_SUITS = ("characters", "dots", "bamboo")
HONOR_CODES = (
    "east",
    "south",
    "west",
    "north",
    "red",
    "green",
    "white",
)
ORPHAN_CODES = frozenset(
    {
        "1m",
        "9m",
        "1p",
        "9p",
        "1s",
        "9s",
        *HONOR_CODES,
    }
)


class MahjongRuleError(ValueError):
    """A Mahjong tile set or winning-shape request was invalid."""


def mahjong_tile_faces() -> list[dict[str, Any]]:
    """Return the stable 34-face catalog used with four shared-deck copies."""
    suffixes = {
        "characters": "m",
        "dots": "p",
        "bamboo": "s",
    }
    faces = [
        {
            "code": f"{rank}{suffixes[suit]}",
            "suit": suit,
            "rank": rank,
            "label": f"{rank} {suit}",
            "kind": "numbered",
        }
        for suit in NUMBERED_SUITS
        for rank in range(1, 10)
    ]
    honor_suits = {
        "east": "wind",
        "south": "wind",
        "west": "wind",
        "north": "wind",
        "red": "dragon",
        "green": "dragon",
        "white": "dragon",
    }
    faces.extend(
        {
            "code": code,
            "suit": honor_suits[code],
            "rank": 0,
            "label": code.title(),
            "kind": "honor",
        }
        for code in HONOR_CODES
    )
    return faces


def mahjong_win_kind(
    concealed_tiles: Iterable[dict[str, Any]],
    *,
    meld_count: int = 0,
) -> str | None:
    """Return standard, seven_pairs, thirteen_orphans, or null."""
    if (
        not isinstance(meld_count, int)
        or isinstance(meld_count, bool)
        or meld_count < 0
        or meld_count > 4
    ):
        raise MahjongRuleError("meld_count must be between zero and four")
    codes = [_tile_code(tile) for tile in concealed_tiles]
    expected = 3 * (4 - meld_count) + 2
    if len(codes) != expected:
        return None
    counts = Counter(codes)
    if any(count > 4 for count in counts.values()):
        raise MahjongRuleError("a Mahjong face cannot appear more than four times")
    if meld_count == 0 and _is_thirteen_orphans(counts):
        return "thirteen_orphans"
    if meld_count == 0 and _is_seven_pairs(counts):
        return "seven_pairs"
    if _is_standard_shape(counts, 4 - meld_count):
        return "standard"
    return None


def classical_core_patterns(
    all_tiles: Iterable[dict[str, Any]],
    *,
    win_kind: str,
    meld_kinds: Iterable[str] = (),
    self_draw: bool = False,
    concealed: bool = True,
) -> list[dict[str, Any]]:
    """Score the intentionally small, public Aigenora classical-core profile."""
    tiles = list(all_tiles)
    codes = [_tile_code(tile) for tile in tiles]
    if win_kind not in {"standard", "seven_pairs", "thirteen_orphans"}:
        raise MahjongRuleError("win_kind is invalid")
    melds = list(meld_kinds)
    if any(kind not in {"chow", "pung", "kong", "concealed_kong"} for kind in melds):
        raise MahjongRuleError("meld kind is invalid")
    patterns = [{"code": "winning_hand", "points": 1}]
    if self_draw:
        patterns.append({"code": "self_draw", "points": 1})
    if concealed:
        patterns.append({"code": "concealed_hand", "points": 1})
    if win_kind == "seven_pairs":
        patterns.append({"code": "seven_pairs", "points": 4})
    elif win_kind == "thirteen_orphans":
        patterns.append({"code": "thirteen_orphans", "points": 13})
    if win_kind == "standard" and len(melds) == 4 and all(
        kind in {"pung", "kong", "concealed_kong"} for kind in melds
    ):
        patterns.append({"code": "all_pungs", "points": 3})
    numbered_suits = {
        _code_suit(code)
        for code in codes
        if _code_suit(code) in {"m", "p", "s"}
    }
    has_honors = any(code in HONOR_CODES for code in codes)
    if len(numbered_suits) == 1:
        patterns.append(
            {
                "code": "mixed_one_suit" if has_honors else "pure_one_suit",
                "points": 3 if has_honors else 6,
            }
        )
    if codes and all(
        code not in HONOR_CODES and code[0] not in {"1", "9"}
        for code in codes
    ):
        patterns.append({"code": "all_simples", "points": 1})
    return patterns


def chow_sequences(
    hand_tiles: Iterable[dict[str, Any]],
    discarded_tile: dict[str, Any],
) -> list[list[str]]:
    """Return every legal two-tile code pair that can chow the discard."""
    target = _tile_code(discarded_tile)
    suit = _code_suit(target)
    if suit not in {"m", "p", "s"}:
        return []
    rank = int(target[0])
    available = Counter(_tile_code(tile) for tile in hand_tiles)
    results: list[list[str]] = []
    for start in range(max(1, rank - 2), min(rank, 7) + 1):
        sequence = [f"{value}{suit}" for value in range(start, start + 3)]
        needed = [code for code in sequence if code != target]
        if len(needed) == 2 and all(available[code] >= needed.count(code) for code in set(needed)):
            results.append(needed)
    return results


def _tile_code(tile: dict[str, Any]) -> str:
    if not isinstance(tile, dict):
        raise MahjongRuleError("every tile must be an object")
    code = tile.get("code")
    if not isinstance(code, str) or code not in _all_codes():
        raise MahjongRuleError("tile code is invalid")
    return code


@lru_cache(maxsize=1)
def _all_codes() -> frozenset[str]:
    return frozenset(str(face["code"]) for face in mahjong_tile_faces())


def _code_suit(code: str) -> str:
    return code[-1] if len(code) == 2 and code[0].isdigit() else "honor"


def _is_seven_pairs(counts: Counter[str]) -> bool:
    return sum(count // 2 for count in counts.values()) == 7 and all(
        count % 2 == 0 for count in counts.values()
    )


def _is_thirteen_orphans(counts: Counter[str]) -> bool:
    return set(counts) == set(ORPHAN_CODES) and any(
        counts[code] == 2 for code in ORPHAN_CODES
    )


def _is_standard_shape(counts: Counter[str], melds_needed: int) -> bool:
    for pair_code, count in sorted(counts.items()):
        if count < 2:
            continue
        remaining = counts.copy()
        remaining[pair_code] -= 2
        if remaining[pair_code] == 0:
            del remaining[pair_code]
        if _can_form_melds(tuple(sorted(remaining.items())), melds_needed):
            return True
    return False


@lru_cache(maxsize=8192)
def _can_form_melds(
    frozen_counts: tuple[tuple[str, int], ...],
    melds_needed: int,
) -> bool:
    counts = Counter(dict(frozen_counts))
    if melds_needed == 0:
        return not counts
    if sum(counts.values()) != melds_needed * 3:
        return False
    code = min(counts, key=_tile_sort_key)
    if counts[code] >= 3:
        next_counts = counts.copy()
        next_counts[code] -= 3
        if next_counts[code] == 0:
            del next_counts[code]
        if _can_form_melds(tuple(sorted(next_counts.items())), melds_needed - 1):
            return True
    suit = _code_suit(code)
    if suit in {"m", "p", "s"}:
        rank = int(code[0])
        if rank <= 7:
            sequence = (code, f"{rank + 1}{suit}", f"{rank + 2}{suit}")
            if all(counts[item] > 0 for item in sequence):
                next_counts = counts.copy()
                for item in sequence:
                    next_counts[item] -= 1
                    if next_counts[item] == 0:
                        del next_counts[item]
                if _can_form_melds(
                    tuple(sorted(next_counts.items())),
                    melds_needed - 1,
                ):
                    return True
    return False


def _tile_sort_key(code: str) -> tuple[int, int]:
    suit_order = {"m": 0, "p": 1, "s": 2}
    suit = _code_suit(code)
    if suit in suit_order:
        return (suit_order[suit], int(code[0]))
    return (3, HONOR_CODES.index(code))
