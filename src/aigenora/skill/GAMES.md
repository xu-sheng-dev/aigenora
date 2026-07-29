# Built-in Games — Aigenora Skill Appendix

> Companion to SKILL.md. Full rules and how-to-play of each built-in game. Read when you want a game's complete rules.

## Real-time Strategy Reference

`tank-battle-v1` is the built-in reference bundle for `flow.mode: "authoritative_realtime"`. Host advances a deterministic 10 Hz public world without waiting for Guest; both local Agents micro-control every owned tank, while humans issue whole-army, scoped-unit or natural-language macro orders. Full state/command streams are retained for optional post-game review, but no semantic re-simulation blocks live play.

The reference Agent uses route-retaining weighted A*: turns, immediate reversals, recent cells, friendly congestion, and reserved cells all have deterministic costs, with side-mirrored tie breaking. A single Whisper can compile persistent orders such as `G0 move to (12,20)`, `G1 patrol between (4,8) and (9,8)`, or `G2 attack tank H0`, as well as delayed, duration-bounded and trigger-based stages. A short-lived `micro_commands` block can directly override selected units until `micro_expires_tick`; units without an override keep following the macro plan. Everything is compiled deterministically into local StrategyStore state and consumed by cheap per-tick hooks; no LLM runs inside the real-time loop.

Before Guest joins, the P2P heartbeat channel measures RTT and jitter. The local snapshot exposes `realtime.transport` with an adaptive `command_lead_ticks` and a `micro`/`macro` recommendation. Low latency remains suitable for per-tick direction input; at roughly 200 ms RTT, persistent macro orders are recommended and the larger lead prevents otherwise-valid Guest commands from arriving after their target tick.

The match can be changed immediately through options: `team_size`, `map_text`, `balance`, `friendly_fire`, `pickup_mode`, and `powerup_duration_ticks`. Changing rule code/data creates a new `ruleset_hash`; changing only options creates a new `match_config_hash`. See HOOKS.md for the engine contract and `docs/design/tank-battle-protocol.md` for the trust model.

> `authoritative_realtime` currently supports only `autonomous`/`hybrid`. A real-time tick cannot block for per-action human input; full human micro-control needs a dedicated non-blocking command queue.

## Fully Human Play

RPS, Coin Flip, Weak Wins All, Guess Number, Gomoku, Connect Four, Reversi, Hero Duel, Crazy Eights, Briscola, and Human Chat declare `human` support. Host and Guest select independently; the peer need not use the same mode:

```bash
python -m aigenora host --daemon --control-mode human --protocol-dir <dir>
python -m aigenora join --daemon --control-mode human <post_id>
```

A daemon `human` session opens its business Web UI by default. Every turn, setup choice, draw, and pass is submitted explicitly by the local person; timeout or invalid input fails the session instead of letting the Agent play. Switching control mode never changes the game's `spec.json` or `protocol_id`.

## Board Games

Three built-in 1v1 full-information board games: **Gomoku** (five-in-a-row on 15×15), **Connect Four** (gravity drop on 7×6), **Reversi/Othello** (flip on 8×8). They integrate with ELO: the protocol governance family is `game:gomoku`/`game:connect4`/`game:reversi`, and closing the session auto-reports the outcome to update ELO (v012 two-party reporting).

**Design red line (D1)**: the spec has no board type — moves use only `row`/`col` integers; board state lives in the hooks `StateStore` (exposed to the web UI via snapshot); win detection is in hooks (guess-number error+abort pattern — an illegal move is rejected and produces no session proof). 1v1 only (`session_loop`).

Use the standard protocol commands to host/join:
```bash
python -m aigenora protocol select --family gomoku
python -m aigenora host --protocol-dir <dir> --options '{"board_size":15}'
python -m aigenora join <post_id>
```

- Both sides default to `hybrid` (greedy heuristic plus human override); `strategy.json` can override (`fixed` cell / `seq` sequence). `human` disables automatic moves entirely.
- The Web UI places stones by clicking cells (gomoku/reversi) or columns (connect4 gravity); an `autonomous` page is read-only.
- Reversi supports pass (no legal move) and endgame stone count.
- An illegal move (out of bounds / occupied / no bracket) → `error` + abort, no session proof, no ELO pollution.

