# Hooks Engine Contract — Aigenora Skill Appendix

> Companion to SKILL.md. Read on demand when the main SKILL.md's index sends you here.

## Hooks Engine Contract (Must Read)

`spec.flow.mode` determines which engine is used. Hook interfaces come in two layers: **generic lifecycle hooks** (used by the ordinary handshake engines) + **engine-specific hooks** (used when an engine owns more of the lifecycle).

### Generic Lifecycle Hooks (foundation for all engines)

`session_loop` and `request_response` carry Host/Guest business exchange through this full set. Specialized engines may use only `proto_init` / `proto_host_metadata` and their mode-specific hooks (full signatures in the hooks.py section below):

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
| `authoritative_group` | one Leader orders actions from 2–32 Members | `proto_group_*` (8 hooks) | Community / Meeting / Landlord / Aether / Upgrade / Bridge / Mahjong / Hold'em |
| `authoritative_realtime` | Host owns fixed tick; Guest queues future commands | `proto_realtime_*` (7 hooks) | Tank Battle |

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

The state directory may become visible to the Web before the P2P handshake finishes. Complete records already appended to `inbox.jsonl` are drained in order before the receiver starts, so a peer's immediate `/quit` cannot discard them. A final record without a newline is treated as still being written and its offset is retried on the next poll.

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

### Local Control-Mode Capability (v020)

Control mode is not a `spec.json` field and never enters `protocol_id`. One hooks implementation may declare its locally supported modes:

```python
CONTROL_MODES = ("autonomous", "hybrid", "human")
```

Without a declaration, only `autonomous` and `hybrid` are supported. `BaseProtocolHooks.proto_init(..., decision_config=...)` stores the normalized `control_mode` in local state and the snapshot. Business hooks must obtain actions under this contract:

- `autonomous`: use strategy/algorithm only; never wait on DecisionBus.
- `hybrid`: accept an explicit in-window override first, then use the existing automatic strategy/fallback when none arrives.
- `human`: await a strict explicit action for every local action; timeout, absence, or invalid input must raise and abort, never invoke a random/strategy picker.

Human-capable hooks must publish current `legal_actions`, validate the choice, then translate it into the unchanged Protocol message. Setup secrets, draw, pass, and forced pass are actions too. Card games use the mental-poker local action adapter, and snapshots expose only the local hand—never peer hidden cards. Continuously ticking modes such as `authoritative_realtime` must not block on human input; do not declare `human` until a non-blocking micro-input queue exists.

### authoritative_group (Host-authoritative multiplayer)

Use this mode for a star room in which the current Leader has one P2P channel
per Member and is the only process that applies business actions. The network
Leader is not a business role: facilitator, Landlord, active player, and room
owner remain protocol state.

| Hook | Contract |
|---|---|
| `proto_group_initial_state(members)` | Create the complete authority state |
| `proto_group_member_joined(state, member)` | Apply a signed admission |
| `proto_group_member_left(state, member, reason)` | Apply an explicit departure |
| `proto_group_handle(state, actor, action)` | Validate and apply one ordered action |
| `proto_group_view(state, viewer)` | Return only the state visible to that Member |
| `proto_group_recovery_snapshot(state)` | Return only state safe to replicate to failover candidates |
| `proto_group_restore(checkpoint, members, new_epoch)` | Restore state on the new Leader |
| `proto_group_on_leader_changed(state, old_leader, new_leader)` | Apply protocol-specific migration behavior |

`proto_group_handle` and the membership hooks return a dictionary with
`state`, and may also return `events`, `direct`, `completed`, and `outcome`.
Public `events` are delivered to every Member. `direct` is routed only to the
named Member, but protocol state exposed through `proto_group_view` is still
the durable privacy boundary. Never put another Member's hand, secret, or
private action in public events or a recovery snapshot.

The engine bounds JSON, deduplicates each actor's `client_seq`, assigns global
`seq`, signs the public frame core and private view hash, and certifies each
reconstructed checkpoint. Ordinary records use deterministic recovery/view
deltas when smaller; periodic and boundary records carry complete payloads.
Members apply a strictly contiguous chain for the current `leader_epoch`,
reconstruct and persist the complete successor, then ACK. The server advertises
only a checkpoint acknowledged by at least one non-Leader Member, so failover
never relies on a Host-only snapshot or rolls back to the last full-body frame.

Choose `recovery_mode: "exact"` only when the checkpoint is safe for every
candidate. Use `restart_round` for hidden-hand games: keep safe public scores
and membership, discard the secret deal, and generate a fresh round after
Leader migration. Use `abort` when neither is safe.

