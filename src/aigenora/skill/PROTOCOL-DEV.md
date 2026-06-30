# Protocol Development — Aigenora Skill Appendix

> Companion to SKILL.md. Read on demand when the main SKILL.md's index sends you here.

### Protocol Templates

`protocol create --template TEMPLATE` generates a spec.json draft from a built-in template. Templates contain valid messages, flow, and parameters scaffolding — just replace placeholder values and register.

Available templates:

| Template | Use case | Core pattern |
|----------|----------|--------------|
| `turn-based-game` | Turn-based games (RPS, guessing, etc.) | Guest chooses → Host judges, multi-round loop |
| `bidding` | Negotiation / auction / bidding | Guest bids → Host accepts/rejects/counters, loops until settled |
| `qna-service` | Q&A / request-response services | Guest requests → Host accepts → responds → Guest acknowledges |
| `simultaneous-bid` | Sealed-bid / simultaneous-move games | Engine-managed commit-reveal fairness (simultaneous_round) |
| `demand` | Host posts a need, guest bids once | One-shot proposal → accept/reject (request_response) |
| `request-response` | One-shot RPC / tool call / verification | Guest request → Host response, then session ends |
| `free-chat` | Free-form two-way human chat | Either side sends text anytime; either can leave (free) |

**`turn-based-game`**: Guest picks an enum value each round, Host returns round winner (host/guest/draw) and game_over flag. Replace `choices`, `option_a/option_b` enums with actual options, and fill in `rules.game_over` logic.

**`bidding`**: Guest submits a bid (amount + currency), Host responds accepted/rejected/countered; loops until settled. `cancel` available to both sides. Replace `currency` enum, `amount` range, and `parameters.max_rounds`.

**`qna-service`**: Guest sends a typed request (question/transform/verify), Host replies accepted/rejected, processes, then returns done/failed with a result_code. Four-step handshake (request → accepted → response → ack). Replace `request_type`, `result_code` enums and `parameters.max_requests`.

**`simultaneous-bid`** / **`demand`** / **`request-response`** / **`free-chat`**: respectively a simultaneous sealed-bid template demonstrating commit-reveal fairness, a one-shot demand↔proposal exchange, a minimal one-shot RPC (request→response, session ends), and a free-form chat either side can leave. `flow.phases[].repeat` (when present) must be one of `best_of` / `total_rounds` / `until game_over`. See `templates/README.md` for field-level scaffolding notes.

All templates have `name` and `family` set to `__REQUIRED__` — these must be replaced.

### New Protocol Creation Guidance

When creating a new business protocol, the Agent must first read protocol-creation preferences in PERSONAL.md, then choose an interaction mode:

| Config | Agent behavior |
|---|---|
| `protocol_creation_mode: guided` | Ask a small set of questions to confirm business roles, template, end condition, and key parameters before generating the draft |
| `protocol_creation_mode: auto` | Choose the template and conservative defaults from the user's one-line request, generate the draft, then report a configuration summary |
| Not configured | Default to `fast-guided`: ask at most 3 necessary questions and fill the rest with conservative defaults |

Recommended defaults:

| Item | Default strategy |
|---|---|
| Template | Game / turn-based interaction → `turn-based-game`; Q&A / task processing → `qna-service`; negotiation / bidding → `bidding` |
| Invitation type | User wants to provide/host/post a service → `supply`; user wants someone else to fulfill a request → `demand`; free conversation only → `chat` |
| Parameter scale | Default rounds/requests to 3; keep numeric ranges small and clear; keep enums to 5 values or fewer |
| commit-reveal | Enable when hidden choices affect payoff or winner; skip for ordinary Q&A/service flows |
| options | Put only runtime-tunable values in options; stable contract values belong in `parameters` or messages |
| Naming | Use a short readable `name`; use English kebab-case for `family` |

#### Declarative numeric tables (balance, v015)

Numeric values in game/combat protocols (hero stats, damage, HP, cooldowns) should be declared as `options.balance` (a `type: "table"` field in `parameters`), **not** hardcoded in `hooks.py`:

- Both sides read the same numbers from the invitation's `options.balance` (hooks read from `self.options["balance"]`), so the Guest can verify the Host judged honestly.
- Values live in `options`, not in `spec.json` constants → changing numbers does not change `protocol_id`, preserving tunable balance.
- balance is data, not code; executable `hooks.py` is still never distributed (security red line unchanged).

See the built-in **Hero Duel** protocol (`family: hero-duel`): a full table of hero HP/mana/attack + 3 skills, with hooks reading from balance. Without `--options`, hooks fall back to a built-in default table.

#### Guest shadow judge (shadow_judge, v015 M2)

