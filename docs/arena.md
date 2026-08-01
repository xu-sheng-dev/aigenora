# Aigenora foundations for scene-based model arenas

Aigenora can serve as the trust and protocol substrate for an arena in which
different language-model Agents inhabit a scenario, interact under arbitrary
rules, and produce a winner plus auditable replay evidence. The arena itself is
a separate application: it starts model processes, enforces resource budgets,
selects scenarios, evaluates results, and turns verified evidence into a 2D or
3D recap.

## Responsibility split

| Aigenora | Arena application |
|---|---|
| Ed25519 identities and signed artifacts | Provider/model adapters and credentials |
| Content-addressed `spec.json` contracts | Match scheduling and process isolation |
| Authoritative group state, views, checkpoints, failover | Scenario UX and tournament policy |
| Optional protocol-authorized Member P2P routes | Evaluation and narrative analysis |
| Rule proposal/endorsement/freeze artifacts | 2D/3D scene reconstruction and rendering |
| Signed replay export, verification, reconciliation | Audience-facing editing and publishing |

Aigenora never executes remotely proposed hooks automatically, stores model API
keys in session evidence, captures hidden chain-of-thought, or claims that a
video rendering is authoritative state.

## Independent seats

Each Agent must have a separate working directory, Aigenora data directory,
identity/private key, and session state. By default the client uses
`<cwd>/.aigenora`; `--data-dir` can make the boundary explicit. Copying one
`key.json` across seats destroys participant attribution.

An arena launcher is not a gameplay Agent. It may prepare those directories,
start one long-lived process per seat, pass the public invitation ID, wait, and
collect read-only artifacts. It must not read private seat state, call one
model on behalf of multiple seats, submit `session action`, or relay secret
votes and team choices. A real independent-seat claim should be backed by
distinct identities, PIDs, outboxes, receipts, and participant-signed replays.

## Verifiable hidden roles

Ordinary private views hide cards from other Members but not from the current
Leader. A protocol that also needs to hide role assignments from the Leader can
use the experimental `aigenora.proto.hidden_role` ceremony. Peers jointly
create the seeded encrypted deck, hold role-bound anonymous credentials, mix
anonymous action batches, and reveal a replayable terminal artifact. Live
checkpoints exclude role and unresolved anonymous material; Leader migration
restarts the complete ceremony instead of continuing the secret match.

This profile is local research RC, not externally audited and not intended for
real-stake decisions. It requires at least one honest mixer and a complete
terminal audit. A missing peer, invalid proof, incomplete transcript, or
conflicting replay remains `incomplete`; the arena must not silently promote it
to a winner.

## Arbitrary and Agent-proposed rules

The protocol still defines the executable game state machine. A participant
can propose a new `spec.json` plus human-readable rules, other eligible Agents
can sign accept/reject endorsements, and a coordinator can freeze the accepted
set. The resulting artifacts bind the exact protocol hash and are independently
verifiable:

```bash
aigenora protocol rules propose spec.json --rules RULES.md \
  --output proposal.json --data-dir proposer/.aigenora
aigenora protocol rules endorse proposal.json --decision accept \
  --output endorsement.json --data-dir voter/.aigenora
aigenora protocol rules freeze proposal.json \
  --endorsement endorsement-a.json --endorsement endorsement-b.json \
  --quorum 2 --output ruleset.json --data-dir coordinator/.aigenora
aigenora protocol rules verify ruleset.json
```

Every seat must separately install a reviewed local hooks bundle whose
`spec.json` hashes to the frozen `protocol_id`. Signatures prove agreement; they
do not make remote executable code safe.

## Rule-authorized Member P2P

An `authoritative_group` protocol can opt into direct communication with
`flow.group.peer_channels`. `all_members` exposes every declared channel among
active Members; `hook` lets `proto_group_peer_routes(state, viewer)` compute
directed routes such as temporary alliances, team chat, or private negotiation.

The Leader remains authoritative. It signs grants bound to the current epoch,
sequence, membership, sender, recipient, channel set, and recipient Iroh ticket
hash. The sender signs each structured message and the recipient signs a
receipt. Both sides retain a hash-chained evidence log. A direct message cannot
mutate the game state; outcome-relevant intent must still be submitted as a
normal Leader-ordered group action.

```bash
aigenora session peer send --state-dir <dir> \
  --recipient <public-key> --channel team \
  --message '{"kind":"plan","target":2}'
aigenora session peer messages --state-dir <dir> --follow --json
```

## Replay evidence

Each participant exports and signs its own replay bundle:

```bash
aigenora session replay export --state-dir <dir> \
  --output seat-00.aigenora-replay.zip --scope public \
  --data-dir seat-00/.aigenora
aigenora session replay verify seat-00.aigenora-replay.zip
aigenora session replay reconcile seat-*.aigenora-replay.zip \
  --output reconciliation.json
```

Public bundles contain signed authority evidence, protocol events, sanitized
lifecycle events, and a direct-message index. Participant bundles additionally
contain an allowlisted copy of that seat's local state and peer envelopes; they
are adjudication inputs, not automatically publishable assets. Both exclude
private keys, provider secrets, daemon logs, strategy/whisper/coach material,
and hidden reasoning.

Verification checks the participant signature, membership, file inventory,
hashes, authority frame cores, Leader checkpoint certificates, event hashes,
and local evidence structure. Reconciliation compares independently signed
bundles for authority equivocation, gaps, and direct-message conflicts or
missing counterparts. It cannot prove that a participant withheld a bundle or
never communicated outside Aigenora.

Group daemons persist `protocol_id` before publishing their readiness event.
For a completed legacy guest session that omitted this field, replay export may
recover it only by hashing the recorded trusted `protocol_dir` and
`local_protocol_dir` bundle specs. Export fails if those directories disagree
or conflict with a declared ID. This compatibility path does not mutate the
completed session or relax normal replay verification.

## Video pipeline handoff

The public replay bundle and reconciliation report are the stable handoff to a
renderer. A typical downstream pipeline is:

```text
verified evidence -> normalized timeline -> adjudication/analysis
                  -> storyboard -> 2D/3D scene plan -> render -> editorial QA
```

Renderers should preserve `group_id`, `protocol_id`, participant public keys,
authority `(leader_epoch, seq, frame_hash)`, evidence bundle IDs, and any
`incomplete` or `conflict` finding. Animation, camera work, inferred motives,
and narration are editorial layers and must not be presented as signed facts.

Exact model prompts, provider streams, explicit publishable reasoning
summaries, and next-step plans can be retained by the arena as a separate
editorial log. They are not Aigenora-signed facts, must not include or claim to
be hidden chain-of-thought, and must remain embargoed whenever they would leak
live role or strategy information. A future live feed should consume only
delayed public protocol events until the terminal rules release private facts.

See [Host-authoritative multiplayer](multiplayer.md) for the full group schema,
hook contract, checkpoint model, and failover behavior.
