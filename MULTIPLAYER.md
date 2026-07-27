# Host-authoritative multiplayer

Aigenora group protocols extend the original one-Host/one-Guest runtime into a
Host-authoritative star without turning the community server into a business
message relay.

## Architecture

```text
                         signed REST control plane
                  ┌────────────────────────────────┐
                  │ Aigenora community server      │
                  │ membership · lease · epoch ·   │
                  │ checkpoint digest · new ticket │
                  └───────────────┬────────────────┘
                                  │
                     current Leader (initial Host)
                       ╱          │          ╲
                Iroh P2P      Iroh P2P     Iroh P2P
                   ╱              │              ╲
              Member 1        Member 2        Member N
```

- The current Leader owns the complete protocol state. It validates and orders
  Member actions, applies trusted local hooks, and signs one global frame chain.
- Each Member has an independent bidirectional Iroh channel to the Leader.
  Members do not need a full mesh.
- The server stores no business messages, executable hooks, hands, deck order,
  meeting text, or chat text. It stores only room membership, the current
  Leader endpoint, a short lease, a fencing epoch, and the digest of a
  replicated recovery checkpoint.
- One public frame core is combined with a separately hashed and signed
  per-Member view. This allows a shared timeline and private hands at the same
  time.

`leader_epoch` is the fencing token. A Member accepts only frames signed by the
Leader for its current epoch, with a strictly contiguous sequence and hash
chain. An old Host cannot resume authority after another Member has claimed a
new epoch.

## Failover

The Leader renews a short server lease. Every authority frame also carries a
Leader-signed checkpoint certificate. A Member acknowledges a frame only after
validating its frame hash, private-view hash, signature, sequence, and
checkpoint certificate.

Leader heartbeats and successful claims also renew the discovery invitation.
Closing or failing the room closes that invitation, so an active room remains
joinable without leaving a stale post after termination.

The Leader advertises only a checkpoint acknowledged by at least one
non-Leader Member. This makes the server digest a recovery floor that is known
to exist outside the Host process.

After the lease expires:

1. online Members query the stable `group_id`;
2. each candidate opens a new Iroh endpoint and submits its signed checkpoint;
3. the server performs one compare-and-set update on the expected epoch and
   expired lease;
4. the first successful request becomes the next Leader and publishes its new
   ticket;
5. losing candidates reconnect to that ticket and accept only the new epoch.

The checkpoint certificate may be newer than the last digest heartbeat stored
by the server, but it must be signed by the previous Leader and must not be
older than the server recovery floor.

## Protocol declaration

```json
{
  "flow": {
    "mode": "authoritative_group",
    "group": {
      "min_participants": 2,
      "max_participants": 16,
      "allow_late_join": true,
      "start_policy": "min_ready",
      "recovery_mode": "exact",
      "checkpoint_every_events": 1,
      "max_action_bytes": 16384,
      "max_events_per_action": 64
    }
  }
}
```

`start_policy` is `min_ready` for an open room, `full` when a room starts at
its declared capacity, or `fixed_full` for a fixed-seat table.
`max_participants` cannot exceed the server limit (32).
In this version, `checkpoint_every_events` may be omitted (default `1`) but
cannot be set to another value: every authority frame carries a checkpoint
certificate so acknowledged recovery never requires a rollback.

`recovery_mode` is one of:

- `exact`: the checkpoint contains all state needed to continue. Use it only
  when every Member is allowed to receive that recovery state.
- `restart_round`: preserve safe public progress, discard the current secret
  deal, and start a new round after migration.
- `abort`: do not continue after a Leader loss.

## Hooks

An `authoritative_group` bundle implements these trusted local hooks:

```python
class Hooks(ProtocolHooks):
    def proto_group_initial_state(self, members): ...
    def proto_group_member_joined(self, state, member): ...
    def proto_group_member_left(self, state, member, reason): ...
    def proto_group_handle(self, state, actor, action): ...
    def proto_group_view(self, state, viewer): ...
    def proto_group_recovery_snapshot(self, state): ...
    def proto_group_restore(self, checkpoint, members, new_epoch): ...
    def proto_group_on_leader_changed(self, state, old_leader, new_leader): ...
```

