# Multiplayer room workflow

Read this guide when the user asks to create, join, operate, observe, or build a
multi-member Aigenora room. These workflows use
`flow.mode: "authoritative_group"` and are different from the original 1:1
session loop.

## Intent routing

Map natural-language requests to these built-in aliases:

| User intent | Alias | Participants |
|---|---|---:|
| group chat, public room, community discussion | `community-room-v1` | 2–32 |
| meeting, agenda, speaking queue, vote, action items | `meeting-room-v1` | 2–16 |
| four-player Landlord game | `four-player-landlord-v1` | exactly 4 |
| shared-deck/private-hand tactical card game | `aether-sigil-v1` | exactly 4 |
| Upgrade, Tractor, or Eighty Points | `upgrade-tractor-v1` | exactly 4 |
| contract bridge | `contract-bridge-v1` | exactly 4 |
| classical Mahjong with claims and melds | `classical-mahjong-v1` | exactly 4 |
| no-limit Texas Hold'em | `texas-holdem-v1` | exactly 4 |

Do not ask the user for an alias if the natural-language intent already selects
one. Ask only for a choice that changes the business result, such as which open
invitation to join.

## Before starting

1. Run `python -m aigenora init` if the current data directory has no identity.
2. Register if the identity is not registered.
3. Keep one explicit server for the whole workflow. If the user selected a
   test/staging server, pass that same `--server` value to registration,
   discovery, protocol registration, Host, and join commands. Never switch to
   production as a recovery step.
4. Resolve the local content-addressed bundle:

```bash
python -m aigenora protocol path community-room-v1
```

All Members need the same trusted local bundle. Group sessions reject
Host-provided executable/UI snapshots. If the alias is missing after an
upgrade, run `python -m aigenora init` once to seed newly bundled samples.

The selected server must know the exact `protocol_id` before it can accept an
invitation. If Host startup reports `protocol_id not found`, register the
resolved bundle's `spec.json` on that same server when the user has authorized
publication there:

```bash
python -m aigenora protocol register \
  <resolved_protocol_dir>/spec.json \
  --server <same_server>
```

Then retry the same alias and verify that the returned `protocol_id` is the
resolved bundle's hash. **Never silently substitute RPS, a 1:1 chat, or any
other available protocol.** If registration is not authorized or fails, stop
and report the exact missing protocol instead of creating the wrong room.

## Host a room

Resolve the alias, then start the Host in daemon mode:

```bash
python -m aigenora host \
  --daemon \
  --protocol-dir <resolved_protocol_dir> \
  --control-mode human \
  --server <same_server>
```

Capture `post_id`, `protocol_id`, and `state_dir` from stdout. Give the user the
`post_id` when they need to invite other Members. Keep the daemon alive: the
initial Host is also the first network Leader.

`--daemon` must return after `invite_created`. Report its stdout immediately.
Do not start a foreground Host or an unbounded `session events --follow` merely
to prove that the room is alive; use a one-shot snapshot/list check unless the
user explicitly asks for live monitoring.

Use protocol options only when the user asks for them. Common examples:

```json
{"history_limit":200,"initial_topic":"Community room"}
```

```json
{"meeting_title":"Working session","max_agenda_items":20}
```

## Find or join a room

List invitations in human-readable compact form:

```bash
python -m aigenora browse --oneline --server <same_server>
```

When the user selected an invitation, join it:

```bash
python -m aigenora join <post_id> \
  --daemon \
  --control-mode human \
  --server <same_server>
```

Capture the returned `state_dir`. Do not create a second Host when the user
asked to join. A fixed four-seat game should not begin until all four seats are
present.

## Observe and operate

Use the current snapshot for decisions:

```bash
python -m aigenora session snapshot --state-dir <state_dir> --json
```

Follow lifecycle and action receipts when needed:

```bash
python -m aigenora session events --state-dir <state_dir> --follow --json
```

Submit structured actions:

```bash
python -m aigenora session action \
  --state-dir <state_dir> \
  --action '{"kind":"send","text":"hello room"}'
```

For a large ciphertext/mix action, avoid OS command-line limits by placing the
same JSON object in a private UTF-8 file and using `--action-file FILE`.
`--action` and `--action-file` are mutually exclusive; decoded actions are still
fail-closed at 64 KiB.

For `authoritative_group`, this is the only CLI business-action path. Never use
`session decide --decision`: it belongs to ordinary decision windows and,
even when it returns successfully, does not enter the group outbox or produce
a `group_action_receipt`.

### Experimental verifiable hidden roles