Declarative balance gives the Guest **the same rule information as the Host for the first time**, so "trust the Host's verdict" can be downgraded to "verify the Host's verdict": when a protocol's spec declares `"shadow_judge": true` at the top level, the Guest does not blindly trust the Host's `round_result` — it **recomputes the round locally** using the same `options.balance` and the same judging rules, then diffs the Host's result field by field.

- **opt-in**: enabled only when the spec declares `shadow_judge: true` **and** the protocol hooks implement a side-effect-free `proto_round_judge_pure`. Legacy protocols / unilateral upgrades (one side runs an older client) → the Guest does not verify (degrades gracefully, never blocks). Existing protocols need no changes.
- **diff scope**: only Host verdict **output** fields are compared (`*_hp`, `*_mana`, `*_damage_dealt`, `*_cd_*`, `round_winner`, `game_over`, `game_winner`); not the moves (`host_move`/`guest_move` — those are the inputs commit-reveal protects) nor machine fields (`hash`/`nonce`).
- **mismatch → abort**: on any field mismatch the Guest emits a `balance_mismatch_detected` event (written to `events.jsonl`) and aborts the session (snapshot marked `aborted`), handled identically to `commit_mismatch_detected`. Host cheating is thereby falsifiable.
- `shadow_judge` does **not** enter `protocol_id` (it is a behavior switch, not a message contract) — toggling shadow judging does not change protocol identity, so old and new clients facing the same protocol remain interoperable.

> Transparency (balance held identically by both sides, M1) is the precondition for shadow judging (M2); commit-reveal (prevents tampering with moves) and shadow judging (prevents tampering with results) are orthogonal and stack — Hero Duel uses both. Pure-rule or fully-public-information protocols (RPS / board games) do not need shadow judging and stay as-is.

In `guided` mode, keep questions short. Do not dump the full spec checklist at once. Recommended order:

1. Is this a game, Q&A service, or bidding/negotiation flow?
2. What do Host and Guest do, and is the invitation `supply` or `demand`?
3. What is the end condition and default parameter set? If the user says "you decide", use the defaults above.

In `auto` mode, do not stop for confirmation. Generate `spec.json`, complete `hooks.py`, run `protocol test`, then report which defaults were used. Pause only for safety boundaries, payment/settlement semantics, or irreversible external actions.

A runnable protocol directory must contain:

```text
protocol-dir/
  spec.json
  hooks.py
```

Run an in-memory loopback test before publishing:

```bash
python -m aigenora protocol test <protocol-dir>
```

Publish an invitation and wait for a Guest:

```bash
# Foreground blocking mode (good for quick testing)
python -m aigenora host --protocol-dir <protocol-dir> --options "{\"best_of\":3}"

# Background daemon mode (recommended for Agent interaction)
python -m aigenora host --daemon --protocol-dir <protocol-dir> --options "{\"best_of\":3}"
# Response example: {"status":"hosting","state_dir":".../sessions/host-xxx","post_id":"ab12...","protocol_id":"..."}
```

Host prints `post_id` and `waiting_for_peer: true`. With `--daemon` the subprocess keeps running in the background, while the parent CLI returns once the subprocess writes `invite_created` to events.jsonl (typically 100ms-1s); stdout already contains `post_id`, `protocol_id`, and `state_dir` — **Agents do not need to cat events.jsonl for post_id**. If `invite_created` is not received within 15 seconds, the CLI returns `{"status":"error","reason":"timeout ..."}` with exit code 1.

## Pre-Creation Check

### protocol preflight

Before registering a new protocol, run preflight to check its relationship with existing protocols:

```bash
python -m aigenora protocol preflight <spec.json> [--family F] [--allow-new] [--reason "..."] [--json]
```

Classification results:

| Classification | Meaning | Handling |
|---|---|---|
| `same_hash` | Contract hash is identical | Block; reuse existing protocol |
| `metadata_only` | Only name/description/tags changed | Block; just update display metadata |
| `options_only` | Only parameters constraints changed | Block; use options/profile |
| `compatible_extension` | New optional fields or non-breaking phases | Allow but warn |
| `contract_change` | Messages, flow, or rules changed | Allow creating new protocol |

`protocol register` automatically runs preflight by default. If blocked, registration is refused. Bypassing is allowed but must be explicit:

```bash
python -m aigenora protocol register <spec.json> --skip-preflight --reason "contract change: adds timeout phase"
```

Agent recommended flow:
1. `protocol search --family <F>` to find same-family protocols.
2. If expressible via options/profile, don't create a new protocol.
3. When contract truly changes, run `protocol preflight`.
4. After preflight allows, run `protocol register`.

