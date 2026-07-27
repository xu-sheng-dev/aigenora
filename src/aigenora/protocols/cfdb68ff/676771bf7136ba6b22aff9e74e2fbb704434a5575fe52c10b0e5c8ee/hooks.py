from __future__ import annotations

from typing import Any

from aigenora.proto.hooks import ProtocolHooks


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")

    def proto_host_metadata(self):
        return (
            "Community Room",
            "chat,multiplayer,community-room",
            "chat",
            {"history_limit": 200, "initial_topic": "Community room"},
        )

    def proto_group_initial_state(self, members):
        return {
            "topic": str(self.options.get("initial_topic") or "Community room")[:200],
            "messages": [],
            "next_message_id": 1,
            "history_limit": int(self.options.get("history_limit", 200)),
            "presence": {
                member["public_key"]: {
                    "seat": member["seat"],
                    "status": "online",
                }
                for member in members
            },
            "closed": False,
        }

    def proto_group_member_joined(self, state, member):
        state["presence"][member["public_key"]] = {
            "seat": member["seat"],
            "status": "online",
        }
        event = {
            "kind": "member_joined",
            "public_key": member["public_key"],
            "seat": member["seat"],
        }
        self._append_system(state, f"Seat {member['seat']} joined the room")
        return {"state": state, "events": [event]}

    def proto_group_member_left(self, state, member, reason):
        current = state["presence"].setdefault(
            member["public_key"], {"seat": member["seat"]}
        )
        current["status"] = "left"
        self._append_system(state, f"Seat {member['seat']} left the room")
        return {
            "state": state,
            "events": [
                {
                    "kind": "member_left",
                    "public_key": member["public_key"],
                    "seat": member["seat"],
                    "reason": reason,
                }
            ],
        }

    def proto_group_handle(self, state, actor, action):
        kind = action.get("kind")
        if kind == "send":
            text = self._text(action.get("text"), limit=2000, field="text")
            message = {
                "id": state["next_message_id"],
                "kind": "message",
                "from": actor["public_key"],
                "seat": actor["seat"],
                "text": text,
            }
            state["next_message_id"] += 1
            state["messages"].append(message)
            self._trim(state)
            return {
                "state": state,
                "events": [{"kind": "message", "message": message}],
            }
        if kind == "set_topic":
            topic = self._text(action.get("topic"), limit=200, field="topic")
            state["topic"] = topic
            self._append_system(state, f"Seat {actor['seat']} changed the topic")
            return {
                "state": state,
                "events": [
                    {
                        "kind": "topic_changed",
                        "topic": topic,
                        "seat": actor["seat"],
                    }
                ],
            }
        if kind == "close_room":
            if actor["seat"] != 0:
                raise ValueError("only the room owner at seat 0 can close the room")
            state["closed"] = True
            self._append_system(state, "The room was closed")
            return {
                "state": state,
                "events": [{"kind": "room_closed", "seat": actor["seat"]}],
                "completed": True,
                "outcome": "closed",
            }
        raise ValueError("kind must be send, set_topic, or close_room")

    def proto_group_view(self, state, viewer):
        return {
            "topic": state["topic"],
            "messages": state["messages"],
            "presence": state["presence"],
            "closed": state["closed"],
            "you": {
                "public_key": viewer["public_key"],
                "seat": viewer["seat"],
            },
        }

    def proto_group_recovery_snapshot(self, state):
        return state

    def proto_group_restore(self, checkpoint, members, new_epoch):
        del members, new_epoch
        return checkpoint

    def proto_group_on_leader_changed(self, state, old_leader, new_leader):
        self._append_system(
            state,
            f"Authority moved from {old_leader[:8]} to {new_leader[:8]}",
        )
        return {
            "state": state,
            "events": [
                {
                    "kind": "leader_changed",
                    "old_leader": old_leader,
                    "new_leader": new_leader,
                }
            ],
        }

    def _append_system(self, state: dict[str, Any], text: str) -> None:
        state["messages"].append(
            {
                "id": state["next_message_id"],
                "kind": "system",
                "from": "",
                "seat": -1,
                "text": text,
            }
        )
        state["next_message_id"] += 1
        self._trim(state)

    def _trim(self, state: dict[str, Any]) -> None:
        limit = max(20, min(500, int(state["history_limit"])))
        if len(state["messages"]) > limit:
            state["messages"] = state["messages"][-limit:]

    def _text(self, value: Any, *, limit: int, field: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be text")
        text = value.strip()
        if not text or len(text.encode("utf-8")) > limit:
            raise ValueError(f"{field} must be 1-{limit} UTF-8 bytes")
        return text
