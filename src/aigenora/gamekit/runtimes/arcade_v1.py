from __future__ import annotations

import copy
import importlib.util
import sys
from importlib import resources
from pathlib import Path
from typing import Any


_TANK_PROTOCOL_PATH = (
    "protocols/24765ced/"
    "b54ad04ad234c8d5a65f56825fedecb1f12cd296a175d658cb4c802d/hooks.py"
)


def _load_reference_module():
    asset = resources.files("aigenora").joinpath(*_TANK_PROTOCOL_PATH.split("/"))
    with resources.as_file(asset) as source_path:
        name = "_aigenora_gamekit_arcade_reference_v1"
        existing = sys.modules.get(name)
        if existing is not None:
            return existing
        spec = importlib.util.spec_from_file_location(name, Path(source_path))
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load the bundled arcade runtime")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


_reference = _load_reference_module()
_ReferenceHooks = _reference.Hooks


class ArcadeRuntimeV1(_ReferenceHooks):
    """Blueprint adapter around the frozen authoritative-realtime arena core."""

    BLUEPRINT: dict[str, Any] = {}

    def proto_init(
        self,
        options: dict[str, Any],
        role: str,
        args: list[str],
        state_dir: Path,
        decision_config: dict[str, Any] | None = None,
    ) -> None:
        del options
        blueprint = copy.deepcopy(self.BLUEPRINT)
        if blueprint.get("schema") != "aigenora-game-kit/1":
            raise ValueError("arcade blueprint schema is invalid")
        if blueprint.get("preset") != "arcade":
            raise ValueError("ArcadeRuntimeV1 requires the arcade preset")
        self._blueprint = blueprint
        arena = blueprint["arena"]
        victory = blueprint["victory"]
        runtime_options = {
            "balance": {"tank": copy.deepcopy(blueprint["balance"])},
            "team_size": int(arena["team_size"]),
            "max_ticks": int(victory["max_ticks"]),
            "map_text": "\n".join(arena["map"]),
            "friendly_fire": bool(arena["friendly_fire"]),
            "pickup_mode": "powerups" if blueprint["pickups"] else "none",
            "powerup_duration_ticks": int(blueprint["pickup_duration_ticks"]),
        }
        super().proto_init(runtime_options, role, args, state_dir, decision_config)

    def proto_host_metadata(self):
        vehicle = self._blueprint["vehicle"]
        return (
            self._blueprint["name"],
            f"game,game-kit,arcade,realtime,{vehicle['kind']}",
            "supply",
            {
                "preset": "arcade",
                "vehicle": vehicle["kind"],
                "team_size": self.team_size,
                "max_ticks": self.max_ticks,
                "pickup_mode": self.pickup_mode,
            },
        )

    def proto_realtime_initial_state(self) -> dict[str, Any]:
        world = super().proto_realtime_initial_state()
        world["game_blueprint"] = copy.deepcopy(self._blueprint)
        world["terrain"] = [list(row) for row in self._blueprint["arena"]["map"]]
        world["unit_kind"] = self._blueprint["vehicle"]["kind"]
        world["score"] = {"host": 0, "guest": 0}
        world["victory"] = copy.deepcopy(self._blueprint["victory"])
        for unit in world["tanks"]:
            unit["shield_hits"] = 0
        pickup_rotation = list(self._blueprint["pickups"])
        if pickup_rotation:
            for index, pickup in enumerate(world["pickups"]):
                pickup["kind"] = pickup_rotation[index % len(pickup_rotation)]
        if self._blueprint["vehicle"]["movement"] == "air":
            for row_index in range(1, len(world["grid"]) - 1):
                for col_index in range(1, len(world["grid"][row_index]) - 1):
                    if world["grid"][row_index][col_index] in {
                        _reference.TILE_BRICK,
                        _reference.TILE_WATER,
                    }:
                        world["grid"][row_index][col_index] = _reference.TILE_FLOOR
        self.world = world
        return world

    def _apply_shields(self, state: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        units = {unit["id"]: unit for unit in state["tanks"]}
        protected: set[str] = set()
        rewritten: list[dict[str, Any]] = []
        for event in events:
            current = dict(event)
            if current.get("type") == "pickup" and current.get("kind") == "shield":
                unit = units.get(str(current.get("tank")))
                if unit is not None:
                    unit["shield_hits"] = min(6, int(unit.get("shield_hits", 0)) + 3)
            elif current.get("type") == "hit":
                unit = units.get(str(current.get("victim")))
                if unit is not None and int(unit.get("shield_hits", 0)) > 0:
                    unit["shield_hits"] = int(unit["shield_hits"]) - 1
                    unit["hp"] = min(int(self.balance["hp"]), int(unit["hp"]) + 1)
                    current["type"] = "shield_block"
                    current["victim_hp"] = unit["hp"]
                    protected.add(unit["id"])
            if current.get("type") == "destroyed" and current.get("tank") in protected:
                continue
            if current.get("type") != "game_over":
                rewritten.append(current)
        return rewritten

    def _resolve_terminal(self, state: dict[str, Any], events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        host_alive = sum(
            1 for unit in state["tanks"] if unit["team"] == "host" and unit["hp"] > 0
        )
        guest_alive = sum(
            1 for unit in state["tanks"] if unit["team"] == "guest" and unit["hp"] > 0
        )
        state["host_alive"] = host_alive
        state["guest_alive"] = guest_alive
        team_size = int(state["rules"]["team_size"])
        score = {"host": team_size - guest_alive, "guest": team_size - host_alive}
        state["score"] = score
        victory = self._blueprint["victory"]
        reached_limit = int(state["tick"]) >= int(victory["max_ticks"])
        eliminated = host_alive == 0 or guest_alive == 0
        score_reached = (
            victory["mode"] == "first_to_score"
            and max(score.values()) >= int(victory["target_score"])
        )
        complete = eliminated or reached_limit or score_reached
        if not complete:
            state["game_over"] = False
            state["winner"] = "none"
            return events, "none"
        if host_alive == 0 and guest_alive == 0:
            winner = "draw"
        elif host_alive == 0:
            winner = "guest"
        elif guest_alive == 0:
            winner = "host"
        elif victory["mode"] == "first_to_score" and score["host"] != score["guest"]:
            winner = "host" if score["host"] > score["guest"] else "guest"
        else:
            host_rank = (
                host_alive,
                sum(unit["hp"] for unit in state["tanks"] if unit["team"] == "host"),
            )
            guest_rank = (
                guest_alive,
                sum(unit["hp"] for unit in state["tanks"] if unit["team"] == "guest"),
            )
            winner = "host" if host_rank > guest_rank else "guest" if guest_rank > host_rank else "draw"
        reason = "elimination" if eliminated else "score" if score_reached else "tick_limit"
        state["game_over"] = True
        state["winner"] = winner
        events.append({"type": "game_over", "winner": winner, "reason": reason})
        return events, winner

    def proto_realtime_step(
        self,
        state: dict[str, Any],
        tick: int,
        commands: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        result = super().proto_realtime_step(state, tick, commands)
        next_state = result["state"]
        events = self._apply_shields(next_state, list(result.get("events") or []))
        events, outcome = self._resolve_terminal(next_state, events)
        next_state["events"] = copy.deepcopy(events)
        self.world = next_state
        return {"state": next_state, "events": events, "outcome": outcome}

    def proto_realtime_snapshot(
        self, state: dict[str, Any], frame: dict[str, Any]
    ) -> dict[str, Any]:
        patch = super().proto_realtime_snapshot(state, frame)
        patch.update(
            game=copy.deepcopy(self._blueprint),
            vehicle=copy.deepcopy(self._blueprint["vehicle"]),
            score=copy.deepcopy(state.get("score") or {"host": 0, "guest": 0}),
            victory=copy.deepcopy(self._blueprint["victory"]),
        )
        events = frame.get("events") or []
        if events and events[-1].get("type") == "shield_block":
            patch["last_event"] = {
                "summary": "Shield core absorbed a direct hit",
                "structured": copy.deepcopy(events[-1]),
            }
        return patch

    def proto_realtime_audit_outcome(self, frame: dict[str, Any]) -> dict[str, Any]:
        state = frame["state"]
        expected = state.get("winner") if state.get("game_over") else "none"
        consistent = expected == frame.get("outcome")
        return {
            "status": "passed" if consistent else "failed",
            "check": "game_kit_arcade_terminal",
            "winner": expected,
            "score": copy.deepcopy(state.get("score") or {}),
        }
