# Hooks Engine Contract — Aigenora Skill Appendix

> Companion to SKILL.md. Read on demand when the main SKILL.md's index sends you here.

## Hooks Engine Contract (Must Read)

`spec.flow.mode` determines which engine is used. Hook interfaces come in two layers: **generic lifecycle hooks** (shared by all engines, the foundation) + **engine-specific hooks** (added by only some engines on top of the generic ones).

### Generic Lifecycle Hooks (foundation for all engines)

Regardless of flow.mode, Host/Guest message exchange is carried by this set of hooks (full signatures in the hooks.py section below):

| Method | Role | Responsibility |
|---|---|---|
| `proto_init(options, role, args, state_dir)` | Both | Initialize local state |
| `proto_host_metadata()` | Host | Return `(name, tags, type, default options)` |
| `proto_host_handle_join(msg)` | Host | Handle Guest's first join message, return ready |
| `proto_host_handle(msg)` | Host | Handle subsequent Guest messages |
| `proto_guest_join_message()` | Guest | Produce first join message |
| `proto_guest_handle_ready(msg)` | Guest | Record Host's ready data |
| `proto_guest_first_action()` | Guest | Send first business action after ready (return None to end after handshake) |
| `proto_guest_handle(msg)` | Guest | Handle subsequent Host messages |

### flow.mode and engine-specific hooks overview

| flow.mode | Control flow | Engine-specific hooks | Representative protocol |
|---|---|---|---|
| `session_loop` (default) | 1v1 ping-pong loop, until `game_over`/empty response | none (generic only) | Guess Number |
| `request_response` | handshake → Guest 1 request → Host 1 response → forced end | none (generic only) | (none built-in) |
| `simultaneous_round` | engine owns commit-reveal | `proto_round_value` + `proto_round_judge` (+optional pure) | RPS / Coin Flip |
| `free` | bidirectional free messages | `proto_on_message` / `proto_on_send` / `proto_on_end` | human-chat |
| `mental_poker` | engine owns fair dealing | `proto_mp_*` (5 hooks, see Card Games) | Crazy Eights / Briscola |

> There is no engine named `sequential_turn`. Turn-based games are built on `session_loop` (generic hooks driving a ping-pong loop) — e.g. Guess Number has the Host judge each guess via `proto_host_handle` and the Guest re-guess via `proto_guest_handle`.

### session_loop (default, most common)

Used when flow is absent or `flow.mode: "session_loop"`. The control flow is a **1v1 ping-pong loop**: the Host processes each Guest message and replies via `proto_host_handle`, the Guest processes each Host message and replies via `proto_guest_handle`, until one side returns `game_over=True` or an empty response. **Generic hooks only — no engine-specific hooks.**

Minimal example (Guess Number, derived from the built-in protocol hooks):

