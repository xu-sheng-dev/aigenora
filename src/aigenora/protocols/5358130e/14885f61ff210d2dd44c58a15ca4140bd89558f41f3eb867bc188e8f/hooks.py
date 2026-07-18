"""Reversi (Othello) hooks for the session_loop engine.

1v1 full-information game: guest=Black (first), host=White (second). A move must
bracket a straight line of opponent stones between the new stone and an existing own
stone; all bracketed stones flip. Move legality is enforced host-side (illegal move
aborts without a session proof). A player with no legal move sends `pass`; the game
ends when neither side can move or the board is full, and the side with more stones
wins. Board state stays in this hooks instance + snapshot bus; only row/col integers
travel in the spec message (security red-line D1).

Both sides auto-play via a positional-weight + flip-count heuristic so
`aigenora protocol test` runs a full game to completion.
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
DIRS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

_POSITIONAL_8 = [
    [120, -20,  20,   5,   5,  20, -20, 120],
    [-20, -40,  -5,  -5,  -5,  -5, -40, -20],
    [ 20,  -5,  15,   3,   3,  15,  -5,  20],
    [  5,  -5,   3,   3,   3,   3,  -5,   5],
    [  5,  -5,   3,   3,   3,   3,  -5,   5],
    [ 20,  -5,  15,   3,   3,  15,  -5,  20],
    [-20, -40,  -5,  -5,  -5,  -5, -40, -20],
    [120, -20,  20,   5,   5,  20, -20, 120],
]


# ---- pure board logic (module-level) ----

def _in(n: int, r: int, c: int) -> bool:
    return 0 <= r < n and 0 <= c < n


def opponent(sym: str) -> str:
    return WHITE if sym == BLACK else BLACK


def flips_in_dir(board: list[list[str]], n: int, r: int, c: int, dr: int, dc: int, sym: str):
    """Stones that would flip if `sym` were placed at empty (r,c) along (dr,dc):
    a run of opponent stones terminated by an own stone. Returns [] if not bracketed.
    Reads from the neighbour of (r,c), so (r,c) itself need not be set yet."""
    opp = opponent(sym)
    rr, cc = r + dr, c + dc
    run = []
    while _in(n, rr, cc) and board[rr][cc] == opp:
        run.append((rr, cc))
        rr += dr
        cc += dc
    if run and _in(n, rr, cc) and board[rr][cc] == sym:
        return run
    return []


def is_legal(board: list[list[str]], n: int, r: int, c: int, sym: str) -> bool:
    if not _in(n, r, c) or board[r][c] != EMPTY:
        return False
    return any(flips_in_dir(board, n, r, c, dr, dc, sym) for dr, dc in DIRS)


def legal_moves(board: list[list[str]], n: int, sym: str):
    return [(r, c) for r in range(n) for c in range(n) if is_legal(board, n, r, c, sym)]


def has_legal(board: list[list[str]], n: int, sym: str) -> bool:
    for r in range(n):
        for c in range(n):
            if board[r][c] == EMPTY and any(flips_in_dir(board, n, r, c, dr, dc, sym) for dr, dc in DIRS):
                return True
    return False


def apply_move(board: list[list[str]], n: int, r: int, c: int, sym: str) -> int:
    """Place sym at (r,c) and flip all bracketed opponent stones. Returns flip count."""
    board[r][c] = sym
    total = 0
    for dr, dc in DIRS:
        for (fr, fc) in flips_in_dir(board, n, r, c, dr, dc, sym):
            board[fr][fc] = sym
            total += 1
    return total


def count_stones(board: list[list[str]], n: int, sym: str) -> int:
    return sum(1 for r in range(n) for c in range(n) if board[r][c] == sym)


def is_full(board: list[list[str]], n: int) -> bool:
    return all(board[r][c] != EMPTY for r in range(n) for c in range(n))


def initial_board(n: int) -> list[list[str]]:
    b = [[EMPTY] * n for _ in range(n)]
    m = n // 2
    b[m - 1][m - 1] = WHITE
    b[m - 1][m] = BLACK
    b[m][m - 1] = BLACK
    b[m][m] = WHITE
    return b


def _pos_value(n: int, r: int, c: int) -> int:
    if n == 8:
        return _POSITIONAL_8[r][c]
    corner = (r in (0, n - 1)) and (c in (0, n - 1))
    edge = (r in (0, n - 1)) or (c in (0, n - 1))
    return 100 if corner else (10 if edge else 1)


def heuristic_move(board: list[list[str]], n: int, sym: str):
    """Best legal move by (positional value + flip count), or None if no legal move."""
    best, best_score = None, -(1 << 30)
    for r in range(n):
        for c in range(n):
            if board[r][c] != EMPTY:
                continue
            flips = sum(len(flips_in_dir(board, n, r, c, dr, dc, sym)) for dr, dc in DIRS)
            if flips == 0:
                continue
            score = _pos_value(n, r, c) + flips
            if score > best_score:
                best_score = score
                best = (r, c)
    return best


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")
    def proto_init(self, options, role, args, state_dir: Path, decision_config: dict[str, Any] | None = None):
        super().proto_init(options, role, args, state_dir, decision_config)
        self.state = StateStore(state_dir)
        self.n = int(options.get("board_size") or 8)
        self.board = initial_board(self.n)
        self.turn = 1
        self._push_snapshot("playing", "guest", None, False, "none")

    def proto_host_metadata(self):
        return ("Reversi", "game,reversi,board", "supply", {"board_size": self.n})

    # ---- join / ready ----
    def proto_host_handle_join(self, msg):
        self.n = int(msg.get("board_size") or self.n)
        self.board = initial_board(self.n)
        self.turn = 1
        self._push_snapshot("playing", "guest", None, False, "none")
        return HookResult({"action": "ready", "board_size": self.n, "first": "guest"})

    def proto_guest_join_message(self):
        return {"action": "join", "board_size": self.n}

    def proto_guest_handle_ready(self, msg):
        self.n = int(msg["board_size"])
        self.board = initial_board(self.n)
        self.turn = 1
        self._push_snapshot("playing", "guest", None, False, "none")

    # ---- guest side ----
    def proto_guest_first_action(self):
        return self._guest_action(self.turn)

    def proto_guest_handle(self, msg):
        action = msg.get("action")
        if action == "round_result":
            turn = int(msg["turn"])
            passed = bool(msg.get("passed"))
            if not passed:
                hr, hc = int(msg["row"]), int(msg["col"])
                apply_move(self.board, self.n, hr, hc, WHITE)
            over = bool(msg.get("game_over"))
            winner = msg.get("winner", "none")
            self.turn = turn + 1
            self._push_snapshot("game_over" if over else "playing", "guest",
                                {"row": int(msg.get("row", 0)), "col": int(msg.get("col", 0)),
                                 "player": "host", "passed": passed}, over, winner)
            if over:
                return HookResult(completed=True)
            return HookResult(self._guest_action(self.turn))
        if action == "end":
            return HookResult(completed=True)
        return HookResult({"action": "error", "reason": "not_your_turn"}, abort=True)

    def _guest_action(self, turn: int) -> dict:
        if has_legal(self.board, self.n, BLACK):
            r, c = self._pick(BLACK, turn)
            flipped = apply_move(self.board, self.n, r, c, BLACK)
            self.turn = turn
            self.details.append(type="move", turn=turn, row=r, col=c, player="guest",
                                flipped=flipped, summary=f"T{turn} guest -> ({r},{c}) flipped {flipped}")
            self._push_snapshot("playing", "host", {"row": r, "col": c, "player": "guest"}, False, "none")
            return {"action": "move", "turn": turn, "row": r, "col": c}
        if self.control_mode == "human":
            # A forced pass is still a local game operation in strict human mode.
            # Wait for an explicit acknowledgement instead of silently sending it.
            self._pick(BLACK, turn)
        self.turn = turn
        self.details.append(type="pass", turn=turn, player="guest", summary=f"T{turn} guest pass")
        self._push_snapshot("playing", "host", {"player": "guest", "passed": True}, False, "none")
        return {"action": "pass", "turn": turn}

    # ---- host side (referee + white) ----
    def proto_host_handle(self, msg):
        action = msg.get("action")
        turn = int(msg.get("turn", self.turn))
        if action == "move":
            gr, gc = int(msg["row"]), int(msg["col"])
            if not is_legal(self.board, self.n, gr, gc, BLACK):
                return HookResult({"action": "error", "turn": turn, "reason": "illegal_move"}, abort=True)
            gflipped = apply_move(self.board, self.n, gr, gc, BLACK)
            self.details.append(type="move", turn=turn, row=gr, col=gc, player="guest",
                                flipped=gflipped, summary=f"T{turn} guest -> ({gr},{gc}) flipped {gflipped}")
            return self._host_reply(turn, guest_passed=False)
        if action == "pass":
            self.details.append(type="pass", turn=turn, player="guest", summary=f"T{turn} guest pass")
            return self._host_reply(turn, guest_passed=True)
        return HookResult({"action": "error", "reason": "not_your_turn"}, abort=True)

    def _host_reply(self, guest_turn: int, guest_passed: bool) -> HookResult:
        host_has = has_legal(self.board, self.n, WHITE)
        hturn = guest_turn + 1
        if host_has:
            hr, hc = self._pick(WHITE, hturn)
            flipped = apply_move(self.board, self.n, hr, hc, WHITE)
            over = self._is_terminal()
            winner = self._winner() if over else "none"
            self.details.append(type="move", turn=hturn, row=hr, col=hc, player="host",
                                flipped=flipped, game_over=over, winner=winner,
                                summary=f"T{hturn} host -> ({hr},{hc}) flipped {flipped}" + (f"  OVER winner={winner}" if over else ""))
            self._push_snapshot("game_over" if over else "playing", "guest",
                                {"row": hr, "col": hc, "player": "host"}, over, winner)
            self.turn = hturn + 1
            return HookResult(self._round_result(hturn, hr, hc, flipped, False, over, winner), completed=over)
        # host has no move -> pass
        if self.control_mode == "human":
            self._pick(WHITE, hturn)
        over = self._is_terminal()
        winner = self._winner() if over else "none"
        self.details.append(type="pass", turn=hturn, player="host", game_over=over, winner=winner,
                            summary=f"T{hturn} host pass" + (f"  OVER winner={winner}" if over else ""))
        self._push_snapshot("game_over" if over else "playing", "guest",
                            {"player": "host", "passed": True}, over, winner)
        self.turn = hturn + 1
        return HookResult(self._round_result(hturn, 0, 0, 0, True, over, winner), completed=over)

    def _is_terminal(self) -> bool:
        if is_full(self.board, self.n):
            return True
        return not has_legal(self.board, self.n, BLACK) and not has_legal(self.board, self.n, WHITE)

    def _winner(self) -> str:
        b = count_stones(self.board, self.n, BLACK)
        w = count_stones(self.board, self.n, WHITE)
        if b > w:
            return "guest"
        if w > b:
            return "host"
        return "none"

    def _round_result(self, turn: int, row: int, col: int, flipped: int,
                      passed: bool, over: bool, winner: str) -> dict:
        return {"action": "round_result", "turn": turn, "row": row, "col": col,
                "player": "host", "flipped": flipped, "passed": passed,
                "game_over": over, "winner": winner,
                "black": count_stones(self.board, self.n, BLACK),
                "white": count_stones(self.board, self.n, WHITE)}

    # ---- move selection ----
    def _pick_auto(self, sym: str):
        strat = self.strategy.read()
        if strat:
            mode = strat.get("mode")
            if mode == "fixed":
                r, c = int(strat.get("row", -1)), int(strat.get("col", -1))
                if is_legal(self.board, self.n, r, c, sym):
                    return (r, c)
            elif mode == "seq":
                seq = strat.get("sequence") or []
                if 0 <= (self.turn - 1) < len(seq):
                    try:
                        r, c = int(seq[self.turn - 1][0]), int(seq[self.turn - 1][1])
                    except (TypeError, ValueError, IndexError):
                        r, c = -1, -1
                    if is_legal(self.board, self.n, r, c, sym):
                        return (r, c)
        return heuristic_move(self.board, self.n, sym)

    def _pick(self, sym: str, match_turn: int):
        if self.control_mode == "human":
            decision = self._await_human_decision("turn", match_turn)
            moves = legal_moves(self.board, self.n, sym)
            if not moves:
                if decision.get("pass") is True or decision.get("action") == "pass":
                    return None
                self._reject_human_decision("turn", match_turn, decision, "no legal move; submit pass=true")
            if decision.get("pass") is True or decision.get("action") == "pass":
                self._reject_human_decision("turn", match_turn, decision, "pass is illegal while a move exists")
            try:
                row, col = int(decision["row"]), int(decision["col"])
            except (KeyError, TypeError, ValueError):
                self._reject_human_decision("turn", match_turn, decision, "row and col must be integers")
            if not is_legal(self.board, self.n, row, col, sym):
                self._reject_human_decision("turn", match_turn, decision, "move does not bracket opponent stones")
            return (row, col)
        auto = self._pick_auto(sym)
        if auto is None:
            auto = heuristic_move(self.board, self.n, sym)
        if auto is None:
            return auto
        if self.bus is None:
            return auto
        # hybrid（auto 模式，默认）：非阻塞读 decide，无则 auto
        if self.decision_mode != "manual":
            d = self._consume_hybrid("turn", match_turn)
            if d and "row" in d and "col" in d:
                try:
                    r, c = int(d["row"]), int(d["col"])
                    if is_legal(self.board, self.n, r, c, sym):
                        return (r, c)
                except (TypeError, ValueError):
                    pass
            return auto
        raise RuntimeError(f"unsupported decision mode: {self.decision_mode}")

    # ---- snapshot ----
    def _push_snapshot(self, phase: str, current: str, last_move, over: bool, winner: str) -> None:
        black = count_stones(self.board, self.n, BLACK)
        white = count_stones(self.board, self.n, WHITE)
        self.snapshot.update(
            board_size=self.n, board=[row[:] for row in self.board],
            turn=self.turn, current=current, phase=phase, winner=winner,
            black=black, white=white, last_move=last_move,
            last_event={"summary": (f"Game over: {winner} wins (B{black}-W{white})" if over
                                     else f"{current} to move · B{black}-W{white}"),
                        "structured": {"game_over": over, "winner": winner, "black": black, "white": white}},
        )

    # ---- display ----
    def proto_display(self, msg, direction):
        action = msg.get("action")
        if action == "round_result":
            if msg.get("passed"):
                line = f"host PASS"
            else:
                line = f"host -> ({msg['row']},{msg['col']}) flipped {msg.get('flipped', 0)}"
            if msg.get("game_over"):
                line += f"   GAME OVER winner={msg.get('winner','none')} (B{msg.get('black')}-W{msg.get('white')})"
            return line
        if action == "move":
            return f"guest -> ({msg['row']},{msg['col']})"
        if action == "pass":
            return "guest PASS"
        if action == "error":
            return f"illegal: {msg.get('reason')}"
        return None
