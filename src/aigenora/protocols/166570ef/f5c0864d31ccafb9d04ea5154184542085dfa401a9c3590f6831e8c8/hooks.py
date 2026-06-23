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
        self.range_min = int(options.get("range_min") or 1)
        self.range_max = int(options.get("range_max") or 100)
        self.max_attempts = int(options.get("max_attempts") or 7)
        self.secret = int(options.get("secret") or random.randint(self.range_min, self.range_max))
        self.attempts = 0
        self.lo = self.range_min
        self.hi = self.range_max
        self.guest_attempt = 1
        self.snapshot.update(
            range_min=self.range_min,
            range_max=self.range_max,
            max_attempts=self.max_attempts,
            attempts=0,
            lo=self.lo,
            hi=self.hi,
        )

    def proto_host_metadata(self):
        return ("Guess Number", "game,guess-number", "supply", {
            "range_min": self.range_min,
            "range_max": self.range_max,
            "max_attempts": self.max_attempts,
        })

    def proto_host_handle_join(self, msg):
        self.max_attempts = int(msg.get("max_attempts", self.max_attempts))
        self.snapshot.update(max_attempts=self.max_attempts, phase="playing")
        return HookResult({"action": "ready", "range_min": self.range_min, "range_max": self.range_max, "max_attempts": self.max_attempts})

    def proto_host_handle(self, msg):
        if msg.get("action") != "guess":
            return HookResult({"action": "error", "reason": "unexpected_action"}, abort=True)
        number = int(msg["number"])
        attempt = int(msg["attempt"])
        if number < self.range_min or number > self.range_max:
            return HookResult({"action": "error", "reason": "invalid_guess"}, abort=True)
        self.attempts += 1
        if number == self.secret:
            self._record_attempt(attempt, number, "hit", over=True, winner="guest")
            return HookResult({"action": "game_over", "winner": "guest", "secret_number": self.secret, "total_attempts": self.attempts}, game_over=True)
        if self.attempts >= self.max_attempts:
            self._record_attempt(attempt, number, "exhausted", over=True, winner="host")
            return HookResult({"action": "game_over", "winner": "host", "secret_number": self.secret, "total_attempts": self.attempts}, game_over=True)
        result = "higher" if number < self.secret else "lower"
        self._record_attempt(attempt, number, result, over=False, winner="none")
        return HookResult({"action": "hint", "attempt": attempt, "result": result, "attempts_used": self.attempts})

    def _record_attempt(self, attempt: int, number: int, result: str, over: bool, winner: str) -> None:
        self.details.append(
            type="guess",
            attempt=attempt,
            number=number,
            result=result,
            attempts_used=self.attempts,
            game_over=over,
            winner=winner,
            summary=f"#{attempt}: guessed {number} -> {result} ({self.attempts}/{self.max_attempts})",
        )
        phase = "game_over" if over else "playing"
        summary = (
            f"#{attempt} guessed {number}: {result}, used {self.attempts}/{self.max_attempts}"
            if not over else
            f"Game over: {winner} wins, answer {self.secret}, {self.attempts} attempts"
        )
        self.snapshot.update(
            phase=phase,
            attempts=self.attempts,
            last_event={
                "summary": summary,
                "structured": {
                    "attempt": attempt,
                    "number": number,
                    "result": result,
                    "game_over": over,
                    "winner": winner,
                },
            },
        )

    def proto_guest_join_message(self):
        return {"action": "join", "max_attempts": self.max_attempts}

    def proto_guest_handle_ready(self, msg):
        self.lo = int(msg["range_min"])
        self.hi = int(msg["range_max"])
        self.range_min = self.lo
        self.range_max = self.hi
        self.max_attempts = int(msg["max_attempts"])
        self.snapshot.update(
            range_min=self.range_min,
            range_max=self.range_max,
            max_attempts=self.max_attempts,
            lo=self.lo,
            hi=self.hi,
            phase="playing",
        )

    def _guess_auto(self) -> dict:
        """Read strategy.json to decide the next guess.

        strategy.json convention (Guess Number protocol-layer convention, not enforced by the engine):
          {"mode": "bisect"}                      # default, bisection
          {"mode": "fixed", "number": 42}         # always guess a fixed value
          {"mode": "seq", "sequence": [50,25,75]} # cycle through a sequence, clamped to lo..hi when out of range
        """
        strat = self.strategy.read()
        number = (self.lo + self.hi) // 2
        if strat:
            mode = strat.get("mode", "bisect")
            if mode == "fixed":
                try:
                    number = int(strat.get("number", number))
                except (TypeError, ValueError):
                    pass
            elif mode == "seq":
                seq = strat.get("sequence") or []
                if seq:
                    try:
                        number = int(seq[(self.guest_attempt - 1) % len(seq)])
                    except (TypeError, ValueError):
                        pass
        number = max(self.lo, min(self.hi, number))
        self.state.write("last_guess", number)
        return {"action": "guess", "attempt": self.guest_attempt, "number": number}

    def _guess(self) -> dict:
        auto = self._guess_auto()
        if self.bus is None:
            return auto
        # hybrid（auto 模式，默认）：非阻塞读 decide，无则 auto
        if self.decision_mode != "manual":
            d = self._consume_hybrid("attempt", self.guest_attempt)
            if d and "number" in d:
                try:
                    number = max(self.lo, min(self.hi, int(d["number"])))
                    self.state.write("last_guess", number)
                    return {"action": "guess", "attempt": self.guest_attempt, "number": number}
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
        fallback = self._guess_auto()
        self._update_timing_snapshot("attempt", self.guest_attempt,
            now + min_think,
            now + max_think, "waiting")
        decision = self.bus.await_latest_decision(
            match_key="attempt", match_value=self.guest_attempt,
            release_at=now + min_think,
            deadline_at=now + max_think,
            fallback_value=fallback,
        )
        self._clear_timing_snapshot()
        number = int(decision.get("number", (self.lo + self.hi) // 2))
        number = max(self.lo, min(self.hi, number))
        self.state.write("last_guess", number)
        return {"action": "guess", "attempt": self.guest_attempt, "number": number}

    def proto_guest_first_action(self):
        return self._guess()

    def proto_guest_handle(self, msg):
        if msg.get("action") == "hint":
            last = self.state.read_int("last_guess")
            if msg["result"] == "higher":
                self.lo = last + 1
            elif msg["result"] == "lower":
                self.hi = last - 1
            attempts_used = int(msg.get("attempts_used", self.guest_attempt))
            self.attempts = attempts_used
            self.snapshot.update(
                lo=self.lo,
                hi=self.hi,
                attempts=attempts_used,
                last_event={
                    "summary": f"#{msg['attempt']} guessed {last}: {msg['result']}, range narrowed to [{self.lo},{self.hi}]",
                    "structured": {
                        "attempt": int(msg["attempt"]),
                        "number": last,
                        "result": msg["result"],
                        "lo": self.lo,
                        "hi": self.hi,
                    },
                },
            )
            self.details.append(
                type="hint",
                attempt=int(msg["attempt"]),
                number=last,
                result=msg["result"],
                lo=self.lo,
                hi=self.hi,
                summary=f"#{msg['attempt']} guessed {last} -> {msg['result']} (range [{self.lo},{self.hi}])",
            )
            self.guest_attempt += 1
            return HookResult(self._guess())
        if msg.get("action") == "game_over":
            winner = msg.get("winner", "none")
            secret = int(msg.get("secret_number", 0))
            total = int(msg.get("total_attempts", self.attempts))
            self.snapshot.update(
                phase="game_over",
                attempts=total,
                last_event={
                    "summary": f"Game over: {winner} wins, answer {secret}, {total} attempts",
                    "structured": {
                        "game_over": True,
                        "winner": winner,
                        "secret_number": secret,
                        "total_attempts": total,
                    },
                },
            )
            self.details.append(
                type="game_over",
                winner=winner,
                secret_number=secret,
                total_attempts=total,
                summary=f"Game over: {winner} wins, answer {secret}, {total} attempts",
            )
            return HookResult(game_over=True)
        return HookResult({"action": "error", "reason": "unexpected_action"}, abort=True)