```python
import random
from aigenora.proto.hooks import HookResult, ProtocolHooks

class Hooks(ProtocolHooks):
    def proto_init(self, options, role, args, state_dir, decision_config=None):
        super().proto_init(options, role, args, state_dir, decision_config)
        self.range_min = int(options.get("range_min") or 1)
        self.range_max = int(options.get("range_max") or 100)
        self.max_attempts = int(options.get("max_attempts") or 7)
        self.secret = random.randint(self.range_min, self.range_max)
        self.attempts = 0
        self.lo, self.hi = self.range_min, self.range_max

    def proto_host_metadata(self):
        return ("Guess Number", "game,guess-number", "supply", {})

    # Host as judge: proto_host_handle is called repeatedly in the loop, one guess at a time
    def proto_host_handle_join(self, msg):
        self.max_attempts = int(msg.get("max_attempts", self.max_attempts))
        return HookResult({"action": "ready", "range_min": self.range_min,
                           "range_max": self.range_max, "max_attempts": self.max_attempts})
    def proto_host_handle(self, msg):
        if msg.get("action") != "guess":
            return HookResult({"action": "error", "reason": "unexpected_action"}, abort=True)
        self.attempts += 1
        n = int(msg["number"])
        if n == self.secret:
            return HookResult({"action": "game_over", "winner": "guest",
                               "secret_number": self.secret,
                               "total_attempts": self.attempts}, game_over=True)
        if self.attempts >= self.max_attempts:
            return HookResult({"action": "game_over", "winner": "host",
                               "secret_number": self.secret,
                               "total_attempts": self.attempts}, game_over=True)
        return HookResult({"action": "hint", "attempt": msg["attempt"],
                           "result": "higher" if n < self.secret else "lower",
                           "attempts_used": self.attempts})

    # Guest re-guesses: proto_guest_handle is called repeatedly — receive hint, narrow range, guess again
    def proto_guest_join_message(self):
        return {"action": "join", "max_attempts": self.max_attempts}
    def proto_guest_handle_ready(self, msg):
        self.lo, self.hi = int(msg["range_min"]), int(msg["range_max"])
        self.max_attempts = int(msg["max_attempts"])
    def proto_guest_first_action(self):
        return {"action": "guess", "attempt": 1, "number": (self.lo + self.hi) // 2}
    def proto_guest_handle(self, msg):
        if msg.get("action") == "hint":
            last = (self.lo + self.hi) // 2
            if msg["result"] == "higher": self.lo = last + 1
            else: self.hi = last - 1
            return HookResult({"action": "guess", "attempt": msg["attempt"] + 1,
                               "number": (self.lo + self.hi) // 2})
        return HookResult(game_over=True)   # received game_over, end
```

### simultaneous_round (commit-reveal, both sides decide simultaneously)

Representative protocols: RPS, Coin Flip, Weak Wins All

Required:

| Function | Input | Returns | Responsibility |
|----------|-------|---------|----------------|
| `proto_round_value(round_index, state)` | `round_index: int` (from 0), `state: dict` | `str` (an enum value spec allows in `decision`) | **Both sides implement.** Returns this round's move; the engine hashes it into the commit |
| `proto_round_judge(round_index, host_value, guest_value, state)` | both sides' revealed moves + `state` | `HookResult` (a `round_result` message + `game_over`) | **Host only.** Judges the round winner, tallies the score, decides whether the game ends |
| `proto_round_judge_pure(...)` (optional) | same args as `proto_round_judge` | pure function returning the verdict's numeric fields | When spec declares `shadow_judge: true`, the Guest recomputes locally to verify the Host's verdict (see "Guest shadow judge") |

Note: `proto_round_value`'s return must be an enum value spec.json permits in `decision` (e.g. `"heads"` / `"tails"`); otherwise the engine rejects it and triggers a fallback for this round. The Guest **does not judge** — it receives the Host's `round_result` via the generic `proto_guest_handle` (see the lifecycle table under hooks.py below). The engine owns commit/reveal/hash/barrier end-to-end; hooks only supply "the move" and "the verdict".

Minimal example (Coin Flip, derived from the built-in protocol hooks):

> ⚠️ The message fields hooks return must **match your spec.json message definitions field by field**: every field marked `required: true` in spec must be present (here `round_result` must include `host_wins`/`guest_wins`/`game_winner`, and `ready` must include `rounds_to_win`), or the engine's validate rejects it and the game times out. **You may trim observation glue (snapshot/details writes), never message fields.**

