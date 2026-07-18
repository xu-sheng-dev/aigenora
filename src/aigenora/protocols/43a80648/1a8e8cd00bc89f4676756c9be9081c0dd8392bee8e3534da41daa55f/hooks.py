"""Rock-Paper-Scissors hooks for simultaneous_round engine.

The engine handles commit-reveal / nonce / hash / barrier / reveal validation,
hooks only need to provide the move (proto_round_value) and the judge (proto_round_judge).
"""
from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

from aigenora.proto.hooks import HookResult, ProtocolHooks
from aigenora.proto.sdk import StateStore


CHOICES = ["rock", "paper", "scissors"]
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
TERMINATIONS = ["first_to_win", "fixed_rounds"]
CHOICE_LABEL = {"rock": "Rock", "paper": "Paper", "scissors": "Scissors"}
# v015 whisper 桥：每个选项的中英别名，用于把 "一直出布" 解析成 fixed:paper
CHOICE_KEYWORDS = {
    "rock": ["rock", "石头", "石", "r"],
    "paper": ["paper", "布", "纸", "p"],
    "scissors": ["scissors", "剪刀", "剪", "s"],
}


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")
    CHOICE_KEYWORDS = CHOICE_KEYWORDS
    # v019-M4: 声明决策 schema。beats 由本协议 run_policy 解释，引擎不读。
    DECISION_SCHEMA = {
        "match_key": "round",
        "value_field": "choice",
        "choices": CHOICE_KEYWORDS,
        "strategy_field": "fixed",
        "policy_family": "rps",
        "beats": BEATS,
    }

    def proto_init(self, options, role, args, state_dir: Path, decision_config: dict[str, Any] | None = None):
        super().proto_init(options, role, args, state_dir, decision_config)
        self.state = StateStore(state_dir)
        self.best_of = int(options.get("best_of") or 3)
        termination = options.get("termination") or "first_to_win"
        if termination not in TERMINATIONS:
            termination = "first_to_win"
        self.termination = termination
        delay = options.get("round_delay_seconds")
        self.round_delay_seconds = int(delay) if delay is not None else 0
        # rounds_to_win 可独立配置（first_to_win 模式下"先胜 N 局"）；
        # 未显式指定时兜底 best_of//2+1（过半胜），保持向后兼容
        rtw = options.get("rounds_to_win")
        self.rounds_to_win = int(rtw) if rtw else self.best_of // 2 + 1
        self.host_wins = 0
        self.guest_wins = 0
        self.fallback_strategy: str = args[0] if args else "random"
        self._last_round_context: dict | None = None  # v019-M4: 缓存上一轮 context
        self.snapshot.update(
            score={"host": 0, "guest": 0},
            round=1,
            best_of=self.best_of,
            termination=self.termination,
            rounds_to_win=self.rounds_to_win,
        )

    # v019-M4: 动态策略 context 与 run_policy

    def build_decision_context(self, match_key: str, match_value: Any) -> dict:
        """提供上一轮 self/opponent 动作与结果。优先读内存缓存。"""
        if self._last_round_context:
            ctx = dict(self._last_round_context)
            ctx["match_key"] = match_key
            ctx["match_value"] = match_value
            ctx["value_field"] = "choice"
            ctx["legal_values"] = CHOICES
            ctx["supported"] = True
            return ctx
        # 回退：读 details.jsonl 末尾一条
        last = self._read_last_details(1)
        if last:
            host_c = last.get("host_choice")
            guest_c = last.get("guest_choice")
            if host_c and guest_c:
                winner = last.get("winner")
                self_role = "host" if self.role == "host" else "guest"
                opp_role = "guest" if self_role == "host" else "host"
                return {
                    "supported": True,
                    "match_key": match_key, "match_value": match_value,
                    "value_field": "choice", "legal_values": CHOICES,
                    "previous": {
                        "self": {"choice": host_c if self_role == "host" else guest_c},
                        "opponent": {"choice": guest_c if self_role == "host" else host_c},
                        "result": "win" if winner == self_role else ("loss" if winner and winner != self_role else "draw"),
                    },
                    "history": [],
                }
        return {"supported": False, "reason": "no_context"}

    def run_policy(self, strategy: dict, context: dict) -> dict:
        """RPS 内置策略：mirror/counter/repeat。读 DECISION_SCHEMA.beats 算克制。"""
        if not context.get("supported"):
            return {"ok": False, "reason": "unsupported_context"}
        policy = strategy.get("policy")
        beats = self.DECISION_SCHEMA.get("beats", {})
        opp = context.get("previous", {}).get("opponent", {}).get("choice")
        if not opp:
            return {"ok": False, "reason": "no_context"}
        if policy == "mirror_previous_opponent":
            return {"ok": True, "decision": {"choice": opp}}
        if policy == "counter_previous_opponent":
            counter = next((k for k, v in beats.items() if v == opp), None)
            return {"ok": True, "decision": {"choice": counter}} if counter else {"ok": False, "reason": "no_counter"}
        if policy == "repeat_own_previous":
            own = context.get("previous", {}).get("self", {}).get("choice")
            return {"ok": True, "decision": {"choice": own}} if own else {"ok": False, "reason": "no_context"}
        return {"ok": False, "reason": "unknown_policy"}

    def proto_host_metadata(self):
        return (
            "Rock-Paper-Scissors",
            "game,rps",
            "supply",
            {
                "best_of": self.best_of,
                "termination": self.termination,
                "round_delay_seconds": self.round_delay_seconds,
            },
        )

    # ---- move strategy ----
    def _pick_auto(self, round_index: int) -> str:
        strat = self.strategy.read()
        if strat:
            mode = strat.get("mode", "random")
            if mode == "fixed":
                fixed = strat.get("fixed")
                if fixed in CHOICES:
                    self._emit_strategy_applied(round_index, strat, fixed)
                    return fixed
            elif mode == "seq":
                seq = [x for x in strat.get("sequence", []) if x in CHOICES]
                if seq:
                    pick = seq[round_index % len(seq)]
                    self._emit_strategy_applied(round_index, strat, pick)
                    return pick
            # v015: 无显式 mode 或 mode=random 时，检查 whisper override（operator_hint）
            if mode == "random":
                override = self._resolve_whisper_override("round", round_index)
                if override and override[0] in CHOICES:
                    self._emit_strategy_applied(round_index, override[1], override[0])
                    return override[0]
            result = random.choice(CHOICES)
            self._emit_strategy_applied(round_index, strat, result)
            return result
        # 无 strategy：也检查 whisper override
        override = self._resolve_whisper_override("round", round_index)
        if override and override[0] in CHOICES:
            self._emit_strategy_applied(round_index, override[1], override[0])
            return override[0]
        s = self.fallback_strategy
        if s in CHOICES:
            self._emit_strategy_applied(round_index, None, s)
            return s
        if s.startswith("seq:"):
            seq = [x for x in s[4:].split(",") if x in CHOICES]
            if seq:
                pick = seq[round_index % len(seq)]
                self._emit_strategy_applied(round_index, None, pick)
                return pick
        result = random.choice(CHOICES)
        self._emit_strategy_applied(round_index, None, result)
        return result

    def _pick(self, round_index: int) -> str:
        if self.control_mode == "human":
            decision = self._await_human_decision("round", round_index)
            choice = decision.get("choice")
            if choice not in CHOICES:
                self._reject_human_decision("round", round_index, decision, "choice must be rock, paper, or scissors")
            return choice
        auto = self._pick_auto(round_index)
        if self.bus is None:
            return auto
        # hybrid（auto 模式，默认）：非阻塞读 decide，无则 auto
        if self.decision_mode != "manual":
            d = self._consume_hybrid("round", round_index)
            if d and d.get("choice") in CHOICES:
                return d["choice"]
            return auto
        raise RuntimeError(f"unsupported decision mode: {self.decision_mode}")

    # ---- simultaneous_round hooks ----
    def proto_round_value(self, round_index: int, state: dict) -> str:
        if self.role == "guest" and self.round_delay_seconds > 0 and round_index > 0:
            time.sleep(self.round_delay_seconds)
        return self._pick(round_index)

    def proto_round_judge(self, round_index: int, host_value: str, guest_value: str, state: dict) -> HookResult:
        if host_value == guest_value:
            winner = "draw"
        elif BEATS[host_value] == guest_value:
            winner = "host"
            self.host_wins += 1
        else:
            winner = "guest"
            self.guest_wins += 1
        over, game_winner = self._evaluate_termination(round_index)
        resp = {
            "action": "round_result",
            "round": round_index,
            "host_choice": host_value,
            "guest_choice": guest_value,
            "round_winner": winner,
            "host_wins": self.host_wins,
            "guest_wins": self.guest_wins,
            "game_over": over,
            "game_winner": game_winner,
        }
        self._record_round(round_index, host_value, guest_value, winner, over, game_winner)
        return HookResult(resp, completed=over)

    def _evaluate_termination(self, round_index: int) -> tuple[bool, str]:
        # round_index starts from 0, completed rounds = round_index + 1
        played = round_index + 1
        if self.termination == "fixed_rounds":
            if played >= self.best_of:
                if self.host_wins > self.guest_wins:
                    return True, "host"
                if self.guest_wins > self.host_wins:
                    return True, "guest"
                return True, "draw"
            return False, "none"
        if self.host_wins >= self.rounds_to_win:
            return True, "host"
        if self.guest_wins >= self.rounds_to_win:
            return True, "guest"
        return False, "none"

    def _record_round(self, round_index: int, host_choice: str, guest_choice: str,
                      winner: str, over: bool, game_winner: str) -> None:
        rd = round_index + 1
        # v019-M4: 缓存上一轮 context，供 build_decision_context 读内存
        self_role = "host" if self.role == "host" else "guest"
        opp_role = "guest" if self_role == "host" else "host"
        self_c = host_choice if self_role == "host" else guest_choice
        opp_c = guest_choice if self_role == "host" else host_choice
        result = "win" if winner == self_role else ("loss" if winner and winner != self_role and winner != "draw" else "draw")
        self._last_round_context = {
            "previous": {"self": {"choice": self_c}, "opponent": {"choice": opp_c}, "result": result},
            "history": [],  # 完整 history 由 details.jsonl 补，这里只缓存最近一条
        }
        self.details.append(
            type="round_result",
            round=rd,
            host_choice=host_choice,
            guest_choice=guest_choice,
            winner=winner,
            host_wins=self.host_wins,
            guest_wins=self.guest_wins,
            game_over=over,
            game_winner=game_winner,
            summary=f"R{rd}: {CHOICE_LABEL[host_choice]} vs {CHOICE_LABEL[guest_choice]} -> {winner} ({self.host_wins}-{self.guest_wins})",
        )
        next_round = rd + 1 if not over else rd
        phase = "playing" if not over else "game_over"
        summary = (
            f"R{rd} done: {winner} wins, score {self.host_wins}-{self.guest_wins}"
            if not over else
            f"Game over: {game_winner} wins ({self.host_wins}-{self.guest_wins})"
        )
        self.snapshot.update(
            phase=phase,
            score={"host": self.host_wins, "guest": self.guest_wins},
            round=next_round,
            last_event={
                "summary": summary,
                "structured": {
                    "round": rd,
                    "winner": winner,
                    "game_over": over,
                    "game_winner": game_winner,
                },
            },
        )

    # ---- display ----
    def proto_display(self, msg, direction):
        if msg.get("action") != "round_result":
            return None
        rd = msg["round"] + 1
        hc = msg["host_choice"]
        gc = msg["guest_choice"]
        rw = msg["round_winner"]
        hw = msg["host_wins"]
        gw = msg["guest_wins"]
        over = msg["game_over"]
        gw_text = msg["game_winner"]
        lines = [f"--- Round {rd} ---",
                 f"Host: {hc:>8}  vs  Guest: {gc}",
                 f"Winner: {rw}",
                 f"Score: Host {hw} - {gw} Guest"]
        if over:
            if gw_text == "host":
                winner_str = "Host wins!"
            elif gw_text == "guest":
                winner_str = "Guest wins!"
            elif gw_text == "draw":
                winner_str = "Draw!"
            else:
                winner_str = "No winner"
            lines.append("")
            lines.append(f"=== Game Over ===  {winner_str}  ({hw}-{gw})")
        return "\n".join(lines)

    # ---- join/ready handshake ----
    def proto_host_handle_join(self, msg):
        requested = int(msg.get("best_of", self.best_of))
        self.best_of = requested
        # guest 可在 join 消息里带 rounds_to_win 表明期望；未带则按 host 配置或 best_of//2+1 兜底
        guest_rtw = msg.get("rounds_to_win")
        if guest_rtw:
            self.rounds_to_win = int(guest_rtw)
        elif "rounds_to_win" not in self.options:
            self.rounds_to_win = requested // 2 + 1
        self.snapshot.update(best_of=self.best_of, rounds_to_win=self.rounds_to_win, phase="playing")
        return HookResult({
            "action": "ready",
            "best_of": requested,
            "rounds_to_win": self.rounds_to_win,
            "termination": self.termination,
            "round_delay_seconds": self.round_delay_seconds,
        })

    def proto_guest_join_message(self):
        # 携带 rounds_to_win（若已配置），让 host 知晓 guest 期望的先胜局数
        msg = {"action": "join", "best_of": self.best_of}
        if "rounds_to_win" in self.options:
            msg["rounds_to_win"] = self.rounds_to_win
        return msg

    def proto_guest_handle_ready(self, msg):
        self.best_of = int(msg["best_of"])
        self.rounds_to_win = int(msg["rounds_to_win"])
        self.termination = msg.get("termination", self.termination)
        self.round_delay_seconds = int(msg.get("round_delay_seconds", self.round_delay_seconds))
        self.snapshot.update(
            best_of=self.best_of,
            rounds_to_win=self.rounds_to_win,
            termination=self.termination,
            phase="playing",
        )

    def proto_guest_handle(self, msg):
        # Guest side round_result: synchronize score/snapshot/details
        if msg.get("action") == "round_result":
            round_index = int(msg["round"])
            self.host_wins = int(msg.get("host_wins", self.host_wins))
            self.guest_wins = int(msg.get("guest_wins", self.guest_wins))
            over = bool(msg.get("game_over"))
            game_winner = msg.get("game_winner", "none")
            self._record_round(
                round_index,
                msg["host_choice"],
                msg["guest_choice"],
                msg["round_winner"],
                over,
                game_winner,
            )
        return HookResult()
