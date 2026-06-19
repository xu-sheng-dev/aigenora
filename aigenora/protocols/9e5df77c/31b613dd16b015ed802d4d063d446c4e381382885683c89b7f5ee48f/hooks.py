"""Coin Flip hooks for simultaneous_round engine.

The engine handles commit/reveal/hash/barrier, hooks only provide the move and the judge.
Outcome rule: guest_choice == host_choice -> guest wins; otherwise host wins (preserves old protocol semantics).
"""
from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

from aigenora.proto.hooks import HookResult, ProtocolHooks
from aigenora.proto.sdk import StateStore


SIDES = ["heads", "tails"]
SIDE_LABEL = {"heads": "Heads", "tails": "Tails"}


class Hooks(ProtocolHooks):
    def proto_init(self, options, role, args, state_dir: Path, decision_config: dict[str, Any] | None = None):
        super().proto_init(options, role, args, state_dir, decision_config)
        self.state = StateStore(state_dir)
        self.best_of = int(options.get("best_of") or 3)
        self.rounds_to_win = self.best_of // 2 + 1
        self.host_wins = 0
        self.guest_wins = 0
        self.snapshot.update(
            score={"host": 0, "guest": 0},
            round=1,
            best_of=self.best_of,
            rounds_to_win=self.rounds_to_win,
        )

    def proto_host_metadata(self):
        return ("Coin Flip", "game,coin", "supply", {"best_of": self.best_of})

    def _pick_auto(self, round_index: int) -> str:
        strat = self.strategy.read()
        if strat:
            mode = strat.get("mode", "random")
            if mode == "fixed":
                fixed = strat.get("fixed")
                if fixed in SIDES:
                    return fixed
            elif mode == "seq":
                seq = [x for x in strat.get("sequence", []) if x in SIDES]
                if seq:
                    return seq[round_index % len(seq)]
        return random.choice(SIDES)

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
        return choice if choice in SIDES else fallback["choice"]

    def proto_round_value(self, round_index: int, state: dict) -> str:
        return self._pick(round_index)

    def proto_round_judge(self, round_index: int, host_value: str, guest_value: str, state: dict) -> HookResult:
        winner = "guest" if guest_value == host_value else "host"
        if winner == "host":
            self.host_wins += 1
        else:
            self.guest_wins += 1
        over = self.host_wins >= self.rounds_to_win or self.guest_wins >= self.rounds_to_win
        if self.host_wins >= self.rounds_to_win:
            game_winner = "host"
        elif self.guest_wins >= self.rounds_to_win:
            game_winner = "guest"
        else:
            game_winner = "none"
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
            summary=f"R{rd}: H:{SIDE_LABEL[host_choice]} G:{SIDE_LABEL[guest_choice]} -> {winner} ({self.host_wins}-{self.guest_wins})",
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

    def proto_host_handle_join(self, msg):
        self.best_of = int(msg.get("best_of", self.best_of))
        self.rounds_to_win = self.best_of // 2 + 1
        self.snapshot.update(best_of=self.best_of, rounds_to_win=self.rounds_to_win, phase="playing")
        return HookResult({"action": "ready", "best_of": self.best_of, "rounds_to_win": self.rounds_to_win})

    def proto_guest_join_message(self):
        return {"action": "join", "best_of": self.best_of}

    def proto_guest_handle_ready(self, msg):
        self.best_of = int(msg["best_of"])
        self.rounds_to_win = int(msg["rounds_to_win"])
        self.snapshot.update(
            best_of=self.best_of,
            rounds_to_win=self.rounds_to_win,
            phase="playing",
        )

    def proto_guest_handle(self, msg):
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
                 f"Host: {SIDE_LABEL[hc]:>6}  vs  Guest: {SIDE_LABEL[gc]}",
                 f"Winner: {rw}",
                 f"Score: Host {hw} - {gw} Guest"]
        if over:
            if gw_text == "host":
                winner_str = "Host wins!"
            elif gw_text == "guest":
                winner_str = "Guest wins!"
            else:
                winner_str = "No winner"
            lines.append("")
            lines.append(f"=== Game Over ===  {winner_str}  ({hw}-{gw})")
        return "\n".join(lines)
