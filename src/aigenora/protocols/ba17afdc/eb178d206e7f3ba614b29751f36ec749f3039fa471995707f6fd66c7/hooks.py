from __future__ import annotations

import copy
import secrets

from aigenora.proto.card_games import standard_card_faces
from aigenora.proto.hooks import ProtocolHooks
from aigenora.proto.poker import (
    best_poker_hand,
    build_side_pots,
    normalize_uncalled_contributions,
)
from aigenora.proto.shared_deck import (
    create_shared_deck,
    draw_cards,
    move_draw_to_zone,
    private_deck_view,
)


STREETS = ("preflop", "flop", "turn", "river")


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")

    def proto_host_metadata(self):
        return (
            "Four-seat No-limit Texas Hold'em",
            "game,cards,multiplayer,poker,holdem",
            "supply",
            {},
        )

    def proto_group_initial_state(self, members):
        stack = self._option_int("starting_stack", 1000, 100, 100000)
        small_blind = self._option_int("small_blind", 5, 1, 10000)
        big_blind = self._option_int("big_blind", 10, 2, 20000)
        if small_blind >= big_blind:
            raise ValueError("small_blind must be lower than big_blind")
        if big_blind > stack:
            raise ValueError("big_blind cannot exceed starting_stack")
        self._option_int("max_hands", 8, 1, 100)
        ordered = self._active_members(members)
        stacks = {member["public_key"]: stack for member in ordered}
        return self._new_hand(
            ordered,
            stacks=stacks,
            hand_number=1,
            button_index=0,
            restart_count=0,
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
            raise ValueError("the Hold'em match is complete")
        if actor["public_key"] != state["current_player"]:
            raise ValueError("it is not this Member's turn to act")
        if state["phase"] != "betting":
            raise ValueError("the table is not accepting betting actions")
        return self._handle_bet(state, actor, action)

    def proto_group_view(self, state, viewer):
        deck_view = private_deck_view(
            state["deck"],
            viewer["public_key"],
            hidden_zones={"burn"},
        )
        board = deck_view["zones"].get("board", {}).get("table", [])
        player_key = viewer["public_key"]
        player = state["players"][player_key]
        to_call = max(0, state["current_bet"] - player["street_contribution"])
        return {
            "phase": state["phase"],
            "street": state["street"],
            "hand_number": state["hand_number"],
            "restart_count": state["restart_count"],
            "recovery_notice": state["recovery_notice"],
            "turn_order": state["turn_order"],
            "players": state["players"],
            "button": state["button"],
            "small_blind_player": state["small_blind_player"],
            "big_blind_player": state["big_blind_player"],
            "current_player": state["current_player"],
            "current_bet": state["current_bet"],
            "last_full_raise": state["last_full_raise"],
            "pot_total": sum(state["contributions"].values()),
            "contributions": state["contributions"],
            "board": board,
            "board_count": len(board),
            "my_hand": deck_view["my_hand"],
            "hand_counts": deck_view["hand_counts"],
            "action_log": state["action_log"],
            "showdown_hands": state["showdown_hands"],
            "pot_results": state["pot_results"],
            "uncalled_refunds": state["uncalled_refunds"],
            "legal": {
                "to_call": to_call,
                "min_raise_to": state["current_bet"] + state["last_full_raise"],
                "max_to": player["street_contribution"] + player["stack"],
                "can_raise": (
                    player_key not in state["acted_since_full_raise"]
                    and player["stack"] > to_call
                ),
            },
            "you": {
                "public_key": player_key,
                "seat": viewer["seat"],
            },
        }

    def proto_group_recovery_snapshot(self, state):
        return {
            "hand_number": state["hand_number"],
            "button_index": state["button_index"],
            "restart_count": state["restart_count"],
            "hand_start_stacks": state["hand_start_stacks"],
        }

    def proto_group_restore(self, checkpoint, members, new_epoch):
        del new_epoch
        ordered = self._active_members(members)
        stacks = {
            member["public_key"]: int(
                checkpoint.get("hand_start_stacks", {}).get(
                    member["public_key"],
                    self._option_int("starting_stack", 1000, 100, 100000),
                )
            )
            for member in ordered
        }
        return self._new_hand(
            ordered,
            stacks=stacks,
            hand_number=int(checkpoint.get("hand_number", 1)),
            button_index=int(checkpoint.get("button_index", 0)),
            restart_count=int(checkpoint.get("restart_count", 0)) + 1,
            recovery_notice=(
                "The interrupted hand was refunded and redealt after a Leader change."
            ),
        )

    def proto_group_on_leader_changed(self, state, old_leader, new_leader):
        state["recovery_notice"] = (
            f"Leader changed from {old_leader[:8]} to {new_leader[:8]}; "
            "the interrupted hand was refunded."
        )
        return {
            "state": state,
            "events": [{
                "kind": "hand_restarted_after_leader_change",
                "old_leader": old_leader,
                "new_leader": new_leader,
                "hand_number": state["hand_number"],
            }],
        }

    def _new_hand(
        self,
        members,
        *,
        stacks,
        hand_number,
        button_index,
        restart_count,
        recovery_notice,
    ):
        if len(members) != 4:
            raise ValueError("Texas Hold'em requires exactly four active Members")
        order = [member["public_key"] for member in members]
        if any(key not in stacks or int(stacks[key]) < 0 for key in order):
            raise ValueError("every Member must have a non-negative stack")
        live = [key for key in order if int(stacks[key]) > 0]
        if len(live) < 2:
            raise ValueError("at least two Members need chips to start a hand")
        button_index %= 4
        while order[button_index] not in live:
            button_index = (button_index + 1) % 4
        button = order[button_index]
        small_blind_player = self._next_live(order, stacks, button)
        big_blind_player = self._next_live(order, stacks, small_blind_player)
        configured_seed = self.options.get("deal_seed")
        seed = (
            f"{configured_seed}:{hand_number}:{restart_count}"
            if isinstance(configured_seed, str) and configured_seed
            else secrets.token_hex(24)
        )
        deck = create_shared_deck(
            standard_card_faces(),
            members,
            hand_size=0,
            seed=seed,
        )
        for _ in range(2):
            for public_key in order:
                if public_key in live:
                    draw_cards(deck, public_key, 1)
        players = {
            member["public_key"]: {
                "seat": int(member["seat"]),
                "stack": int(stacks[member["public_key"]]),
                "folded": member["public_key"] not in live,
                "all_in": False,
                "street_contribution": 0,
                "total_contribution": 0,
            }
            for member in members
        }
        state = {
            "phase": "betting",
            "street": "preflop",
            "hand_number": int(hand_number),
            "button_index": button_index,
            "restart_count": int(restart_count),
            "recovery_notice": recovery_notice,
            "turn_order": order,
            "players": players,
            "button": button,
            "small_blind_player": small_blind_player,
            "big_blind_player": big_blind_player,
            "current_player": "",
            "current_bet": 0,
            "last_full_raise": self._big_blind(),
            "pending": [],
            "acted_since_full_raise": [],
            "contributions": {key: 0 for key in order},
            "hand_start_stacks": {
                key: int(stacks[key])
                for key in order
            },
            "action_log": [],
            "showdown_hands": {},
            "pot_results": [],
            "uncalled_refunds": {key: 0 for key in order},
            "deck": deck,
        }
        self._commit(state, small_blind_player, self._small_blind())
        self._commit(state, big_blind_player, self._big_blind())
        state["current_bet"] = max(
            players[small_blind_player]["street_contribution"],
            players[big_blind_player]["street_contribution"],
        )
        start = self._next_key(order, big_blind_player)
        state["pending"] = self._ordered_eligible(state, start)
        if not state["pending"]:
            return self._run_to_showdown_state(state)
        state["current_player"] = state["pending"][0]
        return state

    def _handle_bet(self, state, actor, action):
        public_key = actor["public_key"]
        player = state["players"][public_key]
        kind = action.get("kind")
        to_call = max(0, state["current_bet"] - player["street_contribution"])
        full_raise = False
        increased = False
        events = []

        if kind == "fold":
            player["folded"] = True
        elif kind == "check":
            if to_call != 0:
                raise ValueError("check is illegal while facing a wager")
        elif kind == "call":
            if to_call <= 0:
                raise ValueError("there is no wager to call")
            self._commit(state, public_key, min(to_call, player["stack"]))
        elif kind in {"bet", "raise"}:
            target = action.get("amount")
            if not isinstance(target, int) or isinstance(target, bool):
                raise ValueError("bet or raise amount must be an integer target")
            if kind == "bet" and state["current_bet"] != 0:
                raise ValueError("bet is legal only when no wager exists")
            if kind == "raise" and state["current_bet"] == 0:
                raise ValueError("use bet when no wager exists")
            full_raise, increased = self._raise_to(
                state,
                public_key,
                target,
            )
        elif kind == "all_in":
            target = player["street_contribution"] + player["stack"]
            if target <= state["current_bet"]:
                self._commit(state, public_key, player["stack"])
            else:
                full_raise, increased = self._raise_to(
                    state,
                    public_key,
                    target,
                    force_all_in=True,
                )
        else:
            raise ValueError(
                "betting accepts fold, check, call, bet, raise, or all_in"
            )

        log = {
            "street": state["street"],
            "public_key": public_key,
            "seat": int(actor["seat"]),
            "kind": kind,
            "amount": int(player["street_contribution"]),
            "stack": int(player["stack"]),
        }
        state["action_log"].append(log)
        events.append({"kind": "holdem_action", **copy.deepcopy(log)})

        remaining = [
            key
            for key in state["turn_order"]
            if not state["players"][key]["folded"]
        ]
        if len(remaining) == 1:
            return self._award_uncontested(state, remaining[0], events)

        if full_raise:
            state["acted_since_full_raise"] = [public_key]
            start = self._next_key(state["turn_order"], public_key)
            state["pending"] = [
                key
                for key in self._ordered_eligible(state, start)
                if key != public_key
            ]
        elif increased:
            if public_key not in state["acted_since_full_raise"]:
                state["acted_since_full_raise"].append(public_key)
            start = self._next_key(state["turn_order"], public_key)
            state["pending"] = [
                key
                for key in self._ordered_eligible(state, start)
                if state["players"][key]["street_contribution"]
                < state["current_bet"]
            ]
        else:
            if public_key not in state["acted_since_full_raise"]:
                state["acted_since_full_raise"].append(public_key)
            state["pending"] = [
                key for key in state["pending"]
                if key != public_key
                and not state["players"][key]["folded"]
                and not state["players"][key]["all_in"]
            ]

        if state["pending"]:
            state["current_player"] = state["pending"][0]
            return {"state": state, "events": events}
        return self._advance_street(state, events)

    def _raise_to(self, state, public_key, target, force_all_in=False):
        player = state["players"][public_key]
        maximum = player["street_contribution"] + player["stack"]
        if target <= state["current_bet"] or target > maximum:
            raise ValueError("raise target must exceed the wager and fit the stack")
        if public_key in state["acted_since_full_raise"]:
            raise ValueError("a short all-in did not reopen this Member's raise")
        raise_size = target - state["current_bet"]
        minimum = (
            self._big_blind()
            if state["current_bet"] == 0
            else state["last_full_raise"]
        )
        is_all_in = target == maximum
        if raise_size < minimum and not (is_all_in or force_all_in):
            raise ValueError("raise target is below the minimum full raise")
        self._commit(
            state,
            public_key,
            target - player["street_contribution"],
        )
        state["current_bet"] = target
        if raise_size >= minimum:
            state["last_full_raise"] = raise_size
            return True, True
        return False, True

    def _advance_street(self, state, events):
        eligible = [
            key
            for key in state["turn_order"]
            if not state["players"][key]["folded"]
            and not state["players"][key]["all_in"]
        ]
        street_index = STREETS.index(state["street"])
        if street_index == len(STREETS) - 1:
            return self._showdown(state, events)
        next_street = STREETS[street_index + 1]
        move_draw_to_zone(state["deck"], "burn", "table", 1)
        reveal_count = 3 if next_street == "flop" else 1
        revealed = move_draw_to_zone(
            state["deck"],
            "board",
            "table",
            reveal_count,
        )
        state["street"] = next_street
        state["current_bet"] = 0
        state["last_full_raise"] = self._big_blind()
        state["acted_since_full_raise"] = []
        for player in state["players"].values():
            player["street_contribution"] = 0
        events.append({
            "kind": "holdem_street",
            "street": next_street,
            "cards": revealed,
        })
        if len(eligible) <= 1:
            return self._run_remaining_board(state, events)
        start = self._next_key(state["turn_order"], state["button"])
        state["pending"] = self._ordered_eligible(state, start)
        state["current_player"] = state["pending"][0]
        return {"state": state, "events": events}

    def _run_remaining_board(self, state, events):
        while state["street"] != "river":
            street_index = STREETS.index(state["street"])
            next_street = STREETS[street_index + 1]
            move_draw_to_zone(state["deck"], "burn", "table", 1)
            revealed = move_draw_to_zone(
                state["deck"],
                "board",
                "table",
                3 if next_street == "flop" else 1,
            )
            state["street"] = next_street
            events.append({
                "kind": "holdem_street",
                "street": next_street,
                "cards": revealed,
            })
        return self._showdown(state, events)

    def _run_to_showdown_state(self, state):
        result = self._run_remaining_board(state, [])
        return result["state"]

    def _showdown(self, state, events):
        board_ids = state["deck"]["zones"].get("board", {}).get("table", [])
        board = [state["deck"]["catalog"][card_id] for card_id in board_ids]
        contenders = [
            key
            for key in state["turn_order"]
            if not state["players"][key]["folded"]
        ]
        ranks = {}
        for public_key in contenders:
            hole = [
                copy.deepcopy(state["deck"]["catalog"][card_id])
                for card_id in state["deck"]["hands"][public_key]
            ]
            result = best_poker_hand([*hole, *board])
            ranks[public_key] = tuple(result["rank"])
            state["showdown_hands"][public_key] = {
                "cards": hole,
                "category": result["category"],
                "rank": result["rank"],
            }
        normalized = normalize_uncalled_contributions(
            state["contributions"]
        )
        state["contributions"] = normalized["contributions"]
        state["uncalled_refunds"] = normalized["refunds"]
        for public_key, refund in state["uncalled_refunds"].items():
            if refund:
                state["players"][public_key]["stack"] += refund
                state["players"][public_key]["total_contribution"] -= refund
        pots = build_side_pots(
            state["contributions"],
            {
                key
                for key in state["turn_order"]
                if state["players"][key]["folded"]
            },
        )
        for index, pot in enumerate(pots):
            best_rank = max(ranks[key] for key in pot["eligible"])
            winners = [
                key for key in pot["eligible"]
                if ranks[key] == best_rank
            ]
            share, odd = divmod(int(pot["amount"]), len(winners))
            for public_key in winners:
                state["players"][public_key]["stack"] += share
            odd_order = [
                key
                for key in self._clockwise_from_button(state)
                if key in winners
            ]
            for public_key in odd_order[:odd]:
                state["players"][public_key]["stack"] += 1
            state["pot_results"].append({
                "pot_index": index,
                "amount": int(pot["amount"]),
                "eligible": pot["eligible"],
                "winners": winners,
                "share": share,
                "odd_chips": odd_order[:odd],
            })
        events.append({
            "kind": "holdem_showdown",
            "hands": copy.deepcopy(state["showdown_hands"]),
            "pots": copy.deepcopy(state["pot_results"]),
            "uncalled_refunds": copy.deepcopy(state["uncalled_refunds"]),
        })
        return self._finish_hand(state, events)

    def _award_uncontested(self, state, winner, events):
        amount = sum(state["contributions"].values())
        state["players"][winner]["stack"] += amount
        state["pot_results"] = [{
            "pot_index": 0,
            "amount": amount,
            "eligible": [winner],
            "winners": [winner],
            "share": amount,
            "odd_chips": [],
        }]
        events.append({
            "kind": "holdem_uncontested",
            "winner": winner,
            "amount": amount,
        })
        return self._finish_hand(state, events)

    def _finish_hand(self, state, events):
        stacks = {
            key: int(player["stack"])
            for key, player in state["players"].items()
        }
        max_hands = self._option_int("max_hands", 8, 1, 100)
        live = [key for key, amount in stacks.items() if amount > 0]
        if state["hand_number"] >= max_hands or len(live) <= 1:
            state["phase"] = "completed"
            winner = max(
                state["turn_order"],
                key=lambda key: (stacks[key], -state["turn_order"].index(key)),
            )
            events.append({
                "kind": "holdem_match_complete",
                "winner": winner,
                "stacks": stacks,
            })
            return {
                "state": state,
                "events": events,
                "completed": True,
                "outcome": winner,
            }
        members = [
            {
                "public_key": key,
                "seat": int(state["players"][key]["seat"]),
                "status": "active",
            }
            for key in state["turn_order"]
        ]
        next_button = (state["button_index"] + 1) % 4
        replacement = self._new_hand(
            members,
            stacks=stacks,
            hand_number=state["hand_number"] + 1,
            button_index=next_button,
            restart_count=state["restart_count"],
            recovery_notice=state["recovery_notice"],
        )
        events.append({
            "kind": "holdem_hand_started",
            "hand_number": replacement["hand_number"],
            "button": replacement["button"],
        })
        return {"state": replacement, "events": events}

    def _commit(self, state, public_key, amount):
        if amount < 0:
            raise ValueError("chip commitment cannot be negative")
        player = state["players"][public_key]
        committed = min(int(amount), int(player["stack"]))
        player["stack"] -= committed
        player["street_contribution"] += committed
        player["total_contribution"] += committed
        state["contributions"][public_key] += committed
        if player["stack"] == 0:
            player["all_in"] = True
        return committed

    def _ordered_eligible(self, state, start):
        order = state["turn_order"]
        start_index = order.index(start)
        rotated = order[start_index:] + order[:start_index]
        return [
            key
            for key in rotated
            if not state["players"][key]["folded"]
            and not state["players"][key]["all_in"]
        ]

    def _clockwise_from_button(self, state):
        order = state["turn_order"]
        start = self._next_key(order, state["button"])
        index = order.index(start)
        return order[index:] + order[:index]

    def _next_live(self, order, stacks, public_key):
        candidate = self._next_key(order, public_key)
        while int(stacks[candidate]) <= 0:
            candidate = self._next_key(order, candidate)
        return candidate

    def _next_key(self, order, public_key):
        return order[(order.index(public_key) + 1) % len(order)]

    def _active_members(self, members):
        active = sorted(
            [member for member in members if member.get("status") == "active"],
            key=lambda member: int(member["seat"]),
        )
        if len(active) != 4:
            raise ValueError("Texas Hold'em requires exactly four active Members")
        return active

    def _small_blind(self):
        return self._option_int("small_blind", 5, 1, 10000)

    def _big_blind(self):
        return self._option_int("big_blind", 10, 2, 20000)

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
