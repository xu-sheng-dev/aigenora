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


class Hooks(ProtocolHooks):
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
        self.rounds_to_win = self.best_of // 2 + 1
        self.host_wins = 0
        self.guest_wins = 0
        self.fallback_strategy: str = args[0] if args else "random"
        self.snapshot.update(
            score={"host": 0, "guest": 0},
            round=1,
            best_of=self.best_of,
            termination=self.termination,
            rounds_to_win=self.rounds_to_win,
        )

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
                    return fixed
            elif mode == "seq":
                seq = [x for x in strat.get("sequence", []) if x in CHOICES]
                if seq:
                    return seq[round_index % len(seq)]
            return random.choice(CHOICES)
        s = self.fallback_strategy
        if s in CHOICES:
            return s
        if s.startswith("seq:"):
            seq = [x for x in s[4:].split(",") if x in CHOICES]
            if seq:
                return seq[round_index % len(seq)]
        return random.choice(CHOICES)

    def _pick(self, round_index: int) -> str:
        if self.bus is None or not self.timing_enabled:
            return self._pick_auto(round_index)
        now = time.monotonic()
        # options 里的 min/max_think_seconds 优先于 spec.timing 默认值（邀约级覆盖）
        min_think = float(self.options.get("min_think_seconds", self.timing["min_think_seconds"]))
        max_think = float(self.options.get("max_think_seconds", self.timing["max_think_seconds"]))
        fallback = {"round": round_index, "choice": self._pick_auto(round_index)}
        self._update_timing_snapshot(
            "round", round_index,
            now + min_think,
            now + max_think, "waiting",
        )
        decision = self.bus.await_latest_decision(
            match_key="round", match_value=round_index,
            release_at=now + min_think,
            deadline_at=now + max_think,
            fallback_value=fallback,
        )
        self._clear_timing_snapshot()
        choice = decision.get("choice")
        return choice if choice in CHOICES else fallback["choice"]

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
        return HookResult(resp, game_over=over)

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
        return {"action": "join", "best_of": self.best_of}

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