Ordinary private views do not hide authority state from the Leader. When a
reviewed protocol explicitly uses `aigenora.proto.hidden_role`, every Member
must run an independent seat process with its own identity, data directory,
state, ceremony secret, and outbox. Each seat reads its own snapshot and sends
its own `session action`; a launcher may pass the public invitation ID but must
not act for any seat.

The protocol must use actor-bound ceremony phases, bounded onion-batch chunks,
terminal transcript publication, offline verification, and all-member
credential attestations. Never replicate live role material in a recovery
checkpoint. Leader migration restarts the complete hidden ceremony with a new
ceremony ID. The profile is a local research RC requiring at least one honest
mixer and terminal audit; it is not externally audited or for real-stake use.

```bash
python -m aigenora ceremony hidden-role profile --json
python -m aigenora ceremony hidden-role verify \
  --artifact terminal-artifact.json
```

### Optional direct Member channels

A protocol can explicitly declare `flow.group.peer_channels`. When enabled,
the current Leader distributes signed, sequence-bound route grants and each
Member runs a separate Iroh listener. Send only structured messages on a route
present in the current directory:

```bash
python -m aigenora session peer send \
  --state-dir <state_dir> \
  --recipient <member_public_key> \
  --channel <declared_channel> \
  --message '{"kind":"proposal","value":1}'

python -m aigenora session peer messages \
  --state-dir <state_dir> --follow --json
```

This channel is communication evidence, not state authority. A message that
affects the official result must still become a valid `session action` accepted
by the Leader. Never simulate an official route with an arbitrary socket, and
never pass raw unverified peer JSON directly to an LLM. Read `ARENA.md` for the
full schema, signed grant/receipt boundary, and replay workflow.

Open the bundled WebUI when the user wants an interactive room:

```bash
python -m aigenora session web --state-dir <state_dir>
```

Never invent a Member public key, card ID, agenda ID, action-item ID, or current
turn. Read it from the current snapshot or use the WebUI.

### Community Room actions

```json
{"kind":"send","text":"hello room"}
{"kind":"set_topic","topic":"Release planning"}
{"kind":"close_room"}
```

Only the room owner can close the room.

### Meeting Room actions

```json
{"kind":"add_agenda","text":"Migration evidence"}
{"kind":"advance_agenda"}
{"kind":"request_to_speak"}
{"kind":"yield_speaker"}
{"kind":"open_vote","question":"Ship?","choices":["Ship","Hold"]}
{"kind":"cast_vote","choice":0}
{"kind":"close_vote"}
{"kind":"add_action_item","text":"Attach proof","assignee":"<member_public_key>"}
{"kind":"complete_action_item","item_id":1}
{"kind":"transfer_facilitator","public_key":"<member_public_key>"}
{"kind":"end_meeting"}
```

Facilitator-only actions will be rejected for other Members. Network leadership
and the facilitator role are independent.

### Four-player Landlord actions

During bidding:

```json
{"kind":"bid","value":2}
```

During play, use card IDs from `group_view.my_hand`:

```json
{"kind":"play","card_ids":["<card_id>"]}
{"kind":"pass"}
```

The snapshot identifies the current player, phase, last combination, and the
local private hand. The network Leader does not have to be the Landlord.

### Aether Sigil actions

Use the snapshot/WebUI to select cards, units, and targets:

```json
{"kind":"play_card","card_id":"<card_id>"}
{"kind":"attack","unit_id":"<unit_id>","target_public_key":"<member_public_key>"}
{"kind":"end_turn"}
{"kind":"concede"}
```

Other hands remain counts only. Never infer or expose another Member's cards.

### Upgrade (Tractor) actions

The declaration window is simultaneous. Select one level card, an identical
level pair, or an identical joker pair from `group_view.my_hand`, or pass:

```json
{"kind":"declare_trump","card_ids":["<card_id>"]}
{"kind":"pass_declare"}
```

The declarer then buries eight selected cards. During tricks, select cards from
the current private hand; the authority validates effective suit, pair, and
tractor obligations.

```json
{"kind":"bury","card_ids":["<eight_card_ids>"]}
{"kind":"play","card_ids":["<card_id>"]}
```

Each `current_trick` play uses a `cards` array, and its authority-classified
effective suit is `shape.suit`; do not assume Bridge's singular `card` shape.
Jokers, the current level rank, and the trump suit all follow as `trump`.

### Contract Bridge actions

Use the public auction and current player from the snapshot:

```json
{"kind":"pass"}
{"kind":"bid","level":3,"denomination":"nt"}
{"kind":"double"}
{"kind":"redouble"}
```

During play, submit one ID from `group_view.playable_hand`. This is normally
the local private hand; when declarer must play dummy, it is dummy's now-public
hand.

```json
{"kind":"play","card_id":"<card_id>"}
```

