from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class HookResult:
    response: dict[str, Any] | None = None
    game_over: bool = False
    abort: bool = False


class ProtocolHooks(ABC):
    bus: Any  # DecisionBus | None
    snapshot: Any  # SnapshotBus
    details: Any  # DetailLog
    strategy: Any  # StrategyStore

    def proto_init(
        self,
        options: dict[str, Any],
        role: str,
        args: list[str],
        state_dir: Path,
        decision_config: dict[str, Any] | None = None,
    ) -> None:
        from aigenora.proto.sdk import DetailLog, SnapshotBus, StrategyStore

        self.options = options
        self.role = role
        self.args = args
        self.state_dir = state_dir
        self.snapshot = SnapshotBus(state_dir)
        self.details = DetailLog(state_dir)
        self.strategy = StrategyStore(state_dir)
        self.timing = None  # v004: spec.timing, set by engine after proto_init
        self.bus = None
        if decision_config and decision_config.get("mode") == "manual" and not args:
            from aigenora.proto.sdk import DecisionBus

            self.bus = DecisionBus(
                state_dir,
                timeout=decision_config.get("timeout_seconds", 120),
                timeout_action=decision_config.get("timeout_action", "forfeit"),
                fallback_value=decision_config.get("fallback_value"),
            )

    def proto_host_metadata(self) -> tuple[str, str, str, dict[str, Any]]:
        return ("Game", "game", "supply", {})

    def proto_host_handle_join(self, msg: dict[str, Any]) -> HookResult:
        return HookResult({"action": "ready"})

    def proto_host_handle(self, msg: dict[str, Any]) -> HookResult:
        return HookResult(abort=True)

    def proto_guest_join_message(self) -> dict[str, Any]:
        return {"action": "join"}

    def proto_guest_handle_ready(self, msg: dict[str, Any]) -> None:
        return None

    def proto_guest_first_action(self) -> dict[str, Any] | None:
        return None

    def proto_guest_handle(self, msg: dict[str, Any]) -> HookResult:
        return HookResult(abort=True)

    def proto_display(self, msg: dict[str, Any], direction: str) -> str | None:
        """Override to return a human-readable string for a message.

        direction: "sent" or "received"
        Return None to skip display.
        """
        return None

    def proto_on_message(self, msg: dict[str, Any]) -> None:
        """Free mode: callback when a peer message is received."""

    def proto_on_send(self, msg: dict[str, Any]) -> None:
        """Free mode: engine callback after locally sending a message (introduced in v006 P5).

        Symmetric with proto_on_message, giving hooks a chance to write the
        "message sent by our side" into observation channels such as SnapshotBus / DetailLog.
        Default empty implementation; existing protocols need no changes.
        """

    def proto_on_end(self) -> None:
        """Free mode: callback when the session ends."""

    # -- simultaneous_round hooks --

    def proto_round_value(self, round_index: int, state: dict) -> str | int | bool:
        """simultaneous_round: return the business value for this round.

        The engine calls this at the start of each round, generating a commit hash
        after obtaining our side's value.
        round_index starts from 0. state is the shared state dict.
        """
        raise NotImplementedError("proto_round_value must be overridden for simultaneous_round")

    def proto_round_judge(
        self,
        round_index: int,
        host_value: str | int | bool,
        guest_value: str | int | bool,
        state: dict,
    ) -> HookResult:
        """simultaneous_round: judge the result of this round.

        The engine calls the Host-side judge after both sides' reveal passes validation.
        Returns a HookResult containing a round_result message and an optional game_over flag.
        """
        raise NotImplementedError("proto_round_judge must be overridden for simultaneous_round")

    # -- v004: timing helpers --

    @property
    def timing_enabled(self) -> bool:
        return self.timing is not None and self.timing.get("mode") != "none"

    def _update_timing_snapshot(self, match_key: str, match_value, release_at: float,
                                 deadline_at: float, phase: str = "waiting") -> None:
        self.snapshot.update(
            timing={"match_key": match_key, "match_value": match_value,
                     "release_at": release_at, "deadline_at": deadline_at, "phase": phase},
        )

    def _clear_timing_snapshot(self) -> None:
        self.snapshot.update(timing={})