## Four-Player Authoritative Card Games

These `authoritative_group` examples use one trusted current Leader for a
fixed four-seat table. Each signed Member view contains that Member's hand and
only counts for other hands. They demonstrate shared-deck/private-hand
protocol construction and safe Leader migration; they are not trustless
dealing protocols.

### Four-player Landlord

Two 54-card decks are shuffled. Each player receives 25 cards and the winning
bidder takes an eight-card bottom. One Landlord plays against three Farmers.
The implementation validates singles, pairs, triples and attachments,
straights, consecutive pairs, airplanes and wings, four-card attachments,
same-rank bombs of four through eight cards, joker bombs, pass/initiative, and
team scoring. Three consecutive passes clear the trick.

```bash
python -m aigenora protocol path four-player-landlord-v1
python -m aigenora host --daemon --protocol-dir <dir> --control-mode human
python -m aigenora join --daemon --control-mode human <post_id>
```

### Aether Sigil

An original four-player tactical card sample with 72 physical cards from 18
faces. All players draw from one pile into private hands, then play public
units, spells, and relics. Turns cover Aether growth, drawing, board limits,
unit combat, shields, elimination, empty-deck fatigue, and last-channeler
victory.

```bash
python -m aigenora protocol path aether-sigil-v1
python -m aigenora host --daemon --protocol-dir <dir> --control-mode human
python -m aigenora join --daemon --control-mode human <post_id>
```

### Upgrade (Tractor)

`upgrade-tractor-v1` is a two-deck partnership trick game. Even seats oppose
odd seats. Each Member receives 25 cards and eight form a hidden kitty. The
declaration window accepts one level card, an identical level pair, or an
identical joker pair; the winner takes and buries the kitty. Leads may be a
single, identical pair, consecutive pairs (a tractor), or a same-effective-suit
throw. Followers must exhaust the effective suit and preserve available pairs
or tractors. Fives score 5; tens and kings score 10. Defender points, final
kitty multiplication, forty-point bands, and level progression are public.

```bash
python -m aigenora protocol path upgrade-tractor-v1
```

The implementation is an explicit Aigenora reference variant. It does not
claim compatibility with every regional Upgrade rule set.

### Contract Bridge

`contract-bridge-v1` deals a standard 52-card board to North, East, South, and
West. The auction implements pass, ascending 1C–7NT bids, double, redouble,
three-pass closure, and a four-pass passed-out board. It derives declarer and
dummy, exposes dummy after the opening lead, lets declarer submit dummy plays,
enforces follow suit, and scores contracts with duplicate vulnerability,
game/part-score, slam, overtrick, and undertrick rules. Director rulings,
irregularities, alerts, claims, and tournament movement are intentionally out
of scope.

```bash
python -m aigenora protocol path contract-bridge-v1
```

### Classical Mahjong Core

`classical-mahjong-v1` uses 136 tiles without flowers. Dealer starts with 14;
others start with 13. It supports draw/discard, chow, pung, open kong,
concealed kong, replacement draws, deterministic claim priority, standard
four-meld-plus-pair hands, seven pairs, and thirteen orphans. Its published
additive classical-core patterns are deliberately compact and must not be
described as the 81-pattern Mahjong Competition Rules profile.

```bash
python -m aigenora protocol path classical-mahjong-v1
```

### Four-seat No-limit Texas Hold'em

`texas-holdem-v1` rotates a button and blinds, deals two private hole cards,
and runs preflop, flop, turn, and river betting. It supports fold, check, call,
bet, raise, and all-in; enforces full minimum raises and the non-reopening
effect of a short all-in; builds a main pot plus arbitrary side pots; evaluates
the best five of seven cards; splits ties; and awards odd chips clockwise after
the button.

```bash
python -m aigenora protocol path texas-holdem-v1
```

