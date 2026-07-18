"""Connect Four hooks for the session_loop engine.

1v1 full-information game with gravity: guest=Red (first), host=Yellow (second). A
move names a column; the stone drops to the lowest empty cell in that column. Move
legality (valid column / column not full) and the four-in-a-row win check are
enforced host-side (error+abort on illegal move — no session proof). Board state
stays in this hooks instance + snapshot bus; only the col/row integers travel in the
spec message (security red-line D1).

Both sides auto-play via a greedy line-score heuristic so `aigenora protocol test`
runs a full game to completion. strategy.json overrides with a fixed column or an
ordered column sequence.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from aigenora.proto.hooks import HookResult, ProtocolHooks
from aigenora.proto.sdk import StateStore


EMPTY = ""
RED = "R"     # guest (first)
YELLOW = "Y"  # host (second)
DIRS = [(0, 1), (1, 0), (1, 1), (1, -1)]
_WIN_LEN = 4
_LINE_WEIGHT = {1: 1, 2: 10, 3: 100, 4: 1000, 5: 100000, 6: 100000}


# ---- pure board logic (module-level) ----

def _in(rows: int, cols: int, r: int, c: int) -> bool:
    return 0 <= r < rows and 0 <= c < cols


def drop_cell(board: list[list[str]], rows: int, cols: int, col: int) -> int | None:
    """Row index where a stone dropped in `col` would land (lowest empty cell),
    or None if the column is full / out of range. Does not mutate the board."""
    if not _in(rows, cols, 0, col):
        return None
    for r in range(rows - 1, -1, -1):
        if board[r][col] == EMPTY:
            return r
    return None


def line_length(board: list[list[str]], rows: int, cols: int, r: int, c: int,
                dr: int, dc: int, sym: str) -> int:
    total = 1
    rr, cc = r + dr, c + dc
    while _in(rows, cols, rr, cc) and board[rr][cc] == sym:
        total += 1
        rr += dr
        cc += dc
    rr, cc = r - dr, c - dc
    while _in(rows, cols, rr, cc) and board[rr][cc] == sym:
        total += 1
        rr -= dr
        cc -= dc
    return total


def is_win(board: list[list[str]], rows: int, cols: int, r: int, c: int, sym: str) -> bool:
    if not _in(rows, cols, r, c) or board[r][c] != sym:
        return False
    return any(line_length(board, rows, cols, r, c, dr, dc, sym) >= _WIN_LEN for dr, dc in DIRS)


def is_full(board: list[list[str]], rows: int, cols: int) -> bool:
    return all(board[r][c] != EMPTY for r in range(rows) for c in range(cols))


def _pos_score(board: list[list[str]], rows: int, cols: int, r: int, c: int, sym: str) -> int:
    board[r][c] = sym
    try:
        return sum(_LINE_WEIGHT.get(line_length(board, rows, cols, r, c, dr, dc, sym),
                                    _LINE_WEIGHT[6]) for dr, dc in DIRS)
    finally:
        board[r][c] = EMPTY


def heuristic_move(board: list[list[str]], rows: int, cols: int, sym: str) -> int:
    """Pick the best column: maximize own offense + 0.9 * opponent offense at the
    landing cell, tie-break toward the centre column. Returns a legal column."""
    opp = YELLOW if sym == RED else RED
    center = (cols - 1) / 2.0
    best_col = -1
    best_key: tuple[int, float] = (-1, 0.0)
    for col in range(cols):
        r = drop_cell(board, rows, cols, col)
        if r is None:
            continue
        own = _pos_score(board, rows, cols, r, col, sym)
        threat = _pos_score(board, rows, cols, r, col, opp)
        score = own + int(threat * 0.9)
        key = (score, -(abs(col - center)))
        if key > best_key:
            best_key = key
            best_col = col
    if best_col < 0:
        # board not full but every column full is impossible; defensive fallback
        for col in range(cols):
            if drop_cell(board, rows, cols, col) is not None:
                return col
    return best_col if best_col >= 0 else 0


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")
    def proto_init(self, options, role, args, state_dir: Path, decision_config: dict[str, Any] | None = None):
        super().proto_init(options, role, args, state_dir, decision_config)
        self.state = StateStore(state_dir)
        self.cols = int(options.get("cols") or 7)
        self.rows = int(options.get("rows") or 6)
        self._reset_board()
        self.turn = 1
        self.snapshot.update(
            cols=self.cols, rows=self.rows,
            board=[row[:] for row in self.board],
            turn=self.turn, current="guest", phase="playing",
            last_move=None, winner="none", moves=0,
        )

    def _reset_board(self):
        self.board = [[EMPTY] * self.cols for _ in range(self.rows)]

    def proto_host_metadata(self):
        return ("Connect Four", "game,connect4,board", "supply",
                {"cols": self.cols, "rows": self.rows})

    # ---- join / ready ----
    def proto_host_handle_join(self, msg):
        self.cols = int(msg.get("cols") or self.cols)
        self.rows = int(msg.get("rows") or self.rows)
        self._reset_board()
        self.turn = 1
        self.snapshot.update(cols=self.cols, rows=self.rows, board=[r[:] for r in self.board],
                             turn=self.turn, current="guest", phase="playing", winner="none", moves=0)
        return HookResult({"action": "ready", "cols": self.cols, "rows": self.rows, "first": "guest"})

    def proto_guest_join_message(self):
        return {"action": "join", "cols": self.cols, "rows": self.rows}

    def proto_guest_handle_ready(self, msg):
        self.cols = int(msg["cols"])
        self.rows = int(msg["rows"])
        self._reset_board()
        self.turn = 1
        self.snapshot.update(cols=self.cols, rows=self.rows, board=[r[:] for r in self.board],
                             turn=self.turn, current="guest", phase="playing")

    # ---- guest side ----
    def proto_guest_first_action(self):
        return self._guest_move()

    def proto_guest_handle(self, msg):
        action = msg.get("action")
        if action == "round_result":
            r, c = int(msg["row"]), int(msg["col"])
            self.board[r][c] = YELLOW
            self.turn = int(msg["turn"]) + 1
            over = bool(msg.get("game_over"))
            winner = msg.get("winner", "none")
            self._record(r, c, "host", int(msg["turn"]), over, winner)
            if over:
                return HookResult(completed=True)
            return HookResult(self._guest_move())
        if action == "end":
            return HookResult(completed=True)
        return HookResult({"action": "error", "reason": "not_your_turn"}, abort=True)

    def _guest_move(self) -> dict:
        col = self._pick(RED, self.turn)
        r = drop_cell(self.board, self.rows, self.cols, col)
        # heuristic guarantees a non-full column; guard anyway
        if r is None:
            col = next(c for c in range(self.cols) if drop_cell(self.board, self.rows, self.cols, c) is not None)
            r = drop_cell(self.board, self.rows, self.cols, col)
        self.board[r][col] = RED
        self._record(r, col, "guest", self.turn, False, "none")
        return {"action": "move", "turn": self.turn, "col": col}

    # ---- host side (referee + yellow) ----
    def proto_host_handle(self, msg):
        if msg.get("action") != "move":
            return HookResult({"action": "error", "reason": "not_your_turn"}, abort=True)
        gcol, gturn = int(msg["col"]), int(msg["turn"])
        if not _in(self.rows, self.cols, 0, gcol):
            return HookResult({"action": "error", "turn": gturn, "reason": "invalid_column"}, abort=True)
        gr = drop_cell(self.board, self.rows, self.cols, gcol)
        if gr is None:
            return HookResult({"action": "error", "turn": gturn, "reason": "column_full"}, abort=True)
        self.board[gr][gcol] = RED
        if is_win(self.board, self.rows, self.cols, gr, gcol, RED):
            self._record(gr, gcol, "guest", gturn, True, "guest")
            return HookResult({"action": "round_result", "turn": gturn, "col": gcol, "row": gr,
                               "player": "guest", "game_over": True, "winner": "guest"}, completed=True)
        # host drop
        hcol = self._pick(YELLOW, gturn + 1)
        hr = drop_cell(self.board, self.rows, self.cols, hcol)
        if hr is None:
            hcol = next(c for c in range(self.cols) if drop_cell(self.board, self.rows, self.cols, c) is not None)
            hr = drop_cell(self.board, self.rows, self.cols, hcol)
        self.board[hr][hcol] = YELLOW
        hturn = gturn + 1
        if is_win(self.board, self.rows, self.cols, hr, hcol, YELLOW):
            self._record(hr, hcol, "host", hturn, True, "host")
            return HookResult({"action": "round_result", "turn": hturn, "col": hcol, "row": hr,
                               "player": "host", "game_over": True, "winner": "host"}, completed=True)
        if is_full(self.board, self.rows, self.cols):
            self._record(hr, hcol, "host", hturn, True, "none")
            return HookResult({"action": "round_result", "turn": hturn, "col": hcol, "row": hr,
                               "player": "host", "game_over": True, "winner": "none"}, completed=True)
        self._record(hr, hcol, "host", hturn, False, "none")
        self.turn = hturn + 1
        return HookResult({"action": "round_result", "turn": hturn, "col": hcol, "row": hr,
                           "player": "host", "game_over": False, "winner": "none"})

    # ---- move selection ----
    def _pick_auto(self, sym: str) -> int:
        strat = self.strategy.read()
        if strat:
            mode = strat.get("mode")
            if mode == "fixed":
                col = int(strat.get("col", -1))
                if _in(self.rows, self.cols, 0, col) and drop_cell(self.board, self.rows, self.cols, col) is not None:
                    return col
            elif mode == "seq":
                seq = strat.get("sequence") or []
                idx = sum(1 for rr in range(self.rows) for cc in range(self.cols) if self.board[rr][cc] == sym)
                if 0 <= idx < len(seq):
                    try:
                        col = int(seq[idx])
                    except (TypeError, ValueError, IndexError):
                        col = -1
                    if _in(self.rows, self.cols, 0, col) and drop_cell(self.board, self.rows, self.cols, col) is not None:
                        return col
        return heuristic_move(self.board, self.rows, self.cols, sym)

    def _pick(self, sym: str, match_turn: int) -> int:
        if self.control_mode == "human":
            decision = self._await_human_decision("turn", match_turn)
            try:
                col = int(decision["col"])
            except (KeyError, TypeError, ValueError):
                self._reject_human_decision("turn", match_turn, decision, "col must be an integer")
            if not _in(self.rows, self.cols, 0, col) or drop_cell(self.board, self.rows, self.cols, col) is None:
                self._reject_human_decision("turn", match_turn, decision, "column is full or outside the board")
            return col
        col = self._pick_auto(sym)
        if self.bus is None:
            return col
        # hybrid（auto 模式，默认）：非阻塞读 decide，无则 auto
        if self.decision_mode != "manual":
            d = self._consume_hybrid("turn", match_turn)
            if d and "col" in d:
                try:
                    c = int(d["col"])
                    if _in(self.rows, self.cols, 0, c) and drop_cell(self.board, self.rows, self.cols, c) is not None:
                        return c
                except (TypeError, ValueError):
                    pass
            return col
        raise RuntimeError(f"unsupported decision mode: {self.decision_mode}")

    # ---- snapshot / details ----
    def _record(self, r: int, c: int, player: str, turn: int, over: bool, winner: str) -> None:
        sym = RED if player == "guest" else YELLOW
        moves = sum(1 for rr in range(self.rows) for cc in range(self.cols) if self.board[rr][cc] != EMPTY)
        self.details.append(
            type="move", turn=turn, row=r, col=c, player=player, stone=sym,
            game_over=over, winner=winner,
            summary=f"T{turn} {player} -> col{c}(row{r})" + (f"  WINNER={winner}" if over else ""),
        )
        phase = "game_over" if over else "playing"
        summary = (f"Game over: {winner} wins" if over and winner != "none"
                   else "Draw (board full)" if over else f"{player} dropped col {c}")
        self.snapshot.update(
            board=[row[:] for row in self.board], turn=turn, current=player,
            phase=phase, winner=winner, moves=moves,
            last_move={"row": r, "col": c, "player": player},
            last_event={"summary": summary,
                        "structured": {"turn": turn, "row": r, "col": c, "player": player,
                                       "game_over": over, "winner": winner}},
        )

    # ---- display ----
    def proto_display(self, msg, direction):
        action = msg.get("action")
        if action == "round_result":
            sym = "X" if msg["player"] == "guest" else "O"
            line = f"{msg['player']}({sym}) -> col {msg['col']} (row {msg['row']})"
            if msg.get("game_over"):
                line += f"   GAME OVER winner={msg.get('winner', 'none')}"
            return line
        if action == "error":
            return f"illegal: {msg.get('reason')}"
        return None
