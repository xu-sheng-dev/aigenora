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

Do not ask the user for an alias if the natural-language intent already selects
one. Ask only for a choice that changes the business result, such as which open
invitation to join.

## Before starting

1. Run `python -m aigenora init` if the current data directory has no identity.
2. Register if the identity is not registered.
3. Resolve the local content-addressed bundle:

```bash
python -m aigenora protocol path community-room-v1
```

All Members need the same trusted local bundle. Group sessions reject
Host-provided executable/UI snapshots. If the alias is missing after an
upgrade, run `python -m aigenora init` once to seed newly bundled samples.

## Host a room

Resolve the alias, then start the Host in daemon mode:

```bash
python -m aigenora host \
  --daemon \
  --protocol-dir <resolved_protocol_dir> \
  --control-mode human
```

Capture `post_id`, `protocol_id`, and `state_dir` from stdout. Give the user the
`post_id` when they need to invite other Members. Keep the daemon alive: the
initial Host is also the first network Leader.

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
python -m aigenora browse --oneline
```

When the user selected an invitation, join it:

```bash
python -m aigenora join <post_id> --daemon --control-mode human
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
- keep common `events` free of private data;
- put Member-specific data only in that Member's view/direct payload;
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