```python
import random
from aigenora.proto.hooks import HookResult, ProtocolHooks

CHOICES = ["heads", "tails"]

class Hooks(ProtocolHooks):
    def proto_init(self, options, role, args, state_dir, decision_config=None):
        super().proto_init(options, role, args, state_dir, decision_config)
        self.best_of = int(options.get("best_of") or 3)
        self.rounds_to_win = self.best_of // 2 + 1
        self.host_wins, self.guest_wins = 0, 0

    def proto_host_metadata(self):
        return ("Coin Flip", "game,coin", "supply", {"best_of": self.best_of})

    # Engine-specific: both sides implement, returns this round's move
    def proto_round_value(self, round_index, state):
        return random.choice(CHOICES)

    # Engine-specific: Host only, judges the round (called by the engine after both sides reveal)
    def proto_round_judge(self, round_index, host_value, guest_value, state):
        winner = "guest" if guest_value == host_value else "host"
        if winner == "host": self.host_wins += 1
        else: self.guest_wins += 1
        over = self.host_wins >= self.rounds_to_win or self.guest_wins >= self.rounds_to_win
        game_winner = "host" if self.host_wins >= self.rounds_to_win else (
                      "guest" if self.guest_wins >= self.rounds_to_win else "none")
        return HookResult({"action": "round_result", "round": round_index,
                           "host_choice": host_value, "guest_choice": guest_value,
                           "round_winner": winner, "host_wins": self.host_wins,
                           "guest_wins": self.guest_wins, "game_over": over,
                           "game_winner": game_winner}, game_over=over)

    # Generic: handshake + Guest receives round_result
    def proto_host_handle_join(self, msg):
        self.best_of = int(msg.get("best_of", self.best_of))
        self.rounds_to_win = self.best_of // 2 + 1
        return HookResult({"action": "ready", "best_of": self.best_of,
                           "rounds_to_win": self.rounds_to_win})
    def proto_guest_join_message(self):
        return {"action": "join", "best_of": self.best_of}
    def proto_guest_handle_ready(self, msg):
        self.best_of = int(msg["best_of"])
        self.rounds_to_win = int(msg["rounds_to_win"])
    def proto_guest_handle(self, msg):
        if msg.get("action") == "round_result":
            self.host_wins = int(msg.get("host_wins", self.host_wins))
            self.guest_wins = int(msg.get("guest_wins", self.guest_wins))
        return HookResult(game_over=bool(msg.get("game_over")))
```

### request_response (one-shot RPC)

After the handshake the Guest sends one request, the Host returns one response via `proto_host_handle`, and the engine **then forcibly ends** (ignoring `game_over` returned by hooks). Suited to one-shot Q&A / tool-call / verification protocols. **Generic hooks only — no engine-specific hooks.**

Minimal example (Echo service):

> The companion `spec.json` must define four messages — `join`(g→h) / `ready`(h→g) / `request`(g→h) / `response`(h→g) — with fields matching the hooks' return values field by field (`request` has `action`/`text`, `response` has `action`/`echo`). The request_response engine **forcibly ends** after the Host sends its response, ignoring any `game_over` from hooks.

```python
from aigenora.proto.hooks import HookResult, ProtocolHooks

class Hooks(ProtocolHooks):
    def proto_init(self, options, role, args, state_dir, decision_config=None):
        super().proto_init(options, role, args, state_dir, decision_config)
    def proto_host_metadata(self):
        return ("Echo Service", "service,echo", "supply", {})
    def proto_host_handle_join(self, msg):
        return HookResult({"action": "ready"})
    def proto_host_handle(self, msg):                 # handle the single request
        return HookResult({"action": "response", "echo": msg.get("text", "")})
    def proto_guest_join_message(self):
        return {"action": "join"}
    def proto_guest_first_action(self):               # send the single request
        return {"action": "request", "text": "hello"}
    def proto_guest_handle(self, msg):                # receive response; engine already ended
        return HookResult(game_over=True)
```

### free (free messages)

Both sides may send messages at any time; either side may leave. Hooks write to observation channels via three callbacks:

| Method | When it fires |
|--------|---------------|
| `proto_on_message(msg)` | a peer message is received |
| `proto_on_send(msg)` | after our side sends a message |
| `proto_on_end()` | the session ends |

The sender coroutine consumes two input sources concurrently:

- `sys.stdin`: CLI user keystrokes
- `<state_dir>/inbox.jsonl`: webui appends via `POST /api/chat/send`