All six games use `restart_round` recovery. A new Leader retains only safe public
progress and starts a fresh deal; checkpoints never contain another Member's
hand or draw order. The current Leader still owns the complete live deal and
can inspect or bias it. Signed frames support auditability, not trustless
shuffling. Use `mental_poker` when the requirement is a two-party
cheat-detectable deal instead.

## Card Games & Mental Poker Fair Dealing

Two built-in 1v1 card games: **Crazy Eights** (crazy-eights, shedding) and **Briscola** (briscola, trick-taking), covering the two main card-game families. They integrate with ELO: the protocol governance family is `game:crazy-eights` / `game:briscola`.

### Mental Poker Fair Dealing

These two protocols use the engine-level **Mental Poker** mechanism so neither side can stack the deck or see the other's hand — dealing is fair, hands stay private, every play is verifiable, and a mid-game daemon crash fails the session (no crash recovery). The full cryptographic mechanism (two-layer encrypted deck, OT private reveal, nullifier ledger, post-game audit, ADR-8 honest-disclosure boundary) is in `HOOKS.md` — most players need no cryptography knowledge.


### Crazy Eights (shedding)

The poker ancestor of UNO. Match the suit or rank, or play an 8 (wild) and name a new suit; first to empty their hand wins. Simplified (ADR-10): no starting discard (first play is unconstrained), drawing ends the turn.

```bash
python -m aigenora protocol select --family crazy-eights
python -m aigenora host --protocol-dir <dir> --options '{"hand_size":5}'
python -m aigenora join <post_id>
```

profiles: `quick`(3 cards) / `standard`(5) / `long`(7).

The `human` card table displays only your hand and current legal actions: play, draw, or pass. Playing an 8 also requires choosing the new suit. Peer hidden cards never enter the snapshot.

### Briscola (trick-taking)

Italian trick-taking game. Each trick both sides play one card; trump (briscola) trumps or same-suit compares by rank; the trick winner takes the cards and accumulates points (A=11 / 3=10 / K=4 / Q=3 / J=2; 120 points total per deck), first to 61 wins. Simplified (ADR-9): trump suit is deterministically derived (same every game, no indicator card); fixed leader / follower (Host always leads, Guest always follows).

```bash
python -m aigenora protocol select --family briscola
python -m aigenora host --protocol-dir <dir>
python -m aigenora join <post_id>
```

profile: `standard` (40-card deck, 3-card hands, 120-point game).

In `human`, the player clicks a legal card from their own hand. Refill/draw phases also use an explicit local action and never call the automatic card picker.

### events.jsonl Event Stream (Mental Poker specific)

Beyond the generic events (`invite_created` / `peer_joined` / `protocol_message` / `session_ended`), Mental Poker protocols emit the following (to observe dealing, plays, and the final audit in daemon mode):

| event type | info | typical use |
|---|---|---|
| `mp_setup_started` / `mp_setup_completed` | role, deck size | deal phase start / done |
| `deal_requested` | owner (host/guest) | deal progress |
| `mp_ot_started` / `mp_ot_completed` | ot_id, direction, label | OT reveal progress (no card face leaked) |
| `draw_started` / `draw_completed` | who, id_b | draw (Crazy Eights) |
| `play_verified` / `play_rejected` | who, id_b, reject reason | play validation passed / rejected |
| `pass_exchanged` | who | pass |
| `mp_opening_sent` / `mp_opening_received` | entry count | final opening exchange |
| `mp_witness_sent` / `mp_witness_received` | witness count | final witness exchange |
| `audit_started` / `audit_passed` / `audit_failed` / `audit_refused` | result, reason | whether the game completed fairly |
| `mp_terminal_receipt_signed` / `mp_terminal_receipt_verified` | transcript hash | final receipt dual-sign |
| `game_over` | winner, audit_status | game end |

**Key (audit gate)**: only when `audit_passed` and the terminal receipt dual-sign verifies do both sides call `/result` to report the outcome and trigger ELO. `audit_failed` / `audit_refused` / illegal-play aborts do not call `/result`; the session is marked failed.
