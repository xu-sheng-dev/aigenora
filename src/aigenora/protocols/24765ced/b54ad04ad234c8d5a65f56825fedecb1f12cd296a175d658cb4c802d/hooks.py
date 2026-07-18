"""Built-in Tank Battle reference game for ``authoritative_realtime``.

The Host owns simulation time and publishes every world frame.  Both local Agents
translate persistent macro orders into micro commands for all of their tanks; Guest
commands target future ticks and are applied only after the real-time engine accepts
them.  The simulation uses integer fixed-point coordinates, simultaneous movement and
fully JSON-serializable state so the resulting journal is suitable for later replay.
"""
from __future__ import annotations

import copy
import heapq
import json
import re
from collections import deque
from hashlib import sha256
from pathlib import Path
from typing import Any

from aigenora.proto.hooks import ProtocolHooks


SCALE = 1000
TANK_HALF = 360
DIRECTIONS = ("up", "down", "left", "right", "none")
CARDINAL = DIRECTIONS[:-1]
ROTATE_180 = {"up": "down", "down": "up", "left": "right", "right": "left"}
VEC = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
    "none": (0, 0),
}

TILE_STEEL = "#"
TILE_BRICK = "B"
TILE_WATER = "W"
TILE_BUSH = "S"
TILE_PICKUP = "P"
TILE_FLOOR = "."
ALLOWED_TILES = frozenset({TILE_STEEL, TILE_BRICK, TILE_WATER, TILE_BUSH, TILE_PICKUP, TILE_FLOOR})
BLOCKS_MOVE = frozenset({TILE_STEEL, TILE_BRICK, TILE_WATER})

NAV_DIRECTION_ORDER = ("up", "left", "right", "down")
NAV_ROUTE_TTL = 48
NAV_STUCK_REPLAN_TICKS = 4
NAV_DEFAULT_ORDER_TICKS = 600
NAV_DEFAULT_PHASE_TICKS = 180

DEFAULT_BALANCE = {
    "hp": 4,
    "speed": 180,          # fixed-point units/tick; 1000 == one map cell
    "fire_cooldown": 7,
    "bullet_speed": 560,
}
# Battlefield dimensions. The map is 180° point-symmetric (grid[r][c] ==
# grid[N-1-r][N-1-c]) so neither side gets a positional advantage.  Rather than
# hand-author a 31x31 ASCII grid (which is error-prone — a single misplaced tile
# silently breaks fairness), ``_build_default_map`` lays out the upper half and
# mirrors it, and ``DEFAULT_MAP`` is derived from that. Edit the coordinate
# lists below to change the layout; symmetry is guaranteed by construction and
# asserted at import time.
DEFAULT_MAP_N = 31
DEFAULT_MAP_BRIDGE_COLS: tuple[int, ...] = (11, 15, 19)
# The river occupies only the central segment of the middle row (RIVER_START..RIVER_END),
# leaving open land corridors on both flanks. A fully-spanning river lets the two sides
# stall indefinitely on opposite banks because the reference Agent's lane-alignment
# pathfinding struggles to funnel every tank through a few narrow bridges; the open
# flanks give tanks a reliable overland route so engagements still happen.
DEFAULT_MAP_RIVER_START = 8
DEFAULT_MAP_RIVER_END = 23  # exclusive


def _build_default_map(n: int = DEFAULT_MAP_N) -> str:
    """Programmatically build the 180° point-symmetric default battlefield.

    Layout (upper half only; the lower half is the 180° mirror):
      * a central river across the *middle segment* of the middle row, broken by
        bridge gaps, with open land corridors on both flanks;
      * steel chokepoint pillars flanking the central bridge;
      * brick bunkers, steel strongpoints, bush cover and pickups in each quadrant.
    """
    wall, floor, brick, steel, water, bush, pickup = "#", ".", "B", "S", "W", "S", "P"
    mid = n // 2
    grid = [[floor] * n for _ in range(n)]
    for i in range(n):
        grid[0][i] = wall
        grid[n - 1][i] = wall
        grid[i][0] = wall
        grid[i][n - 1] = wall

    def place(r: int, c: int, ch: str) -> None:
        """Set a tile and its 180° mirror, keeping the map point-symmetric."""
        grid[r][c] = ch
        grid[n - 1 - r][n - 1 - c] = ch

    # Central river across the middle segment only (open flanks remain passable).
    for c in range(DEFAULT_MAP_RIVER_START, DEFAULT_MAP_RIVER_END):
        if c not in DEFAULT_MAP_BRIDGE_COLS:
            grid[mid][c] = water  # mid row maps to itself under 180° rotation

    # Steel pillars flanking the central bridge (chokepoint).
    for c in (mid - 1, mid + 1):
        place(mid - 1, c, steel)

    # Brick bunkers (upper half; mirrors fill the lower half).
    for r, c in [
        (3, 3), (3, 4), (4, 3),
        (3, 11), (3, 12), (4, 12),
        (3, 18), (3, 19), (4, 19),
        (3, 26), (3, 27), (4, 27),
        (8, 7), (8, 8), (9, 7),
        (8, 22), (8, 23), (9, 23),
    ]:
        place(r, c, brick)

    # Steel strongpoints (hard objectives).
    for r, c in [(6, mid), (10, 6), (10, n - 1 - 6)]:
        place(r, c, steel)

    # Bush concealment patches.
    for r, c in [
        (6, 4), (6, 5), (5, 4),
        (11, 10), (11, 11),
    ]:
        place(r, c, bush)

    # Pickups (symmetric pairs across the centre).
    for r, c in [(2, mid), (mid - 2, mid), (5, 9)]:
        place(r, c, pickup)

    # Defence-in-depth: assert the construction is truly point-symmetric so a
    # future edit that breaks fairness fails loudly at import time.
    assert all(
        grid[r][c] == grid[n - 1 - r][n - 1 - c]
        for r in range(n)
        for c in range(n)
    ), "DEFAULT_MAP lost 180° point symmetry"
    return "\n".join("".join(row) for row in grid)


DEFAULT_MAP = _build_default_map()


def parse_map(text: str) -> list[list[str]]:
    rows = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    if len(rows) < 5 or len(rows) > 128:
        raise ValueError("map must contain between 5 and 128 non-empty rows")
    width = len(rows[0])
    if width < 5 or width > 128:
        raise ValueError("map width must be between 5 and 128 cells")
    grid: list[list[str]] = []
    for index, row in enumerate(rows):
        if len(row) != width:
            raise ValueError(f"map row {index} width {len(row)} != {width}")
        unknown = sorted(set(row) - ALLOWED_TILES)
        if unknown:
            raise ValueError(f"map row {index} contains unsupported tiles: {unknown!r}")
        grid.append(list(row))
    if any(cell != TILE_STEEL for cell in grid[0] + grid[-1]):
        raise ValueError("map top and bottom borders must be steel (#)")
    if any(row[0] != TILE_STEEL or row[-1] != TILE_STEEL for row in grid):
        raise ValueError("map left and right borders must be steel (#)")
    return grid


def serialize_map(grid: list[list[str]]) -> str:
    return "\n".join("".join(row) for row in grid)


def _cell_center(row: int, col: int) -> tuple[int, int]:
    return col * SCALE + SCALE // 2, row * SCALE + SCALE // 2


def _tile_at(grid: list[list[str]], x: int, y: int) -> str:
    row, col = y // SCALE, x // SCALE
    if row < 0 or col < 0 or row >= len(grid) or col >= len(grid[0]):
        return TILE_STEEL
    return grid[row][col]


def _rect_blocked(grid: list[list[str]], x: int, y: int) -> bool:
    # Treat an edge exactly on a tile boundary as touching, not entering, the
    # neighbouring tile.  The one-unit inset also keeps 180°-rotated positions
    # collision-equivalent under integer floor division.
    edge = TANK_HALF - 1
    for dx in (-edge, 0, edge):
        for dy in (-edge, 0, edge):
            if _tile_at(grid, x + dx, y + dy) in BLOCKS_MOVE:
                return True
    return False


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return abs(a[0] - b[0]) < 2 * TANK_HALF and abs(a[1] - b[1]) < 2 * TANK_HALF


def _spawn_cells(grid: list[list[str]], team_size: int) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    available = [
        (row, col)
        for row in range(1, len(grid) - 1)
        for col in range(1, len(grid[0]) - 1)
        if grid[row][col] in (TILE_FLOOR, TILE_BUSH, TILE_PICKUP)
    ]
    if len(available) < team_size * 2:
        raise ValueError(
            f"map has {len(available)} spawnable cells but {team_size * 2} are required"
        )
    guest = available[:team_size]
    guest_set = set(guest)
    host = [cell for cell in reversed(available) if cell not in guest_set][:team_size]
    if len(host) != team_size:
        raise ValueError("map cannot provide disjoint Host and Guest spawns")
    return host, guest