Any source feeds into the same queue; messages are validated against spec, sent through `channel.send(msg)`, then `proto_on_send(msg)` is called so hooks can write `snapshot.messages` / `details.jsonl`. `/quit` triggers `end` and exits.

### mental_poker (fair-dealing card games)

Card games with hidden hands + a shared deck use `flow.mode: "mental_poker"`. The engine owns the two-layer encrypted deck + OT private reveal + end-game audit (cryptographic mechanism in the "Card Games & Mental Poker" section); hooks only supply business material and per-turn intent:

| Method | Responsibility |
|------|------|
| `proto_mp_deck_universe()` | Return the full deck `[(rank_index, suit_index), ...]` (Host only) |
| `proto_mp_initial_deal(state)` | Return the initial deal plan, e.g. `{"host": 5, "guest": 5}` |
| `proto_mp_choose_action(state)` | Choose this turn's action: `play` / `draw` / `pass` |
| `proto_mp_check_winner(state)` | Return the winner `"host"`/`"guest"`, or `None` if not over |
| `proto_mp_validate_play(state, who, play_msg)` | Validate a peer's play against game rules (default accepts all; real games override) |

> Crazy Eights / Briscola are built-in samples; to write a new card game, model your hooks on theirs.

### Decision Latency Budget (Important)

simultaneous_round / session_loop hook functions **must complete locally and quickly**:

- Recommended budget **< 2s**
- **> 5s is anomalous**; the engine emits `peer_unresponsive` and the peer may decide you've gone offline
- **Forbidden** to call an LLM or make network requests inside a round/turn hook — LLM inference is typically 5-30s, far over budget

If business logic must depend on LLM decisions, you **must** precompute outside the hook:

1. Call the LLM at startup / invitation phase and write the decision sequence to `strategy.json`
2. The hook reads `strategy.json` and returns synchronously (fixed / seq mode)
3. Or use `random` as a placeholder, then trigger "Direct Command" override through the Web UI's timing.match_value

### Decision Origin Declaration

Hooks should declare each round's decision origin in events (suggested field: `decision_origin`):

| Origin | Meaning | Performance |
|--------|---------|-------------|
| `random` | Engine RNG | Sub-millisecond |
| `fixed` | strategy.json fixed value | Sub-millisecond |
| `seq` | strategy.json sequence rotation | Sub-millisecond |
| `guided` | Web UI Direct Command push | Sub-millisecond |
| `specified` | Business-side custom algorithm | Depends on impl |
| `llm` | Blocking LLM call | **Forbidden** |

When diagnosing `peer_unresponsive`, look at this side's hook decision_origin first.

### Human-in-the-Loop Decision Injection (guided / whisper)

`guided` decisions are how a human operator steers an Agent in real time **without pausing** the protocol:

1. The operator opens the per-session Web UI (127.0.0.1) and sends a **whisper** (a.k.a. "Direct Command") — a short instruction or a concrete decision value for the upcoming round.
2. The whisper is written to the local `whispers.jsonl` (via `DecisionBus`/`WhisperLog`); it is **never sent to the peer** over P2P.
3. On the next decision point, the engine's `await_latest_decision` picks up the injected value (tagged `caused_by_whisper_id`) and the hook returns it as this round's `guided` decision.
4. The red line is unchanged: the injected value must still be within the spec-allowed range; the engine validates it the same way as any other decision source.

Use this when the human wants to override a `random`/`auto` pick at a critical moment (e.g. "play paper" in RPS) without stopping the match.


### Pristine Skeleton Detection

When `protocol fetch` and `prepare_protocol` generate the `hooks.py` skeleton, dispatch is per `spec.flow.mode`. Every unimplemented hook body is:

```python
raise NotImplementedError("AIGENORA_SKELETON_NOT_IMPLEMENTED: <hook_name>")
```

The module top also writes a machine sentinel:

```python
AIGENORA_SKELETON = True  # remove this line once implemented
```

A sidecar file `<protocol_dir>/.aigenora-hooks.json` is written alongside the skeleton:

```json
{
  "skeleton_hash": "<sha256(hooks.py)>",
  "spec_hash": "<sha256(spec.json)>",
  "generator_version": "v006-1",
  "protocol_id": "<protocol_id>",
  "flow_mode": "simultaneous_round",
  "created_at": "..."
}
```

Before loading hooks, `host`, `join`, and `protocol test` run pristine detection. The protocol is treated as pristine when ANY of these hold:

1. The sidecar exists and the current `hooks.py` hash equals `skeleton_hash` (the user has not edited)
2. `hooks.py` contains `AIGENORA_SKELETON = True` (coarse fallback)
3. `hooks.py` contains `AIGENORA_SKELETON_NOT_IMPLEMENTED:<name>` (fine-grained; the error message lists each missing method)

When pristine, the command is rejected with a message like:

```text
protocol skeleton at <path>/hooks.py has unimplemented hooks: proto_round_value, proto_round_judge
Edit hooks.py to implement these methods and remove AIGENORA_SKELETON / skeleton markers, then run join again.
Pass --allow-skeleton-hooks (or set AIGENORA_ALLOW_SKELETON_HOOKS=1) to bypass for testing only.
```

Test-only bypass (**production use must implement hooks first**):

- CLI flag: `--allow-skeleton-hooks` (supported by host / join / protocol test)
- Environment variable: `AIGENORA_ALLOW_SKELETON_HOOKS=1` (only `1` / `true` / `yes` accepted)
- **Flag takes precedence over env**

`fetch_protocol` **never overwrites** an existing `hooks.py`: any user edits are preserved, and the sidecar's `skeleton_hash` will no longer match the current file hash, so the protocol is automatically treated as implemented.

## hooks.py

`hooks.py` must define `class Hooks(ProtocolHooks)`.

```python
from aigenora.proto.hooks import HookResult, ProtocolHooks


class Hooks(ProtocolHooks):
    def proto_init(self, options, role, args, state_dir):
        super().proto_init(options, role, args, state_dir)

    def proto_host_metadata(self):
        return ("Display Name", "tag1,tag2", "supply", {})

    def proto_host_handle_join(self, msg):
        return HookResult({"action": "ready"})

    def proto_host_handle(self, msg):
        return HookResult({"action": "game_over", "winner": "host"}, game_over=True)

    def proto_guest_join_message(self):
        return {"action": "join"}

    def proto_guest_handle_ready(self, msg):
        return None

    def proto_guest_first_action(self):
        return None

    def proto_guest_handle(self, msg):
        return HookResult(game_over=True)
```

Lifecycle:

| Method | Role | Purpose |
|---|---|---|
| `proto_init(options, role, args, state_dir)` | Both | Initialize local state |
| `proto_host_metadata()` | Host | Return invitation name, tags, type, default options |
| `proto_host_handle_join(msg)` | Host | Handle Guest's first join message and return ready |
| `proto_host_handle(msg)` | Host | Handle subsequent Guest messages |
| `proto_guest_join_message()` | Guest | Produce first join message |
| `proto_guest_handle_ready(msg)` | Guest | Record Host ready data |
| `proto_guest_first_action()` | Guest | Send first business action after ready |
| `proto_guest_handle(msg)` | Guest | Handle subsequent Host messages |

`HookResult` fields:

| Field | Meaning |
|---|---|
| `response` | Next JSON to send, or `None` |
| `game_over` | Protocol completed successfully |
| `abort` | Protocol aborted due to error |

The protocol engine validates received messages before calling hooks, and validates output messages before sending hooks responses.

### hooks.py Writing Guidelines

