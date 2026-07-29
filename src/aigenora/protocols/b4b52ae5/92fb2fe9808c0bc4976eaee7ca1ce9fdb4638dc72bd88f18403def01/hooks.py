from __future__ import annotations

import copy
import secrets

from aigenora.proto.hooks import ProtocolHooks
from aigenora.proto.mahjong import (
    chow_sequences,
    classical_core_patterns,
    mahjong_tile_faces,
    mahjong_win_kind,
)
from aigenora.proto.shared_deck import (
    create_shared_deck,
    discard_cards,
    draw_cards,
    move_discard_to_zone,
    move_hand_to_zone,
    private_deck_view,
    take_from_hand,
)


WINDS = ("east", "south", "west", "north")


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")

    def proto_host_metadata(self):
        return (
            "Classical Mahjong Core",
            "game,tiles,multiplayer,mahjong,shared-deck",
            "supply",
            {},
        )

    def proto_group_initial_state(self, members):
        starting_points = self._option_int(
            "starting_points", 25000, 0, 100000
        )
        self._option_int("max_hands", 4, 1, 32)
        active = self._active_members(members)
        return self._new_hand(
            active,
            scores={member["public_key"]: starting_points for member in active},
            hand_number=1,
            dealer_index=0,
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
            raise ValueError("the Mahjong match is complete")
        if state["phase"] == "discard":
            if actor["public_key"] != state["current_player"]:
                raise ValueError("it is not this Member's turn")
            return self._handle_turn(state, actor, action)
        if state["phase"] == "claim":
            return self._handle_claim(state, actor, action)
        raise ValueError("the Mahjong table is not accepting actions")

    def proto_group_view(self, state, viewer):
        deck_view = private_deck_view(state["deck"], viewer["public_key"])
        catalog = state["deck"]["catalog"]
        rivers = {
            public_key: [
                copy.deepcopy(catalog[card_id])
                for card_id in card_ids
            ]
            for public_key, card_ids in state["rivers"].items()
        }
        return {
            "phase": state["phase"],
            "hand_number": state["hand_number"],
            "restart_count": state["restart_count"],
            "recovery_notice": state["recovery_notice"],
            "turn_order": state["turn_order"],
            "players": state["players"],
            "dealer": state["dealer"],
            "current_player": state["current_player"],
            "wall_count": deck_view["draw_count"],
            "hand_counts": deck_view["hand_counts"],
            "my_hand": deck_view["my_hand"],
            "rivers": rivers,
            "melds": state["meld_groups"],
            "last_discard": state["last_discard"],
            "claim_response_count": len(state["claim_responses"]),
            "claim_required_count": len(state["claim_eligible"]),
            "my_claim": state["claim_responses"].get(viewer["public_key"]),
            "scores": state["scores"],
            "last_result": state["last_result"],
            "you": {
                "public_key": viewer["public_key"],
                "seat": viewer["seat"],
                "wind": WINDS[int(viewer["seat"])],
                "may_claim": viewer["public_key"] in state["claim_eligible"],
            },
        }

    def proto_group_recovery_snapshot(self, state):
        return {
            "hand_number": state["hand_number"],
            "dealer_index": state["dealer_index"],
            "restart_count": state["restart_count"],
            "scores": state["scores"],
        }

    def proto_group_restore(self, checkpoint, members, new_epoch):
        del new_epoch
        active = self._active_members(members)
        scores = {
            member["public_key"]: int(
                checkpoint.get("scores", {}).get(member["public_key"], 0)
            )
            for member in active
        }
        return self._new_hand(
            active,
            scores=scores,
            hand_number=int(checkpoint.get("hand_number", 1)),
            dealer_index=int(checkpoint.get("dealer_index", 0)),
            restart_count=int(checkpoint.get("restart_count", 0)) + 1,
            recovery_notice=(
                "The interrupted hidden wall was discarded after a Leader change."
            ),
        )

    def proto_group_on_leader_changed(self, state, old_leader, new_leader):
        state["recovery_notice"] = (
            f"Leader changed from {old_leader[:8]} to {new_leader[:8]}; "
            "the private hand was restarted."
        )
        return {
            "state": state,
            "events": [{
                "kind": "mahjong_hand_restarted_after_leader_change",
                "old_leader": old_leader,
                "new_leader": new_leader,
                "hand_number": state["hand_number"],
            }],
        }

    def _new_hand(
        self,
        members,
        *,
        scores,
        hand_number,
        dealer_index,
        restart_count,
        recovery_notice,
    ):
        if len(members) != 4:
            raise ValueError("Classical Mahjong requires exactly four active Members")
        order = [member["public_key"] for member in members]
        dealer_index %= 4
        configured_seed = self.options.get("deal_seed")
        seed = (
            f"{configured_seed}:{hand_number}:{restart_count}"
            if isinstance(configured_seed, str) and configured_seed
            else secrets.token_hex(24)
        )
        deck = create_shared_deck(
            mahjong_tile_faces(),
            members,
            hand_size=13,
            seed=seed,
            copies=4,
        )
        dealer = order[dealer_index]
        draw_cards(deck, dealer, 1)
        return {
            "phase": "discard",
            "hand_number": int(hand_number),
            "dealer_index": dealer_index,
            "restart_count": int(restart_count),
            "recovery_notice": recovery_notice,
            "turn_order": order,
            "players": {
                member["public_key"]: {
                    "seat": int(member["seat"]),
                    "wind": WINDS[int(member["seat"])],
                }
                for member in members
            },
            "dealer": dealer,
            "current_player": dealer,
            "must_discard_after_claim": False,
            "rivers": {key: [] for key in order},
            "meld_groups": {key: [] for key in order},
            "last_discard": None,
            "claim_eligible": [],
            "claim_responses": {},
            "scores": scores,
            "last_result": None,
            "deck": deck,
        }

    def _handle_turn(self, state, actor, action):
        kind = action.get("kind")
        if kind == "discard":
            return self._discard(state, actor, action)
        if kind == "win":
            if state.get("must_discard_after_claim", False):
                raise ValueError(
                    "cannot declare a self-draw win before drawing a tile"
                )
            return self._win(state, actor["public_key"], self_draw=True)
        if kind == "concealed_kong":
            return self._concealed_kong(state, actor, action)
        raise ValueError("turn accepts discard, win, or concealed_kong")

    def _discard(self, state, actor, action):
        public_key = actor["public_key"]
        card_id = action.get("card_id")
        if (
            not isinstance(card_id, str)
            or card_id not in state["deck"]["hands"][public_key]
        ):
            raise ValueError("card_id is not in this Member's hand")
        card = copy.deepcopy(state["deck"]["catalog"][card_id])
        take_from_hand(state["deck"], public_key, [card_id])
        discard_cards(state["deck"], [card_id])
        state["must_discard_after_claim"] = False
        state["rivers"][public_key].append(card_id)
        state["last_discard"] = {
            "card_id": card_id,
            "card": card,
            "public_key": public_key,
            "seat": int(actor["seat"]),
        }
        state["phase"] = "claim"
        state["current_player"] = ""
        state["claim_eligible"] = [
            key for key in state["turn_order"] if key != public_key
        ]
        state["claim_responses"] = {}
        return {
            "state": state,
            "events": [{
                "kind": "mahjong_discard",
                **copy.deepcopy(state["last_discard"]),
            }],
        }

    def _concealed_kong(self, state, actor, action):
        public_key = actor["public_key"]
        card_ids = action.get("card_ids")
        cards = self._hand_cards(state, public_key, card_ids, expected=4)
        if len({card["code"] for card in cards}) != 1:
            raise ValueError("a concealed kong needs four identical tile faces")
        move_hand_to_zone(
            state["deck"],
            public_key,
            "melds",
            public_key,
            card_ids,
        )
        group = {
            "kind": "concealed_kong",
            "owner": public_key,
            "cards": cards,
        }
        state["meld_groups"][public_key].append(group)
        events = [{"kind": "mahjong_meld", **copy.deepcopy(group)}]
        if not state["deck"]["draw_pile"]:
            return self._finish_draw(state, events)
        draw_cards(state["deck"], public_key, 1)
        state["must_discard_after_claim"] = False
        events.append({"kind": "replacement_draw", "public_key": public_key})
        return {"state": state, "events": events}

    def _handle_claim(self, state, actor, action):
        public_key = actor["public_key"]
        if public_key not in state["claim_eligible"]:
            raise ValueError("this Member cannot respond to the discard")
        if public_key in state["claim_responses"]:
            raise ValueError("this Member already responded to the claim window")
        kind = action.get("kind")
        response = {"kind": kind}
        discard = state["last_discard"]["card"]
        if kind == "pass_claim":
            pass
        elif kind == "win":
            concealed = self._hand_faces(state, public_key) + [discard]
            if mahjong_win_kind(
                concealed,
                meld_count=len(state["meld_groups"][public_key]),
            ) is None:
                raise ValueError("the discard does not complete a supported win")
        elif kind in {"pung", "kong"}:
            expected = 2 if kind == "pung" else 3
            card_ids = action.get("card_ids")
            cards = self._hand_cards(
                state, public_key, card_ids, expected=expected
            )
            if any(card["code"] != discard["code"] for card in cards):
                raise ValueError(f"{kind} tiles must match the discard")
            response["card_ids"] = list(card_ids)
        elif kind == "chow":
            discarder = state["last_discard"]["public_key"]
            if public_key != self._next_key(state["turn_order"], discarder):
                raise ValueError("only the next seat may chow")
            card_ids = action.get("card_ids")
            cards = self._hand_cards(state, public_key, card_ids, expected=2)
            codes = sorted(card["code"] for card in cards)
            choices = [
                sorted(choice)
                for choice in chow_sequences(
                    self._hand_faces(state, public_key),
                    discard,
                )
            ]
            if codes not in choices:
                raise ValueError("card_ids do not form a legal chow")
            response["card_ids"] = list(card_ids)
        else:
            raise ValueError("claim accepts pass_claim, win, pung, kong, or chow")
        state["claim_responses"][public_key] = response
        events = [{
            "kind": "claim_response_received",
            "public_key": public_key,
            "response_count": len(state["claim_responses"]),
        }]
        if len(state["claim_responses"]) < len(state["claim_eligible"]):
            return {"state": state, "events": events}
        return self._resolve_claims(state, events)

    def _resolve_claims(self, state, events):
        ordered = self._clockwise_after(
            state["turn_order"],
            state["last_discard"]["public_key"],
        )
        winners = [
            key for key in ordered
            if state["claim_responses"][key]["kind"] == "win"
        ]
        if winners:
            winner = winners[0]
            events.append({"kind": "claim_resolved", "claim": "win", "winner": winner})
            return self._win(state, winner, self_draw=False, events=events)
        meld_claimants = [
            key for key in ordered
            if state["claim_responses"][key]["kind"] in {"pung", "kong"}
        ]
        if meld_claimants:
            claimant = meld_claimants[0]
            response = state["claim_responses"][claimant]
            events.append({
                "kind": "claim_resolved",
                "claim": response["kind"],
                "winner": claimant,
            })
            return self._apply_claimed_meld(
                state, claimant, response, events
            )
        chow_claimants = [
            key for key in ordered
            if state["claim_responses"][key]["kind"] == "chow"
        ]
        if chow_claimants:
            claimant = chow_claimants[0]
            response = state["claim_responses"][claimant]
            events.append({
                "kind": "claim_resolved",
                "claim": "chow",
                "winner": claimant,
            })
            return self._apply_claimed_meld(
                state, claimant, response, events
            )
        events.append({"kind": "claim_resolved", "claim": "none"})
        return self._advance_after_unclaimed(state, events)

    def _apply_claimed_meld(self, state, claimant, response, events):
        kind = response["kind"]
        card_ids = response["card_ids"]
        cards = self._hand_cards(
            state,
            claimant,
            card_ids,
            expected=2 if kind in {"pung", "chow"} else 3,
        )
        move_hand_to_zone(
            state["deck"],
            claimant,
            "melds",
            claimant,
            card_ids,
        )
        discard_id = state["last_discard"]["card_id"]
        claimed = move_discard_to_zone(
            state["deck"],
            "melds",
            claimant,
            discard_id,
        )
        discarder = state["last_discard"]["public_key"]
        state["rivers"][discarder].remove(discard_id)
        group = {
            "kind": kind,
            "owner": claimant,
            "from": discarder,
            "cards": [*cards, claimed],
        }
        state["meld_groups"][claimant].append(group)
        state["phase"] = "discard"
        state["current_player"] = claimant
        state["must_discard_after_claim"] = kind in {"pung", "chow"}
        state["last_discard"] = None
        state["claim_eligible"] = []
        state["claim_responses"] = {}
        events.append({"kind": "mahjong_meld", **copy.deepcopy(group)})
        if kind == "kong":
            if not state["deck"]["draw_pile"]:
                return self._finish_draw(state, events)
            draw_cards(state["deck"], claimant, 1)
            events.append({"kind": "replacement_draw", "public_key": claimant})
        return {"state": state, "events": events}

    def _advance_after_unclaimed(self, state, events):
        next_player = self._next_key(
            state["turn_order"],
            state["last_discard"]["public_key"],
        )
        state["last_discard"] = None
        state["claim_eligible"] = []
        state["claim_responses"] = {}
        if not state["deck"]["draw_pile"]:
            return self._finish_draw(state, events)
        draw_cards(state["deck"], next_player, 1)
        state["phase"] = "discard"
        state["current_player"] = next_player
        state["must_discard_after_claim"] = False
        events.append({"kind": "mahjong_draw", "public_key": next_player})
        return {"state": state, "events": events}

    def _win(self, state, winner, *, self_draw, events=None):
        events = list(events or [])
        concealed = self._hand_faces(state, winner)
        discarder = None
        if not self_draw:
            concealed.append(state["last_discard"]["card"])
            discarder = state["last_discard"]["public_key"]
        win_kind = mahjong_win_kind(
            concealed,
            meld_count=len(state["meld_groups"][winner]),
        )
        if win_kind is None:
            raise ValueError("the hand does not match a supported winning shape")
        all_tiles = list(concealed)
        meld_kinds = []
        concealed_hand = True
        for group in state["meld_groups"][winner]:
            all_tiles.extend(group["cards"])
            meld_kinds.append(group["kind"])
            if group["kind"] != "concealed_kong":
                concealed_hand = False
        patterns = classical_core_patterns(
            all_tiles,
            win_kind=win_kind,
            meld_kinds=meld_kinds,
            self_draw=self_draw,
            concealed=concealed_hand,
        )
        points = sum(int(pattern["points"]) for pattern in patterns)
        delta = {key: 0 for key in state["turn_order"]}
        if self_draw:
            for key in state["turn_order"]:
                if key != winner:
                    delta[key] -= points
                    delta[winner] += points
        else:
            delta[discarder] -= points * 3
            delta[winner] += points * 3
        for key, amount in delta.items():
            state["scores"][key] += amount
        state["last_result"] = {
            "kind": "self_draw" if self_draw else "discard_win",
            "winner": winner,
            "discarder": discarder,
            "win_kind": win_kind,
            "patterns": patterns,
            "points": points,
            "score_delta": delta,
        }
        events.append({
            "kind": "mahjong_win",
            **copy.deepcopy(state["last_result"]),
        })
        return self._finish_hand(state, events)

    def _finish_draw(self, state, events):
        state["last_result"] = {
            "kind": "wall_exhausted",
            "winner": "",
            "points": 0,
        }
        events.append({"kind": "mahjong_wall_exhausted"})
        return self._finish_hand(state, events)

    def _finish_hand(self, state, events):
        max_hands = self._option_int("max_hands", 4, 1, 32)
        if state["hand_number"] >= max_hands:
            state["phase"] = "completed"
            high_score = max(state["scores"].values())
            winners = [
                key
                for key in state["turn_order"]
                if state["scores"][key] == high_score
            ]
            outcome = winners[0] if len(winners) == 1 else "draw"
            events.append({
                "kind": "mahjong_match_complete",
                "winner": outcome,
                "winners": winners,
                "scores": copy.deepcopy(state["scores"]),
            })
            return {
                "state": state,
                "events": events,
                "completed": True,
                "outcome": outcome,
            }
        members = [
            {
                "public_key": key,
                "seat": int(state["players"][key]["seat"]),
                "status": "active",
            }
            for key in state["turn_order"]
        ]
        replacement = self._new_hand(
            members,
            scores=state["scores"],
            hand_number=state["hand_number"] + 1,
            dealer_index=(state["dealer_index"] + 1) % 4,
            restart_count=state["restart_count"],
            recovery_notice=state["recovery_notice"],
        )
        replacement["last_result"] = state["last_result"]
        events.append({
            "kind": "mahjong_hand_started",
            "hand_number": replacement["hand_number"],
            "dealer": replacement["dealer"],
        })
        return {"state": replacement, "events": events}

    def _hand_cards(self, state, public_key, card_ids, *, expected):
        if (
            not isinstance(card_ids, list)
            or len(card_ids) != expected
            or len(set(card_ids)) != expected
            or any(not isinstance(card_id, str) for card_id in card_ids)
        ):
            raise ValueError(f"card_ids must contain {expected} unique tile ids")
        hand = state["deck"]["hands"][public_key]
        if any(card_id not in hand for card_id in card_ids):
            raise ValueError("a selected tile is not in this Member's hand")
        return [
            copy.deepcopy(state["deck"]["catalog"][card_id])
            for card_id in card_ids
        ]

    def _hand_faces(self, state, public_key):
        return [
            copy.deepcopy(state["deck"]["catalog"][card_id])
            for card_id in state["deck"]["hands"][public_key]
        ]

    def _clockwise_after(self, order, public_key):
        index = order.index(public_key)
        return [
            order[(index + offset) % len(order)]
            for offset in range(1, len(order))
        ]

    def _next_key(self, order, public_key):
        return order[(order.index(public_key) + 1) % len(order)]

    def _active_members(self, members):
        active = sorted(
            [member for member in members if member.get("status") == "active"],
            key=lambda member: int(member["seat"]),
        )
        if len(active) != 4:
            raise ValueError("Classical Mahjong requires exactly four active Members")
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