def init_world(grid: list[list[str]], team_size: int, rules: dict[str, Any]) -> dict[str, Any]:
    grid = copy.deepcopy(grid)
    host_cells, guest_cells = _spawn_cells(grid, team_size)
    hp = int(rules["balance"]["hp"])
    tanks: list[dict[str, Any]] = []
    for index, (row, col) in enumerate(host_cells):
        x, y = _cell_center(row, col)
        tanks.append(
            {
                "id": f"h{index}",
                "team": "host",
                "x": x,
                "y": y,
                "facing": "up",
                "hp": hp,
                "fire_cd": 0,
                "last_move": "none",
                "last_fire": False,
                "speed_until": 0,
                "rapid_until": 0,
            }
        )
    for index, (row, col) in enumerate(guest_cells):
        x, y = _cell_center(row, col)
        tanks.append(
            {
                "id": f"g{index}",
                "team": "guest",
                "x": x,
                "y": y,
                "facing": "down",
                "hp": hp,
                "fire_cd": 0,
                "last_move": "none",
                "last_fire": False,
                "speed_until": 0,
                "rapid_until": 0,
            }
        )
    pickups: list[dict[str, Any]] = []
    kinds = ("repair", "speed", "rapid")
    pickup_cells: list[tuple[int, int]] = []
    for row, cells in enumerate(grid):
        for col, tile in enumerate(cells):
            if tile != TILE_PICKUP:
                continue
            grid[row][col] = TILE_FLOOR
            pickup_cells.append((row, col))
    if rules["pickup_mode"] == "powerups":
        for index, (row, col) in enumerate(pickup_cells):
            x, y = _cell_center(row, col)
            pair_index = min(index, len(pickup_cells) - 1 - index)
            pickups.append(
                {
                    "id": f"p{index}",
                    "kind": kinds[pair_index % len(kinds)],
                    "x": x,
                    "y": y,
                    "active": True,
                }
            )
    return {
        "schema_version": 1,
        "tick": 0,
        "rows": len(grid),
        "cols": len(grid[0]),
        "grid": grid,
        "tanks": tanks,
        "bullets": [],
        "pickups": pickups,
        "events": [],
        "host_alive": team_size,
        "guest_alive": team_size,
        "game_over": False,
        "winner": "none",
        "rules": copy.deepcopy(rules),
    }


