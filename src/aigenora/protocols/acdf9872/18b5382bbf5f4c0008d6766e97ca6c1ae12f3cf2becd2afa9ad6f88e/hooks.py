from __future__ import annotations

import copy
import secrets
from collections import Counter

from aigenora.proto.card_games import standard_card_faces
from aigenora.proto.hooks import ProtocolHooks
from aigenora.proto.shared_deck import (
    create_shared_deck,
    discard_cards,
    draw_cards,
    move_hand_to_zone,
    private_deck_view,
    take_from_hand,
)
from aigenora.proto.tractor import (
    TractorRuleError,
    classify_tractor_play,
    tractor_card_strength,
    validate_tractor_follow,
    winning_tractor_play_index,
)


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")

    def proto_host_metadata(self):
        return (
            "Upgrade (Tractor)",
            "game,cards,multiplayer,upgrade,tractor,partnership",
            "supply",
            {},
        )

    def proto_group_initial_state(self, members):
        start_level = self._option_int("start_level", 2, 2, 14)
        target_level = self._option_int("target_level", 14, 3, 14)
        if target_level <= start_level:
            raise ValueError("target_level must be higher than start_level")
        self._option_int("max_hands", 16, 1, 64)
        active = self._active_members(members)
        order = [member["public_key"] for member in active]
        return self._new_hand(
            active,
            levels={"team0": start_level, "team1": start_level},
            declarer_team="team0",
            starter=order[0],
            hand_number=1,
            restart_count=0,
            match_points={"team0": 0, "team1": 0},
            recovery_notice="",
        )

    def proto_group_member_joined(self, state, member):
        return {
            "state": state,
            "events": [{
                "kind": "member_rejoined",
                "public_key": member["public_key"],
                "seat": member["seat"],
            }],
        }

    def proto_group_member_left(self, state, member, reason):
        return {
            "state": state,
            "events": [{
                "kind": "member_disconnected",
                "public_key": member["public_key"],
                "reason": reason,
            }],
        }

    def proto_group_handle(self, state, actor, action):
        if state["phase"] == "completed":
            raise ValueError("the Upgrade match is complete")
        if state["phase"] == "declaration":
            return self._handle_declaration(state, actor, action)
        if actor["public_key"] != state["current_player"]:
            raise ValueError("it is not this Member's turn")
        if state["phase"] == "bury":
            return self._handle_bury(state, actor, action)
        if state["phase"] == "playing":
            return self._handle_play(state, actor, action)
        raise ValueError("the Upgrade table is not accepting actions")

    def proto_group_view(self, state, viewer):
        deck_view = private_deck_view(
            state["deck"],
            viewer["public_key"],
            hidden_zones={"kitty"},
        )
        return {
            "phase": state["phase"],
            "hand_number": state["hand_number"],
            "restart_count": state["restart_count"],
            "recovery_notice": state["recovery_notice"],
            "players": state["players"],
            "turn_order": state["turn_order"],
            "current_player": state["current_player"],
            "starter": state["starter"],
            "declarer": state["declarer"],
            "declarer_team": state["declarer_team"],
            "levels": state["levels"],
            "level_rank": state["level_rank"],
            "trump_suit": state["trump_suit"],
            "declarations": state["declarations"],
            "winning_declaration": state["winning_declaration"],
            "kitty_count": state["kitty_count"],
            "kitty_revealed": state["kitty_revealed"],
            "current_trick": state["current_trick"],
            "trick_history": state["trick_history"],
            "captured_points": state["captured_points"],
            "defender_points": state["defender_points"],
            "match_points": state["match_points"],
            "my_hand": deck_view["my_hand"],
            "hand_counts": deck_view["hand_counts"],
            "last_result": state["last_result"],
            "you": {
                "public_key": viewer["public_key"],
                "seat": viewer["seat"],
                "team": self._team(int(viewer["seat"])),
                "is_declarer": viewer["public_key"] == state["declarer"],
            },
        }

    def proto_group_recovery_snapshot(self, state):
        return {
            "hand_number": state["hand_number"],
            "restart_count": state["restart_count"],
            "levels": state["levels"],
            "declarer_team": state["declarer_team"],
            "starter": state["starter"],
            "match_points": state["match_points"],
        }

    def proto_group_restore(self, checkpoint, members, new_epoch):
        del new_epoch
        active = self._active_members(members)
        order = [member["public_key"] for member in active]
        starter = checkpoint.get("starter")
        if starter not in order:
            starter = order[0]
        return self._new_hand(
            active,
            levels={
                "team0": int(checkpoint.get("levels", {}).get("team0", 2)),
                "team1": int(checkpoint.get("levels", {}).get("team1", 2)),
            },
            declarer_team=str(checkpoint.get("declarer_team", "team0")),
            starter=starter,
            hand_number=int(checkpoint.get("hand_number", 1)),
            restart_count=int(checkpoint.get("restart_count", 0)) + 1,
            match_points={
                "team0": int(
                    checkpoint.get("match_points", {}).get("team0", 0)
                ),
                "team1": int(
                    checkpoint.get("match_points", {}).get("team1", 0)
                ),
            },
            recovery_notice=(
                "The interrupted private deal and kitty were discarded after a Leader change."
            ),
        )

    def proto_group_on_leader_changed(self, state, old_leader, new_leader):
        state["recovery_notice"] = (
            f"Leader changed from {old_leader[:8]} to {new_leader[:8]}; "
            "the private hand was redealt."
        )
        return {
            "state": state,
            "events": [{
                "kind": "upgrade_hand_restarted_after_leader_change",
                "old_leader": old_leader,
                "new_leader": new_leader,
                "hand_number": state["hand_number"],
            }],
        }

    def _new_hand(
        self,
        members,
        *,
        levels,
        declarer_team,
        starter,
        hand_number,
        restart_count,
        match_points,
        recovery_notice,
    ):
        if len(members) != 4:
            raise ValueError("Upgrade requires exactly four active Members")
        order = [member["public_key"] for member in members]
        if starter not in order:
            raise ValueError("starter must be an active Member")
        if declarer_team not in {"team0", "team1"}:
            raise ValueError("declarer_team is invalid")
        level_rank = int(levels[declarer_team])
        configured_seed = self.options.get("deal_seed")
        seed = (
            f"{configured_seed}:{hand_number}:{restart_count}"
            if isinstance(configured_seed, str) and configured_seed
            else secrets.token_hex(24)
        )
        deck = create_shared_deck(
            standard_card_faces(include_jokers=True),
            members,
            hand_size=25,
            seed=seed,
            copies=2,
        )
        return {
            "phase": "declaration",
            "hand_number": int(hand_number),
            "restart_count": int(restart_count),
            "recovery_notice": recovery_notice,
            "players": {
                member["public_key"]: {
                    "seat": int(member["seat"]),
                    "team": self._team(int(member["seat"])),
                }
                for member in members
            },
            "turn_order": order,
            "current_player": "",
            "starter": starter,
            "declarer": "",
            "declarer_team": declarer_team,
            "levels": {
                "team0": int(levels["team0"]),
                "team1": int(levels["team1"]),
            },
            "level_rank": level_rank,
            "trump_suit": None,
            "declarations": {},
            "winning_declaration": None,
            "kitty_count": 8,
            "kitty_revealed": [],
            "current_trick": [],
            "trick_history": [],
            "captured_points": {"team0": 0, "team1": 0},
            "defender_points": 0,
            "match_points": {
                "team0": int(match_points["team0"]),
                "team1": int(match_points["team1"]),
            },
            "last_result": None,
            "deck": deck,
        }

    def _handle_declaration(self, state, actor, action):
        public_key = actor["public_key"]
        if public_key in state["declarations"]:
            raise ValueError("this Member has already declared or passed")
        kind = action.get("kind")
        if kind == "pass_declare":
            declaration = {
                "kind": "pass_declare",
                "public_key": public_key,
                "seat": int(actor["seat"]),
                "strength": 0,
            }
        elif kind == "declare_trump":
            card_ids = action.get("card_ids")
            cards = self._hand_cards(
                state,
                public_key,
                card_ids,
                minimum=1,
                maximum=2,
            )
            declaration = self._declaration_from_cards(
                state, actor, cards
            )
            declaration["card_ids"] = list(card_ids)
            declaration["cards"] = cards
            current_strength = (
                int(state["winning_declaration"]["strength"])
                if state["winning_declaration"]
                else 0
            )
            if int(declaration["strength"]) <= current_strength:
                raise ValueError("the declaration must be stronger than the current one")
            state["winning_declaration"] = copy.deepcopy(declaration)
        else:
            raise ValueError(
                "declaration accepts pass_declare or declare_trump"
            )
        state["declarations"][public_key] = declaration
        events = [{
            "kind": "upgrade_declaration",
            **copy.deepcopy(declaration),
        }]
        if len(state["declarations"]) < 4:
            return {"state": state, "events": events}
        self._finish_declaration(state)
        events.append({
            "kind": "upgrade_declarer_selected",
            "declarer": state["declarer"],
            "declarer_team": state["declarer_team"],
            "trump_suit": state["trump_suit"],
            "level_rank": state["level_rank"],
        })
        return {"state": state, "events": events}

    def _declaration_from_cards(self, state, actor, cards):
        if len(cards) == 1:
            card = cards[0]
            if int(card["rank"]) != state["level_rank"] or card["suit"] == "joker":
                raise ValueError("a single declaration must be one level card")
            strength = 1
            trump_suit = card["suit"]
        else:
            first, second = cards
            if (
                first["suit"] != second["suit"]
                or int(first["rank"]) != int(second["rank"])
            ):
                raise ValueError("a pair declaration needs identical card faces")
            rank = int(first["rank"])
            if rank == state["level_rank"] and first["suit"] != "joker":
                strength = 2
                trump_suit = first["suit"]
            elif rank == 16 and first["suit"] == "joker":
                strength = 3
                trump_suit = None
            elif rank == 17 and first["suit"] == "joker":
                strength = 4
                trump_suit = None
            else:
                raise ValueError(
                    "a pair declaration needs level cards or identical jokers"
                )
        return {
            "kind": "declare_trump",
            "public_key": actor["public_key"],
            "seat": int(actor["seat"]),
            "strength": strength,
            "trump_suit": trump_suit,
        }

    def _finish_declaration(self, state):
        winning = state["winning_declaration"]
        if winning:
            declarer = winning["public_key"]
            state["trump_suit"] = winning["trump_suit"]
        else:
            declarer = state["starter"]
            state["trump_suit"] = None
        state["declarer"] = declarer
        state["declarer_team"] = self._team_of_key(state, declarer)
        draw_cards(state["deck"], declarer, len(state["deck"]["draw_pile"]))
        state["phase"] = "bury"
        state["current_player"] = declarer

    def _handle_bury(self, state, actor, action):
        if action.get("kind") != "bury":
            raise ValueError("the declarer must bury exactly eight cards")
        card_ids = action.get("card_ids")
        self._hand_cards(
            state,
            actor["public_key"],
            card_ids,
            minimum=8,
            maximum=8,
        )
        protected = set(
            (state["winning_declaration"] or {}).get("card_ids", [])
        )
        if protected.intersection(card_ids):
            raise ValueError("the winning declaration cards cannot be buried")
        move_hand_to_zone(
            state["deck"],
            actor["public_key"],
            "kitty",
            "table",
            card_ids,
        )
        state["phase"] = "playing"
        state["current_player"] = actor["public_key"]
        return {
            "state": state,
            "events": [{
                "kind": "upgrade_kitty_buried",
                "declarer": actor["public_key"],
                "count": 8,
            }],
        }

    def _handle_play(self, state, actor, action):
        if action.get("kind") != "play":
            raise ValueError("trick play accepts only play")
        public_key = actor["public_key"]
        card_ids = action.get("card_ids")
        cards = self._hand_cards(
            state,
            public_key,
            card_ids,
            minimum=1,
            maximum=25,
        )
        context = {
            "level_rank": state["level_rank"],
            "trump_suit": state["trump_suit"],
        }
        if state["current_trick"]:
            lead = state["current_trick"][0]["shape"]
            hand = self._hand_faces(state, public_key)
            validate_tractor_follow(
                hand,
                cards,
                lead,
                **context,
            )
            shape = self._safe_shape(cards, context)
        else:
            shape = classify_tractor_play(cards, **context)
        take_from_hand(state["deck"], public_key, card_ids)
        discard_cards(state["deck"], card_ids)
        play = {
            "public_key": public_key,
            "seat": int(actor["seat"]),
            "team": self._team(int(actor["seat"])),
            "cards": cards,
            "shape": shape,
        }
        state["current_trick"].append(play)
        events = [{"kind": "upgrade_play", **copy.deepcopy(play)}]
        if len(state["current_trick"]) < 4:
            state["current_player"] = self._next_key(
                state["turn_order"], public_key
            )
            return {"state": state, "events": events}

        winner_index = self._trick_winner(state["current_trick"], context)
        winner = state["current_trick"][winner_index]["public_key"]
        winning_team = self._team_of_key(state, winner)
        points = sum(
            self._card_points(card)
            for entry in state["current_trick"]
            for card in entry["cards"]
        )
        state["captured_points"][winning_team] += points
        if winning_team != state["declarer_team"]:
            state["defender_points"] += points
        lead_count = int(state["current_trick"][0]["shape"]["count"])
        state["trick_history"].append({
            "number": len(state["trick_history"]) + 1,
            "winner": winner,
            "team": winning_team,
            "points": points,
            "plays": copy.deepcopy(state["current_trick"]),
        })
        state["current_trick"] = []
        events.append({
            "kind": "upgrade_trick_won",
            "winner": winner,
            "team": winning_team,
            "points": points,
            "trick_number": len(state["trick_history"]),
        })
        if all(
            not state["deck"]["hands"][key]
            for key in state["turn_order"]
        ):
            return self._finish_hand(
                state,
                final_winner=winner,
                final_lead_count=lead_count,
                events=events,
            )
        state["current_player"] = winner
        return {"state": state, "events": events}

    def _finish_hand(self, state, *, final_winner, final_lead_count, events):
        kitty_ids = state["deck"]["zones"]["kitty"]["table"]
        kitty = [
            copy.deepcopy(state["deck"]["catalog"][card_id])
            for card_id in kitty_ids
        ]
        state["kitty_revealed"] = kitty
        final_team = self._team_of_key(state, final_winner)
        kitty_points = sum(self._card_points(card) for card in kitty)
        kitty_multiplier = 2 * final_lead_count
        kitty_award = 0
        if final_team != state["declarer_team"]:
            kitty_award = kitty_points * kitty_multiplier
            state["captured_points"][final_team] += kitty_award
            state["defender_points"] += kitty_award
        defenders = (
            "team1" if state["declarer_team"] == "team0" else "team0"
        )
        defender_points = state["defender_points"]
        if defender_points >= 80:
            controlling_team = defenders
            level_steps = 1 + (defender_points - 80) // 40
        else:
            controlling_team = state["declarer_team"]
            level_steps = 3 if defender_points == 0 else (
                2 if defender_points < 40 else 1
            )
        target = self._option_int("target_level", 14, 3, 14)
        state["levels"][controlling_team] = min(
            target,
            int(state["levels"][controlling_team]) + level_steps,
        )
        for team, amount in state["captured_points"].items():
            state["match_points"][team] += int(amount)
        state["last_result"] = {
            "defender_points": defender_points,
            "kitty_points": kitty_points,
            "kitty_multiplier": kitty_multiplier,
            "kitty_award": kitty_award,
            "controlling_team": controlling_team,
            "level_steps": level_steps,
            "levels": copy.deepcopy(state["levels"]),
        }
        events.append({
            "kind": "upgrade_hand_complete",
            **copy.deepcopy(state["last_result"]),
        })
        max_hands = self._option_int("max_hands", 16, 1, 64)
        if (
            state["levels"][controlling_team] >= target
            or state["hand_number"] >= max_hands
        ):
            state["phase"] = "completed"
            events.append({
                "kind": "upgrade_match_complete",
                "winner": controlling_team,
                "levels": copy.deepcopy(state["levels"]),
            })
            return {
                "state": state,
                "events": events,
                "completed": True,
                "outcome": controlling_team,
            }
        members = [
            {
                "public_key": key,
                "seat": int(state["players"][key]["seat"]),
                "status": "active",
            }
            for key in state["turn_order"]
        ]
        candidates = [
            key for key in state["turn_order"]
            if self._team_of_key(state, key) == controlling_team
        ]
        starter = final_winner if final_winner in candidates else candidates[0]
        replacement = self._new_hand(
            members,
            levels=state["levels"],
            declarer_team=controlling_team,
            starter=starter,
            hand_number=state["hand_number"] + 1,
            restart_count=state["restart_count"],
            match_points=state["match_points"],
            recovery_notice=state["recovery_notice"],
        )
        replacement["last_result"] = state["last_result"]
        events.append({
            "kind": "upgrade_hand_started",
            "hand_number": replacement["hand_number"],
            "starter": replacement["starter"],
        })
        return {"state": replacement, "events": events}

    def _trick_winner(self, trick, context):
        lead_kind = trick[0]["shape"]["kind"]
        if lead_kind != "throw":
            winner = 0
            for index in range(1, len(trick)):
                try:
                    relative = winning_tractor_play_index(
                        [trick[winner]["cards"], trick[index]["cards"]],
                        **context,
                    )
                except TractorRuleError:
                    continue
                if relative == 1:
                    winner = index
            return winner
        winner = 0
        lead_suit = trick[0]["shape"]["suit"]
        for index in range(1, len(trick)):
            candidate = trick[index]
            suit = candidate["shape"].get("suit")
            winner_suit = trick[winner]["shape"].get("suit")
            if suit not in {lead_suit, "trump"}:
                continue
            if winner_suit != "trump" and suit == "trump":
                winner = index
                continue
            if suit == winner_suit and self._throw_strength(
                candidate["cards"], context
            ) > self._throw_strength(trick[winner]["cards"], context):
                winner = index
        return winner

    def _throw_strength(self, cards, context):
        return tuple(sorted(
            (
                tractor_card_strength(card, **context)
                for card in cards
            ),
            reverse=True,
        ))

    def _safe_shape(self, cards, context):
        try:
            return classify_tractor_play(cards, **context)
        except TractorRuleError:
            return {
                "kind": "mixed",
                "count": len(cards),
                "suit": "mixed",
                "pair_count": sum(
                    count // 2
                    for count in Counter(
                        (card["suit"], int(card["rank"]))
                        for card in cards
                    ).values()
                ),
            }

    def _card_points(self, card):
        rank = int(card["rank"])
        if rank == 5:
            return 5
        if rank in {10, 13}:
            return 10
        return 0

    def _hand_cards(
        self,
        state,
        public_key,
        card_ids,
        *,
        minimum,
        maximum,
    ):
        if (
            not isinstance(card_ids, list)
            or len(card_ids) < minimum
            or len(card_ids) > maximum
            or len(set(card_ids)) != len(card_ids)
            or any(not isinstance(card_id, str) for card_id in card_ids)
        ):
            raise ValueError(
                f"card_ids must contain {minimum}-{maximum} unique ids"
            )
        hand = state["deck"]["hands"][public_key]
        if any(card_id not in hand for card_id in card_ids):
            raise ValueError("a selected card is not in this Member's hand")
        return [
            copy.deepcopy(state["deck"]["catalog"][card_id])
            for card_id in card_ids
        ]

    def _hand_faces(self, state, public_key):
        return [
            state["deck"]["catalog"][card_id]
            for card_id in state["deck"]["hands"][public_key]
        ]

    def _next_key(self, order, public_key):
        return order[(order.index(public_key) + 1) % len(order)]

    def _team_of_key(self, state, public_key):
        return str(state["players"][public_key]["team"])

    def _team(self, seat):
        return "team0" if int(seat) % 2 == 0 else "team1"

    def _active_members(self, members):
        active = sorted(
            [member for member in members if member.get("status") == "active"],
            key=lambda member: int(member["seat"]),
        )
        if len(active) != 4:
            raise ValueError("Upgrade requires exactly four active Members")
        return active

    def _option_int(self, name, default, minimum, maximum):
        value = self.options.get(name, default)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < minimum
            or value > maximum
        ):
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return value