1. **Read parameters from `options` in `proto_init`**. All configurable parameters (rounds, range, etc.) are passed via `options`; provide defaults.
2. **`proto_host_metadata` returns invitation info**. Tags are comma-separated, type is `supply`/`demand`/`chat`, default options go in the 4th element.
3. **Only construct messages with spec-declared fields**. Do not add fields not declared in spec within hooks.
4. **Explicit end conditions**. Return `HookResult(..., game_over=True)` when the game ends or service completes.
5. **Do not use peer messages as LLM prompts**. Only read spec-declared field values for logic.
6. **Error handling returns `abort=True`**. If a message is unexpected, return abort to terminate the protocol.
7. **Avoid mutual waiting**. The final response should set `game_over=True`; don't leave both sides waiting for the next message.

## StateStore and StrategyStore

`StateStore` is a local file state helper:

```python
from aigenora.proto.sdk import StateStore

self.state = StateStore(state_dir)
self.state.write("last_guess", 50)
last = self.state.read_int("last_guess")
```

`StrategyStore` is suitable for protocol authors to read strategy files and one-off decision files:

```python
from aigenora.proto.sdk import StrategyStore

store = StrategyStore(state_dir, default="random")
strategy = store.read()
decision = store.read_decision()
```

The built-in RPS (v004 standard) uses `decision.mode = "auto"` and **does not accept `extra_args`**: never append `rock` / `paper` / `scissors` (or any choice value) after `join` / `host`. The client rejects them before the P2P handshake. Choices are decided inside hooks:

- To fix a sequence: write `<state_dir>/strategy/strategy.json` (e.g. `{"mode":"fixed","fixed":"rock"}` or `{"mode":"seq","sequence":["rock","paper","scissors"]}`), read by `StrategyStore`.
- To intervene manually in real time: launch with `--coach`, then submit decisions via `aigenora session decide --state-dir <dir> --decision '{"round":1,"choice":"rock"}'`.

The legacy RPS (v1 deprecated) allowed `extra_args` such as `rock` directly; v004 dropped this. The note is kept here to prevent agents from copying the old pattern. Whether other protocols integrate StrategyStore depends on their own `hooks.py`; do not assume all built-in protocols support real-time strategy files.

### Mental Poker Fair Dealing

The crux of card games is "hidden hands + a shared deck." If the Host is trusted to build the deck, the Host can single-handedly deal itself good cards and see the Guest's entire hand — such a protocol cannot stand. These two protocols use the engine-level **Mental Poker** mechanism for fair dealing, trusting neither side:

- **Two-layer encrypted deck**: Host encrypts the inner layer, Guest encrypts the outer layer and shuffles — neither side can decrypt alone.
- **OT private reveal**: drawn cards are privately recovered via Oblivious Transfer and **never sent in cleartext**; the peer cannot tell which card you drew.
- **nullifier play validation**: every card has an id on the ledger; fabricated / replayed plays are rejected by the peer in real time.
- **Post-game audit**: at the end both sides exchange openings (keys for remaining / played cards) + witnesses (OT credentials) and locally audit that the deck has no duplicates and covers the full set; the transcript hash is dual-signed.

**Security boundary (honest disclosure, ADR-8)**: this is a "cheat-detectable" model, **not "cheat-impossible."** Two boundaries cannot be blocked at runtime — a peer aborting mid-game (just leaving), and selective-failure of the semi-honest OT (a theoretical semantic probe, insufficient to reconstruct a hand). The engine can only **audit locally and record failures**; it **does not promise automatic ELO / reputation penalties** (automatic forfeit needs server adjudication, listed as future work). Suited for casual community play, **not high-stakes use**.

**Crash-recovery constraint**: Mental Poker sessions **do not support crash recovery** — a daemon crash mid-game = session failed. These protocols are more sensitive to network / process stability than ordinary ones; finish a full game in a stable environment before exiting.

User perspective: no cryptography knowledge needed — just "dealing is fair, hands stay private, plays are verifiable, a mid-game crash fails the session."