def world_to_json(world: dict[str, Any]) -> str:
    return json.dumps(world, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_world(world_json: str, balance: dict[str, Any] | None = None) -> dict[str, Any]:
    del balance
    world = json.loads(world_json)
    if not isinstance(world, dict):
        raise ValueError("world must be a JSON object")
    return world


def _commands_by_unit(commands: Any, side: str) -> dict[str, dict[str, Any]]:
    if isinstance(commands, dict):
        source = [dict(value, unit_id=key) for key, value in commands.items()]
    else:
        source = commands or []
    prefix = "h" if side == "host" else "g"
    return {
        command["unit_id"]: command
        for command in source
        if isinstance(command, dict) and str(command.get("unit_id", "")).startswith(prefix)
    }


def _movement_phase(
    tanks: list[dict[str, Any]],
    grid: list[list[str]],
    tick: int,
    speed: int,
) -> None:
    live = [tank for tank in tanks if tank["hp"] > 0]
    current = {tank["id"]: (tank["x"], tank["y"]) for tank in live}
    proposed: dict[str, tuple[int, int]] = {}
    candidates: set[str] = set()
    for tank in live:
        move = tank["last_move"]
        vx, vy = VEC[move]
        unit_speed = speed * 3 // 2 if tick < tank.get("speed_until", 0) else speed
        position = (tank["x"] + vx * unit_speed, tank["y"] + vy * unit_speed)
        proposed[tank["id"]] = position
        if move != "none" and not _rect_blocked(grid, *position):
            candidates.add(tank["id"])

    # Resolve collisions symmetrically.  If two proposed positions overlap, neither
    # side gets ID-order priority.  Iterate because rejecting one move can make its
    # current position block another proposal.
    changed = True
    while changed:
        active = set(candidates)
        rejected: set[str] = set()
        for index, first in enumerate(live):
            for second in live[index + 1 :]:
                first_id, second_id = first["id"], second["id"]
                first_pos = proposed[first_id] if first_id in active else current[first_id]
                second_pos = proposed[second_id] if second_id in active else current[second_id]
                if not _overlap(first_pos, second_pos):
                    continue
                rejected.update(unit_id for unit_id in (first_id, second_id) if unit_id in active)
        candidates.difference_update(rejected)
        changed = bool(rejected)
        # At most the number of live tanks iterations; every changed pass rejects one.

    for tank in live:
        if tank["id"] in candidates:
            tank["x"], tank["y"] = proposed[tank["id"]]


def sim_step(
    world: dict[str, Any],
    host_commands: Any,
    guest_commands: Any,
) -> dict[str, Any]:
    """Advance one authoritative fixed-point tick without mutating ``world``."""
    state = copy.deepcopy(world)
    tick = int(world["tick"]) + 1
    state["tick"] = tick
    state["events"] = []
    events = state["events"]
    tanks = state["tanks"]
    grid = state["grid"]
    rules = state["rules"]
    balance = rules["balance"]
    commands = {
        **_commands_by_unit(host_commands, "host"),
        **_commands_by_unit(guest_commands, "guest"),
    }

    for tank in tanks:
        if tank["hp"] <= 0:
            continue
        if tank["fire_cd"] > 0:
            tank["fire_cd"] -= 1
        command = commands.get(tank["id"])
        if command:
            move = command.get("move", "none")
            aim = command.get("aim", "none")
            tank["last_move"] = move if move in DIRECTIONS else "none"
            tank["last_fire"] = bool(command.get("fire", False))
            if aim in CARDINAL:
                tank["facing"] = aim
            elif tank["last_move"] in CARDINAL:
                tank["facing"] = tank["last_move"]

    _movement_phase(tanks, grid, tick, int(balance["speed"]))

    new_bullets: list[dict[str, Any]] = []
    for tank in sorted(tanks, key=lambda item: item["id"]):
        if tank["hp"] <= 0 or not tank["last_fire"] or tank["fire_cd"] > 0:
            continue
        vx, vy = VEC[tank["facing"]]
        x = tank["x"] + vx * (TANK_HALF + 50)
        y = tank["y"] + vy * (TANK_HALF + 50)
        new_bullets.append(
            {
                "id": f"b{tick}-{tank['id']}",
                "team": tank["team"],
                "owner_id": tank["id"],
                "x": x,
                "y": y,
                "dir": tank["facing"],
            }
        )
        cooldown = int(balance["fire_cooldown"])
        tank["fire_cd"] = max(1, cooldown // 2) if tick < tank.get("rapid_until", 0) else cooldown
        events.append({"type": "fire", "tank": tank["id"], "x": x, "y": y})
    state["bullets"].extend(new_bullets)

    surviving: list[dict[str, Any]] = []
    collision_grid = copy.deepcopy(grid)
    live_targets = [tank for tank in tanks if tank["hp"] > 0]
    hit_intents: list[tuple[str, str, str]] = []
    destroyed_bricks: dict[tuple[int, int], list[str]] = {}
    bullet_speed = int(balance["bullet_speed"])
    substeps = max(1, (bullet_speed + 249) // 250)
    base_step, remainder = divmod(bullet_speed, substeps)
    for bullet in sorted(state["bullets"], key=lambda item: item["id"]):
        vx, vy = VEC[bullet["dir"]]
        active = True
        for index in range(substeps):
            distance = base_step + (1 if index < remainder else 0)
            x = bullet["x"] + vx * distance
            y = bullet["y"] + vy * distance
            tile = _tile_at(collision_grid, x, y)
            if tile == TILE_STEEL:
                active = False
                break
            if tile == TILE_BRICK:
                row, col = y // SCALE, x // SCALE
                destroyed_bricks.setdefault((row, col), []).append(bullet["id"])
                active = False
                break
            victim = None
            for tank in sorted(live_targets, key=lambda item: item["id"]):
                if tank["id"] == bullet["owner_id"]:
                    continue
                if not rules["friendly_fire"] and tank["team"] == bullet["team"]:
                    continue
                if abs(x - tank["x"]) < TANK_HALF and abs(y - tank["y"]) < TANK_HALF:
                    victim = tank
                    break
            if victim is not None:
                hit_intents.append((bullet["id"], bullet["owner_id"], victim["id"]))
                active = False
                break
            bullet["x"], bullet["y"] = x, y
        if active:
            surviving.append(bullet)
    state["bullets"] = surviving

    for (row, col), bullet_ids in sorted(destroyed_bricks.items()):
        grid[row][col] = TILE_FLOOR
        events.append(
            {
                "type": "brick_destroyed",
                "bullet": sorted(bullet_ids)[0],
                "bullets": sorted(bullet_ids),
                "row": row,
                "col": col,
            }
        )

    damage: dict[str, int] = {}
    for _, _, victim_id in hit_intents:
        damage[victim_id] = damage.get(victim_id, 0) + 1
    displayed_hp = {tank["id"]: tank["hp"] for tank in live_targets}
    by_id = {tank["id"]: tank for tank in tanks}
    for _, attacker_id, victim_id in sorted(hit_intents):
        displayed_hp[victim_id] = max(0, displayed_hp[victim_id] - 1)
        events.append(
            {
                "type": "hit",
                "attacker": attacker_id,
                "victim": victim_id,
                "victim_hp": displayed_hp[victim_id],
            }
        )
    for victim_id, amount in sorted(damage.items()):
        victim = by_id[victim_id]
        old_hp = victim["hp"]
        victim["hp"] = max(0, old_hp - amount)
        if old_hp > 0 and victim["hp"] == 0:
            events.append({"type": "destroyed", "tank": victim["id"], "team": victim["team"]})

    if rules["pickup_mode"] == "powerups":
        duration = int(rules["powerup_duration_ticks"])
        for pickup in state["pickups"]:
            if not pickup["active"]:
                continue
            collectors = [
                tank
                for tank in tanks
                if tank["hp"] > 0
                and abs(tank["x"] - pickup["x"]) < TANK_HALF
                and abs(tank["y"] - pickup["y"]) < TANK_HALF
            ]
            if len(collectors) != 1:
                continue
            collector = collectors[0]
            pickup["active"] = False
            kind = pickup["kind"]
            if kind == "repair":
                collector["hp"] = min(int(balance["hp"]), collector["hp"] + 1)
            elif kind == "speed":
                collector["speed_until"] = tick + duration
            elif kind == "rapid":
                collector["rapid_until"] = tick + duration
            events.append({"type": "pickup", "tank": collector["id"], "pickup": pickup["id"], "kind": kind})

    state["host_alive"] = sum(1 for tank in tanks if tank["team"] == "host" and tank["hp"] > 0)
    state["guest_alive"] = sum(1 for tank in tanks if tank["team"] == "guest" and tank["hp"] > 0)
    host_dead = state["host_alive"] == 0
    guest_dead = state["guest_alive"] == 0
    reached_limit = tick >= int(rules["max_ticks"])
    if host_dead or guest_dead or reached_limit:
        state["game_over"] = True
        if host_dead and guest_dead:
            winner = "draw"
        elif host_dead:
            winner = "guest"
        elif guest_dead:
            winner = "host"
        else:
            host_score = (
                state["host_alive"],
                sum(tank["hp"] for tank in tanks if tank["team"] == "host"),
            )
            guest_score = (
                state["guest_alive"],
                sum(tank["hp"] for tank in tanks if tank["team"] == "guest"),
            )
            winner = "host" if host_score > guest_score else "guest" if guest_score > host_score else "draw"
        state["winner"] = winner
        events.append({"type": "game_over", "winner": winner, "reason": "elimination" if not reached_limit else "tick_limit"})
    return state


def _match_maneuver(text: str, choices: dict[str, list[str]]) -> str | None:
    """Return the most specific maneuver alias present in ``text``."""
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    aliases = sorted(
        (
            (str(alias).lower(), str(maneuver))
            for maneuver, values in choices.items()
            for alias in values
            if str(alias).strip()
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for alias, maneuver in aliases:
        if alias in normalized:
            return maneuver
    return None


def _split_tactical_clauses(text: str, choices: dict[str, list[str]]) -> list[str]:
    """Split a tactical whisper without breaking unit lists or ``(x, y)`` coordinates."""
    protected = re.sub(
        r"(\d+)\s*[,，]\s*(\d+)",
        lambda match: f"{match.group(1)}§{match.group(2)}",
        text,
    )
    # ``h0,h1`` is a unit list, not a clause boundary.
    protected = re.sub(
        r"([hgHG]\d{1,2})\s*[,，]\s*(?=[hgHG]\d{1,2})",
        lambda match: match.group(1) + "、",
        protected,
    )
    protected = re.sub(r"(?=(?:然后|随后|接着|再让|再由|同时让))", "；", protected)
    raw = [
        part.replace("§", ",").strip(" ，,;；。\t")
        for part in re.split(r"[；;。\n，,]+", protected)
        if part.strip(" ，,;；。\t")
    ]
    clauses: list[str] = []
    for part in raw:
        if _match_maneuver(part, choices) is not None:
            clauses.append(part)
            continue
        # A trailing duration/target modifier belongs to the previous executable clause.
        if clauses and re.search(r"(?:持续|维持|坐标|第\s*\d+\s*行|tick|帧|拍)", part, re.I):
            clauses[-1] += "，" + part
    return clauses[:8]


def _extract_tactical_units(
    clause: str,
    *,
    role: str,
    known_units: set[str],
) -> tuple[list[str] | None, bool]:
    """Return explicit local unit ids; ``None`` means whole army."""
    prefix = "h" if role == "host" else "g" if role == "guest" else ""
    explicit = [
        match.lower()
        for match in re.findall(r"(?<![a-z0-9])([hg]\d{1,2})(?!\d)", clause, re.I)
        if not prefix or match.lower().startswith(prefix)
    ]
    for number in re.findall(r"(?<!\d)(\d{1,2})\s*号(?:坦克|战车|车)?", clause):
        if prefix:
            explicit.append(prefix + str(int(number)))
    mentioned = bool(explicit)
    if not mentioned or re.search(r"(?:全军|全体|所有|全部单位|whole army|all units)", clause, re.I):
        return None, mentioned
    unique = sorted(
        {
            unit_id
            for unit_id in explicit
            if not known_units or unit_id in known_units
        },
        key=lambda unit_id: (int(unit_id[1:]), unit_id[0]),
    )
    return unique, True


def _extract_tactical_targets(clause: str, rows: int, cols: int) -> list[dict[str, int]]:
    """Extract up to eight map coordinates in their source order."""
    matches: list[tuple[int, int, int]] = []
    for item in re.finditer(r"第?\s*(\d+)\s*行.*?第?\s*(\d+)\s*列", clause, re.I):
        matches.append((item.start(), int(item.group(1)), int(item.group(2))))
    for item in re.finditer(
        r"(?:坐标|目标|position|coordinate)?\s*[（(]?\s*(\d+)\s*[,，/]\s*(\d+)\s*[)）]?",
        clause,
        re.I,
    ):
        # Conventional map coordinates are written as (x, y) == (column, row).
        matches.append((item.start(), int(item.group(2)), int(item.group(1))))
    targets: list[dict[str, int]] = []
    for _, raw_row, raw_col in sorted(matches)[:8]:
        row, col = raw_row, raw_col
        if rows > 2:
            row = max(1, min(rows - 2, row))
        if cols > 2:
            col = max(1, min(cols - 2, col))
        target = {"row": row, "col": col}
        if not targets or targets[-1] != target:
            targets.append(target)
    return targets


def _extract_tactical_target(clause: str, rows: int, cols: int) -> dict[str, int] | None:
    targets = _extract_tactical_targets(clause, rows, cols)
    return targets[0] if targets else None


def parse_tactical_whisper(
    text: str,
    context: dict[str, Any] | None,
    choices: dict[str, list[str]],
) -> dict[str, Any] | None:
    """Compile bounded natural language into a deterministic multi-unit battle plan.

    This parser deliberately does not call an LLM.  It accepts up to eight clauses and
    understands unit scopes, relative/absolute tick timing, durations, coordinates and
    simple force-strength triggers.  The result is regular strategy data consumed by the
    local Agent on every tick.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    context = context if isinstance(context, dict) else {}
    world = context.get("world") if isinstance(context.get("world"), dict) else {}
    role = str(context.get("role") or "")
    current_tick = int(context.get("tick") or world.get("tick") or 0)
    rows, cols = int(world.get("rows") or 0), int(world.get("cols") or 0)
    known_units = {
        str(tank.get("id"))
        for tank in world.get("tanks", [])
        if isinstance(tank, dict)
        and tank.get("team") == role
        and int(tank.get("hp") or 0) > 0
    }
    enemy_prefix = "g" if role == "host" else "h" if role == "guest" else ""
    known_enemy_units = {
        str(tank.get("id"))
        for tank in world.get("tanks", [])
        if isinstance(tank, dict)
        and tank.get("team") != role
        and int(tank.get("hp") or 0) > 0
    }
    clauses = _split_tactical_clauses(text[:2000], choices)
    orders: list[dict[str, Any]] = []
    assumptions: list[str] = []
    for clause in clauses:
        maneuver = _match_maneuver(clause, choices)
        if maneuver is None:
            continue
        unit_ids, units_were_mentioned = _extract_tactical_units(
            clause,
            role=role,
            known_units=known_units,
        )
        if units_were_mentioned and unit_ids == []:
            assumptions.append(f"Ignored an invalid local unit scope: {clause[:80]}")
            continue

        targets = _extract_tactical_targets(clause, rows, cols)
        if maneuver == "move_to" and not targets:
            assumptions.append(f"Ignored move order without a coordinate: {clause[:80]}")
            continue
        if maneuver == "patrol" and len(targets) < 2:
            assumptions.append(f"Ignored patrol order without two coordinates: {clause[:80]}")
            continue
        target_unit_id = next(
            (
                match.lower()
                for match in re.findall(r"(?<![a-z0-9])([hg]\d{1,2})(?!\d)", clause, re.I)
                if match.lower().startswith(enemy_prefix)
                and (not known_enemy_units or match.lower() in known_enemy_units)
            ),
            None,
        )
        if target_unit_id is not None and maneuver == "assault" and re.search(
            r"(?:attack|target|charge|攻击|攻打|冲向|追击)", clause, re.I
        ):
            maneuver = "attack_unit"
        if maneuver == "attack_unit" and target_unit_id is None:
            assumptions.append(f"Ignored attack order without a live enemy unit: {clause[:80]}")
            continue

        start_tick = current_tick
        relative = re.search(r"(\d+)\s*(?:tick|ticks|帧|拍|刻)\s*(?:后|之后|later)", clause, re.I)
        absolute = re.search(r"(?:第|到|at)\s*(\d+)\s*(?:tick|帧|拍|刻)", clause, re.I)
        if relative:
            start_tick = current_tick + min(100000, int(relative.group(1)))
        elif absolute:
            start_tick = max(current_tick, int(absolute.group(1)))
        elif orders and re.match(r"^(?:然后|随后|接着|再)", clause):
            previous = orders[-1]
            start_tick = int(
                previous.get("expires_tick")
                or (int(previous.get("start_tick", current_tick)) + NAV_DEFAULT_PHASE_TICKS)
            )
            assumptions.append(
                f"Scheduled '{clause[:24]}' {NAV_DEFAULT_PHASE_TICKS} ticks after the previous phase."
            )

        duration = re.search(r"(?:持续|维持|for)\s*(\d+)\s*(?:tick|ticks|帧|拍|刻)", clause, re.I)
        trigger = None
        alive = re.search(
            r"(?:(敌方|敌军|对方)\s*)?(?:兵力)?(?:只剩|剩余|低于|少于|不超过)\s*(\d+)\s*(?:辆|台|个)?",
            clause,
        )
        if alive:
            trigger = {
                "type": "enemy_alive_lte" if alive.group(1) else "own_alive_lte",
                "value": max(0, min(32, int(alive.group(2)))),
            }
        hp = re.search(r"(?:血量|生命|hp)\s*(?:低于|少于|不超过|<=?)\s*(\d+)", clause, re.I)
        if hp:
            trigger = {"type": "unit_hp_lte", "value": max(0, min(99, int(hp.group(1))))}

        order: dict[str, Any] = {
            "id": f"phase-{len(orders) + 1:02d}",
            "kind": maneuver,
            "start_tick": start_tick,
            "source_clause": clause[:200],
        }
        if unit_ids is not None:
            order["unit_ids"] = unit_ids[:32]
        if maneuver == "patrol":
            order["waypoints"] = targets[:8]
        elif targets:
            order["target"] = targets[0]
        if target_unit_id is not None:
            order["target_unit_id"] = target_unit_id
        if duration:
            order["expires_tick"] = start_tick + min(100000, int(duration.group(1)))
        if trigger is not None:
            order["trigger"] = trigger
        orders.append(order)

    if not orders:
        return None
    digest = sha256((str(current_tick) + "\0" + text).encode("utf-8")).hexdigest()[:10]
    default_maneuver = next(
        (
            str(order["kind"])
            for order in orders
            if "unit_ids" not in order
            and int(order.get("start_tick", current_tick)) <= current_tick
            and "trigger" not in order
        ),
        "assault",
    )
    plan = {
        "version": 1,
        "id": f"whisper-{current_tick}-{digest}",
        "issued_at_tick": current_tick,
        "source_text": text[:500],
        "orders": orders,
        "assumptions": assumptions[:8],
    }
    return {
        "scope": "persist",
        "target_policy": "persist",
        "value": None,
        "policy": None,
        "strategy_patch": {
            "mode": "tactical_plan",
            "maneuver": default_maneuver,
            "order": None,
            "plan": plan,
        },
        "raw_text": text,
        "confidence": 0.96 if not assumptions else 0.9,
    }


class Hooks(ProtocolHooks):
    """Reference RTS hook: game semantics only; scheduling/transport lives in engine."""

    DECISION_SCHEMA = {
        "match_key": "tick",
        "value_field": "maneuver",
        # strategy_field="maneuver" lets the whisper bridge persist a natural-language
        # order as strategy["maneuver"]; _compute_commands reads that key directly (its
        # second-priority source, after an explicit order.kind object and before the
        # operator_hint fallback). Keep this aligned with the keys in _compute_commands.
        "strategy_field": "maneuver",
        "policy_family": "realtime-rts",
        "intent_parser": "protocol_hook",
        # Declare tactical maneuvers and persistent order types so the whisper bridge
        # (parse_whisper_to_intent) recognises natural-language orders ("全军压上去"
        # -> assault) and materializes them as a real strategy write, returning
        # ack_status="strategy_active" instead of "unparsed". Each value is the list
        # of substrings that map to that maneuver; long aliases first to win matches.
        "choices": {
            "assault": [
                "全军突击", "全军压上", "集中火力", "火力集中", "压上去", "压上", "全歼", "出击", "总攻",
                "突击", "冲锋", "进攻", "冲啊", "冲过去", "打过去", "打他", "歼灭",
                "assault", "attack", "charge", "advance", "push",
            ],
            "flank_left": [
                "左翼包围后集中火力", "左翼包抄", "包抄左翼", "左翼包围", "从左边", "左路", "左翼", "左侧",
                "flank left", "left flank",
            ],
            "flank_right": [
                "右翼包围后集中火力", "右翼包抄", "包抄右翼", "右翼包围", "从右边", "右路", "右翼", "右侧",
                "flank right", "right flank",
            ],
            "retreat": [
                "撤退", "后撤", "退守", "拉回来", "撤回来", "退回来", "收缩",
                "retreat", "fall back", "withdraw",
            ],
            "defend": [
                "固守", "守住", "龟缩", "守家", "防守", "防御", "坚守", "卡住",
                "defend", "hold position", "hold the line",
            ],
            "surround": [
                "包围", "合围", "围歼", "围杀", "包饺子", "surround", "encircle",
            ],
            "hold": [
                "原地不动", "别动", "停止前进", "停火", "停止", "原地待命",
                "hold fire", "hold", "stop", "standby",
            ],
            "move_to": [
                "沿途不停留", "移动到坐标", "前往坐标", "开到坐标", "驶向坐标",
                "move to coordinate", "move to", "go to", "proceed to",
            ],
            "patrol": [
                "来回巡逻", "往返巡逻", "区间巡逻", "巡逻",
                "patrol between", "patrol from", "patrol",
            ],
            "attack_unit": [
                "对着目标冲过去开火", "冲过去开火", "冲向目标", "追击目标", "追击",
                "攻击", "攻打", "attack target tank", "attack unit", "attack tank", "chase target", "chase",
            ],
        },
    }

    def proto_init(
        self,
        options: dict[str, Any],
        role: str,
        args: list[str],
        state_dir: Path,
        decision_config: dict[str, Any] | None = None,
    ) -> None:
        super().proto_init(options, role, args, state_dir, decision_config)
        table = options.get("balance")
        model = table.get("tank") if isinstance(table, dict) else None
        if not isinstance(model, dict):
            model = DEFAULT_BALANCE
        self.balance = {
            "hp": int(model.get("hp", DEFAULT_BALANCE["hp"])),
            "speed": int(model.get("speed", DEFAULT_BALANCE["speed"])),
            "fire_cooldown": int(model.get("fire_cooldown", DEFAULT_BALANCE["fire_cooldown"])),
            "bullet_speed": int(model.get("bullet_speed", DEFAULT_BALANCE["bullet_speed"])),
        }
        self.team_size = max(1, min(32, int(options.get("team_size", 5))))
        self.max_ticks = int(options.get("max_ticks", 1800))
        self.map_text = str(options.get("map_text") or DEFAULT_MAP)
        self.friendly_fire = bool(options.get("friendly_fire", False))
        self.pickup_mode = str(options.get("pickup_mode", "none"))
        self.powerup_duration_ticks = int(options.get("powerup_duration_ticks", 100))
        self.fallback_maneuver = args[0] if args else "assault"
        self.world: dict[str, Any] | None = None
        self._navigation: dict[str, dict[str, Any]] = {}
        self._patrol_progress: dict[str, dict[str, Any]] = {}
        self._reserved_cells: set[tuple[int, int]] = set()
        self._active_plan_status: dict[str, Any] | None = None
        self._transport_profile: dict[str, Any] = {
            "status": "unavailable",
            "recommended_control": "macro",
            "micro_suitable": False,
        }
        # Fail before the P2P handshake if the map cannot host the declared armies.
        grid = parse_map(self.map_text)
        _spawn_cells(grid, self.team_size)

    def proto_parse_whisper_intent(
        self,
        text: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Protocol-owned compiler for complex, bounded real-time tactical orders."""
        return parse_tactical_whisper(text, context, self.DECISION_SCHEMA["choices"])

    def proto_host_metadata(self) -> tuple[str, str, str, dict[str, Any]]:
        return (
            "Tank Battle · Realtime",
            "game,tank-battle,rts,realtime",
            "supply",
            {
                "team_size": self.team_size,
                "max_ticks": self.max_ticks,
                "friendly_fire": self.friendly_fire,
                "pickup_mode": self.pickup_mode,
            },
        )

    def _rules(self) -> dict[str, Any]:
        return {
            "balance": copy.deepcopy(self.balance),
            "team_size": self.team_size,
            "max_ticks": self.max_ticks,
            "friendly_fire": self.friendly_fire,
            "pickup_mode": self.pickup_mode,
            "powerup_duration_ticks": self.powerup_duration_ticks,
        }

    def proto_realtime_initial_state(self) -> dict[str, Any]:
        self.world = init_world(parse_map(self.map_text), self.team_size, self._rules())
        return self.world

    def proto_realtime_commands(self, state: dict[str, Any], target_tick: int) -> list[dict[str, Any]]:
        self.world = state
        return self._compute_commands(state, target_tick)

    def proto_realtime_transport_update(self, profile: dict[str, Any]) -> None:
        self._transport_profile = copy.deepcopy(profile) if isinstance(profile, dict) else {}

    def proto_realtime_validate_commands(
        self,
        side: str,
        commands: list[dict[str, Any]],
        state: dict[str, Any],
        target_tick: int,
    ) -> list[dict[str, Any]]:
        if side not in ("host", "guest"):
            raise ValueError("side must be host or guest")
        if target_tick <= int(state["tick"]):
            raise ValueError("target_tick must be in the future")
        tanks = {tank["id"]: tank for tank in state["tanks"]}
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for index, command in enumerate(commands):
            if not isinstance(command, dict):
                raise ValueError(f"commands[{index}] must be an object")
            unknown = set(command) - {"unit_id", "move", "aim", "fire"}
            if unknown:
                raise ValueError(f"commands[{index}] has unknown fields: {sorted(unknown)!r}")
            unit_id = command.get("unit_id")
            if not isinstance(unit_id, str) or unit_id not in tanks:
                raise ValueError(f"commands[{index}].unit_id is unknown")
            if unit_id in seen:
                raise ValueError(f"duplicate command for unit {unit_id}")
            tank = tanks[unit_id]
            if tank["team"] != side:
                raise ValueError(f"unit {unit_id} is not owned by {side}")
            if tank["hp"] <= 0:
                raise ValueError(f"unit {unit_id} is destroyed")
            move = command.get("move", "none")
            aim = command.get("aim", "none")
            fire = command.get("fire", False)
            if move not in DIRECTIONS:
                raise ValueError(f"commands[{index}].move is invalid")
            if aim not in DIRECTIONS:
                raise ValueError(f"commands[{index}].aim is invalid")
            if not isinstance(fire, bool):
                raise ValueError(f"commands[{index}].fire must be boolean")
            seen.add(unit_id)
            normalized.append({"unit_id": unit_id, "move": move, "aim": aim, "fire": fire})
        return sorted(normalized, key=lambda item: item["unit_id"])

    def proto_realtime_step(
        self,
        state: dict[str, Any],
        tick: int,
        commands: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        if tick != int(state["tick"]) + 1:
            raise ValueError("tank simulation tick discontinuity")
        new_state = sim_step(state, commands.get("host", []), commands.get("guest", []))
        self.world = new_state
        return {
            "state": new_state,
            "events": list(new_state["events"]),
            "outcome": new_state["winner"] if new_state["game_over"] else "none",
        }

    def proto_realtime_snapshot(
        self,
        state: dict[str, Any],
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        self.world = state
        events = frame.get("events") or []
        last_summary = None
        if events:
            event = events[-1]
            labels = {
                "fire": "Tank fired",
                "hit": "Shell hit",
                "destroyed": "Tank destroyed",
                "brick_destroyed": "Brick wall destroyed",
                "pickup": "Tank collected a power-up",
                "game_over": "Battle ended",
            }
            last_summary = labels.get(event.get("type"), event.get("type", "Battle updated"))
        patch = {
            "phase": "game_over" if state["game_over"] else "battle",
            "team_size": state["rules"]["team_size"],
            "host_alive": state["host_alive"],
            "guest_alive": state["guest_alive"],
            "world": state,
            "combat_events": events,
        }
        if self._active_plan_status:
            patch["tactical_plan"] = copy.deepcopy(self._active_plan_status)
        if last_summary:
            patch["last_event"] = {"summary": last_summary, "structured": events[-1]}
        return patch

    def proto_realtime_audit_outcome(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Cheap post-game condition check; never changes the accepted outcome."""
        state = frame["state"]
        host_alive = sum(1 for tank in state["tanks"] if tank["team"] == "host" and tank["hp"] > 0)
        guest_alive = sum(1 for tank in state["tanks"] if tank["team"] == "guest" and tank["hp"] > 0)
        internally_consistent = (
            host_alive == state["host_alive"]
            and guest_alive == state["guest_alive"]
            and state["game_over"]
            and state["winner"] == frame["outcome"]
        )
        decisive = host_alive == 0 or guest_alive == 0 or state["tick"] >= state["rules"]["max_ticks"]
        return {
            "status": "passed" if internally_consistent and decisive else "failed",
            "check": "terminal_condition",
            "host_alive": host_alive,
            "guest_alive": guest_alive,
        }

    def build_decision_context(self, match_key: str, match_value: Any) -> dict[str, Any]:
        if not self.world:
            return {"supported": False, "reason": "no_world"}
        side = self.role
        return {
            "supported": True,
            "match_key": match_key,
            "match_value": match_value,
            "team": side,
            "world": self.world,
            "transport": copy.deepcopy(self._transport_profile),
            "command_schema": {"unit_id": "id", "move": list(DIRECTIONS), "aim": list(DIRECTIONS), "fire": "boolean"},
            "macro_order_schema": {
                "kinds": ["move_to", "patrol", "attack_unit", "assault", "defend", "retreat"],
                "fields": ["unit_ids", "target", "waypoints", "target_unit_id", "start_tick", "expires_tick", "trigger"],
            },
        }

    def _compute_commands(self, state: dict[str, Any], target_tick: int) -> list[dict[str, Any]]:
        strategy = self.strategy.read() or {}
        quick_order = strategy.get("order") if isinstance(strategy.get("order"), dict) else {}
        plan = strategy.get("plan") if isinstance(strategy.get("plan"), dict) else {}
        if quick_order and plan:
            quick_tick = int(quick_order.get("issued_at_tick", -1))
            plan_tick = int(plan.get("issued_at_tick", -1))
            if quick_tick < plan_tick:
                quick_order = {}
        expires = quick_order.get("expires_tick")
        if isinstance(expires, int) and target_tick > expires:
            quick_order = {}

        fallback = str(
            strategy.get("maneuver")
            or self._hint_maneuver(strategy.get("operator_hint"))
            or self.fallback_maneuver
        )
        if fallback not in self.DECISION_SCHEMA["choices"]:
            fallback = self.fallback_maneuver
        own = [tank for tank in state["tanks"] if tank["team"] == self.role and tank["hp"] > 0]
        direct_by_unit: dict[str, dict[str, Any]] = {}
        direct = strategy.get("micro_commands")
        direct_expires = strategy.get("micro_expires_tick")
        if isinstance(direct, list) and isinstance(direct_expires, int) and target_tick <= direct_expires:
            try:
                normalized_direct = self.proto_realtime_validate_commands(
                    self.role, direct, state, target_tick
                )
                direct_by_unit = {item["unit_id"]: item for item in normalized_direct}
            except ValueError:
                direct_by_unit = {}
        self._reserved_cells = set()
        active_order_ids: set[str] = set()
        commands: list[dict[str, Any]] = []
        for index, tank in enumerate(sorted(own, key=lambda item: item["id"])):
            if tank["id"] in direct_by_unit:
                commands.append(direct_by_unit[tank["id"]])
                continue
            order: dict[str, Any] = {}
            unit_maneuver = fallback
            if quick_order:
                scope = quick_order.get("unit_ids") if isinstance(quick_order.get("unit_ids"), list) else None
                if scope is None or tank["id"] in {str(unit_id) for unit_id in scope}:
                    order = quick_order
                    unit_maneuver = str(quick_order.get("kind") or fallback)
            elif plan:
                selected = self._plan_order_for_unit(state, tank, plan, target_tick)
                if selected is not None:
                    order = selected
                    unit_maneuver = str(selected.get("kind") or fallback)
                    active_order_ids.add(str(selected.get("id") or ""))
            if unit_maneuver not in self.DECISION_SCHEMA["choices"]:
                unit_maneuver = fallback
            commands.append(
                self._auto_command(state, tank, index, unit_maneuver, order, target_tick)
            )

        if plan:
            self._active_plan_status = {
                "id": plan.get("id"),
                "issued_at_tick": plan.get("issued_at_tick"),
                "source_text": plan.get("source_text"),
                "active_order_ids": sorted(value for value in active_order_ids if value),
                "orders": copy.deepcopy(plan.get("orders") or []),
                "assumptions": copy.deepcopy(plan.get("assumptions") or []),
            }
        else:
            self._active_plan_status = None
        return commands

    def _plan_order_for_unit(
        self,
        state: dict[str, Any],
        tank: dict[str, Any],
        plan: dict[str, Any],
        target_tick: int,
    ) -> dict[str, Any] | None:
        selected = None
        for candidate in plan.get("orders") or []:
            if not isinstance(candidate, dict):
                continue
            scope = candidate.get("unit_ids")
            if isinstance(scope, list) and tank["id"] not in {str(value) for value in scope}:
                continue
            start_tick = candidate.get("start_tick")
            if isinstance(start_tick, int) and target_tick < start_tick:
                continue
            expires_tick = candidate.get("expires_tick")
            if isinstance(expires_tick, int) and target_tick > expires_tick:
                continue
            if not self._trigger_allows(state, tank, candidate.get("trigger")):
                continue
            selected = candidate
        return selected

    @staticmethod
    def _trigger_allows(
        state: dict[str, Any],
        tank: dict[str, Any],
        trigger: Any,
    ) -> bool:
        if not isinstance(trigger, dict):
            return True
        kind = str(trigger.get("type") or "")
        value = int(trigger.get("value") or 0)
        own = tank["team"]
        enemy = "guest" if own == "host" else "host"
        if kind == "own_alive_lte":
            return int(state[f"{own}_alive"]) <= value
        if kind == "enemy_alive_lte":
            return int(state[f"{enemy}_alive"]) <= value
        if kind == "unit_hp_lte":
            return int(tank["hp"]) <= value
        return False

    @classmethod
    def _hint_maneuver(cls, hint: Any) -> str:
        """Map an operator_hint free-text note to a maneuver.

        This is the third-priority fallback in _compute_commands (below an explicit
        order.kind / strategy.maneuver). It reuses the alias table declared in
        DECISION_SCHEMA["choices"] so the whisper bridge and this hint path recognise
        exactly the same vocabulary — a user's natural-language order produces the
        same maneuver whether it arrives as a structured whisper intent or as a raw
        operator_hint string. Long aliases win over short ones (avoids e.g. "r" or
        generic substrings stealing the match).
        """
        if not isinstance(hint, str):
            return ""
        text = hint.lower()
        choices = cls.DECISION_SCHEMA.get("choices") or {}
        # (alias, maneuver) pairs, longest alias first.
        pairs = sorted(
            ((alias.lower(), maneuver) for maneuver, aliases in choices.items() for alias in aliases),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for alias, maneuver in pairs:
            if alias and alias in text:
                return maneuver
        return ""

    @staticmethod
    def _axis_cell(value: int, limit: int, team: str) -> int:
        """Map a sub-cell coordinate to a navigation cell with mirrored tie-breaking.

        At an exact cell boundary there are two equally-near centres.  Choosing the
        numerically larger cell for both teams breaks 180° symmetry.  Guest chooses the
        lower cell while Host chooses the higher one, so mirrored positions always map
        to mirrored navigation cells.
        """
        cell = int(value) // SCALE
        if int(value) % SCALE == 0 and team == "guest":
            cell -= 1
        return max(1, min(limit - 2, cell))

    @classmethod
    def _tank_cell(cls, state: dict[str, Any], tank: dict[str, Any]) -> tuple[int, int]:
        return (
            cls._axis_cell(int(tank["y"]), int(state["rows"]), str(tank["team"])),
            cls._axis_cell(int(tank["x"]), int(state["cols"]), str(tank["team"])),
        )

    @staticmethod
    def _canonical_cell(
        state: dict[str, Any],
        team: str,
        cell: tuple[int, int],
    ) -> tuple[int, int]:
        if team == "host":
            return cell
        return int(state["rows"]) - 1 - cell[0], int(state["cols"]) - 1 - cell[1]

    @staticmethod
    def _from_canonical_cell(
        state: dict[str, Any],
        team: str,
        cell: tuple[int, int],
    ) -> tuple[int, int]:
        if team == "host":
            return cell
        return int(state["rows"]) - 1 - cell[0], int(state["cols"]) - 1 - cell[1]

    @staticmethod
    def _unit_index(unit_id: Any) -> int:
        match = re.search(r"(\d+)$", str(unit_id))
        return int(match.group(1)) if match else 0

    def _auto_command(
        self,
        state: dict[str, Any],
        tank: dict[str, Any],
        index: int,
        maneuver: str,
        order: dict[str, Any],
        target_tick: int,
    ) -> dict[str, Any]:
        enemy_side = "guest" if tank["team"] == "host" else "host"
        enemies = sorted(
            (unit for unit in state["tanks"] if unit["team"] == enemy_side and unit["hp"] > 0),
            key=lambda unit: unit["id"],
        )
        if not enemies or maneuver == "hold":
            return {"unit_id": tank["id"], "move": "none", "aim": "none", "fire": False}

        def enemy_key(unit: dict[str, Any]) -> tuple[int, int, int, int]:
            cell = self._tank_cell(state, unit)
            canonical = self._canonical_cell(state, tank["team"], cell)
            return (
                abs(int(unit["x"]) - int(tank["x"])) + abs(int(unit["y"]) - int(tank["y"])),
                canonical[0],
                canonical[1],
                self._unit_index(unit["id"]),
            )

        nearest = min(
            enemies,
            key=enemy_key,
        )
        target_unit_id = str(order.get("target_unit_id") or "")
        designated = next((enemy for enemy in enemies if enemy["id"] == target_unit_id), None)
        if designated is not None:
            nearest = designated
        visible_enemies = [enemy for enemy in enemies if self._line_of_fire(state, tank, enemy)]
        if designated is not None and designated in visible_enemies:
            visible = designated
        else:
            visible = min(visible_enemies, key=enemy_key) if visible_enemies else None

        rows, cols = state["rows"], state["cols"]
        active_pickups = [item for item in state.get("pickups", []) if item.get("active")]
        pickup_target = None
        pickup_id = None
        explicit_target = order.get("target") if isinstance(order.get("target"), dict) else None
        patrol_target = None
        patrol_index = None
        waypoints = order.get("waypoints") if isinstance(order.get("waypoints"), list) else []
        valid_waypoints = [
            {
                "row": max(1, min(rows - 2, int(item.get("row", 1)))),
                "col": max(1, min(cols - 2, int(item.get("col", 1)))),
            }
            for item in waypoints
            if isinstance(item, dict)
        ]
        if maneuver == "patrol" and len(valid_waypoints) >= 2:
            patrol_key = (str(order.get("id") or "patrol"), tuple((item["row"], item["col"]) for item in valid_waypoints))
            progress = self._patrol_progress.setdefault(tank["id"], {"key": None, "index": 0})
            if progress.get("key") != patrol_key:
                progress.update(key=patrol_key, index=0)
            patrol_index = int(progress.get("index", 0)) % len(valid_waypoints)
            current_cell = self._tank_cell(state, tank)
            current_target = valid_waypoints[patrol_index]
            if current_cell == (current_target["row"], current_target["col"]):
                patrol_index = (patrol_index + 1) % len(valid_waypoints)
                progress["index"] = patrol_index
            patrol_target = valid_waypoints[patrol_index]
        if explicit_target is None and active_pickups and maneuver in ("assault", "surround"):
            max_hp = int(state["rules"]["balance"]["hp"])
            useful = [
                item
                for item in active_pickups
                if item.get("kind") != "repair" or tank["hp"] < max_hp
            ]
            if useful and (tank["hp"] < max_hp or index % 3 == 0):
                def pickup_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
                    cell = (int(item["y"]) // SCALE, int(item["x"]) // SCALE)
                    canonical = self._canonical_cell(state, tank["team"], cell)
                    return (
                        abs(int(item["x"]) - int(tank["x"])) + abs(int(item["y"]) - int(tank["y"])),
                        canonical[0],
                        canonical[1],
                        str(item.get("kind") or ""),
                    )

                candidate = min(
                    useful,
                    key=pickup_key,
                )
                pickup_distance = abs(candidate["x"] - tank["x"]) + abs(candidate["y"] - tank["y"])
                enemy_distance = abs(nearest["x"] - tank["x"]) + abs(nearest["y"] - tank["y"])
                if pickup_distance <= enemy_distance:
                    pickup_target = (candidate["y"] // SCALE, candidate["x"] // SCALE)
                    pickup_id = str(candidate.get("id") or "")

        enemy_cell = self._tank_cell(state, nearest)
        enemy_canonical = self._canonical_cell(state, tank["team"], enemy_cell)
        if patrol_target is not None:
            target = (patrol_target["row"], patrol_target["col"])
            target_key = (
                "patrol",
                str(order.get("id") or "order"),
                int(patrol_index or 0),
                target[0],
                target[1],
            )
        elif explicit_target is not None:
            target = (
                max(1, min(rows - 2, int(explicit_target.get("row", enemy_cell[0])))),
                max(1, min(cols - 2, int(explicit_target.get("col", enemy_cell[1])))),
            )
            target_key: tuple[Any, ...] = (
                "explicit",
                str(order.get("id") or "order"),
                target[0],
                target[1],
            )
        elif pickup_target is not None:
            target = pickup_target
            target_key = ("pickup", pickup_id)
        elif maneuver == "retreat":
            target = self._from_canonical_cell(
                state,
                tank["team"],
                (rows - 2, 1 + (index * 3) % max(1, cols - 2)),
            )
            target_key = ("retreat", index)
        elif maneuver == "defend":
            target = self._from_canonical_cell(
                state,
                tank["team"],
                (rows - 4, 1 + (index * 4) % max(1, cols - 2)),
            )
            target_key = ("defend", index)
        elif maneuver == "flank_left":
            target = self._from_canonical_cell(
                state,
                tank["team"],
                (enemy_canonical[0], 1),
            )
            target_key = ("flank_left", nearest["id"])
        elif maneuver == "flank_right":
            target = self._from_canonical_cell(
                state,
                tank["team"],
                (enemy_canonical[0], cols - 2),
            )
            target_key = ("flank_right", nearest["id"])
        elif maneuver == "surround":
            offsets = ((-2, -2), (-2, 0), (-2, 2), (0, -3), (0, 3), (2, -2), (2, 0), (2, 2))
            dr, dc = offsets[index % len(offsets)]
            canonical_target = (
                max(1, min(rows - 2, enemy_canonical[0] + dr)),
                max(1, min(cols - 2, enemy_canonical[1] + dc)),
            )
            target = self._from_canonical_cell(state, tank["team"], canonical_target)
            target_key = ("surround", nearest["id"], index)
        elif maneuver == "attack_unit" and designated is not None:
            target = enemy_cell
            target_key = ("attack_unit", designated["id"])
        else:
            target = enemy_cell
            target_key = ("assault", nearest["id"])

        move = self._path_direction(state, tank, target, target_key, target_tick)
        if visible is not None and maneuver in ("assault", "defend") and explicit_target is None:
            move = "none"
        aim = self._direction_to(tank, visible or nearest) if visible is not None else (
            move if move != "none" else self._direction_to(tank, nearest)
        )
        return {
            "unit_id": tank["id"],
            "move": move,
            "aim": aim,
            "fire": visible is not None,
        }

    @staticmethod
    def _direction_to(tank: dict[str, Any], target: dict[str, Any]) -> str:
        dx, dy = target["x"] - tank["x"], target["y"] - tank["y"]
        if abs(dx) >= abs(dy):
            return "right" if dx > 0 else "left"
        return "down" if dy > 0 else "up"

    @staticmethod
    def _line_of_fire(state: dict[str, Any], tank: dict[str, Any], enemy: dict[str, Any]) -> bool:
        dx, dy = enemy["x"] - tank["x"], enemy["y"] - tank["y"]
        if abs(dx) < TANK_HALF:
            direction = 1 if dy > 0 else -1
            for y in range(tank["y"] + direction * SCALE // 2, enemy["y"], direction * SCALE // 2):
                if _tile_at(state["grid"], tank["x"], y) in (TILE_STEEL, TILE_BRICK):
                    return False
            return True
        if abs(dy) < TANK_HALF:
            direction = 1 if dx > 0 else -1
            for x in range(tank["x"] + direction * SCALE // 2, enemy["x"], direction * SCALE // 2):
                if _tile_at(state["grid"], x, tank["y"]) in (TILE_STEEL, TILE_BRICK):
                    return False
            return True
        return False

    @staticmethod
    def _navigation_order(team: str) -> tuple[str, ...]:
        return NAV_DIRECTION_ORDER if team == "host" else tuple(
            ROTATE_180[direction] for direction in NAV_DIRECTION_ORDER
        )

    def _plan_route(
        self,
        state: dict[str, Any],
        tank: dict[str, Any],
        start: tuple[int, int],
        target: tuple[int, int],
        memory: dict[str, Any],
        *,
        banned_first: str | None = None,
    ) -> list[tuple[int, int]]:
        """Weighted A* with mirrored tie-breaking, congestion and turn costs."""
        rows, cols = int(state["rows"]), int(state["cols"])
        target = (
            max(1, min(rows - 2, int(target[0]))),
            max(1, min(cols - 2, int(target[1]))),
        )
        occupied: dict[tuple[int, int], dict[str, Any]] = {
            self._tank_cell(state, unit): unit
            for unit in state["tanks"]
            if int(unit.get("hp") or 0) > 0 and unit.get("id") != tank.get("id")
        }
        goals = {target}
        if state["grid"][target[0]][target[1]] in BLOCKS_MOVE or target in occupied:
            goals = set()
            for direction in self._navigation_order(str(tank["team"])):
                vx, vy = VEC[direction]
                candidate = target[0] + vy, target[1] + vx
                if (
                    1 <= candidate[0] < rows - 1
                    and 1 <= candidate[1] < cols - 1
                    and state["grid"][candidate[0]][candidate[1]] not in BLOCKS_MOVE
                    and candidate not in occupied
                ):
                    goals.add(candidate)
        if not goals:
            return [start]
        if start in goals:
            return [start]

        recent_counts: dict[tuple[int, int], int] = {}
        for cell in memory.get("recent_cells") or []:
            recent_counts[cell] = recent_counts.get(cell, 0) + 1
        path_order = self._navigation_order(str(tank["team"]))
        direction_rank = {direction: index for index, direction in enumerate(path_order)}
        last_nonzero = str(memory.get("last_nonzero") or "none")

        def heuristic(cell: tuple[int, int]) -> int:
            return min(abs(cell[0] - goal[0]) + abs(cell[1] - goal[1]) for goal in goals) * 10

        start_key = (start, "none")
        canonical_start = self._canonical_cell(state, str(tank["team"]), start)
        start_heuristic = heuristic(start)
        heap: list[tuple[int, int, int, int, int, int, int, int, int, str]] = [
            (
                start_heuristic * 6 // 5,
                start_heuristic,
                0,
                0,
                canonical_start[0],
                canonical_start[1],
                0,
                start[0],
                start[1],
                "none",
            )
        ]
        best: dict[tuple[tuple[int, int], str], int] = {start_key: 0}
        parents: dict[
            tuple[tuple[int, int], str],
            tuple[tuple[int, int], str] | None,
        ] = {start_key: None}
        while heap:
            _, _, cost, turns, _, _, _, row, col, incoming = heapq.heappop(heap)
            cell = (row, col)
            key = (cell, incoming)
            if cost != best.get(key):
                continue
            if cell in goals:
                route = [cell]
                cursor = key
                while parents[cursor] is not None:
                    cursor = parents[cursor]  # type: ignore[assignment]
                    route.append(cursor[0])
                route.reverse()
                return route

            for direction in path_order:
                if cell == start and banned_first == direction:
                    continue
                vx, vy = VEC[direction]
                nxt = row + vy, col + vx
                if not (1 <= nxt[0] < rows - 1 and 1 <= nxt[1] < cols - 1):
                    continue
                if state["grid"][nxt[0]][nxt[1]] in BLOCKS_MOVE:
                    continue
                step_cost = 10
                if nxt in occupied and nxt not in goals:
                    step_cost += 70
                if nxt in self._reserved_cells and nxt not in goals:
                    step_cost += 55
                step_cost += recent_counts.get(nxt, 0) * 6
                next_turns = turns
                if incoming != "none" and direction != incoming:
                    step_cost += 3
                    next_turns += 1
                if incoming != "none" and direction == ROTATE_180[incoming]:
                    step_cost += 18
                if cell == start and last_nonzero in CARDINAL:
                    if direction != last_nonzero:
                        step_cost += 2
                    if direction == ROTATE_180[last_nonzero]:
                        step_cost += 28
                next_cost = cost + step_cost
                next_key = (nxt, direction)
                if next_cost >= best.get(next_key, 1 << 60):
                    continue
                best[next_key] = next_cost
                parents[next_key] = key
                canonical = self._canonical_cell(state, str(tank["team"]), nxt)
                remaining = heuristic(nxt)
                heapq.heappush(
                    heap,
                    (
                        next_cost + remaining * 6 // 5,
                        remaining,
                        next_cost,
                        next_turns,
                        canonical[0],
                        canonical[1],
                        direction_rank[direction],
                        nxt[0],
                        nxt[1],
                        direction,
                    ),
                )
        return [start]

    def _path_direction(
        self,
        state: dict[str, Any],
        tank: dict[str, Any],
        target: tuple[int, int],
        target_key: tuple[Any, ...],
        target_tick: int,
    ) -> str:
        """Follow a committed cell route and break repeated two-edge oscillations."""
        start = self._tank_cell(state, tank)
        position = int(tank["x"]), int(tank["y"])
        memory = self._navigation.setdefault(
            str(tank["id"]),
            {
                "route": [],
                "target_key": None,
                "planned_target": None,
                "planned_at": -1,
                "last_position": None,
                "last_output": "none",
                "last_nonzero": "none",
                "stuck_ticks": 0,
                "blocked_ticks": 0,
                "recent_cells": deque(maxlen=14),
                "recent_moves": deque(maxlen=8),
                "yield_until": -1,
                "cycle_breaks": 0,
            },
        )
        if memory.get("last_position") == position and memory.get("last_output") in CARDINAL:
            memory["stuck_ticks"] = int(memory.get("stuck_ticks", 0)) + 1
        elif memory.get("last_position") != position:
            memory["stuck_ticks"] = 0
        recent_cells: deque[tuple[int, int]] = memory["recent_cells"]
        if not recent_cells or recent_cells[-1] != start:
            recent_cells.append(start)

        target_changed = memory.get("target_key") != target_key
        if target_changed:
            memory["target_key"] = target_key
            memory["route"] = []
            memory["blocked_ticks"] = 0
            memory["recent_moves"].clear()
        route = list(memory.get("route") or [])
        if start in route:
            route = route[route.index(start) :]
        else:
            route = []
        planned_target = memory.get("planned_target")
        target_drift = (
            abs(planned_target[0] - target[0]) + abs(planned_target[1] - target[1])
            if isinstance(planned_target, tuple)
            else 1 << 30
        )
        drift_threshold = max(4, min(12, max(1, len(route) // 4)))
        target_drifted = target_drift > drift_threshold and (
            (target_tick + self._unit_index(tank["id"])) % 4 == 0
        )
        route_invalid = any(
            state["grid"][row][col] in BLOCKS_MOVE
            for row, col in route[:4]
        )
        needs_plan = (
            not route
            or target_changed
            or target_drifted
            or route_invalid
            or int(memory.get("stuck_ticks", 0)) >= NAV_STUCK_REPLAN_TICKS
            or target_tick - int(memory.get("planned_at", -1)) >= NAV_ROUTE_TTL
        )
        if needs_plan:
            banned = None
            if int(memory.get("stuck_ticks", 0)) >= NAV_STUCK_REPLAN_TICKS:
                last_output = str(memory.get("last_output") or "none")
                banned = last_output if last_output in CARDINAL else None
            route = self._plan_route(
                state,
                tank,
                start,
                target,
                memory,
                banned_first=banned,
            )
            memory["planned_target"] = target
            # Spread periodic replans across ticks; a 32-unit, 128x128 match must not
            # put every A* search on the same 100 ms simulation deadline.
            memory["planned_at"] = target_tick + self._unit_index(tank["id"]) % 24
            memory["stuck_ticks"] = 0

        candidate = "none"
        next_cell = None
        if target_tick > int(memory.get("yield_until", -1)) and len(route) > 1:
            next_cell = route[1]
            occupied = {
                self._tank_cell(state, unit)
                for unit in state["tanks"]
                if int(unit.get("hp") or 0) > 0 and unit.get("id") != tank.get("id")
            }
            if next_cell in occupied or next_cell in self._reserved_cells:
                memory["blocked_ticks"] = int(memory.get("blocked_ticks", 0)) + 1
                if int(memory["blocked_ticks"]) >= NAV_STUCK_REPLAN_TICKS:
                    dr, dc = next_cell[0] - start[0], next_cell[1] - start[1]
                    blocked_direction = next(
                        (
                            direction
                            for direction in CARDINAL
                            if VEC[direction] == (dc, dr)
                        ),
                        None,
                    )
                    route = self._plan_route(
                        state,
                        tank,
                        start,
                        target,
                        memory,
                        banned_first=blocked_direction,
                    )
                    memory["blocked_ticks"] = 0
                    next_cell = route[1] if len(route) > 1 else None
            else:
                memory["blocked_ticks"] = 0

            if next_cell is not None and next_cell not in occupied and next_cell not in self._reserved_cells:
                dr, dc = next_cell[0] - start[0], next_cell[1] - start[1]
                route_direction = next(
                    (
                        direction
                        for direction in CARDINAL
                        if VEC[direction] == (dc, dr)
                    ),
                    "none",
                )
                row_center = start[0] * SCALE + SCALE // 2
                col_center = start[1] * SCALE + SCALE // 2
                lane_tolerance = SCALE // 2 - TANK_HALF
                if route_direction in ("left", "right"):
                    offset = int(tank["y"]) - row_center
                    candidate = (
                        "up" if offset > 0 else "down"
                    ) if abs(offset) > lane_tolerance else route_direction
                elif route_direction in ("up", "down"):
                    offset = int(tank["x"]) - col_center
                    candidate = (
                        "left" if offset > 0 else "right"
                    ) if abs(offset) > lane_tolerance else route_direction

        history = list(memory["recent_moves"])
        if (
            candidate in CARDINAL
            and len(history) >= 3
            and history[-3] == history[-1]
            and history[-2] == candidate
            and history[-1] == ROTATE_180[candidate]
        ):
            # A,B,A,B is a genuine two-edge loop. Yield briefly and force a route with
            # fresh congestion/history costs rather than spending the match vibrating.
            candidate = "none"
            route = []
            memory["yield_until"] = target_tick + 2
            memory["cycle_breaks"] = int(memory.get("cycle_breaks", 0)) + 1

        memory["route"] = route
        memory["last_position"] = position
        memory["last_output"] = candidate
        memory["recent_moves"].append(candidate)
        if candidate in CARDINAL:
            memory["last_nonzero"] = candidate
            if next_cell is not None:
                self._reserved_cells.add(next_cell)
        return candidate