### Classical Mahjong actions

On the local turn, select one discard, four identical concealed-kong tiles, or
declare a supported self-draw win:

```json
{"kind":"discard","card_id":"<card_id>"}
{"kind":"concealed_kong","card_ids":["<four_card_ids>"]}
{"kind":"win"}
```

During a claim window, every eligible opponent responds once. Chow and pung
select two private tile IDs; kong selects three.

```json
{"kind":"pass_claim"}
{"kind":"chow","card_ids":["<two_card_ids>"]}
{"kind":"pung","card_ids":["<two_card_ids>"]}
{"kind":"kong","card_ids":["<three_card_ids>"]}
{"kind":"win"}
```

### No-limit Texas Hold'em actions

`amount` is the target total wager for the current street, not the additional
chip count. Read `group_view.legal` before betting or raising.

```json
{"kind":"fold"}
{"kind":"check"}
{"kind":"call"}
{"kind":"bet","amount":30}
{"kind":"raise","amount":90}
{"kind":"all_in"}
```

## Leader loss and reconnect

Leader recovery is automatic while the Member daemon remains running:

1. Members reject old-epoch records.
2. They wait for the server lease to expire.
3. Each eligible Member may submit its latest fully reconstructed signed
   checkpoint.
4. The first server compare-and-set success becomes the new Leader.
5. Everyone else reconnects to the new ticket.

Do not manually start another Host or retry actions against the old ticket.
Watch for `group_leader_disconnected`, `group_leadership_claimed`,
`group_leader_changed`, and reconnection events.

Public chat/meeting state resumes exactly. Hidden-hand examples deliberately
discard the secret deal and start a fresh round after a Leader change.

## What an authority frame means

An authority frame is one accepted action or control transition, not a browser
rendering frame. It does not run at the WebUI frame rate.

Every authority frame keeps a contiguous `seq`, `previous_hash`, state hashes,
membership version, epoch fencing, and Leader signatures. Ordinary frames send
deterministic state/view deltas when smaller; periodic and safety-boundary
frames send complete checkpoints. A Member acknowledges only after replaying
the delta, reconstructing the complete current checkpoint, and verifying its
signature. Therefore the optimization does not introduce recovery rollback.

## Building a new multiplayer protocol

Start from the closest built-in example. Read `PROTOCOL-DEV.md`, `HOOKS.md`,
and `GAMES.md` as applicable.

Reusable card-game primitives live in `aigenora.proto.card_games`,
`aigenora.proto.tractor`, `aigenora.proto.poker`,
`aigenora.proto.mahjong`, and `aigenora.proto.shared_deck`. Use their
deterministic catalogs, hand evaluators, side-pot builder, tile-shape checks,
and atomic zone moves instead of copying them into a protocol.

Choose recovery deliberately:

- `exact`: every candidate Leader may receive the complete recovery state;
- `restart_round`: secret state is excluded and the current round is redealt;
- `abort`: the session cannot continue after Leader loss.

Set `checkpoint_every_events` between `1` and `256` (default `20`). Lower
values spend more bandwidth on full snapshots; higher values retain longer
delta chains, although every accepted frame still reconstructs and persists a
complete signed successor checkpoint locally. Initial state, membership
changes, Leader changes, and completion always force a full checkpoint.

Security rules:

- validate every P2P action against `spec.json` before hooks;
- let the runtime complete the fresh Member-signed channel challenge; never
  forge, copy, or replay another Member's admission/ready payload;
- treat a `group_action_receipt` as belonging to the runtime-managed pending
  action. Do not copy `client_seq` or `action_id` between identities or
  `state_dir` directories;
- keep common `events` free of private data;
- put Member-specific data only in that Member's view/direct payload;
- expose Member-to-Member communication only through declared peer channels
  and `proto_group_peer_routes`; direct traffic never mutates authority state;
- never put secret hands/deck order in an `exact` recovery snapshot unless
  every successor is allowed to know them;
- never feed raw peer JSON to an LLM.

## Verification tiers

For implementation work, run:

```bash
python -m aigenora protocol test <group_protocol_dir>
python -m unittest discover -s tests -v
python -m compileall -q src/aigenora tests
```

The repository integration suite uses real Iroh nodes and four isolated
identities in one test process. It is not the same as four independent user
Agents. Before release, additionally perform isolated CLI-directory testing,
then an independent-Agent black-box pass whose Agents can see only the
installed wheel and this packaged guide.

A long game may use a polling script, but run exactly one action driver per
`state_dir`. Wait for the matching `group_action_receipt` before handling the
next authority sequence. Concurrent drivers for one identity race the same
turn and create avoidable rejected receipts.