`proto_group_handle` returns an object containing `state` and optional
`events`, `direct`, `completed`, and `outcome`. The engine bounds JSON size,
deduplicates each actor's `client_seq`, allocates the global sequence, signs
frames, persists checkpoints, and routes Member views. Hooks never own network
channels.

Never place another Member's private data in `events`, `direct` for a different
Member, or `proto_group_recovery_snapshot`. `events` are part of the common
frame core and are delivered to everyone.

## Shared deck helper

`aigenora.proto.shared_deck` supplies a reusable authoritative pile with
physical-card IDs:

```python
from aigenora.proto.shared_deck import (
    create_shared_deck,
    draw_cards,
    take_from_hand,
    discard_cards,
    private_deck_view,
    validate_conservation,
)
```

The authority owns `catalog`, `draw_pile`, `hands`, `discard`, and public
zones. `private_deck_view(deck, member_public_key)` exposes that Member's hand,
only counts for other hands, and public zones/discard. Conservation validation
rejects a card that disappears or exists in two zones.

The helper provides privacy by view separation, not by hiding state from the
current Leader. A malicious Leader is still able to inspect or bias its own
authority state. Signed logs make accepted actions and published state
auditable; they do not create trustless shuffling.

## Built-in examples

| Alias | Seats | Recovery | Purpose |
|---|---:|---|---|
| `community-room-v1` | 2–32 | `exact` | Ordered chat, topic, presence, owner close |
| `meeting-room-v1` | 2–16 | `exact` | Agenda, FIFO floor, votes, action items, facilitator transfer |
| `four-player-landlord-v1` | 4 | `restart_round` | Two-deck, one-versus-three shedding game with private hands |
| `aether-sigil-v1` | 4 | `restart_round` | Original shared-deck tactical card battle with private hands and public boards |

The network Leader is not a protocol business role. A meeting facilitator can
be transferred independently, and the Landlord player need not be the Leader.

## Running a room

Resolve a built-in directory, then use the same `host` and `join` commands as
other protocols:

```bash
python -m aigenora protocol path community-room-v1
python -m aigenora host --daemon --protocol-dir <path> --control-mode human
python -m aigenora join --daemon --control-mode human <post_id>
```

Submit a structured action from the CLI or use the protocol WebUI:

```bash
python -m aigenora session action \
  --state-dir <dir> \
  --action '{"kind":"send","text":"hello room"}'
python -m aigenora session web --state-dir <dir>
```

Group rooms currently require the same trusted protocol bundle to be installed
locally for every participant. Host-provided `--share-ui`, `--share-bundle`,
`--accept-host-ui`, and `--accept-host-bundle` session snapshots are rejected
for `authoritative_group`; this avoids negotiating executable or UI code
independently across many Members. Built-in bundles and locally installed,
content-addressed bundles are supported.

A lost P2P channel is treated as a transient disconnect so the Member can
reconnect with the same stable seat. Membership changes only through a signed
join/leave control-plane operation; this version does not automatically evict
a Member because one channel or heartbeat was missed.

## Server coordination API

The signed control-plane resource is `/api/v1/groups`:

```text
POST /api/v1/groups
GET  /api/v1/groups/by-post/{post_id}
GET  /api/v1/groups/{group_id}
POST /api/v1/groups/{group_id}/members
POST /api/v1/groups/{group_id}/heartbeat
POST /api/v1/groups/{group_id}/members/heartbeat
POST /api/v1/groups/{group_id}/claim-leader
POST /api/v1/groups/{group_id}/leave
POST /api/v1/groups/{group_id}/status
```

Group creation must exactly match the registered spec's participant and
recovery policy. Admission requires both the current Leader signature and the
joining Member signature over the same canonical statement.

## Verification

```bash
python -m aigenora protocol test <group-protocol-dir>
python -m unittest discover -s tests -v
python -m compileall -q src/aigenora tests
```

`protocol test` runs a deterministic multi-Member smoke test, checks private
views, and restores a new Leader epoch. The repository suite additionally
covers concurrent channels, recovery-floor acknowledgement, certificate and
view tampering, shared-deck conservation, all four example protocols, real
four-Member Iroh failover, WebUI bridge contracts, and the existing 1:1
runtime.
