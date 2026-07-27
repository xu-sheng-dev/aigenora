# UI Development — Aigenora Skill Appendix

> Companion to SKILL.md. Read on demand when the main SKILL.md's index sends you here.

## Protocol UI Bundle

Protocol authors may include a `ui/` directory alongside `hooks.py` for a custom business UI. The preferred path is durable author publication through the community-server bundle endpoint. When the platform has no UI, Host may also offer an immutable UI-only snapshot during a formal join handshake after both sides explicitly consent. Separately, a trusted Host and Guest may opt into a cohesive current-Session executable bundle containing exactly one `hooks.py` plus its matched `ui/`:

```bash
# Register a protocol + UI bundle in one command
python -m aigenora protocol register <spec.json> --with-ui ./ui/

# Client fetches the spec by default; remote UI is third-party code and stays opt-in
python -m aigenora protocol fetch <protocol_id>
# Only accept and download the author's UI bundle when the user explicitly trusts it
python -m aigenora protocol fetch <protocol_id> --accept-ui

# Host allows on-demand delivery of local ui/; no Guest request means no files are sent
python -m aigenora host --daemon --share-ui --protocol-dir <protocol-dir>
# Guest accepts this Host's UI only as a fallback when local/platform UI is unavailable
python -m aigenora join --daemon --accept-host-ui <post_id>

# High risk: Host offers executable Python plus its matched UI
python -m aigenora host --daemon --share-bundle --protocol-dir <protocol-dir>
# Guest explicitly trusts this Host to run that bundle only for the current Session
python -m aigenora join --daemon --accept-host-bundle <post_id>
```

### Three Independent Remote-Code Decisions (safe default: reject each)

An author-published platform bundle and a UI-only snapshot sent by an individual Host are both third-party Web code (HTML/JS/CSS). Loading either executes that code. A full Host bundle additionally executes Python. Consent must remain separate: `--accept-ui` accepts only the platform-author UI; `--accept-host-ui` permits only this Session's Host UI-only fallback; `--accept-host-bundle` permits one trusted Host's signed `hooks.py + ui/` for only this Session. None implies another.

When an Agent handles remote UI:

1. For platform-author UI, read `accept_remote_ui: always|never|ask`, mapping it to `--accept-ui`.
2. For Host P2P UI, independently read `accept_host_ui_p2p: always|never|ask`, mapping it to `--accept-host-ui`; never infer it from the first field.
3. For executable Host bundles, independently read `accept_host_bundle_p2p: always|never|ask`, mapping it to `--accept-host-bundle`; an `ask` prompt must identify the Host and warn that the worker is not a complete sandbox.
4. While creating an invitation, read `share_ui_with_guests: always|never|ask` and `share_bundle_with_guests: always|never|ask`. Distinguish UI-only `--share-ui` from executable `--share-bundle` in the plain-language pre-post confirmation.
5. Reuse a trust attitude explicitly stated in this conversation for this action. Otherwise ask once only when that remote source would actually be used. Rejection is the default.
6. Change a corresponding PERSONAL field only when the user says to remember it or make it the future default; do not reorder/delete unrelated content.

Constraints:

- No preference and no answer means reject every remote source.
- Ask once per source in the current session, then reuse that source's answer.
- Client-bundled built-in UIs are not remote author code and do not require this decision.
- Editing PERSONAL.md must touch only the corresponding field the user requested to save.

> `web_ui` (`auto` / `headless` / `off`) controls whether the local broadcast service/browser starts. It is orthogonal to `accept_remote_ui`, `accept_host_ui_p2p`, and `accept_host_bundle_p2p`.

UI files are content-addressed by `manifest_hash` (sha256 of the canonical file manifest); the server never overwrites a published manifest, so updating platform UI means computing a new hash and re-finalizing (`POST /api/v1/protocols/{id}/ui-batch` stages → `ui-finalize` atomically publishes). P2P transfers the same kind of immutable snapshot built when Host starts and never auto-uploads it to the platform. Both paths enforce 512 KB/file, 5 MB total, and 100 files; allowed extensions `.html .htm .js .mjs .css .svg .png .jpg .jpeg .gif .webp .ico .woff .woff2 .json .txt` (no `.map`); paths reject `..`, absolute paths, backslashes, Windows reserved names, and cross-platform case/Unicode-normalization collisions, while Host snapshot construction also rejects symlinks/junctions that resolve outside `ui/`. They also validate strict Base64, per-file size/SHA256, manifest, and `index.html`. P2P validates each frame on arrival; installation uses unique staging plus rollback backup.

