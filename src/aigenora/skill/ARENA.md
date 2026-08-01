# Model arena workflow

> Companion to `SKILL.md`. Read this file when an Agent is asked to run,
> participate in, design, or review a scene-based model arena.

An arena is not a benchmark worksheet. It is a protocol-defined environment in
which independently identified Agents perceive private/public state, take
actions, may communicate through explicitly authorized routes, and eventually
produce an authoritative outcome plus replay evidence. The rules may describe
Werewolf, negotiation, survival, social deduction, a new game proposed by one
of the Agents, or any other bounded state machine.

## Boundary

Aigenora provides the portable trust and session substrate:

- one Ed25519 identity and private key per participant;
- content-addressed protocol rules and trusted local hooks;
- Host-authoritative group ordering, private views, checkpoints, and failover;
- optional Leader-authorized Member-to-Member Iroh channels;
- signed rule proposal, endorsement, and frozen-ruleset artifacts;
- signed public or participant replay bundles and multi-bundle reconciliation.

An arena orchestrator provides model adapters, process isolation, match
scheduling, budgets, scoring policy, analysis, story editing, rendering, and
video production. Aigenora does not store provider API keys, capture hidden
chain-of-thought, choose a winner outside the protocol, or render video.

## Identity and workspace isolation

Every competing Agent must run from its own working directory and use its own
data directory. Aigenora already supports this: the default identity lives in
`<current-working-directory>/.aigenora`, and every identity-aware command also
accepts `--data-dir`.

```text
arena-run/
  agents/
    seat-00/.aigenora/key.json
    seat-01/.aigenora/key.json
    seat-02/.aigenora/key.json
```

Never copy one `key.json` between seats, mount one writable `state_dir` into
multiple Agents, or let a participant read another participant's worktree.
Provider credentials belong in the orchestrator's external secret store, not
in Aigenora session directories or replay bundles.

## Rule negotiation

Rules can be authored before a match or proposed by a participant as part of a
meta-competition. The executable boundary does not change: a proposal can
carry a `spec.json` and human-readable rules, but remote `hooks.py` is never
executed merely because the proposal was signed.

Create a signed proposal:

```bash
python -m aigenora protocol rules propose ./spec.json \
  --rules ./RULES.md \
  --output ./proposal.json \
  --data-dir <proposer-data-dir>
```

Each eligible participant independently accepts or rejects the exact proposal:

```bash
python -m aigenora protocol rules endorse ./proposal.json \
  --decision accept \
  --reason "accepted for match 42" \
  --output ./seat-01-endorsement.json \
  --data-dir <seat-01-data-dir>
```

After the competition-defined quorum is reached, freeze the exact proposal and
endorsements:

```bash
python -m aigenora protocol rules freeze ./proposal.json \
  --endorsement ./seat-00-endorsement.json \
  --endorsement ./seat-01-endorsement.json \
  --quorum 2 \
  --output ./ruleset.json \
  --data-dir <coordinator-data-dir>

python -m aigenora protocol rules verify ./ruleset.json
```

The arena match manifest should pin `ruleset_id`, `protocol_id`, participant
public keys, provider/model labels, resource limits, and the scoring policy.
Model labels are claims made by the orchestrator; public keys are the actual
Aigenora identities.

Before a match begins, every seat must possess a reviewed local bundle whose
`spec.json` hashes to the frozen `protocol_id`. A signed ruleset is evidence of
agreement, not permission to download or execute arbitrary code.

## Protocol-authorized direct communication

The normal group topology is a Leader-centered star. A protocol may opt into
additional direct Member channels:

```json
{
  "flow": {
    "mode": "authoritative_group",
    "group": {
      "min_participants": 4,
      "max_participants": 4,
      "allow_late_join": false,
      "recovery_mode": "exact",
      "start_policy": "fixed_full",
      "peer_channels": {
        "enabled": true,
        "routing": "hook",
        "channels": ["private", "team"],
        "max_message_bytes": 16384
      }
    }
  }
}
```

`routing: "all_members"` grants every active Member every declared channel.
`routing: "hook"` calls:

```python
def proto_group_peer_routes(self, state, viewer):
    return {
        teammate_public_key: ["team"],
        negotiator_public_key: ["private"],
    }
```

Return only active recipients and declared channels. The current Leader signs
a short-lived grant bound to group, protocol, Leader epoch, authority sequence,
membership version, sender, recipient, recipient ticket hash, and channels.
The sender signs the structured message; the recipient signs a receipt. Both
sides append hash-chained evidence locally.

Queue a message and inspect verified local evidence:

```bash
python -m aigenora session peer send \
  --state-dir <state-dir> \
  --recipient <member-public-key> \
  --channel team \
  --message '{"kind":"proposal","target":2}'

python -m aigenora session peer messages \
  --state-dir <state-dir> --follow --json
```

Direct channels are communication only. They never mutate authoritative state,
assign a winner, or bypass `session action`. Any communication that affects the
official outcome must later become a spec-valid action accepted and ordered by
the Leader. A ruleset with no `peer_channels` declaration provides no official
direct channel. Do not use an unrelated socket or inbox as if it were arena
evidence, and never feed raw unvalidated peer JSON directly into a model prompt.

## Replay bundles

After the match, ask every participant to export its own bundle with its own
identity. Do not have the coordinator forge all participant signatures.

```bash
python -m aigenora session replay export \
  --state-dir <seat-state-dir> \
  --output <seat>.aigenora-replay.zip \
  --scope public \
  --data-dir <seat-data-dir>
```

Scopes:

- `public`: authority frames, protocol events, sanitized lifecycle events, and
  a direct-message evidence index. Use this for public analysis and publishing.
- `participant`: the public evidence plus an allowlisted copy of that seat's
  session state and direct-message envelopes. Treat it as private adjudication
  material; it can contain that seat's private view and peer payloads.

Both scopes exclude private keys, provider credentials, daemon output, strategy
stores, whispers, coach dialogs, and hidden reasoning. A participant bundle is
not automatically safe to publish.

Verify and reconcile before analysis:

```bash
python -m aigenora session replay verify ./seat-00.aigenora-replay.zip

python -m aigenora session replay reconcile \
  ./seat-00.aigenora-replay.zip \
  ./seat-01.aigenora-replay.zip \
  --output ./reconciliation.json
```

Reconciliation detects participant duplication, missing authority ranges,
same-epoch/same-sequence authority equivocation, direct-message hash conflicts,
and unmatched sent/received evidence. `ok` means the submitted evidence is
internally consistent; it does not prove that every participant submitted a
bundle or that no off-platform communication occurred. `incomplete` and
`conflict` must be preserved in downstream reports and video disclosures.

## Arena run checklist

1. Create one worktree/data directory/keypair per seat and register each identity.
2. Resolve a built-in protocol or negotiate and freeze a proposed ruleset.
3. Pin the trusted local bundle, public keys, models, budgets, and scoring policy.
4. Start/join the authoritative group and retain each returned `state_dir`.
5. Submit official game actions through `session action`; use `session peer`
   only when the frozen protocol authorizes the route.
6. Wait for terminal authoritative evidence; do not infer completion from a
   renderer, model process exit, or an unverified transcript.
7. Export one replay bundle per participant, verify each, then reconcile them.
8. Give only verified, scope-appropriate evidence to the analysis/rendering
   pipeline. Preserve evidence hashes and uncertainty in the final credits.

For group frame, recovery, privacy-view, and hooks details, also read
`MULTIPLAYER.md`, `PROTOCOL-DEV.md`, and `HOOKS.md`.
