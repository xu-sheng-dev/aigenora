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
    # v019-M2: dead code（bridge 从未读取），保留只为兼容；一律以 DECISION_SCHEMA.numeric 为准。
    WHISPER_NUMERIC: bool = False
    # v019-M2: 协议声明决策 schema（当前代码库不存在，M2 全新引入）。子类按需覆盖。
    # 形如 {"match_key":"round","value_field":"choice","choices":{...},"policy_family":"rps","beats":{...}}
    DECISION_SCHEMA: dict[str, Any] | None = None

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

    # -- v012/v019: hybrid 干预（auto 模式默认可干预，不阻塞） --

    def _consume_hybrid(self, match_key: str, match_value: Any) -> dict | None:
        """默认 hybrid 干预入口（auto 模式）：返回匹配的已提交 decision dict，无则 None。

        v019-M1: 改用定向消费 consume_latest_for_match()，不再用 read_pending() 全量推进 offset，
        避免当前轮读到未来轮 decision 时把未来 record 一并越过。同时发布 decision/state.json，
        让 Web/CLI/producer 知道当前窗口；consumed/fallback 后写 finalized 关闭窗口。

        与 --coach 的 await_latest_decision 区别：**不等 max_think**。
        - ``min_think_seconds == 0``（默认）：定向消费一次（非阻塞），无决策立即返回 None，
          下一层用 auto 值——对局毫秒级自动推进，但人工通过 ``session decide`` 提前提交的决策
          会被读到并生效。
        - ``min_think_seconds > 0``：poll 到该秒数给一个干预窗口，到时仍无决策立即返回 None。
        - ``session strategy``（持续策略）在协议的 ``_pick_auto`` 里即时生效，是默认模式下
          "随时改打法"的主要手段；本方法只覆盖"单次 decide 提前提交"。

        想逐手实时盯着每一步打，用 ``--coach``（走 ``await_latest_decision``，hold min + 等 max）。
        """
        if self.bus is None:
            return None
        from aigenora.proto.sdk import EventBus

        bus = EventBus(self.state_dir)
        min_think = float(self.options.get("min_think_seconds", 0) or 0)
        poll = getattr(self.bus, "POLL_INTERVAL", 0.2)
        now = time.monotonic()
        release_at = now
        deadline_at = now + min_think

        # v019-M1: 发布当前窗口状态，让 Web/CLI 知道该写哪个 match_key/match_value
        self.bus.publish_state({
            "match_key": match_key,
            "match_value": match_value,
            "mode": "hybrid",
            "release_at": release_at,
            "deadline_at": deadline_at,
            "waiting_for": "decision",
        })
        bus.emit("local_decision_window_started", data={
            "match_key": match_key, "match_value": match_value,
            "mode": "hybrid",
            "release_at": release_at, "deadline_at": deadline_at,
        })

        # v019-M3: materialize 适用于当前窗口的 pending intents / active policy（M2/M3 接入点）
        self._materialize_pending_intents(match_key, match_value)
        self._materialize_active_policy(match_key, match_value)

        # 定向消费（非阻塞或短窗口轮询）
        def _try_consume() -> dict | None:
            return self.bus.consume_latest_for_match(match_key, match_value, consumer_id=f"hybrid:{self.role}")

        if min_think > 0:
            deadline = time.monotonic() + min_think
            while time.monotonic() < deadline:
                d = _try_consume()
                if d is not None:
                    self.bus.finalize_window(match_key, match_value, "consumed")
                    bus.emit("local_decision_consumed", data={
                        "match_key": match_key, "match_value": match_value,
                        "decision": d, "origin": (d.get("_meta") or {}).get("origin"),
                    })
                    return d
                time.sleep(poll)
        d = _try_consume()
        if d is not None:
            self.bus.finalize_window(match_key, match_value, "consumed")
            bus.emit("local_decision_consumed", data={
                "match_key": match_key, "match_value": match_value,
                "decision": d, "origin": (d.get("_meta") or {}).get("origin"),
            })
            return d

        # 无 decision：fallback，写 finalized 关闭窗口
        self.bus.finalize_window(match_key, match_value, "fallback")
        bus.emit("local_decision_fallback", data={
            "match_key": match_key, "match_value": match_value, "reason": "no_decision",
        })
        return None

    # v019-M2/M3 接入点：默认空实现，由 M2/M3 填充。先占位保证 _consume_hybrid 可调用。
    def _materialize_pending_intents(self, match_key: str, match_value: Any) -> None:
        """窗口打开时 materialize 适用于当前窗口的 pending intents。

        v019-M2: 读取 InterventionIntentStore，把适用于当前窗口的 pending intent
        落成 strategy/decision/policy。不得覆盖更高优先级 explicit decision。
        """
        try:
            from aigenora.proto.intervention_intent import InterventionIntentStore
            store = InterventionIntentStore(self.state_dir)
            pending = store.pending_for_window(match_key, match_value)
            for intent_rec in pending:
                self._materialize_one_intent(intent_rec, match_key, match_value, store)
        except Exception:
            pass  # intent materialize 是辅助，不影响主流程

    def _materialize_one_intent(self, intent_rec: dict, match_key: str, match_value: Any, store: Any) -> None:
        """materialize 单条 intent。"""
        from aigenora.proto.decide_gateway import submit_decision
        from aigenora.proto.sdk import EventBus

        intent_id = intent_rec.get("intent_id")
        policy = intent_rec.get("policy")
        value = intent_rec.get("value")
        meta = intent_rec.get("_meta") or {}
        bus = EventBus(self.state_dir)

        try:
            if policy is not None:
                # 一次性策略 intent（如"下一轮克制对方上一轮"）：
                # 目标窗口已打开，直接运行策略/脚本产出 decision。
                # 先检查当前窗口是否已有显式 decision；若有，跳过（不覆盖人工临时战术）。
                if self.bus is not None:
                    existing = self.bus.peek_latest_for_match(match_key, match_value)
                    if existing is not None and (existing.get("_meta") or {}).get("origin") not in ("policy_runner", "script_runner", None):
                        store.mark(intent_id, "materialized", reason="explicit_decision_present")
                        bus.emit("intent_materialized", data={"intent_id": intent_id, "reason": "explicit_decision_present"})
                        return

                # 构造 context 并运行策略
                schema = self.get_decision_schema()
                context = self.build_decision_context(match_key, match_value)
                if not context.get("supported"):
                    store.mark(intent_id, "failed", reason="unsupported_context")
                    bus.emit("intent_failed", data={"intent_id": intent_id, "reason": "unsupported_context"})
                    return

                policy_mode = policy.get("mode", "policy")
                if policy_mode == "script":
                    # 脚本策略：经引擎沙箱运行
                    from aigenora.proto import script_runner
                    timeout_ms = int(policy.get("timeout_ms", 1000))
                    result = script_runner.run_script(
                        policy, context, state_dir=str(self.state_dir), schema=schema, timeout_ms=timeout_ms,
                    )
                else:
                    # 内置策略：调协议 run_policy
                    result = self.run_policy(policy, context)

                if not result.get("ok"):
                    reason = result.get("reason", "unknown")
                    store.mark(intent_id, "failed", reason=reason)
                    bus.emit("intent_failed", data={"intent_id": intent_id, "reason": reason})
                    return

                decision = result.get("decision") or {}
                mk_field = schema.get("match_key", match_key)
                if mk_field not in decision:
                    decision[mk_field] = match_value
                elif decision[mk_field] != match_value:
                    store.mark(intent_id, "failed", reason="match_mismatch")
                    bus.emit("intent_failed", data={"intent_id": intent_id, "reason": "match_mismatch"})
                    return

                origin = "script_runner" if policy_mode == "script" else "policy_runner"
                res = submit_decision(
                    str(self.state_dir), decision,
                    origin=origin, agent_id=meta.get("agent_id"),
                    caused_by_whisper_id=meta.get("caused_by_whisper_id"),
                    require_match_key=True,
                )
                if res.get("ok"):
                    store.mark(intent_id, "materialized", reason="policy_decision_written",
                               detail={"decision_id": res.get("decision_id")})
                    bus.emit("intent_materialized", data={
                        "intent_id": intent_id, "decision_id": res.get("decision_id"),
                        "match_key": match_key, "match_value": match_value, "origin": origin,
                    })
                    bus.emit("policy_generated", data={
                        "match_key": match_key, "match_value": match_value,
                        "decision": decision, "decision_id": res.get("decision_id"),
                        "origin": origin, "reason": result.get("reason"),
                        "source": "intent",
                    })
                else:
                    store.mark(intent_id, "failed", reason=res.get("reason") or "submit_failed")
                    bus.emit("intent_failed", data={"intent_id": intent_id, "reason": res.get("reason")})
                return

            if value is not None:
                # 固定值 intent → 写 decision
                res = submit_decision(
                    str(self.state_dir),
                    {match_key: match_value, **value},
                    origin="intent_materialize",
                    agent_id=meta.get("agent_id"),
                    caused_by_whisper_id=meta.get("caused_by_whisper_id"),
                    require_match_key=True,
                )
                if res.get("ok"):
                    store.mark(intent_id, "materialized", reason="decision_written", detail={"decision_id": res.get("decision_id")})
                    bus.emit("intent_materialized", data={"intent_id": intent_id, "decision_id": res.get("decision_id"), "match_key": match_key, "match_value": match_value})
                else:
                    store.mark(intent_id, "failed", reason=res.get("reason") or "submit_failed")
                    bus.emit("intent_failed", data={"intent_id": intent_id, "reason": res.get("reason")})
        except Exception as e:
            store.mark(intent_id, "failed", reason=f"exception:{type(e).__name__}")
            bus.emit("intent_failed", data={"intent_id": intent_id, "reason": str(e)})

    def _materialize_active_policy(self, match_key: str, match_value: Any) -> None:
        """v019-M3: 窗口打开时 materialize active policy/script 为当前窗口 decision。

        引擎编排（不含策略逻辑）：
        1. 读 StrategyStore，若 mode 不是 policy/script，返回。
        2. 先检查当前窗口是否已有未消费的显式 decision；若有，skip。
        3. 调 build_decision_context() 构造 context（协议负责）。
        4. mode=policy → 调 self.run_policy()（协议实现）。
           mode=script → 调 script_runner.run_script()（引擎沙箱）。
        5. 校验输出 decision，调 submit_decision() 写入。
        """
        try:
            from aigenora.proto.sdk import EventBus
            from aigenora.proto.decide_gateway import submit_decision
            from aigenora.proto import script_runner

            strat = self.strategy.read() or {}
            mode = strat.get("mode")
            if mode not in ("policy", "script"):
                return

            bus = EventBus(self.state_dir)

            # 1. 已有显式 decision 则 skip
            if self.bus is not None:
                existing = self.bus.peek_latest_for_match(match_key, match_value)
                if existing is not None and (existing.get("_meta") or {}).get("origin") not in ("policy_runner", "script_runner", None):
                    bus.emit("policy_skipped", data={
                        "match_key": match_key, "match_value": match_value,
                        "reason": "explicit_decision_present",
                    })
                    return

            # 2. 构造 context
            schema = self.get_decision_schema()
            context = self.build_decision_context(match_key, match_value)
            if not context.get("supported"):
                bus.emit("policy_failed", data={
                    "match_key": match_key, "match_value": match_value,
                    "reason": "unsupported_context",
                })
                return

            # 3. 运行策略
            if mode == "policy":
                result = self.run_policy(strat, context)
            else:
                timeout_ms = int(strat.get("timeout_ms", 1000))
                result = script_runner.run_script(
                    strat, context, state_dir=str(self.state_dir), schema=schema, timeout_ms=timeout_ms,
                )

            # 4. 处理结果
            if not result.get("ok"):
                reason = result.get("reason", "unknown")
                event_name = "policy_timeout" if reason == "policy_timeout" else "policy_failed"
                bus.emit(event_name, data={
                    "match_key": match_key, "match_value": match_value,
                    "reason": reason, "script_id": strat.get("script_id"),
                })
                return

            decision = result.get("decision") or {}
            # 5. 校验 match key/value
            mk_field = schema.get("match_key", match_key)
            if mk_field not in decision:
                decision[mk_field] = match_value
            elif decision[mk_field] != match_value:
                bus.emit("policy_failed", data={
                    "match_key": match_key, "match_value": match_value,
                    "reason": "match_mismatch", "got": decision[mk_field],
                })
                return

            # 6. 写入 decision
            origin = "policy_runner" if mode == "policy" else "script_runner"
            res = submit_decision(
                str(self.state_dir), decision,
                origin=origin, agent_id=strat.get("_meta", {}).get("agent_id"),
                caused_by_whisper_id=strat.get("_meta", {}).get("caused_by_whisper_id"),
                require_match_key=True,
            )
            if res.get("ok"):
                bus.emit("policy_generated", data={
                    "match_key": match_key, "match_value": match_value,
                    "decision": decision, "decision_id": res.get("decision_id"),
                    "origin": origin, "reason": result.get("reason"),
                })
            else:
                bus.emit("policy_failed", data={
                    "match_key": match_key, "match_value": match_value,
                    "reason": res.get("reason") or "submit_failed",
                })
        except Exception:
            pass  # policy materialize 是辅助，不影响主流程

    # -- v019-M3: 协议策略接口（默认实现，协议覆盖） --

    def get_decision_schema(self) -> dict:
        """返回协议的 DECISION_SCHEMA。无 schema 时从 CHOICE_KEYWORDS 兼容生成。"""
        if self.DECISION_SCHEMA:
            return self.DECISION_SCHEMA
        if self.CHOICE_KEYWORDS:
            return {"match_key": "round", "value_field": "choice", "choices": self.CHOICE_KEYWORDS}
        return {}

    def build_decision_context(self, match_key: str, match_value: Any) -> dict:
        """构造决策上下文。默认返回 unsupported；协议覆盖后提供历史/合法动作。

        IO 契约：优先读内存缓存（self._last_round_context），回退 details.jsonl 只 seek 末尾 N 行。
        """
        return {"supported": False, "reason": "unsupported_context"}

    def run_policy(self, strategy: dict, context: dict) -> dict:
        """协议内置策略逻辑（同步快路径）。默认返回 unsupported_policy。

        引擎不解释 policy 字符串、不读 beats；由协议自己实现 mirror/counter/repeat 等。
        """
        return {"ok": False, "reason": "unsupported_policy"}

    def _read_last_details(self, n: int = 1) -> list[dict]:
        """基类 helper：seek 读 details.jsonl 末尾 N 行，不全量扫描。"""
        import json as _json
        fp = Path(self.state_dir) / "details.jsonl"
        if not fp.exists():
            return []
        try:
            lines = fp.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        result = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                result.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue
        return result

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

