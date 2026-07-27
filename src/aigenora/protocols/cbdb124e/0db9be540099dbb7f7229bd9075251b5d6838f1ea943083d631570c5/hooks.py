from __future__ import annotations

import copy
import secrets

from aigenora.proto.hooks import ProtocolHooks
from aigenora.proto.shared_deck import (
    create_shared_deck,
    discard_cards,
    draw_cards,
    move_zone_to_discard,
    private_deck_view,
    put_in_zone,
    take_from_hand,
)


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")

    def proto_host_metadata(self):
        return (
            "Aether Sigil",
            "game,cards,multiplayer,shared-deck,original",
            "supply",
            {"starting_life": 20},
        )

    def proto_group_initial_state(self, members):
        return self._new_match(members, restart_count=0, recovery_notice="")

    def proto_group_member_joined(self, state, member):
        return {
            "state": state,
            "events": [
                {
                    "kind": "member_rejoined",
                    "public_key": member["public_key"],
                    "seat": member["seat"],
                }
            ],
        }

    def proto_group_member_left(self, state, member, reason):
        return {
            "state": state,
            "events": [
                {
                    "kind": "member_disconnected",
                    "public_key": member["public_key"],
                    "reason": reason,
                }
            ],
        }

    def proto_group_handle(self, state, actor, action):
        if state["phase"] != "playing":
            raise ValueError("the match is complete")
        public_key = actor["public_key"]
        if public_key != state["current_player"]:
            raise ValueError("it is not this Member's turn")
        kind = action.get("kind")
        if kind == "play_card":
            return self._play_card(state, actor, action)
        if kind == "attack":
            return self._attack(state, actor, action)
        if kind == "end_turn":
            return self._end_turn(state, actor)
        if kind == "concede":
            return self._concede(state, actor)
        raise ValueError("unsupported Aether Sigil action")

    def proto_group_view(self, state, viewer):
        deck_view = private_deck_view(state["deck"], viewer["public_key"])
        return {
            "phase": state["phase"],
            "restart_count": state["restart_count"],
            "recovery_notice": state["recovery_notice"],
            "turn_number": state["turn_number"],
            "turn_order": state["turn_order"],
            "current_player": state["current_player"],
            "players": state["players"],
            "units": deck_view["zones"].get("units", {}),
            "relics": deck_view["zones"].get("relics", {}),
            "unit_state": state["unit_state"],
            "draw_count": deck_view["draw_count"],
            "discard": deck_view["discard"],
            "hand_counts": deck_view["hand_counts"],
            "my_hand": deck_view["my_hand"],
            "winner": state["winner"],
            "you": {
                "public_key": viewer["public_key"],
                "seat": viewer["seat"],
            },
        }

    def proto_group_recovery_snapshot(self, state):
        return {
            "restart_count": state["restart_count"],
            "public_outcome": state["winner"],
        }

    def proto_group_restore(self, checkpoint, members, new_epoch):
        del new_epoch
        return self._new_match(
            members,
            restart_count=int(checkpoint.get("restart_count", 0)) + 1,
            recovery_notice="The previous hidden deal was discarded after a Leader change.",
        )

    def proto_group_on_leader_changed(self, state, old_leader, new_leader):
        state["recovery_notice"] = (
            f"Leader changed from {old_leader[:8]} to {new_leader[:8]}; "
            "a fresh shared deck was dealt."
        )
        return {
            "state": state,
            "events": [
                {
                    "kind": "match_restarted_after_leader_change",
                    "old_leader": old_leader,
                    "new_leader": new_leader,
                    "restart_count": state["restart_count"],
                }
            ],
        }

    def _new_match(self, members, *, restart_count, recovery_notice):
        active = sorted(
            [member for member in members if member.get("status") == "active"],
            key=lambda member: member["seat"],
        )
        if len(active) != 4:
            raise ValueError("Aether Sigil requires exactly four active Members")
        configured_seed = self.options.get("deal_seed")
        seed = (
            f"{configured_seed}:{restart_count}"
            if isinstance(configured_seed, str) and configured_seed
            else secrets.token_hex(24)
        )
        starting_life = int(self.options.get("starting_life", 20))
        if starting_life < 10 or starting_life > 50:
            raise ValueError("starting_life must be between 10 and 50")
        deck = create_shared_deck(
            self._card_faces(),
            active,
            hand_size=5,
            seed=seed,
            copies=4,
        )
        order = [member["public_key"] for member in active]
        return {
            "phase": "playing",
            "restart_count": restart_count,
            "recovery_notice": recovery_notice,
            "turn_number": 1,
            "turn_order": order,
            "current_player": order[0],
            "players": {
                member["public_key"]: {
                    "seat": member["seat"],
                    "life": starting_life,
                    "shield": 0,
                    "energy": 1,
                    "max_energy": 1,
                    "fatigue": 0,
                    "defeated": False,
                }
                for member in active
            },
            "deck": deck,
            "unit_state": {},
            "winner": "",
        }

    def _play_card(self, state, actor, action):
        public_key = actor["public_key"]
        card_id = action.get("card_id")
        card = self._card_in_hand(state, public_key, card_id)
        player = state["players"][public_key]
        cost = int(card["cost"])
        if player["energy"] < cost:
            raise ValueError("not enough Aether")
        card_type = card["card_type"]
        events = []
        if card_type == "unit":
            units = state["deck"]["zones"].get("units", {}).get(public_key, [])
            if len(units) >= 3:
                raise ValueError("the unit row is full")
            take_from_hand(state["deck"], public_key, [card_id])
            put_in_zone(state["deck"], "units", public_key, [card_id])
            state["unit_state"][card_id] = {
                "damage": 0,
                "exhausted": True,
                "power_bonus": self._relic_total(
                    state, public_key, "unit_power"
                ),
            }
            events.append(
                {
                    "kind": "unit_summoned",
                    "public_key": public_key,
                    "card": card,
                }
            )
        elif card_type == "relic":
            relics = state["deck"]["zones"].get("relics", {}).get(public_key, [])
            if len(relics) >= 2:
                raise ValueError("the relic row is full")
            take_from_hand(state["deck"], public_key, [card_id])
            put_in_zone(state["deck"], "relics", public_key, [card_id])
            if card["effect"] == "max_energy":
                player["max_energy"] = min(
                    10, player["max_energy"] + int(card["amount"])
                )
            events.append(
                {
                    "kind": "relic_played",
                    "public_key": public_key,
                    "card": card,
                }
            )
        elif card_type == "spell":
            events.extend(self._resolve_spell(state, actor, action, card))
            take_from_hand(state["deck"], public_key, [card_id])
            discard_cards(state["deck"], [card_id])
            events.insert(
                0,
                {
                    "kind": "spell_cast",
                    "public_key": public_key,
                    "card": card,
                },
            )
        else:
            raise ValueError("unknown card type")
        player["energy"] -= cost
        completed = self._winner_if_finished(state)
        if not completed and state["players"][public_key]["defeated"]:
            events.extend(self._begin_viable_turn(state, public_key))
            completed = self._winner_if_finished(state)
        if completed:
            events.append({"kind": "game_over", "winner": completed})
            return {
                "state": state,
                "events": events,
                "completed": True,
                "outcome": completed,
            }
        return {"state": state, "events": events}

    def _resolve_spell(self, state, actor, action, card):
        public_key = actor["public_key"]
        effect = card["effect"]
        amount = int(card.get("amount", 0))
        if effect == "damage_player":
            target = self._active_opponent(
                state, public_key, action.get("target_public_key")
            )
            amount += self._relic_total(state, public_key, "spell_power")
            dealt = self._damage_player(state, target, amount)
            return [
                {
                    "kind": "spell_damage",
                    "target": target,
                    "amount": dealt,
                }
            ]
        if effect == "heal_self":
            player = state["players"][public_key]
            before = player["life"]
            player["life"] = min(int(self.options.get("starting_life", 20)), before + amount)
            return [
                {
                    "kind": "healed",
                    "public_key": public_key,
                    "amount": player["life"] - before,
                }
            ]
        if effect == "draw":
            return self._draw_or_fatigue(state, public_key, amount)
        if effect == "destroy_unit":
            target_card = action.get("target_card_id")
            owner, target = self._public_unit(state, target_card)
            if owner == public_key:
                raise ValueError("Dissolve must target an opposing unit")
            power = int(target["power"]) + int(
                state["unit_state"][target_card]["power_bonus"]
            )
            if power > int(card["max_power"]):
                raise ValueError("the target unit is too powerful for Dissolve")
            self._destroy_unit(state, owner, target_card)
            return [
                {
                    "kind": "unit_dissolved",
                    "owner": owner,
                    "card_id": target_card,
                }
            ]
        if effect == "shield_self":
            state["players"][public_key]["shield"] += amount
            return [
                {
                    "kind": "shield_gained",
                    "public_key": public_key,
                    "amount": amount,
                }
            ]
        if effect == "ready_all":
            for card_id in (
                state["deck"]["zones"].get("units", {}).get(public_key, [])
            ):
                state["unit_state"][card_id]["exhausted"] = False
            return [{"kind": "units_readied", "public_key": public_key}]
        raise ValueError("unknown spell effect")

    def _attack(self, state, actor, action):
        public_key = actor["public_key"]
        unit_id = action.get("unit_id")
        units = state["deck"]["zones"].get("units", {}).get(public_key, [])
        if unit_id not in units:
            raise ValueError("unit_id is not controlled by this Member")
        unit_state = state["unit_state"][unit_id]
        if unit_state["exhausted"]:
            raise ValueError("this unit is exhausted")
        attacker = state["deck"]["catalog"][unit_id]
        attack_power = int(attacker["power"]) + int(unit_state["power_bonus"])
        target_card_id = action.get("target_card_id")
        events = []
        if target_card_id:
            owner, defender = self._public_unit(state, target_card_id)
            if owner == public_key:
                raise ValueError("a unit cannot attack an allied unit")
            defender_state = state["unit_state"][target_card_id]
            defend_power = int(defender["power"]) + int(
                defender_state["power_bonus"]
            )
            defender_state["damage"] += attack_power
            unit_state["damage"] += defend_power
            events.append(
                {
                    "kind": "unit_combat",
                    "attacker": unit_id,
                    "defender": target_card_id,
                    "attack_power": attack_power,
                    "counter_power": defend_power,
                }
            )
            if defender_state["damage"] >= int(defender["guard"]):
                self._destroy_unit(state, owner, target_card_id)
                events.append({"kind": "unit_destroyed", "card_id": target_card_id})
            if unit_state["damage"] >= int(attacker["guard"]):
                self._destroy_unit(state, public_key, unit_id)
                events.append({"kind": "unit_destroyed", "card_id": unit_id})
            else:
                unit_state["exhausted"] = True
        else:
            target = self._active_opponent(
                state, public_key, action.get("target_public_key")
            )
            dealt = self._damage_player(state, target, attack_power)
            unit_state["exhausted"] = True
            events.append(
                {
                    "kind": "direct_attack",
                    "attacker": unit_id,
                    "target": target,
                    "amount": dealt,
                }
            )
        winner = self._winner_if_finished(state)
        if winner:
            events.append({"kind": "game_over", "winner": winner})
            return {
                "state": state,
                "events": events,
                "completed": True,
                "outcome": winner,
            }
        return {"state": state, "events": events}

    def _end_turn(self, state, actor):
        public_key = actor["public_key"]
        healing = self._relic_total(state, public_key, "end_heal")
        events = []
        if healing:
            player = state["players"][public_key]
            cap = int(self.options.get("starting_life", 20))
            before = player["life"]
            player["life"] = min(cap, player["life"] + healing)
            events.append(
                {
                    "kind": "relic_heal",
                    "public_key": public_key,
                    "amount": player["life"] - before,
                }
            )
        state["turn_number"] += 1
        events.extend(self._begin_viable_turn(state, public_key))
        winner = self._winner_if_finished(state)
        if winner:
            events.append({"kind": "game_over", "winner": winner})
            return {
                "state": state,
                "events": events,
                "completed": True,
                "outcome": winner,
            }
        return {"state": state, "events": events}

    def _concede(self, state, actor):
        public_key = actor["public_key"]
        state["players"][public_key]["defeated"] = True
        state["players"][public_key]["life"] = 0
        events = [{"kind": "conceded", "public_key": public_key}]
        winner = self._winner_if_finished(state)
        if winner:
            events.append({"kind": "game_over", "winner": winner})
            return {
                "state": state,
                "events": events,
                "completed": True,
                "outcome": winner,
            }
        events.extend(self._begin_viable_turn(state, public_key))
        return {"state": state, "events": events}

    def _begin_viable_turn(self, state, after_public_key):
        events = []
        candidate = self._next_active(state, after_public_key)
        for _ in state["turn_order"]:
            state["current_player"] = candidate
            events.extend(self._begin_turn(state, candidate))
            if not state["players"][candidate]["defeated"]:
                return events
            if self._winner_if_finished(state):
                return events
            candidate = self._next_active(state, candidate)
        return events

    def _begin_turn(self, state, public_key):
        player = state["players"][public_key]
        player["max_energy"] = min(10, player["max_energy"] + 1)
        player["energy"] = player["max_energy"]
        for card_id in state["deck"]["zones"].get("units", {}).get(public_key, []):
            state["unit_state"][card_id]["exhausted"] = False
        return [
            {"kind": "turn_started", "public_key": public_key}
        ] + self._draw_or_fatigue(state, public_key, 1)

    def _draw_or_fatigue(self, state, public_key, count):
        events = []
        for _ in range(count):
            if state["deck"]["draw_pile"]:
                draw_cards(state["deck"], public_key, 1)
                events.append({"kind": "card_drawn", "public_key": public_key})
            else:
                player = state["players"][public_key]
                player["fatigue"] += 1
                dealt = self._damage_player(state, public_key, player["fatigue"])
                events.append(
                    {
                        "kind": "fatigue",
                        "public_key": public_key,
                        "amount": dealt,
                    }
                )
        return events

    def _damage_player(self, state, public_key, amount):
        player = state["players"][public_key]
        absorbed = min(player["shield"], amount)
        player["shield"] -= absorbed
        damage = amount - absorbed
        player["life"] = max(0, player["life"] - damage)
        if player["life"] == 0:
            player["defeated"] = True
        return damage

    def _winner_if_finished(self, state):
        active = [
            public_key
            for public_key in state["turn_order"]
            if not state["players"][public_key]["defeated"]
        ]
        if len(active) == 1:
            state["phase"] = "completed"
            state["winner"] = active[0]
            return active[0]
        return ""

    def _next_active(self, state, public_key):
        order = state["turn_order"]
        index = order.index(public_key)
        for offset in range(1, len(order) + 1):
            candidate = order[(index + offset) % len(order)]
            if not state["players"][candidate]["defeated"]:
                return candidate
        raise ValueError("no active Member remains")

    def _active_opponent(self, state, actor, target):
        if (
            not isinstance(target, str)
            or target == actor
            or target not in state["players"]
            or state["players"][target]["defeated"]
        ):
            raise ValueError("target_public_key must identify an active opponent")
        return target

    def _card_in_hand(self, state, public_key, card_id):
        if not isinstance(card_id, str):
            raise ValueError("card_id must be text")
        if card_id not in state["deck"]["hands"][public_key]:
            raise ValueError("card is not in this Member's hand")
        return copy.deepcopy(state["deck"]["catalog"][card_id])

    def _public_unit(self, state, card_id):
        if not isinstance(card_id, str):
            raise ValueError("target_card_id must be text")
        for owner, cards in state["deck"]["zones"].get("units", {}).items():
            if card_id in cards:
                return owner, state["deck"]["catalog"][card_id]
        raise ValueError("target unit does not exist")

    def _destroy_unit(self, state, owner, card_id):
        move_zone_to_discard(state["deck"], "units", owner, card_id)
        state["unit_state"].pop(card_id, None)

    def _relic_total(self, state, public_key, effect):
        total = 0
        for card_id in state["deck"]["zones"].get("relics", {}).get(public_key, []):
            card = state["deck"]["catalog"][card_id]
            if card.get("effect") == effect:
                total += int(card.get("amount", 0))
        return total

    def _card_faces(self):
        return [
            {"name": "Dawnling", "card_type": "unit", "cost": 1, "power": 2, "guard": 1},
            {"name": "Iron Finch", "card_type": "unit", "cost": 1, "power": 1, "guard": 2},
            {"name": "Glass Hound", "card_type": "unit", "cost": 2, "power": 3, "guard": 2},
            {"name": "Tide Scribe", "card_type": "unit", "cost": 2, "power": 2, "guard": 3},
            {"name": "Aether Guard", "card_type": "unit", "cost": 3, "power": 2, "guard": 5},
            {"name": "Void Lynx", "card_type": "unit", "cost": 3, "power": 4, "guard": 2},
            {"name": "Verdant Giant", "card_type": "unit", "cost": 4, "power": 4, "guard": 6},
            {"name": "Ember Drake", "card_type": "unit", "cost": 5, "power": 6, "guard": 4},
            {"name": "Cinder Shot", "card_type": "spell", "cost": 1, "effect": "damage_player", "amount": 2},
            {"name": "Mending Rain", "card_type": "spell", "cost": 2, "effect": "heal_self", "amount": 4},
            {"name": "Star Map", "card_type": "spell", "cost": 2, "effect": "draw", "amount": 2},
            {"name": "Dissolve", "card_type": "spell", "cost": 3, "effect": "destroy_unit", "max_power": 3},
            {"name": "Pulse Shield", "card_type": "spell", "cost": 2, "effect": "shield_self", "amount": 4},
            {"name": "Rally Spark", "card_type": "spell", "cost": 2, "effect": "ready_all", "amount": 0},
            {"name": "Sun Dial", "card_type": "relic", "cost": 2, "effect": "end_heal", "amount": 1},
            {"name": "Prism Lens", "card_type": "relic", "cost": 3, "effect": "spell_power", "amount": 1},
            {"name": "Living Forge", "card_type": "relic", "cost": 3, "effect": "unit_power", "amount": 1},
            {"name": "Deep Reservoir", "card_type": "relic", "cost": 2, "effect": "max_energy", "amount": 1}
        ]