**Security (UI implementers only)**: a modern UI runs in a sandboxed iframe (`allow-scripts allow-same-origin allow-popups allow-modals`, but **not** `allow-forms`) on a separate local port. `allow-same-origin` preserves the UI server's own isolated origin so Chromium can render it reliably; it does not make that origin equal to the broadcast parent. The isolated server exposes no `/api/*`, restricts `frame-ancestors` to the broadcast origin, and the UI must talk to the parent via `postMessage` (never same-origin `fetch`). Older built-in UI already trusted as part of the local client install may use the legacy same-origin fallback (detected when `index.html` lacks `parent.postMessage` or a `.legacy-ui` marker exists). **Remote platform-author and Host P2P UI may not enter that legacy path**: without the bridge, `/api/ui-available` reports `remote_ui_bridge_required` and the client falls back to generic Web/CLI. The full postMessage schema and iframe contract live in the project repository — **most users never author a UI and can skip this section**.

After a platform bundle is verified, the client writes `<protocol_dir>/.aigenora-ui.json` (source, manifest, and per-file hashes); a matching manifest skips redundant installation. A Host P2P UI-only snapshot and its sidecar live under this Session's `state_dir/ui-artifact/`, never becoming an ordinary protocol cache. Old-Session files are not silently executed in a later match and cannot be redistributed by another Host with `--share-ui`.

### Executable Host Bundle Runtime

Bundle v1 contains exactly `hooks.py` plus `ui/`; it contains no dependencies, installers, archives, or additional Python files. Its signed offer binds Host and Guest public keys, invitation id, Guest nonce, `protocol_id`, manifest, and current handshake. Exact bilateral capability is required before any hooks bytes are sent. Guest validates the whole snapshot and its local verified `spec.json`, then atomically installs it under `state_dir/bundle-artifact/` with provenance in `.aigenora-bundle.json`.

The main Agent process never compiles or imports received hooks. One restricted subprocess is pinned to one Session and receives only validated business values plus a private state directory. It uses isolated Python flags, a minimal environment, bounded JSON RPC and logs, and denies network, subprocesses, dependency/native loading, and filesystem writes outside that state. Invalid RPC, timeout, or crash kills the worker and fails the Session. This is defense in depth, **not a complete Python or operating-system sandbox**; use it only for a Host the user explicitly trusts. Transfer/install failure after bundle transfer begins aborts without same-channel fallback. The worker and temporary staging/cwd are cleaned while bounded provenance and audit events remain. Received artifacts are not scanned, reused, or re-shared.

## Business UI Source

Business UI always runs from the Guest/local participant's own localhost service. If a full executable Host bundle is accepted, its hooks and matched UI are selected together; local/platform UI must not replace only its UI half. Without a full bundle, UI-only sources resolve in this order:

1. Local/built-in UI pre-installed with the wheel.
2. Protocol-author platform bundle accepted with `--accept-ui`.
3. Only if the first two do not exist, a P2P bundle accepted bilaterally with Host `--share-ui` + Guest `--accept-host-ui`.
4. Generic Raw/Debug or CLI when none is usable.

Guest never opens the Host's live Web URL. P2P files land in the Guest's current Session directory and pass the relevant validation and isolation flow. A UI snapshot or full-bundle source is excluded from `spec.json`, `protocol_id`, and Session Proof canonical data.

Broadcast UI behavior:

- Detects current-session `ui_dir/index.html` or `<protocol_dir>/ui/index.html` → loads in iframe; parent-page Business button enabled
- Doesn't exist, or a remote UI lacks the required `postMessage` bridge → parent page shows "No business UI", Business is disabled, auto-falls back to Raw/Debug

If you are a user Agent and see “No business UI” after `join`, this does not block the session (CLI decisions still work), but protocol-specific controls are absent. Prefer accepting the author's platform bundle; if the platform has none and this Host is trusted, rejoin explicitly with `--accept-host-ui`; or install matching `ui/` from an authoritative source.

### Mode-Aware Human Action UI (v020)

After loading, a business UI must call `GET /api/info` (or the `info` postMessage method from an isolated-origin UI) and render according to local `control_mode`:

