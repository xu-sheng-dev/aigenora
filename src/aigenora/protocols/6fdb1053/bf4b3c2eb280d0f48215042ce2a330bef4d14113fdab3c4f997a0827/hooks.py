"""Gomoku (Five-in-a-Row) hooks for the session_loop engine.

1v1 full-information game: guest=Black (first), host=White (second). Move legality
(bounds / occupied) and the five-in-a-row win check are enforced host-side in
proto_host_handle, mirroring the guess-number error+abort pattern (an illegal move
aborts without producing a session proof). Board state lives in this hooks instance
plus the snapshot bus (exposed to the web UI); it never enters the spec message —
only the row/col integer coordinates do (security red-line D1).

Both sides auto-play via a greedy line-score heuristic so `aigenora protocol test`
runs a full game to completion. strategy.json can override with a fixed cell or an
ordered sequence.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from aigenora.proto.hooks import HookResult, ProtocolHooks
from aigenora.proto.sdk import StateStore


EMPTY = ""
BLACK = "B"   # guest (first)
WHITE = "W"   # host (second)
DIRS = [(0, 1), (1, 0), (1, 1), (1, -1)]
# run length -> heuristic weight (>=5 == winning line)
_LINE_WEIGHT = {1: 1, 2: 10, 3: 100, 4: 1000, 5: 100000, 6: 100000}


# ---- pure board logic (module-level so tests can exercise directly) ----

def _in(n: int, r: int, c: int) -> bool:
    return 0 <= r < n and 0 <= c < n


def line_length(board: list[list[str]], n: int, r: int, c: int, dr: int, dc: int, sym: str) -> int:
    """Contiguous run of `sym` through (r,c) along (dr,dc), counting (r,c) itself.
    Caller guarantees board[r][c] == sym (place hypothetically before calling)."""
    total = 1
    rr, cc = r + dr, c + dc
    while _in(n, rr, cc) and board[rr][cc] == sym:
        total += 1
        rr += dr
        cc += dc
    rr, cc = r - dr, c - dc
    while _in(n, rr, cc) and board[rr][cc] == sym:
        total += 1
        rr -= dr
        cc -= dc
    return total


def is_win(board: list[list[str]], n: int, r: int, c: int, sym: str) -> bool:
    if not _in(n, r, c) or board[r][c] != sym:
        return False
    return any(line_length(board, n, r, c, dr, dc, sym) >= 5 for dr, dc in DIRS)


def is_full(board: list[list[str]], n: int) -> bool:
    return all(board[r][c] != EMPTY for r in range(n) for c in range(n))


def _pos_score(board: list[list[str]], n: int, r: int, c: int, sym: str) -> int:
    """Heuristic value of placing `sym` on the empty cell (r,c): sum of run weights
    across the four directions with (r,c) hypothetically occupied."""
    board[r][c] = sym
    try:
        score = 0
        for dr, dc in DIRS:
            ln = line_length(board, n, r, c, dr, dc, sym)
            score += _LINE_WEIGHT.get(ln, _LINE_WEIGHT[6])
        return score
    finally:
        board[r][c] = EMPTY


def _has_neighbor(board: list[list[str]], n: int, r: int, c: int) -> bool:
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            rr, cc = r + dr, c + dc
            if _in(n, rr, cc) and board[rr][cc] != EMPTY:
                return True
    return False


def heuristic_move(board: list[list[str]], n: int, sym: str) -> tuple[int, int]:
    """Greedy: maximize own offense + 0.9 * opponent offense (defense), tie-break by
    proximity to center. Considers only cells adjacent to existing stones so games
    stay compact. Always returns a legal cell when any empty cell exists."""
    if all(board[r][c] == EMPTY for r in range(n) for c in range(n)):
        return (n // 2, n // 2)
    opp = WHITE if sym == BLACK else BLACK
    center = (n - 1) / 2.0
    best = None
    best_key: tuple[int, float] = (-1, 0.0)
    for r in range(n):
        for c in range(n):
            if board[r][c] != EMPTY or not _has_neighbor(board, n, r, c):
                continue
            own = _pos_score(board, n, r, c, sym)
            threat = _pos_score(board, n, r, c, opp)
            score = own + int(threat * 0.9)
            key = (score, -(abs(r - center) + abs(c - center)))
            if key > best_key:
                best_key = key
                best = (r, c)
    if best is None:
        for r in range(n):
            for c in range(n):
                if board[r][c] == EMPTY:
                    return (r, c)
    return best if best is not None else (n // 2, n // 2)


class Hooks(ProtocolHooks):
    def proto_init(self, options, role, args, state_dir: Path, decision_config: dict[str, Any] | None = None):
        super().proto_init(options, role, args, state_dir, decision_config)
        self.state = StateStore(state_dir)
        self.board_size = int(options.get("board_size") or 15)
        self.board: list[list[str]] = [[EMPTY] * self.board_size for _ in range(self.board_size)]
        self.turn = 1  # next guest move number (guest plays odd turns)
        self.snapshot.update(
            board_size=self.board_size,
            board=[row[:] for row in self.board],
            turn=self.turn,
            current="guest",
            phase="playing",
            last_move=None,
            winner="none",
            moves=0,
        )

    def proto_host_metadata(self):
        return ("Gomoku", "game,gomoku,board", "supply", {"board_size": self.board_size})

    # ---- join / ready ----
    def proto_host_handle_join(self, msg):
        self.board_size = int(msg.get("board_size") or self.board_size)
        self.board = [[EMPTY] * self.board_size for _ in range(self.board_size)]
        self.turn = 1
        self.snapshot.update(board_size=self.board_size, board=[r[:] for r in self.board],
                             turn=self.turn, current="guest", phase="playing", winner="none", moves=0)
        return HookResult({"action": "ready", "board_size": self.board_size, "first": "guest"})

    def proto_guest_join_message(self):
        return {"action": "join", "board_size": self.board_size}

    def proto_guest_handle_ready(self, msg):
        self.board_size = int(msg["board_size"])
        self.board = [[EMPTY] * self.board_size for _ in range(self.board_size)]
        self.turn = 1
        self.snapshot.update(board_size=self.board_size, board=[r[:] for r in self.board],
                             turn=self.turn, current="guest", phase="playing")

    # ---- guest side ----
    def proto_guest_first_action(self):
        return self._guest_move()

    def proto_guest_handle(self, msg):
        action = msg.get("action")
        if action == "round_result":
            r, c = int(msg["row"]), int(msg["col"])
            self.board[r][c] = WHITE
            self.turn = int(msg["turn"]) + 1
            over = bool(msg.get("game_over"))
            winner = msg.get("winner", "none")
            self._record(r, c, "host", int(msg["turn"]), over, winner)
            if over:
                return HookResult(game_over=True)
            return HookResult(self._guest_move())
        if action == "end":
            return HookResult(game_over=True)
        return HookResult({"action": "error", "reason": "unexpected_action"}, abort=True)

    def _guest_move(self) -> dict:
        r, c = self._pick(BLACK, self.turn)
        self.board[r][c] = BLACK
        mv = {"action": "move", "turn": self.turn, "row": r, "col": c}
        self._record(r, c, "guest", self.turn, False, "none")
        return mv

    # ---- host side (referee + white) ----
    def proto_host_handle(self, msg):
        if msg.get("action") != "move":
            return HookResult({"action": "error", "reason": "not_your_turn"}, abort=True)
        gr, gc, gturn = int(msg["row"]), int(msg["col"]), int(msg["turn"])
        if not _in(self.board_size, gr, gc):
            return HookResult({"action": "error", "turn": gturn, "reason": "out_of_bounds"}, abort=True)
        if self.board[gr][gc] != EMPTY:
            return HookResult({"action": "error", "turn": gturn, "reason": "occupied"}, abort=True)
        # guest (black) stone
        self.board[gr][gc] = BLACK
        if is_win(self.board, self.board_size, gr, gc, BLACK):
            self._record(gr, gc, "guest", gturn, True, "guest")
            return HookResult({"action": "round_result", "turn": gturn, "row": gr, "col": gc,
                               "player": "guest", "game_over": True, "winner": "guest"}, game_over=True)
        # host (white) stone
        hr, hc = self._pick(WHITE, gturn + 1)
        self.board[hr][hc] = WHITE
        hturn = gturn + 1
        if is_win(self.board, self.board_size, hr, hc, WHITE):
            self._record(hr, hc, "host", hturn, True, "host")
            return HookResult({"action": "round_result", "turn": hturn, "row": hr, "col": hc,
                               "player": "host", "game_over": True, "winner": "host"}, game_over=True)
        if is_full(self.board, self.board_size):
            self._record(hr, hc, "host", hturn, True, "none")
            return HookResult({"action": "round_result", "turn": hturn, "row": hr, "col": hc,
                               "player": "host", "game_over": True, "winner": "none"}, game_over=True)
        self._record(hr, hc, "host", hturn, False, "none")
        self.turn = hturn + 1
        return HookResult({"action": "round_result", "turn": hturn, "row": hr, "col": hc,
                           "player": "host", "game_over": False, "winner": "none"})

    # ---- move selection ----
    def _pick_auto(self, sym: str) -> tuple[int, int]:
        strat = self.strategy.read()
        if strat:
            mode = strat.get("mode")
            if mode == "fixed":
                r, c = int(strat.get("row", -1)), int(strat.get("col", -1))
                if _in(self.board_size, r, c) and self.board[r][c] == EMPTY:
                    return (r, c)
            elif mode == "seq":
                seq = strat.get("sequence") or []
                idx = sum(1 for rr in range(self.board_size) for cc in range(self.board_size)
                          if self.board[rr][cc] == sym)
                if 0 <= idx < len(seq):
                    try:
                        r, c = int(seq[idx][0]), int(seq[idx][1])
                    except (TypeError, ValueError, IndexError):
                        r, c = -1, -1
                    if _in(self.board_size, r, c) and self.board[r][c] == EMPTY:
                        return (r, c)
        return heuristic_move(self.board, self.board_size, sym)

    def _pick(self, sym: str, match_turn: int) -> tuple[int, int]:
        auto = self._pick_auto(sym)
        if self.bus is None or not self.timing_enabled:
            return auto
        now = time.monotonic()
        min_think = float(self.options.get("min_think_seconds", self.timing["min_think_seconds"]))
        max_think = float(self.options.get("max_think_seconds", self.timing["max_think_seconds"]))
        self._update_timing_snapshot("turn", match_turn, now + min_think, now + max_think, "waiting")
        decision = self.bus.await_latest_decision(
            match_key="turn", match_value=match_turn,
            release_at=now + min_think, deadline_at=now + max_think,
            fallback_value={"row": auto[0], "col": auto[1]},
        )
        self._clear_timing_snapshot()
        r, c = int(decision.get("row", auto[0])), int(decision.get("col", auto[1]))
        if _in(self.board_size, r, c) and self.board[r][c] == EMPTY:
            return (r, c)
        return auto

    # ---- snapshot / details ----
    def _record(self, r: int, c: int, player: str, turn: int, over: bool, winner: str) -> None:
        sym = BLACK if player == "guest" else WHITE
        moves = sum(1 for rr in range(self.board_size) for cc in range(self.board_size)
                    if self.board[rr][cc] != EMPTY)
        self.details.append(
            type="move",
            turn=turn,
            row=r,
            col=c,
            player=player,
            stone=sym,
            game_over=over,
            winner=winner,
            summary=f"T{turn} {player} -> ({r},{c})" + (f"  WINNER={winner}" if over else ""),
        )
        phase = "game_over" if over else "playing"
        summary = (f"Game over: {winner} wins" if over and winner != "none"
                   else "Draw (board full)" if over else f"{player} played ({r},{c})")
        self.snapshot.update(
            board=[row[:] for row in self.board],
            turn=turn,
            current=player,
            phase=phase,
            winner=winner,
            moves=moves,
            last_move={"row": r, "col": c, "player": player},
            last_event={
                "summary": summary,
                "structured": {"turn": turn, "row": r, "col": c, "player": player,
                               "game_over": over, "winner": winner},
            },
        )

    # ---- display ----
    def proto_display(self, msg, direction):
        action = msg.get("action")
        if action == "round_result":
            over = bool(msg.get("game_over"))
            sym = "X" if msg["player"] == "guest" else "O"
            line = f"{msg['player']}({sym}) -> ({msg['row']},{msg['col']})"
            if over:
                line += f"   GAME OVER winner={msg.get('winner', 'none')}"
            return line
        if action == "error":
            return f"illegal: {msg.get('reason')}"
        return None
