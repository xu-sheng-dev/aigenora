from __future__ import annotations

import time
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class HookResult:
    response: dict[str, Any] | None = None
    completed: bool = False
    abort: bool = False


class ProtocolHooks(ABC):
    bus: Any  # DecisionBus | None
    snapshot: Any  # SnapshotBus
    details: Any  # DetailLog
    strategy: Any  # StrategyStore

    # v015 whisper 桥：协议声明的选项关键词，用于把 whisper 自然语言解析成 strategy/decide。
    # 子类按需覆盖，形如 {"rock": ["rock", "石头", "r"], ...}。None 表示该协议不支持 whisper 解析。
    CHOICE_KEYWORDS: dict[str, list[str]] | None = None
    # 数字类协议（guess-number/weak-wins-all）设为 True，whisper 含数字时按数字解析
    WHISPER_NUMERIC: bool = False

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
        self.decision_mode = None  # None | "auto" | "manual"
        # auto 模式也创建 bus：默认 hybrid（毫秒级 auto + 人工可干预）；
        # manual（--coach）才阻塞逐手等待。命令行带 fallback 策略参数（args 非空）时不建 bus。
        if decision_config and not args:
            mode = decision_config.get("mode")
            if mode in ("manual", "auto"):
                from aigenora.proto.sdk import DecisionBus

                self.bus = DecisionBus(
                    state_dir,
                    timeout=decision_config.get("timeout_seconds", 120),
                    timeout_action=decision_config.get("timeout_action", "forfeit"),
                    fallback_value=decision_config.get("fallback_value"),
                )
                self.decision_mode = mode

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

    # -- mental_poker hooks (v016) --
    # Engine-owned control plane: all mp_* wire messages are sent by _run_mental_poker_*.
    # Hooks only supply business material + per-turn intent. Private views (hand, deck
    # state) are injected by the engine via the shared ``state`` dict (no proto_ctx).

    def proto_mp_deck_universe(self) -> list[tuple[int, int]]:
        """mental_poker: return the full deck as a list of (rank_index, suit_index).

        The engine encodes each card (aead_deck.encode_card) and seals the inner layer
        to build blob_A. Only the Host role is asked (Host is the inner encryptor).
        """
        raise NotImplementedError("proto_mp_deck_universe must be overridden for mental_poker")

    def proto_mp_initial_deal(self, state: dict) -> dict:
        """mental_poker: return the initial deal plan, e.g. {"host": 5, "guest": 5}.

        The engine runs one OT per dealt card. ``state`` carries the engine-injected
        read-only view (_mp_role / _mp_deck_view).
        """
        raise NotImplementedError("proto_mp_initial_deal must be overridden for mental_poker")

    def proto_mp_choose_action(self, state: dict) -> dict:
        """mental_poker: choose this turn's action.

        Return one of:
          {"kind": "play", "id_b": "<id-B of a card in hand>"}
          {"kind": "draw"}
          {"kind": "pass"}
        ``state`` carries the engine-injected private view, including ``_mp_hand``
        (set of id-B in hand) and ``_mp_hand_cards`` (id-B -> (rank, suit), filled
        after each disclosure). The engine validates the play via mental_poker.validate_play.
        """
        raise NotImplementedError("proto_mp_choose_action must be overridden for mental_poker")

    def proto_mp_check_winner(self, state: dict) -> str | None:
        """mental_poker: return "host" / "guest" if the game has a winner, else None.

        Called by the engine after each turn; the engine ends the play loop and moves
        to the audit phase once a winner is returned. When both sides stall
        (``state["_mp_stalled"]`` is True — consecutive passes), return the winner by
        the game's tie-break rule (e.g. fewer cards in hand).
        """
        raise NotImplementedError("proto_mp_check_winner must be overridden for mental_poker")

    def proto_mp_validate_play(self, state: dict, who: str, play_msg: dict[str, Any]):
        """mental_poker: validate a peer's play against game rules (M2 双向校验).

        Called by the engine AFTER cryptographic verification (keys/blob/card face)
        passes and BEFORE ``apply_play``. ``who`` is the player who played ("host"/
        "guest"), ``play_msg`` is the verified ``mp_play`` dict (id_b/rank/suit/
        k_inner/k_outer, plus optional ``call_suit``).

        Return ``mental_poker.ValidationResult(False, reason)`` to reject (the engine
        sends an ``error`` and aborts); return ``ValidationResult(True)`` to accept.
        Default accepts everything (M1 test protocols have no rules); real games
        override to enforce ``can_play`` / ``call_suit`` rules.
        """
        from . import mental_poker

        return mental_poker.ValidationResult(True)

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

    # -- v012: hybrid 干预（auto 模式默认可干预，不阻塞） --

    def _consume_hybrid(self, match_key: str, match_value: Any) -> dict | None:
        """默认 hybrid 干预入口（auto 模式）：返回匹配的已提交 decision dict，无则 None。

        与 --coach 的 await_latest_decision 区别：**不等 max_think**。
        - ``min_think_seconds == 0``（默认）：``read_pending`` 一次（非阻塞），无决策立即返回 None，
          下一层用 auto 值——对局毫秒级自动推进，但人工通过 ``session decide`` 提前提交的决策
          会被读到并生效。
        - ``min_think_seconds > 0``：poll 到该秒数给一个干预窗口，到时仍无决策立即返回 None。
        - ``session strategy``（持续策略）在协议的 ``_pick_auto`` 里即时生效，是默认模式下
          "随时改打法"的主要手段；本方法只覆盖"单次 decide 提前提交"。

        想逐手实时盯着每一步打，用 ``--coach``（走 ``await_latest_decision``，hold min + 等 max）。
        """
        if self.bus is None:
            return None
        # hybrid 默认 min_think=0（毫秒级）；用户可通过 options.min_think_seconds 配短干预窗口。
        # 不读 spec.timing.min_think_seconds（那是 --coach 的 hold 默认值，默认 1s 会拖慢 hybrid）。
        min_think = float(self.options.get("min_think_seconds", 0) or 0)
        poll = getattr(self.bus, "POLL_INTERVAL", 0.2)
        if min_think > 0:
            deadline = time.monotonic() + min_think
            while time.monotonic() < deadline:
                for d in self.bus.read_pending():
                    if d.get(match_key) == match_value:
                        return d
                time.sleep(poll)
        for d in self.bus.read_pending():
            if d.get(match_key) == match_value:
                return d
        return None

    # -- v015: whisper 桥 + 战术生效反馈 --

    def _emit_strategy_applied(self, round_index: int, strategy: dict | None, result: Any) -> None:
        """emit strategy_applied 事件，让用户看到"战术已生效"。

        在各协议 _pick_auto/_bid_auto 的 return 前调用。strategy 为 None 表示用默认随机。
        """
        try:
            from aigenora.proto.sdk import EventBus
            EventBus(self.state_dir).emit("strategy_applied", {
                "round": round_index,
                "strategy": strategy or {},
                "result": result,
            })
        except Exception:
            pass  # 事件反馈是辅助功能，不影响主流程

    def _resolve_whisper_override(self, match_key: str, match_value: Any) -> tuple[Any, dict | None] | None:
        """读取 strategy.operator_hint，尝试解析成 override 值。

        返回 (override_value, source_command) 或 None（无 hint 或无法解析）。
        override_value 直接可用于 _pick_auto 的返回；source_command 是解析出的
        {"type":..., "payload":...} 供审计。

        本方法只读 operator_hint 字符串，不写入文件（写入由 web.py/CLI 的 whisper
        入口负责，这里只做 hooks 侧的兜底解析）。
        """
        try:
            strat = self.strategy.read() or {}
        except Exception:
            return None
        hint = strat.get("operator_hint")
        if not hint or not isinstance(hint, str) or not hint.strip():
            return None
        # 延迟导入避免循环依赖
        from aigenora.proto.whisper_bridge import parse_whisper_to_command
        cmd = parse_whisper_to_command(
            hint,
            self.CHOICE_KEYWORDS,
            match_key=match_key,
            match_value=match_value,
        )
        if cmd is None:
            return None
        payload = cmd.get("payload") or {}
        # 只处理持久 strategy（operator_hint 本身是持久的）；单次 decide 由 web/CLI 入口提交
        if cmd.get("type") != "strategy":
            return None
        fixed = payload.get("fixed")
        if fixed is None:
            return None
        return (fixed, cmd)

