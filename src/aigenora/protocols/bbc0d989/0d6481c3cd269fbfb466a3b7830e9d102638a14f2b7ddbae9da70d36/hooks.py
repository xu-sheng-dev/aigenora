from __future__ import annotations

import copy
import secrets
from collections import Counter

from aigenora.proto.hooks import ProtocolHooks
from aigenora.proto.shared_deck import (
    create_shared_deck,
    discard_cards,
    draw_cards,
    private_deck_view,
    take_from_hand,
)


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")

    def proto_host_metadata(self):
        return (
            "Four-player Landlord",
            "game,cards,multiplayer,landlord",
            "supply",
            {},
        )

    def proto_group_initial_state(self, members):
        return self._new_round(
            members,
            scores={member["public_key"]: 0 for member in members},
            round_number=1,
            restart_count=0,
            recovery_notice="",
        )

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
        if state["phase"] == "completed":
            raise ValueError("the round is complete")
        if actor["public_key"] != state["current_player"]:
            raise ValueError("it is not this Member's turn")
        if state["phase"] == "bidding":
            return self._handle_bid(state, actor, action)
        if state["phase"] == "playing":
            return self._handle_play(state, actor, action)
        raise ValueError("the table is not accepting actions")

    def proto_group_view(self, state, viewer):
        deck_view = private_deck_view(state["deck"], viewer["public_key"])
        return {
            "phase": state["phase"],
            "round_number": state["round_number"],
            "restart_count": state["restart_count"],
            "recovery_notice": state["recovery_notice"],
            "players": state["players"],
            "turn_order": state["turn_order"],
            "current_player": state["current_player"],
            "bids": state["bids"],
            "highest_bid": state["highest_bid"],
            "highest_bidder": state["highest_bidder"],
            "landlord": state["landlord"],
            "bottom_revealed": state["bottom_revealed"],
            "last_play": state["last_play"],
            "passes": state["passes"],
            "bombs": state["bombs"],
            "scores": state["scores"],
            "hand_counts": deck_view["hand_counts"],
            "my_hand": deck_view["my_hand"],
            "discard": deck_view["discard"],
            "you": {
                "public_key": viewer["public_key"],
                "seat": viewer["seat"],
                "is_landlord": viewer["public_key"] == state["landlord"],
            },
        }

    def proto_group_recovery_snapshot(self, state):
        return {
            "round_number": state["round_number"],
            "restart_count": state["restart_count"],
            "scores": state["scores"],
        }

    def proto_group_restore(self, checkpoint, members, new_epoch):
        del new_epoch
        scores = {
            member["public_key"]: int(checkpoint.get("scores", {}).get(member["public_key"], 0))
            for member in members
        }
        return self._new_round(
            members,
            scores=scores,
            round_number=int(checkpoint.get("round_number", 1)) + 1,
            restart_count=int(checkpoint.get("restart_count", 0)) + 1,
            recovery_notice="The previous private deal was discarded after a Leader change.",
        )

    def proto_group_on_leader_changed(self, state, old_leader, new_leader):
        state["recovery_notice"] = (
            f"Leader changed from {old_leader[:8]} to {new_leader[:8]}; "
            "the private deal was restarted."
        )
        return {
            "state": state,
            "events": [
                {
                    "kind": "round_restarted_after_leader_change",
                    "old_leader": old_leader,
                    "new_leader": new_leader,
                    "round_number": state["round_number"],
                }
            ],
        }

    def _new_round(
        self,
        members,
        *,
        scores,
        round_number,
        restart_count,
        recovery_notice,
    ):
        active = sorted(
            [member for member in members if member.get("status") == "active"],
            key=lambda member: member["seat"],
        )
        if len(active) != 4:
            raise ValueError("Four-player Landlord requires exactly four active Members")
        configured_seed = self.options.get("deal_seed")
        seed = (
            f"{configured_seed}:{round_number}:{restart_count}"
            if isinstance(configured_seed, str) and configured_seed
            else secrets.token_hex(24)
        )
        deck = create_shared_deck(
            self._card_faces(),
            active,
            hand_size=25,
            seed=seed,
            copies=2,
        )
        order = [member["public_key"] for member in active]
        return {
            "phase": "bidding",
            "round_number": round_number,
            "restart_count": restart_count,
            "recovery_notice": recovery_notice,
            "players": {
                member["public_key"]: {
                    "seat": member["seat"],
                    "role": "unassigned",
                }
                for member in active
            },
            "turn_order": order,
            "current_player": order[0],
            "bids": {},
            "highest_bid": 0,
            "highest_bidder": "",
            "landlord": "",
            "bottom_revealed": [],
            "last_play": None,
            "passes": 0,
            "bombs": 0,
            "scores": scores,
            "deck": deck,
        }

    def _handle_bid(self, state, actor, action):
        if action.get("kind") != "bid":
            raise ValueError("bidding accepts only a bid action")
        value = action.get("value")
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > 3
        ):
            raise ValueError("bid value must be 0, 1, 2, or 3")
        public_key = actor["public_key"]
        if public_key in state["bids"]:
            raise ValueError("this Member has already bid")
        state["bids"][public_key] = value
        if value > state["highest_bid"]:
            state["highest_bid"] = value
            state["highest_bidder"] = public_key
        events = [
            {
                "kind": "bid",
                "public_key": public_key,
                "seat": actor["seat"],
                "value": value,
            }
        ]
        bidding_done = value == 3 or len(state["bids"]) == len(state["turn_order"])
        if bidding_done and not state["highest_bidder"]:
            replacement = self._new_round(
                [
                    {
                        "public_key": key,
                        "seat": value["seat"],
                        "status": "active",
                    }
                    for key, value in state["players"].items()
                ],
                scores=state["scores"],
                round_number=state["round_number"] + 1,
                restart_count=state["restart_count"],
                recovery_notice="All Members passed; the authority redealt.",
            )
            events.append(
                {
                    "kind": "all_passed_redeal",
                    "round_number": replacement["round_number"],
                }
            )
            return {"state": replacement, "events": events}
        if bidding_done:
            landlord = state["highest_bidder"]
            bottom_count = len(state["deck"]["draw_pile"])
            bottom = draw_cards(state["deck"], landlord, bottom_count)
            state["bottom_revealed"] = bottom
            state["landlord"] = landlord
            for key, player in state["players"].items():
                player["role"] = "landlord" if key == landlord else "farmer"
            state["phase"] = "playing"
            state["current_player"] = landlord
            events.append(
                {
                    "kind": "landlord_selected",
                    "public_key": landlord,
                    "bid": state["highest_bid"],
                    "bottom": bottom,
                }
            )
        else:
            state["current_player"] = self._next_unbid(state, public_key)
        return {"state": state, "events": events}

    def _handle_play(self, state, actor, action):
        kind = action.get("kind")
        if kind == "pass":
            if state["last_play"] is None:
                raise ValueError("the trick leader cannot pass")
            if state["last_play"]["public_key"] == actor["public_key"]:
                raise ValueError("the last player cannot pass to itself")
            state["passes"] += 1
            events = [{"kind": "pass", "public_key": actor["public_key"]}]
            if state["passes"] >= len(state["turn_order"]) - 1:
                winner = state["last_play"]["public_key"]
                state["last_play"] = None
                state["passes"] = 0
                state["current_player"] = winner
                events.append({"kind": "trick_reset", "public_key": winner})
            else:
                state["current_player"] = self._next_player(
                    state, actor["public_key"]
                )
            return {"state": state, "events": events}
        if kind != "play":
            raise ValueError("playing accepts play or pass")
        card_ids = action.get("card_ids")
        faces = self._cards_from_hand(state, actor["public_key"], card_ids)
        combination = self._classify(faces)
        previous = state["last_play"]
        if previous is not None and not self._beats(
            combination, previous["combination"]
        ):
            raise ValueError("the selected cards do not beat the previous play")
        take_from_hand(state["deck"], actor["public_key"], card_ids)
        discard_cards(state["deck"], card_ids)
        if combination["tier"] > 0:
            state["bombs"] += 1
        public_play = {
            "public_key": actor["public_key"],
            "seat": actor["seat"],
            "cards": faces,
            "combination": combination,
        }
        state["last_play"] = public_play
        state["passes"] = 0
        events = [{"kind": "play", **copy.deepcopy(public_play)}]
        if not state["deck"]["hands"][actor["public_key"]]:
            outcome, delta = self._score_round(state, actor["public_key"])
            state["phase"] = "completed"
            for key, amount in delta.items():
                state["scores"][key] = int(state["scores"].get(key, 0)) + amount
            events.append(
                {
                    "kind": "game_over",
                    "outcome": outcome,
                    "winner": actor["public_key"],
                    "score_delta": delta,
                }
            )
            return {
                "state": state,
                "events": events,
                "completed": True,
                "outcome": outcome,
            }
        state["current_player"] = self._next_player(state, actor["public_key"])
        return {"state": state, "events": events}

    def _cards_from_hand(self, state, public_key, card_ids):
        if (
            not isinstance(card_ids, list)
            or not card_ids
            or len(card_ids) > 20
            or len(set(card_ids)) != len(card_ids)
            or any(not isinstance(card_id, str) for card_id in card_ids)
        ):
            raise ValueError("card_ids must be a non-empty unique array")
        hand = state["deck"]["hands"][public_key]
        if any(card_id not in hand for card_id in card_ids):
            raise ValueError("a selected card is not in this Member's hand")
        return [copy.deepcopy(state["deck"]["catalog"][card_id]) for card_id in card_ids]

    def _classify(self, cards):
        values = [int(card["value"]) for card in cards]
        counts = Counter(values)
        count_values = sorted(counts.values())
        size = len(cards)
        joker_count = sum(1 for card in cards if card["suit"] == "joker")
        if joker_count == size and size >= 2:
            return {
                "kind": "joker_bomb",
                "primary": max(values),
                "length": size,
                "tier": 2,
            }
        if len(counts) == 1 and size >= 4:
            return {
                "kind": "bomb",
                "primary": values[0],
                "length": size,
                "tier": 1,
            }
        if size == 1:
            return {"kind": "single", "primary": values[0], "length": 1, "tier": 0}
        if size == 2 and count_values == [2]:
            return {"kind": "pair", "primary": values[0], "length": 2, "tier": 0}
        if size == 3 and count_values == [3]:
            return {"kind": "triple", "primary": values[0], "length": 3, "tier": 0}
        if size == 4 and count_values == [1, 3]:
            primary = next(value for value, count in counts.items() if count == 3)
            return {"kind": "triple_single", "primary": primary, "length": 4, "tier": 0}
        if size == 5 and count_values == [2, 3]:
            primary = next(value for value, count in counts.items() if count == 3)
            return {"kind": "triple_pair", "primary": primary, "length": 5, "tier": 0}
        ordered = sorted(counts)
        if (
            size >= 5
            and all(count == 1 for count in counts.values())
            and max(ordered) <= 14
            and self._consecutive(ordered)
        ):
            return {"kind": "straight", "primary": max(ordered), "length": size, "tier": 0}
        if (
            size >= 6
            and size % 2 == 0
            and all(count == 2 for count in counts.values())
            and max(ordered) <= 14
            and self._consecutive(ordered)
        ):
            return {
                "kind": "pair_straight",
                "primary": max(ordered),
                "length": size,
                "tier": 0,
            }
        triple_ranks = sorted(
            value for value, count in counts.items() if count == 3 and value <= 14
        )
        if len(triple_ranks) >= 2 and self._consecutive(triple_ranks):
            wing_counts = [
                count for value, count in counts.items() if value not in triple_ranks
            ]
            triples = len(triple_ranks)
            if size == triples * 3 and not wing_counts:
                kind = "airplane"
            elif size == triples * 4 and len(wing_counts) == triples and all(
                count == 1 for count in wing_counts
            ):
                kind = "airplane_single"
            elif size == triples * 5 and len(wing_counts) == triples and all(
                count == 2 for count in wing_counts
            ):
                kind = "airplane_pair"
            else:
                kind = ""
            if kind:
                return {
                    "kind": kind,
                    "primary": max(triple_ranks),
                    "length": size,
                    "tier": 0,
                }
        four_ranks = [value for value, count in counts.items() if count == 4]
        if len(four_ranks) == 1:
            primary = four_ranks[0]
            remainder = [
                count for value, count in counts.items() if value != primary
            ]
            if size == 6 and sum(remainder) == 2:
                return {
                    "kind": "four_two_single",
                    "primary": primary,
                    "length": 6,
                    "tier": 0,
                }
            if size == 8 and len(remainder) == 2 and all(
                count == 2 for count in remainder
            ):
                return {
                    "kind": "four_two_pair",
                    "primary": primary,
                    "length": 8,
                    "tier": 0,
                }
        raise ValueError("the selected cards are not a supported combination")

    def _beats(self, candidate, previous):
        if candidate["tier"] != previous["tier"]:
            return candidate["tier"] > previous["tier"]
        if candidate["tier"] > 0:
            if candidate["length"] != previous["length"]:
                return candidate["length"] > previous["length"]
            return candidate["primary"] > previous["primary"]
        return (
            candidate["kind"] == previous["kind"]
            and candidate["length"] == previous["length"]
            and candidate["primary"] > previous["primary"]
        )

    def _score_round(self, state, winner):
        base = max(1, int(state["highest_bid"])) * (2 ** int(state["bombs"]))
        landlord_won = winner == state["landlord"]
        delta = {}
        for public_key in state["turn_order"]:
            if public_key == state["landlord"]:
                delta[public_key] = 3 * base if landlord_won else -3 * base
            else:
                delta[public_key] = -base if landlord_won else base
        return ("landlord" if landlord_won else "farmers"), delta

    def _next_unbid(self, state, public_key):
        candidate = self._next_player(state, public_key)
        while candidate in state["bids"]:
            candidate = self._next_player(state, candidate)
        return candidate

    def _next_player(self, state, public_key):
        order = state["turn_order"]
        return order[(order.index(public_key) + 1) % len(order)]

    def _consecutive(self, values):
        return all(right == left + 1 for left, right in zip(values, values[1:]))

    def _card_faces(self):
        faces = []
        ranks = [
            ("3", 3),
            ("4", 4),
            ("5", 5),
            ("6", 6),
            ("7", 7),
            ("8", 8),
            ("9", 9),
            ("10", 10),
            ("J", 11),
            ("Q", 12),
            ("K", 13),
            ("A", 14),
            ("2", 15),
        ]
        for suit in ("clubs", "diamonds", "hearts", "spades"):
            for rank, value in ranks:
                faces.append({"rank": rank, "value": value, "suit": suit})
        faces.extend(
            [
                {"rank": "Black Joker", "value": 16, "suit": "joker"},
                {"rank": "Red Joker", "value": 17, "suit": "joker"},
            ]
        )
        return faces