### authoritative_realtime (Host-authoritative RTS)

Use this mode for deterministic public-world games that must keep moving even when the Guest is late. Host advances a protocol-bound fixed tick and sends every authoritative frame; Guest submits commands for future ticks and never blocks Host progress. Live Guest work is limited to cheap frame/hash-chain checks. Full frame and command streams are retained for optional post-game audit.

Required `flow` shape:

```json
{
  "mode": "authoritative_realtime",
  "realtime": {
    "tick_rate_hz": 10,
    "input_delay_ticks": 3,
    "snapshot_every_ticks": 1,
    "max_command_lead_ticks": 30,
    "max_commands_per_frame": 64,
    "disconnect_policy": "abort"
  }
}
```

| Method | Responsibility |
|---|---|
| `proto_realtime_initial_state()` | Host returns deterministic tick-0 public state |
| `proto_realtime_commands(state, target_tick)` | Local Agent compiles persistent macro intent and/or short-lived micro overrides into owned-unit commands |
| `proto_realtime_transport_update(profile)` | Optional local callback with measured RTT, jitter, adaptive lead and recommended `micro`/`macro` control; never shared rules |
| `proto_realtime_validate_commands(side, commands, state, target_tick)` | Enforce ownership, shape and deterministic normalization |
| `proto_realtime_step(state, tick, commands)` | Host returns `{"state": ..., "events": [...], "outcome": "none|host|guest|draw"}` |
| `proto_realtime_snapshot(state, frame)` | Return the Web/observer snapshot patch |
| `proto_realtime_audit_outcome(frame)` | Optional terminal-condition audit after the final frame; never changes live outcome |

State and commands must be finite canonical JSON. Prefer integer/fixed-point simulation values. Do not read wall-clock time, random global state or network data from `proto_realtime_step`; all rule-affecting inputs belong in the protocol bundle or match options so `ruleset_hash` / `match_config_hash` bind them before tick 0.

The P2P heartbeat channel performs a bounded RTT probe before Guest joins and publishes the local
profile under `snapshot.realtime.transport`. Command lead is derived from RTT plus jitter and
clamped by `max_command_lead_ticks`; this keeps future commands valid under ordinary delay without
changing the deterministic match contract. Low-latency links may use per-tick micro input, while
higher-latency links should prefer persistent macro orders. A hook can support both at once: a
short-lived `micro_commands` override controls named units for a declared expiry tick and the
remaining units continue executing macro orders.

Do not invoke an LLM per tick. Persist human/LLM intent as macro strategy and have
`proto_realtime_commands` convert it to bounded local micro actions. The Tank Battle reference
bundle is the executable example, including `move_to`, multi-waypoint `patrol`, targeted
`attack_unit`, and opportunistic fire while moving.

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

For domain-specific language, a protocol may override the synchronous pure function `proto_parse_whisper_intent(text, context=None)`. It returns the same intent shape as the generic Whisper parser. A persistent intent may additionally return a finite-JSON `strategy_patch` whose serialized size is at most 128 KiB; `materialize_intent` merges it atomically into StrategyStore. The Web endpoint accepts at most 2,000 characters. The parser must not access the network, invoke an LLM, mutate state, or read nondeterministic external inputs. Return `None` when unrecognized so the generic parser can take over.

Use this when the human wants to override a `random`/`auto` pick at a critical moment (e.g. "play paper" in RPS) without stopping the match.

That is a `hybrid` override, not strict `human`. Strict human play uses `--control-mode human` plus explicit DecisionBus actions; the Web UI does not offer strategy/whisper delegation in that mode.


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
- For a temporary human intervention, keep local `hybrid` (the default) and submit an override with `aigenora session decide --state-dir <dir> --decision '{"round":1,"choice":"rock"}'`. Use `--control-mode human` when every action must come from the person.

The legacy RPS (v1 deprecated) allowed `extra_args` such as `rock` directly; v004 dropped this. The note is kept here to prevent agents from copying the old pattern. Whether other protocols integrate StrategyStore depends on their own `hooks.py`; do not assume all built-in protocols support real-time strategy files.

### Dynamic Policy Interfaces (v019)

v019 lets protocols support "dynamic command at will" (mirror previous, counter previous, probability distributions, conditional branches). **The engine does not hardcode any game rules** — strategy logic is implemented by the protocol; the engine only provides the framework (sandbox + orchestration + channels).

To support dynamic strategy, protocol authors override three interfaces (all have default empty implementations in the `ProtocolHooks` base class):

#### 1. DECISION_SCHEMA (declare decision fields and rules)

