from __future__ import annotations

import copy
import secrets

from aigenora.proto.card_games import (
    SUITS,
    card_by_id,
    standard_card_faces,
    validate_follow_suit,
    winning_trick_index,
)
from aigenora.proto.hooks import ProtocolHooks
from aigenora.proto.shared_deck import (
    create_shared_deck,
    discard_cards,
    private_deck_view,
    take_from_hand,
)


DENOMINATIONS = ("clubs", "diamonds", "hearts", "spades", "nt")
COMPASS = ("north", "east", "south", "west")
VULNERABILITY = (
    "none", "ns", "ew", "both",
    "ns", "ew", "both", "none",
    "ew", "both", "none", "ns",
    "both", "none", "ns", "ew",
)


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")

    def proto_host_metadata(self):
        return (
            "Contract Bridge",
            "game,cards,multiplayer,bridge,partnership",
            "supply",
            {},
        )

    def proto_group_initial_state(self, members):
        board_number = self._option_int("board_number", 1, 1, 32)
        return self._new_board(
            members,
            board_number=board_number,
            scores={"ns": 0, "ew": 0},
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
            raise ValueError("the board is complete")
        if state["phase"] == "auction":
            if actor["public_key"] != state["current_player"]:
                raise ValueError("it is not this Member's turn to call")
            return self._handle_call(state, actor, action)
        if state["phase"] == "playing":
            return self._handle_play(state, actor, action)
        raise ValueError("the bridge table is not accepting actions")

    def proto_group_view(self, state, viewer):
        deck_view = private_deck_view(state["deck"], viewer["public_key"])
        dummy_cards = []
        if state["dummy_revealed"] and state["dummy"]:
            dummy_cards = [
                copy.deepcopy(state["deck"]["catalog"][card_id])
                for card_id in state["deck"]["hands"][state["dummy"]]
            ]
        playable_cards = deck_view["my_hand"]
        if (
            state["phase"] == "playing"
            and state["current_hand_owner"] == state["dummy"]
            and viewer["public_key"] == state["declarer"]
        ):
            playable_cards = dummy_cards
        return {
            "phase": state["phase"],
            "board_number": state["board_number"],
            "restart_count": state["restart_count"],
            "recovery_notice": state["recovery_notice"],
            "players": state["players"],
            "turn_order": state["turn_order"],
            "dealer": state["dealer"],
            "vulnerability": state["vulnerability"],
            "current_player": state["current_player"],
            "current_hand_owner": state["current_hand_owner"],
            "auction": state["auction"],
            "last_bid": state["last_bid"],
            "doubled": state["doubled"],
            "contract": state["contract"],
            "declarer": state["declarer"],
            "dummy": state["dummy"],
            "dummy_revealed": state["dummy_revealed"],
            "dummy_hand": dummy_cards,
            "current_trick": state["current_trick"],
            "trick_history": state["trick_history"],
            "tricks": state["tricks"],
            "scores": state["scores"],
            "my_hand": deck_view["my_hand"],
            "playable_hand": playable_cards,
            "hand_counts": deck_view["hand_counts"],
            "you": {
                "public_key": viewer["public_key"],
                "seat": viewer["seat"],
                "compass": COMPASS[int(viewer["seat"])],
                "partnership": self._partnership(int(viewer["seat"])),
            },
        }

    def proto_group_recovery_snapshot(self, state):
        return {
            "board_number": state["board_number"],
            "restart_count": state["restart_count"],
            "scores": state["scores"],
        }

    def proto_group_restore(self, checkpoint, members, new_epoch):
        del new_epoch
        return self._new_board(
            members,
            board_number=int(checkpoint.get("board_number", 1)),
            scores={
                "ns": int(checkpoint.get("scores", {}).get("ns", 0)),
                "ew": int(checkpoint.get("scores", {}).get("ew", 0)),
            },
            restart_count=int(checkpoint.get("restart_count", 0)) + 1,
            recovery_notice=(
                "The previous private board was discarded after a Leader change."
            ),
        )

    def proto_group_on_leader_changed(self, state, old_leader, new_leader):
        state["recovery_notice"] = (
            f"Leader changed from {old_leader[:8]} to {new_leader[:8]}; "
            "the private board was redealt."
        )
        return {
            "state": state,
            "events": [{
                "kind": "board_restarted_after_leader_change",
                "old_leader": old_leader,
                "new_leader": new_leader,
                "board_number": state["board_number"],
            }],
        }

    def _new_board(
        self,
        members,
        *,
        board_number,
        scores,
        restart_count,
        recovery_notice,
    ):
        active = sorted(
            [member for member in members if member.get("status") == "active"],
            key=lambda member: int(member["seat"]),
        )
        if len(active) != 4:
            raise ValueError("Contract Bridge requires exactly four active Members")
        configured_seed = self.options.get("deal_seed")
        seed = (
            f"{configured_seed}:{board_number}:{restart_count}"
            if isinstance(configured_seed, str) and configured_seed
            else secrets.token_hex(24)
        )
        deck = create_shared_deck(
            standard_card_faces(),
            active,
            hand_size=13,
            seed=seed,
        )
        order = [member["public_key"] for member in active]
        dealer = order[(board_number - 1) % 4]
        return {
            "phase": "auction",
            "board_number": board_number,
            "restart_count": restart_count,
            "recovery_notice": recovery_notice,
            "players": {
                member["public_key"]: {
                    "seat": int(member["seat"]),
                    "compass": COMPASS[int(member["seat"])],
                    "partnership": self._partnership(int(member["seat"])),
                }
                for member in active
            },
            "turn_order": order,
            "dealer": dealer,
            "vulnerability": VULNERABILITY[(board_number - 1) % 16],
            "current_player": dealer,
            "current_hand_owner": "",
            "auction": [],
            "last_bid": None,
            "last_non_pass": "",
            "doubled": 0,
            "consecutive_passes": 0,
            "contract": None,
            "declarer": "",
            "dummy": "",
            "dummy_revealed": False,
            "current_trick": [],
            "trick_history": [],
            "tricks": {"ns": 0, "ew": 0},
            "scores": scores,
            "deck": deck,
        }

    def _handle_call(self, state, actor, action):
        kind = action.get("kind")
        public_key = actor["public_key"]
        call = {
            "public_key": public_key,
            "seat": int(actor["seat"]),
            "kind": kind,
        }
        if kind == "pass":
            state["consecutive_passes"] += 1
        elif kind == "bid":
            level = action.get("level")
            denomination = action.get("denomination")
            if (
                not isinstance(level, int)
                or isinstance(level, bool)
                or level < 1
                or level > 7
                or denomination not in DENOMINATIONS
            ):
                raise ValueError("a bid requires level 1-7 and a valid denomination")
            candidate = self._bid_value(level, str(denomination))
            if state["last_bid"] and candidate <= int(state["last_bid"]["value"]):
                raise ValueError("a new bid must outrank the current contract")
            call.update({
                "level": level,
                "denomination": denomination,
                "value": candidate,
            })
            state["last_bid"] = copy.deepcopy(call)
            state["doubled"] = 0
            state["last_non_pass"] = "bid"
            state["consecutive_passes"] = 0
        elif kind == "double":
            if (
                not state["last_bid"]
                or state["doubled"] != 0
                or state["last_non_pass"] != "bid"
                or self._team_of_key(state, public_key)
                == self._team_of_key(state, state["last_bid"]["public_key"])
            ):
                raise ValueError("double is not legal in the current auction")
            state["doubled"] = 1
            state["last_non_pass"] = "double"
            state["consecutive_passes"] = 0
        elif kind == "redouble":
            if (
                not state["last_bid"]
                or state["doubled"] != 1
                or state["last_non_pass"] != "double"
                or self._team_of_key(state, public_key)
                != self._team_of_key(state, state["last_bid"]["public_key"])
            ):
                raise ValueError("redouble is not legal in the current auction")
            state["doubled"] = 2
            state["last_non_pass"] = "redouble"
            state["consecutive_passes"] = 0
        else:
            raise ValueError("auction accepts pass, bid, double, or redouble")
        state["auction"].append(call)
        events = [{"kind": "bridge_call", **copy.deepcopy(call)}]

        if state["last_bid"] is None and len(state["auction"]) == 4:
            state["phase"] = "completed"
            events.append({"kind": "board_passed_out"})
            return {
                "state": state,
                "events": events,
                "completed": True,
                "outcome": "passed_out",
            }
        if state["last_bid"] and state["consecutive_passes"] >= 3:
            self._start_play(state)
            events.append({
                "kind": "contract_set",
                "contract": copy.deepcopy(state["contract"]),
                "declarer": state["declarer"],
                "dummy": state["dummy"],
            })
            return {"state": state, "events": events}
        state["current_player"] = self._next_key(state, public_key)
        return {"state": state, "events": events}

    def _start_play(self, state):
        final = state["last_bid"]
        declaring_team = self._team_of_key(state, final["public_key"])
        declarer = next(
            call["public_key"]
            for call in state["auction"]
            if call["kind"] == "bid"
            and call["denomination"] == final["denomination"]
            and self._team_of_key(state, call["public_key"]) == declaring_team
        )
        declarer_seat = int(state["players"][declarer]["seat"])
        dummy = state["turn_order"][(declarer_seat + 2) % 4]
        opening_leader = state["turn_order"][(declarer_seat + 1) % 4]
        state["contract"] = {
            "level": int(final["level"]),
            "denomination": final["denomination"],
            "doubled": state["doubled"],
            "partnership": declaring_team,
        }
        state["declarer"] = declarer
        state["dummy"] = dummy
        state["phase"] = "playing"
        state["current_hand_owner"] = opening_leader
        state["current_player"] = opening_leader

    def _handle_play(self, state, actor, action):
        if action.get("kind") != "play":
            raise ValueError("card play accepts only play")
        expected_actor = (
            state["declarer"]
            if state["current_hand_owner"] == state["dummy"]
            else state["current_hand_owner"]
        )
        if actor["public_key"] != expected_actor:
            raise ValueError("it is not this Member's turn to play")
        card_id = action.get("card_id")
        owner = state["current_hand_owner"]
        if not isinstance(card_id, str) or card_id not in state["deck"]["hands"][owner]:
            raise ValueError("card_id is not in the active hand")
        card = copy.deepcopy(card_by_id(state["deck"], card_id))
        if state["current_trick"]:
            led_suit = state["current_trick"][0]["card"]["suit"]
            hand = [
                state["deck"]["catalog"][candidate]
                for candidate in state["deck"]["hands"][owner]
            ]
            validate_follow_suit(hand, card, led_suit)
        take_from_hand(state["deck"], owner, [card_id])
        discard_cards(state["deck"], [card_id])
        play = {
            "public_key": actor["public_key"],
            "hand_owner": owner,
            "seat": int(state["players"][owner]["seat"]),
            "card": card,
        }
        state["current_trick"].append(play)
        events = [{"kind": "bridge_card_played", **copy.deepcopy(play)}]
        if not state["dummy_revealed"]:
            state["dummy_revealed"] = True
            events.append({"kind": "dummy_revealed", "dummy": state["dummy"]})

        if len(state["current_trick"]) < 4:
            next_owner = self._next_key(state, owner)
            state["current_hand_owner"] = next_owner
            state["current_player"] = (
                state["declarer"] if next_owner == state["dummy"] else next_owner
            )
            return {"state": state, "events": events}

        cards = [entry["card"] for entry in state["current_trick"]]
        denomination = state["contract"]["denomination"]
        winner_index = winning_trick_index(
            cards,
            led_suit=str(cards[0]["suit"]),
            trump_suit=denomination if denomination in SUITS else None,
        )
        winner = state["current_trick"][winner_index]["hand_owner"]
        team = self._team_of_key(state, winner)
        state["tricks"][team] += 1
        completed_trick = copy.deepcopy(state["current_trick"])
        state["trick_history"].append({
            "number": len(state["trick_history"]) + 1,
            "winner": winner,
            "cards": completed_trick,
        })
        state["current_trick"] = []
        events.append({
            "kind": "bridge_trick_won",
            "winner": winner,
            "partnership": team,
            "trick_number": len(state["trick_history"]),
        })
        if len(state["trick_history"]) == 13:
            return self._complete_board(state, events)
        state["current_hand_owner"] = winner
        state["current_player"] = (
            state["declarer"] if winner == state["dummy"] else winner
        )
        return {"state": state, "events": events}

    def _complete_board(self, state, events):
        contract = state["contract"]
        declaring_team = contract["partnership"]
        tricks = int(state["tricks"][declaring_team])
        vulnerable = state["vulnerability"] in {declaring_team, "both"}
        score = self._duplicate_score(
            level=int(contract["level"]),
            denomination=str(contract["denomination"]),
            doubled=int(contract["doubled"]),
            tricks=tricks,
            vulnerable=vulnerable,
        )
        scoring_team = declaring_team if score >= 0 else (
            "ew" if declaring_team == "ns" else "ns"
        )
        state["scores"][scoring_team] += abs(score)
        state["phase"] = "completed"
        outcome = (
            f"{declaring_team}_made"
            if score >= 0
            else f"{declaring_team}_down"
        )
        events.append({
            "kind": "bridge_board_complete",
            "outcome": outcome,
            "contract_score": score,
            "tricks": tricks,
            "scores": copy.deepcopy(state["scores"]),
        })
        return {
            "state": state,
            "events": events,
            "completed": True,
            "outcome": outcome,
        }

    def _duplicate_score(
        self,
        *,
        level,
        denomination,
        doubled,
        tricks,
        vulnerable,
    ):
        required = level + 6
        delta = tricks - required
        multiplier = (1, 2, 4)[doubled]
        if delta < 0:
            undertricks = -delta
            if doubled == 0:
                return -undertricks * (100 if vulnerable else 50)
            if vulnerable:
                penalty = 200 + max(0, undertricks - 1) * 300
            else:
                penalty = 100
                if undertricks >= 2:
                    penalty += min(undertricks - 1, 2) * 200
                if undertricks >= 4:
                    penalty += (undertricks - 3) * 300
            return -penalty * (2 if doubled == 2 else 1)

        if denomination in {"clubs", "diamonds"}:
            base = level * 20
            ordinary_overtrick = 20
        elif denomination in {"hearts", "spades"}:
            base = level * 30
            ordinary_overtrick = 30
        else:
            base = 40 + max(0, level - 1) * 30
            ordinary_overtrick = 30
        contract_points = base * multiplier
        total = contract_points
        total += 300 if contract_points >= 100 and not vulnerable else 0
        total += 500 if contract_points >= 100 and vulnerable else 0
        total += 50 if contract_points < 100 else 0
        if level == 6:
            total += 750 if vulnerable else 500
        elif level == 7:
            total += 1500 if vulnerable else 1000
        if doubled == 1:
            total += 50
        elif doubled == 2:
            total += 100
        if delta:
            if doubled == 0:
                total += delta * ordinary_overtrick
            else:
                total += delta * (200 if vulnerable else 100) * (
                    2 if doubled == 2 else 1
                )
        return total

    def _bid_value(self, level, denomination):
        return (int(level) - 1) * len(DENOMINATIONS) + DENOMINATIONS.index(denomination)

    def _next_key(self, state, public_key):
        order = state["turn_order"]
        return order[(order.index(public_key) + 1) % len(order)]

    def _team_of_key(self, state, public_key):
        return str(state["players"][public_key]["partnership"])

    def _partnership(self, seat):
        return "ns" if int(seat) % 2 == 0 else "ew"

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
