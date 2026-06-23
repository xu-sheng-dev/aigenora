"""Weak Wins All hooks for simultaneous_round engine.

The engine handles commit/reveal/hash/barrier, hooks only provide the bid and the judge.
Rule: the lower bid wins and takes the sum of both bids; if equal, each side gets its own bid back (draw scores).
The final round forces all-in (= remaining points), guaranteed by hooks in proto_round_value.
"""
from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

from aigenora.proto.hooks import HookResult, ProtocolHooks
from aigenora.proto.sdk import StateStore


class Hooks(ProtocolHooks):
    def proto_init(self, options, role, args, state_dir: Path, decision_config: dict[str, Any] | None = None):
        super().proto_init(options, role, args, state_dir, decision_config)
        self.state = StateStore(state_dir)
        self.total_points = int(options.get("total_points") or 25)
        self.total_rounds = int(options.get("total_rounds") or 5)
        self.host_remaining = self.total_points
        self.guest_remaining = self.total_points
        self.host_score = 0
        self.guest_score = 0
        self.snapshot.update(
            score={"host": 0, "guest": 0},
            round=1,
            total_rounds=self.total_rounds,
            total_points=self.total_points,
            remaining={"host": self.host_remaining, "guest": self.guest_remaining},
        )

    def proto_host_metadata(self):
        return (
            "Weak Wins All",
            "game,weak-wins-all",
            "supply",
            {"total_points": self.total_points, "total_rounds": self.total_rounds},
        )

    # ---- bid strategy ----
    def _my_remaining(self) -> int:
        return self.host_remaining if self.role == "host" else self.guest_remaining

    def _bid_auto(self, round_index: int, remaining: int) -> int:
        # last round forces all-in
        if round_index >= self.total_rounds - 1:
            return remaining
        rounds_left = max(1, self.total_rounds - round_index)
        default_bid = max(1, remaining // rounds_left)
        strat = self.strategy.read()
        if not strat:
            return min(default_bid, remaining)
        mode = strat.get("mode", "even_split")
        if mode == "fixed":
            try:
                bid = int(strat.get("bid", default_bid))
            except (TypeError, ValueError):
                bid = default_bid
        elif mode == "percent":
            try:
                pct = float(strat.get("percent", 0))
            except (TypeError, ValueError):
                pct = 0.0
            bid = max(1, int(round(remaining * pct / 100.0)))
        elif mode == "seq":
            seq = strat.get("sequence") or []
            if not seq:
                return min(default_bid, remaining)
            try:
                bid = int(seq[round_index % len(seq)])
            except (TypeError, ValueError):
                bid = default_bid
        elif mode == "random":
            bid = random.randint(1, max(1, remaining))
        else:
            bid = default_bid
        return max(0, min(bid, remaining))

    def _pick(self, round_index: int) -> int:
        remaining = self._my_remaining()
        auto = self._bid_auto(round_index, remaining)
        if self.bus is None:
            return auto
        # hybrid（auto 模式，默认）：非阻塞读 decide，无则 auto
        if self.decision_mode != "manual":
            d = self._consume_hybrid("round", round_index)
            if d and "bid" in d:
                try:
                    bid = int(d["bid"])
                    if 0 <= bid <= remaining:
                        return bid
                except (TypeError, ValueError):
                    pass
            return auto
        # --coach（manual）：阻塞逐手等待
        if not self.timing_enabled:
            return auto
        now = time.monotonic()
        # options 里的 min/max_think_seconds 优先于 spec.timing 默认值（邀约级覆盖）
        min_think = float(self.options.get("min_think_seconds", self.timing["min_think_seconds"]))
        max_think = float(self.options.get("max_think_seconds", self.timing["max_think_seconds"]))
        fallback_bid = self._bid_auto(round_index, remaining)
        fallback = {"round": round_index, "bid": fallback_bid}
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
        try:
            bid = int(decision.get("bid", fallback_bid))
        except (TypeError, ValueError):
            bid = fallback_bid
        bid = max(0, min(bid, remaining))
        # last round all-in enforced
        if round_index >= self.total_rounds - 1:
            bid = remaining
        return bid

    # ---- simultaneous_round hooks ----
    def proto_round_value(self, round_index: int, state: dict) -> int:
        return self._pick(round_index)

    def proto_round_judge(self, round_index: int, host_value: int, guest_value: int, state: dict) -> HookResult:
        host_bid = max(0, min(int(host_value), self.host_remaining))
        guest_bid = max(0, min(int(guest_value), self.guest_remaining))
        self.host_remaining -= host_bid
        self.guest_remaining -= guest_bid
        total = host_bid + guest_bid
        if host_bid == guest_bid:
            winner = "draw"
            self.host_score += host_bid
            self.guest_score += guest_bid
        elif host_bid < guest_bid:
            winner = "host"
            self.host_score += total
        else:
            winner = "guest"
            self.guest_score += total
        over = (round_index + 1) >= self.total_rounds
        if over:
            if self.host_score > self.guest_score:
                game_winner = "host"
            elif self.guest_score > self.host_score:
                game_winner = "guest"
            else:
                game_winner = "draw"
        else:
            game_winner = "none"
        resp = {
            "action": "round_result",
            "round": round_index,
            "host_bid": host_bid,
            "guest_bid": guest_bid,
            "round_winner": winner,
            "host_score": self.host_score,
            "guest_score": self.guest_score,
            "host_remaining": self.host_remaining,
            "guest_remaining": self.guest_remaining,
            "game_over": over,
            "game_winner": game_winner,
        }
        self._record_round(round_index, host_bid, guest_bid, winner, over, game_winner)
        return HookResult(resp, game_over=over)

    def _record_round(self, round_index: int, host_bid: int, guest_bid: int,
                      winner: str, over: bool, game_winner: str) -> None:
        rd = round_index + 1
        self.details.append(
            type="round_result",
            round=rd,
            host_bid=host_bid,
            guest_bid=guest_bid,
            winner=winner,
            host_score=self.host_score,
            guest_score=self.guest_score,
            host_remaining=self.host_remaining,
            guest_remaining=self.guest_remaining,
            game_over=over,
            game_winner=game_winner,
            summary=f"R{rd}: H:{host_bid} G:{guest_bid} -> {winner} (score {self.host_score}-{self.guest_score}, left {self.host_remaining}-{self.guest_remaining})",
        )
        next_round = rd + 1 if not over else rd
        phase = "playing" if not over else "game_over"
        summary = (
            f"R{rd} done: {winner} wins, score {self.host_score}-{self.guest_score}, left {self.host_remaining}-{self.guest_remaining}"
            if not over else
            f"Game over: {game_winner} wins ({self.host_score}-{self.guest_score})"
        )
        self.snapshot.update(
            phase=phase,
            score={"host": self.host_score, "guest": self.guest_score},
            round=next_round,
            remaining={"host": self.host_remaining, "guest": self.guest_remaining},
            last_event={
                "summary": summary,
                "structured": {
                    "round": rd,
                    "winner": winner,
                    "host_bid": host_bid,
                    "guest_bid": guest_bid,
                    "game_over": over,
                    "game_winner": game_winner,
                },
            },
        )

    # ---- join/ready handshake ----
    def proto_host_handle_join(self, msg):
        self.total_points = int(msg.get("total_points", self.total_points))
        self.total_rounds = int(msg.get("total_rounds", self.total_rounds))
        self.host_remaining = self.total_points
        self.guest_remaining = self.total_points
        self.snapshot.update(
            total_points=self.total_points,
            total_rounds=self.total_rounds,
            remaining={"host": self.host_remaining, "guest": self.guest_remaining},
            phase="playing",
        )
        return HookResult({
            "action": "ready",
            "total_points": self.total_points,
            "total_rounds": self.total_rounds,
        })

    def proto_guest_join_message(self):
        return {"action": "join", "total_points": self.total_points, "total_rounds": self.total_rounds}

    def proto_guest_handle_ready(self, msg):
        self.total_points = int(msg["total_points"])
        self.total_rounds = int(msg["total_rounds"])
        self.host_remaining = self.total_points
        self.guest_remaining = self.total_points
        self.snapshot.update(
            total_points=self.total_points,
            total_rounds=self.total_rounds,
            remaining={"host": self.host_remaining, "guest": self.guest_remaining},
            phase="playing",
        )

    def proto_guest_handle(self, msg):
        if msg.get("action") == "round_result":
            round_index = int(msg["round"])
            self.host_score = int(msg.get("host_score", self.host_score))
            self.guest_score = int(msg.get("guest_score", self.guest_score))
            self.host_remaining = int(msg.get("host_remaining", self.host_remaining))
            self.guest_remaining = int(msg.get("guest_remaining", self.guest_remaining))
            over = bool(msg.get("game_over"))
            game_winner = msg.get("game_winner", "none")
            self._record_round(
                round_index,
                int(msg["host_bid"]),
                int(msg["guest_bid"]),
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
        hb = msg["host_bid"]
        gb = msg["guest_bid"]
        rw = msg["round_winner"]
        hs = msg["host_score"]
        gs = msg["guest_score"]
        hr = msg["host_remaining"]
        gr = msg["guest_remaining"]
        over = msg["game_over"]
        gw_text = msg["game_winner"]
        lines = [f"--- Round {rd} ---",
                 f"Host bid: {hb}  vs  Guest bid: {gb}",
                 f"Winner: {rw}",
                 f"Score: Host {hs} - {gs} Guest",
                 f"Remaining: Host {hr} - {gr} Guest"]
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
            lines.append(f"=== Game Over ===  {winner_str}  ({hs}-{gs})")
        return "\n".join(lines)