- `autonomous`: read-only spectating; hide/disable action buttons. `POST /api/decide` returns 409.
- `hybrid`: keep strategy and Agent controls while allowing temporary human actions.
- `human`: make legal game actions the primary surface, hide strategy editing and Whisper, and never imply that an automatic fallback will play for the user.

`/api/info` also provides `role`, `peer_control_mode`, `supported_control_modes`, `decision_schema`, `ui_artifact`, and current-Session `bundle_artifact` provenance when present. `/api/ui-available` exposes UI provenance so a page can label Local, Platform Author, Host P2P UI, or Host P2P Bundle. Peer mode is display-only. Cover every action window hooks may publish (including setup/draw/pass/forced pass), show its deadline, and render only local private information present in the snapshot. Submit through `/api/decide`; never construct P2P messages directly or mutate the Protocol.

### Authoritative Group UI Bridge

An `authoritative_group` UI negotiates `["snapshot", "action"]` in its
`aigenora-ui` hello message. Read only the replacement snapshot delivered by
the parent bridge, then submit the protocol action object directly with bridge
method `action`; the parent maps it to local `POST /api/group/action`. Do not
wrap the action, assign `client_seq`, sign a frame, or contact the Leader
directly—the group runtime owns all of those steps.

The snapshot contains the current Member's signed view, group/epoch/sequence
metadata, and no complete authority state. Render only fields present in that
view. Reset transient UI selections when the hand or legal-action set changes,
show queued actions as pending Leader validation, and display a migration
notice when `leader_epoch` increases. Never merge an older `(epoch, seq)` over
a newer snapshot.

Group bundles must be installed locally and content-addressed on every Member.
The group runtime rejects Host P2P UI and executable bundle sharing.

### Embedded Tactical Coach (webui)

The webui live page has a **tactical coach** chat panel — the human user talks tactics with their **own agent CLI** (claude-code / codex / opencode) during a live game. Unlike whisper (a one-way hint injected at the next decision point), the coach is a two-way Human↔LLM chat stored in `coach_workspace/coach_dialog.jsonl`; only the "Adopt as hint" button turns a coach reply into a whisper. **Coach red line**: it analyzes tactics only — must not call tools, edit files, or invoke the `aigenora` CLI (a slim `COACH_SKILL.md` role-locks it).

The tactical coach is for analysis/intervention in `autonomous`/`hybrid`. Strict `human` hides strategy/Whisper delegation so “the person acts” cannot be confused with “the Agent acts for them.”

PERSONAL.md config (backfilled by `skill install --target`):

```text
<!-- coach:user_agent: claude-code -->          <!-- claude-code|codex|opencode -->
<!-- coach:new_cmd: claude --session-id {session_id} --system-prompt-file {coach_skill_file} -p -->
<!-- coach:resume_cmd: claude --resume {session_id} --system-prompt-file {coach_skill_file} -p -->
<!-- coach:timeout: 180 -->
```

If the configured CLI is unavailable, the coach checks the most recently installed Aigenora skill target and then the installed supported CLIs instead of blindly launching `claude`. If none is available, the Web panel receives a precise missing-binary error naming the executable and the PERSONAL.md fields that can fix it.

The built-in claude-code and Codex defaults feed the prompt via **stdin** (no `{prompt}` in the template), avoiding silent corruption of multiline situation/question text by Windows npm shims. Codex uses JSON events only to capture its real `thread_id`, resumes with `codex exec resume`, and reads the UTF-8 final reply from `-o {coach_output_file}`. The prompt includes bounded public real-time context such as tank positions/HP, recent combat, the active plan, and `realtime.transport` latency advice.

**⚠️ Global-config isolation (important):** agent CLIs auto-load global configuration that can overpower the coach role. The claude-code default uses `--system-prompt-file` to replace global instructions without breaking login. The Codex default uses `--ignore-user-config --ignore-rules` plus a read-only sandbox. If you override either command, preserve equivalent isolation, stdin-safe multiline input, real session resume, and UTF-8 output handling. OpenCode has a built-in baseline command, but custom installations may still need an explicit isolation override.

**Agent guidance:** when the user asks to analyze the current game or identify the opponent's pattern, open the web UI tactical coach panel; the user explicitly starts it with the bottom-left button.
