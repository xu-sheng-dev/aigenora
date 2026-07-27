from __future__ import annotations

from typing import Any

from aigenora.proto.hooks import ProtocolHooks


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")

    def proto_host_metadata(self):
        return (
            "Meeting Room",
            "meeting,multiplayer,agenda,vote",
            "chat",
            {"meeting_title": "Working session", "max_agenda_items": 20},
        )

    def proto_group_initial_state(self, members):
        facilitator = min(members, key=lambda member: member["seat"])
        return {
            "title": str(self.options.get("meeting_title") or "Working session")[:200],
            "facilitator": facilitator["public_key"],
            "members": {
                member["public_key"]: {
                    "seat": member["seat"],
                    "status": member.get("status", "active"),
                }
                for member in members
            },
            "agenda": [],
            "active_agenda": -1,
            "speaking_queue": [],
            "active_speaker": "",
            "vote": None,
            "vote_history": [],
            "action_items": [],
            "next_id": 1,
            "ended": False,
        }

    def proto_group_member_joined(self, state, member):
        state["members"][member["public_key"]] = {
            "seat": member["seat"],
            "status": "active",
        }
        return {
            "state": state,
            "events": [
                {
                    "kind": "member_joined",
                    "public_key": member["public_key"],
                    "seat": member["seat"],
                }
            ],
        }

    def proto_group_member_left(self, state, member, reason):
        public_key = member["public_key"]
        state["members"].setdefault(
            public_key, {"seat": member["seat"]}
        )["status"] = "left"
        state["speaking_queue"] = [
            item for item in state["speaking_queue"] if item != public_key
        ]
        if state["active_speaker"] == public_key:
            state["active_speaker"] = ""
            self._advance_speaker(state)
        return {
            "state": state,
            "events": [
                {
                    "kind": "member_left",
                    "public_key": public_key,
                    "reason": reason,
                }
            ],
        }

    def proto_group_handle(self, state, actor, action):
        kind = action.get("kind")
        events: list[dict[str, Any]] = []
        if kind == "add_agenda":
            if len(state["agenda"]) >= int(self.options.get("max_agenda_items", 20)):
                raise ValueError("agenda is full")
            text = self._text(action.get("text"), 500, "text")
            item = {
                "id": self._id(state),
                "text": text,
                "created_by": actor["public_key"],
                "status": "pending",
            }
            state["agenda"].append(item)
            if state["active_agenda"] < 0:
                state["active_agenda"] = 0
                item["status"] = "active"
            events.append({"kind": "agenda_added", "item": item})
        elif kind == "advance_agenda":
            self._require_facilitator(state, actor)
            current = state["active_agenda"]
            if 0 <= current < len(state["agenda"]):
                state["agenda"][current]["status"] = "done"
            next_index = current + 1
            state["active_agenda"] = (
                next_index if next_index < len(state["agenda"]) else -1
            )
            if state["active_agenda"] >= 0:
                state["agenda"][state["active_agenda"]]["status"] = "active"
            events.append(
                {
                    "kind": "agenda_advanced",
                    "active_agenda": state["active_agenda"],
                }
            )
        elif kind == "request_to_speak":
            public_key = actor["public_key"]
            if (
                state["active_speaker"] != public_key
                and public_key not in state["speaking_queue"]
            ):
                state["speaking_queue"].append(public_key)
            self._advance_speaker(state)
            events.append(
                {
                    "kind": "speaking_queue_changed",
                    "active_speaker": state["active_speaker"],
                    "queue": state["speaking_queue"],
                }
            )
        elif kind == "yield_speaker":
            if state["active_speaker"] != actor["public_key"]:
                raise ValueError("only the active speaker can yield")
            state["active_speaker"] = ""
            self._advance_speaker(state)
            events.append(
                {
                    "kind": "speaker_yielded",
                    "active_speaker": state["active_speaker"],
                }
            )
        elif kind == "open_vote":
            self._require_facilitator(state, actor)
            if state["vote"] is not None:
                raise ValueError("a vote is already open")
            question = self._text(action.get("question"), 500, "question")
            choices = action.get("choices")
            if (
                not isinstance(choices, list)
                or len(choices) < 2
                or len(choices) > 6
            ):
                raise ValueError("choices must contain 2-6 items")
            clean_choices = [
                self._text(choice, 100, "choice") for choice in choices
            ]
            state["vote"] = {
                "id": self._id(state),
                "question": question,
                "choices": clean_choices,
                "ballots": {},
                "status": "open",
            }
            events.append({"kind": "vote_opened", "vote": state["vote"]})
        elif kind == "cast_vote":
            vote = state["vote"]
            if not isinstance(vote, dict) or vote.get("status") != "open":
                raise ValueError("no vote is open")
            choice = action.get("choice")
            if (
                not isinstance(choice, int)
                or isinstance(choice, bool)
                or choice < 0
                or choice >= len(vote["choices"])
            ):
                raise ValueError("choice is out of range")
            vote["ballots"][actor["public_key"]] = choice
            events.append(
                {
                    "kind": "vote_cast",
                    "public_key": actor["public_key"],
                    "choice": choice,
                }
            )
        elif kind == "close_vote":
            self._require_facilitator(state, actor)
            vote = state["vote"]
            if not isinstance(vote, dict):
                raise ValueError("no vote is open")
            counts = [0 for _ in vote["choices"]]
            for choice in vote["ballots"].values():
                counts[choice] += 1
            closed = {
                **vote,
                "status": "closed",
                "counts": counts,
            }
            state["vote_history"].append(closed)
            state["vote"] = None
            events.append({"kind": "vote_closed", "vote": closed})
        elif kind == "add_action_item":
            text = self._text(action.get("text"), 500, "text")
            assignee = action.get("assignee") or actor["public_key"]
            if not self._is_active_member(state, assignee):
                raise ValueError("assignee must be a member public key")
            item = {
                "id": self._id(state),
                "text": text,
                "assignee": assignee,
                "created_by": actor["public_key"],
                "done": False,
            }
            state["action_items"].append(item)
            events.append({"kind": "action_item_added", "item": item})
        elif kind == "complete_action_item":
            item = self._find_by_id(
                state["action_items"], action.get("item_id")
            )
            if (
                actor["public_key"] != state["facilitator"]
                and actor["public_key"] != item["assignee"]
            ):
                raise ValueError("only the facilitator or assignee can complete it")
            item["done"] = True
            events.append({"kind": "action_item_completed", "item_id": item["id"]})
        elif kind == "transfer_facilitator":
            self._require_facilitator(state, actor)
            target = action.get("public_key")
            if not self._is_active_member(state, target):
                raise ValueError("public_key must identify an active member")
            state["facilitator"] = target
            events.append({"kind": "facilitator_transferred", "public_key": target})
        elif kind == "end_meeting":
            self._require_facilitator(state, actor)
            state["ended"] = True
            events.append({"kind": "meeting_ended"})
            return {
                "state": state,
                "events": events,
                "completed": True,
                "outcome": "ended",
            }
        else:
            raise ValueError("unsupported meeting action")
        return {"state": state, "events": events}

    def proto_group_view(self, state, viewer):
        vote = state["vote"]
        public_vote = None
        if isinstance(vote, dict):
            counts = [0 for _ in vote["choices"]]
            for choice in vote["ballots"].values():
                counts[choice] += 1
            public_vote = {
                "id": vote["id"],
                "question": vote["question"],
                "choices": vote["choices"],
                "counts": counts,
                "ballot_count": len(vote["ballots"]),
                "my_choice": vote["ballots"].get(viewer["public_key"]),
                "status": vote["status"],
            }
        return {
            "title": state["title"],
            "facilitator": state["facilitator"],
            "members": state["members"],
            "agenda": state["agenda"],
            "active_agenda": state["active_agenda"],
            "speaking_queue": state["speaking_queue"],
            "active_speaker": state["active_speaker"],
            "vote": public_vote,
            "vote_history": state["vote_history"],
            "action_items": state["action_items"],
            "ended": state["ended"],
            "you": {
                "public_key": viewer["public_key"],
                "seat": viewer["seat"],
                "is_facilitator": viewer["public_key"] == state["facilitator"],
            },
        }

    def proto_group_recovery_snapshot(self, state):
        return state

    def proto_group_restore(self, checkpoint, members, new_epoch):
        del members, new_epoch
        return checkpoint

    def proto_group_on_leader_changed(self, state, old_leader, new_leader):
        return {
            "state": state,
            "events": [
                {
                    "kind": "network_leader_changed",
                    "old_leader": old_leader,
                    "new_leader": new_leader,
                    "facilitator": state["facilitator"],
                }
            ],
        }

    def _require_facilitator(self, state, actor):
        if actor["public_key"] != state["facilitator"]:
            raise ValueError("this action requires the meeting facilitator")

    def _is_active_member(self, state, public_key):
        return (
            isinstance(public_key, str)
            and isinstance(state["members"].get(public_key), dict)
            and state["members"][public_key].get("status") == "active"
        )

    def _advance_speaker(self, state):
        if not state["active_speaker"] and state["speaking_queue"]:
            state["active_speaker"] = state["speaking_queue"].pop(0)

    def _id(self, state):
        value = state["next_id"]
        state["next_id"] += 1
        return value

    def _find_by_id(self, items, item_id):
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            raise ValueError("item_id must be an integer")
        for item in items:
            if item["id"] == item_id:
                return item
        raise ValueError("item does not exist")

    def _text(self, value, limit, field):
        if not isinstance(value, str):
            raise ValueError(f"{field} must be text")
        text = value.strip()
        if not text or len(text.encode("utf-8")) > limit:
            raise ValueError(f"{field} must be 1-{limit} UTF-8 bytes")
        return text
