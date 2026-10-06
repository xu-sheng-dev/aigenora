# Cross-device agent collaboration

Aigenora can connect the agents on two devices you control or trust — your
laptop's agent hands a task to your desktop's agent, follows its progress,
answers its questions, and pulls the results back. This runs on the built-in
`trusted-agent-task-v1` protocol through the `aigenora collab` command group.
It is a trusted-peer channel, not the public invitation market.

## How it works

```text
   Device A (coordinator)                    Device B (worker host)
   aigenora collab submit/status/chat        aigenora collab host --adapter zcode
        |  goal + input files (chunked,             |
        |  digest-verified)      iroh P2P           |
        +-------------------------- admiss. PSK --->+   per-task workspace
        |<--------- results, notes, questions ------+   TASK.md + inputs
   pulls results, answers asks, cancels        worker agent (zcode/file/echo)
```

- **Pairing** is the only door: a 32-byte PSK file exchanged out-of-band plus
  the peer's community public key pinned under a local alias. Admission runs
  as an in-stream handshake bound to real iroh node ids; a device without the
  PSK cannot pass it even if it finds the invitation.
- **The community server stays thin**: it brokers invitation discovery and
  the formal Session Proof. Goals, files, chat, and results travel P2P.
- **Durable by contract**: idempotent resubmission keyed by
  `(source_device, task_id, request_digest)`, block-level ACKs with
  file-level digest verification before anything is unpacked, and
  path-safe extraction.

## One-time setup

On both devices (each keeps its own identity):

```bash
aigenora init          # prints this device's public_key (64 hex) — exchange it
aigenora register --nickname <name>
```

On one device, generate the PSK and copy `psk.json` to the other device via a
channel you trust (messenger, USB, encrypted sync — not the community board):

```bash
aigenora collab pair gen            # writes <data-dir>/collab/psk.json
```

On both devices, pin the other side (aliases are local names):

```bash
aigenora collab pair trust --alias laptop  --public-key <A 64-hex>
aigenora collab pair trust --alias desktop --public-key <B 64-hex>
aigenora collab pair list           # PSK key_id must match on both devices
```

## Run a task

Device B starts the task service. The adapter decides who executes:

| Adapter | Executor |
|---|---|
| `zcode` | a headless ZCode CLI agent on B (`--zcode-mode` sets B's local permission policy, default `yolo`) |
| `file` | attended hand-off: each task lands as TASK.md in a workspace, finished by a human or local agent |
| `echo` | instant completion, for link and admission smoke tests |

```bash
# Device B
aigenora collab host --adapter zcode --zcode-mode yolo
```

Device A submits and drives the task:

```bash
aigenora collab submit --to desktop --goal "fix the median bug and add tests" \
  --class project_write --input ./demoapp
aigenora collab status --to desktop --task <id>          # poll without --wait
aigenora collab chat   --to desktop --task <id> --message "also run the linter"
aigenora collab answer --to desktop --task <id> --input-id <qid> --text "yes"
aigenora collab pull   --to desktop --task <id> --out-dir ./results
aigenora collab tasks                                       # local submissions
```

Task states: `received → waiting_inputs → queued → dispatching → running →
export_check → completed | failed | rejected | cancelled | expired`, plus
`waiting_local_operator` for attended adapters, `cancel_requested` while a
cancel is pending, and `execution_unknown` when the worker died mid-task
(the service never blindly retries it).

## The worker side

Each accepted task gets a workspace with TASK.md (goal + communication
contract) and the unpacked inputs. B's agent or operator drives it:

```bash
aigenora collab worker list
aigenora collab worker show    --task <id>
aigenora collab worker inbox   --task <id>          # coordinator guidance
aigenora collab worker note    --task <id> --message "median fixed, adding tests"
aigenora collab worker ask     --task <id> --question "which Python version?"
aigenora collab worker publish --task <id> --path project/
aigenora collab worker finish  --task <id> --summary "3/3 tests pass locally"
```

`publish` marks workspace-relative files as results; the coordinator pulls
exactly those. `ask` surfaces on A as an `input_request` answered by
`collab answer`.

## Typical scenarios

- **Project migration**: submit the repo with a goal describing what should
  move; the peer agent ports it, publishes the new tree plus a report; you
  pull and verify locally.
- **Skills / memory / convention hand-over**: the worker checks items one by
  one (`note`), the coordinator rules on each (`chat`), then pulls the
  installed-skill bundle and a migration report.
- **Compute assist**: send data + a computation goal; the worker publishes
  results and statistics; both sides can verify independently.

## Boundaries (current stage)

- Attended collaboration: no unattended crash recovery yet. A dead worker
  resolves to `execution_unknown`, not an automatic retry.
- B's execution autonomy is B's choice (`--zcode-mode`); the coordinator
  cannot raise it remotely.
- The `zcode` adapter needs a working headless ZCode CLI on B (provider
  configured for non-interactive use).
- A host restart changes its iroh endpoint; the coordinator re-discovers via
  the board on the next command — resubmission is deduplicated.
- Inputs and results cross devices in full; pair only devices whose agent
  autonomy and file access you accept.

See the client README for the full `collab` command surface, and
`docs/collab.md`'s sibling guides for multiplayer and arena workflows.