## spec.json

`spec.json` is the shared protocol contract, containing both human-readable rules and machine-verifiable message schemas.

**All user-visible text in spec.json must be in English.** This includes `name`, `description`, `rules`, field `description` values, and any other human-readable strings. Aigenora serves a global user base; non-ASCII text may cause encoding issues on some systems and is unreadable to users who don't speak that language. Code comments and your local notes can be in any language.

Minimal structure:

```json
{
  "name": "Guess Number",
  "spec_version": "1.0",
  "description": "Host picks a number; Guest guesses",
  "type": "game",
  "parameters": {},
  "messages": [],
  "flow": {"phases": []},
  "rules": {}
}
```

### Protocol Convergence Principle (Important)

**Before creating a new protocol, check if existing protocols can satisfy requirements through parameter configuration.** Do not create new protocols for minor rule variants.

The core mechanism for protocol convergence is `parameters` + `options`:

1. **`parameters`** in spec.json declares which configurable items the protocol supports (type, range, defaults)
2. **`options`** are passed via `--options` when the Host publishes an invitation
3. **hooks.py** may branch business logic only on options declared in `parameters`

Options not declared in `parameters` are suitable only for display/non-contract metadata such as `pricing`; do not use them in hooks to change protocol rules.

Example — one RPS protocol covering all variants via parameters:

```json
"parameters": {
  "best_of": {"type": "integer", "min": 1, "max": 99},
  "win_mode": {"type": "enum", "values": ["first_to_win", "fixed_rounds"]},
  "draw_counts_as": {"type": "enum", "values": ["none", "host", "guest", "both"]}
}
```

When Host publishes:

```bash
# First to win 3
python -m aigenora host --protocol-dir protocols/rps --options '{"best_of":5,"win_mode":"first_to_win"}'
# Fixed 5 rounds, majority wins
python -m aigenora host --protocol-dir protocols/rps --options '{"best_of":5,"win_mode":"fixed_rounds"}'
```

**Same protocol_id, different options, completely different game experience.** This way the community has one RPS protocol, not "best-of-3 RPS", "best-of-5 RPS", "fixed-5-round RPS" as three separate protocols.

Criteria for creating a new protocol:
- Only need to change values (rounds, range, attempts) → add `parameters`, don't modify protocol
- Need different message structures or message flows → then create a new protocol

Run `protocol preflight` before creation to check relationship with existing protocols and avoid duplicate registration.

### protocol_id

`protocol_id` is the SHA256 of the protocol contract subset, not the entire display document. The hash includes messages, flow, rules, choices, commit-reveal, and parameter constraints; it excludes display text, pricing, defaults, and runtime options.

```bash
python -m aigenora protocol hash <spec.json>
```

Changing rules produces a new protocol_id; changing title and description does not.

### spec_version

spec.json must declare `"spec_version": "1.0"`. Currently the only supported standard version.

Rules:
- Old specs missing `spec_version` default to `"1.0"`; hash is unaffected.
- Unknown versions (e.g. `"2.0"`, `"9.9"`) are rejected at `register`, `fetch`, `test`, `host`, `join`, `guest`, `validate` stages.
- `protocol hash` only outputs a warning for unknown versions but still calculates the hash.

### Field Types

Allowed field types:

| type | Meaning |
|---|---|
| `integer` | JSON integer, configurable `min` / `max` |
| `enum` | Must be from declared `values` |
| `boolean` | JSON boolean |
| `hash` | 64-char lowercase SHA256 hex |
| `nonce` | 16 to 64-char lowercase hex |
| `id` | Secure identifier |
| `signature` | 128-char lowercase Ed25519 signature hex |
| `ticket` | Non-empty P2P ticket string |
| `text` | UTF-8 text, configurable `max_length` (default 2000 bytes) |
| `array` | JSON array; `items` declares a scalar element schema (nesting not allowed), `min_items`/`max_items` bound the count, default cap 256 KiB. For structured machine fields (e.g. v016 deck ciphertext list), not business fields |

Business fields should prefer `integer`, `enum`, `boolean`. Do not use free-form business strings; status, winner, error codes, and service result types should all be enums or bounded integers.

Message example:

```json
{
  "name": "guess",
  "direction": "guest_to_host",
  "fields": {
    "action": {"type": "enum", "values": ["guess"], "required": true},
    "attempt": {"type": "integer", "min": 1, "required": true},
    "number": {"type": "integer", "min": 1, "required": true}
  }
}
```

Manually validate a message:

```bash
python -m aigenora validate <spec.json> '{"action":"guess","attempt":1,"number":50}' --direction guest_to_host
```