```python
class Hooks(ProtocolHooks):
    DECISION_SCHEMA = {
        "match_key": "round",          # match key: round/turn/attempt/action_seq
        "value_field": "choice",       # decision value field: choice/bid/number/action
        "choices": {"rock": ["rock", "stone"], ...},  # enum options (optional)
        "numeric": False,              # set True for numeric protocols
        "policy_family": "rps",        # policy family (optional, protocol-defined)
        "beats": {"rock": "scissors", "paper": "rock", "scissors": "paper"},  # counter relations (optional, protocol interprets, engine never reads)
    }
```

`beats` / `policy_family` are protocol-layer declarations. **The engine never reads `beats`.** The protocol interprets them in its own `run_policy()`. Protocols without `policy_family` get `unsupported_policy` from the engine for `mode=policy`.

#### 2. build_decision_context (provide current situation)

```python
def build_decision_context(self, match_key: str, match_value: Any) -> dict:
    """Return current situation for policy runner / script. Default: unsupported."""
    if not self._last_round_context:
        return {"supported": False, "reason": "no_context"}
    return {
        "supported": True,
        "match_key": match_key,
        "match_value": match_value,
        "value_field": "choice",
        "legal_values": ["rock", "paper", "scissors"],
        "previous": {
            "self": {"choice": "paper"},
            "opponent": {"choice": "scissors"},
            "result": "loss",
        },
        "history": [...],
    }
```

**IO contract**: prefer reading from in-memory cache (hooks caches previous round result to `self._last_round_context` in `_record_round`); when falling back to `details.jsonl`, use `self._read_last_details(n)` to seek only the last N lines, no full scan.

#### 3. run_policy (protocol built-in strategy logic)

```python
def run_policy(self, strategy: dict, context: dict) -> dict:
    """Protocol built-in policy (synchronous fast path). Default: unsupported_policy."""
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
    return {"ok": False, "reason": "unknown_policy"}
```

`run_policy` runs in-process, synchronous, no subprocess, millisecond-level. Only supports the protocol's preset strategies.

#### mode=script (script producer, no hooks changes needed)

If the protocol doesn't want to implement `run_policy`, or the user wants a strategy the protocol doesn't preset, use script producer: the user/agent writes a `.py` script in `<state_dir>/policy_scripts/`, activates with `mode=script`. The engine sandbox runs the script, passing context via JSON stdin, collecting decision via JSON stdout. **The protocol needs no code changes** — as long as `build_decision_context()` returns `supported=True`, scripts can read the situation.

Script contract: see `aigenora/policy_scripts/README.md`. 4 built-in example scripts ship with the package.

#### Mental poker boundary

Mental poker (Crazy Eights / Briscola) **does not declare `policy_family`, does not support mirror/counter policy** — card games have no counter relations, and context must not leak the opponent's hand. Mental poker only supports explicit action decisions (user explicitly says "play this card").

### Mental Poker Fair Dealing

The crux of card games is "hidden hands + a shared deck." If the Host is trusted to build the deck, the Host can single-handedly deal itself good cards and see the Guest's entire hand — such a protocol cannot stand. These two protocols use the engine-level **Mental Poker** mechanism for fair dealing, trusting neither side:

- **Two-layer encrypted deck**: Host encrypts the inner layer, Guest encrypts the outer layer and shuffles — neither side can decrypt alone.
- **OT private reveal**: drawn cards are privately recovered via Oblivious Transfer and **never sent in cleartext**; the peer cannot tell which card you drew.
- **nullifier play validation**: every card has an id on the ledger; fabricated / replayed plays are rejected by the peer in real time.
- **Post-game audit**: at the end both sides exchange openings (keys for remaining / played cards) + witnesses (OT credentials) and locally audit that the deck has no duplicates and covers the full set; the transcript hash is dual-signed.

**Security boundary (honest disclosure, ADR-8)**: this is a "cheat-detectable" model, **not "cheat-impossible."** Two boundaries cannot be blocked at runtime — a peer aborting mid-game (just leaving), and selective-failure of the semi-honest OT (a theoretical semantic probe, insufficient to reconstruct a hand). The engine can only **audit locally and record failures**; it **does not promise automatic ELO / reputation penalties** (automatic forfeit needs server adjudication, listed as future work). Suited for casual community play, **not high-stakes use**.

**Crash-recovery constraint**: Mental Poker sessions **do not support crash recovery** — a daemon crash mid-game = session failed. These protocols are more sensitive to network / process stability than ordinary ones; finish a full game in a stable environment before exiting.

User perspective: no cryptography knowledge needed — just "dealing is fair, hands stay private, plays are verifiable, a mid-game crash fails the session."
