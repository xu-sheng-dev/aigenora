# UI Development — Aigenora Skill Appendix

> Companion to SKILL.md. Read on demand when the main SKILL.md's index sends you here.

## Protocol UI Bundle

Protocol authors may include a `ui/` directory alongside `hooks.py` for a custom business UI. The preferred path is durable author publication through the community-server bundle endpoint. When the platform has no UI, Host may also offer an immutable snapshot during a formal join handshake after both sides explicitly consent:

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
```

### Trust Decisions for Two Remote UI Sources (safe default: reject each)

An author-published platform bundle and a bundle sent by an individual Host over P2P are both third-party Web code (HTML/JS/CSS). Loading either executes that code. Consent must remain separate: `--accept-ui` accepts only the platform author bundle; `--accept-host-ui` permits only this session's Host UI and is requested only when local/platform UI is unavailable. Rejecting both never blocks generic Web/CLI play.

When an Agent handles remote UI:

1. For platform-author UI, read `accept_remote_ui: always|never|ask`, mapping it to `--accept-ui`.
2. For Host P2P UI, independently read `accept_host_ui_p2p: always|never|ask`, mapping it to `--accept-host-ui`; never infer it from the first field.
3. While creating an invitation, read `share_ui_with_guests: always|never|ask` and include `--share-ui` in the plain-language pre-post confirmation.
4. Reuse a trust attitude explicitly stated in this conversation for this action. Otherwise ask once only when that remote source would actually be used. Rejection is the default.
5. Change a corresponding PERSONAL field only when the user says to remember it or make it the future default; do not reorder/delete unrelated content.

Constraints:

- No preference and no answer means reject both remote sources.
- Ask once per source in the current session, then reuse that source's answer.
- Client-bundled built-in UIs are not remote author code and do not require this decision.
- Editing PERSONAL.md must touch only the corresponding field the user requested to save.

> `web_ui` (`auto` / `headless` / `off`) controls whether the local broadcast service/browser starts. `accept_remote_ui` and `accept_host_ui_p2p` independently control the two remote code sources. All three are orthogonal.

UI files are content-addressed by `manifest_hash` (sha256 of the canonical file manifest); the server never overwrites a published manifest, so updating platform UI means computing a new hash and re-finalizing (`POST /api/v1/protocols/{id}/ui-batch` stages → `ui-finalize` atomically publishes). P2P transfers the same kind of immutable snapshot built when Host starts and never auto-uploads it to the platform. Both paths enforce 512 KB/file, 5 MB total, and 100 files; allowed extensions `.html .htm .js .mjs .css .svg .png .jpg .jpeg .gif .webp .ico .woff .woff2 .json .txt` (no `.map`); paths reject `..`, absolute paths, backslashes, Windows reserved names, and cross-platform case/Unicode-normalization collisions, while Host snapshot construction also rejects symlinks/junctions that resolve outside `ui/`. They also validate strict Base64, per-file size/SHA256, manifest, and `index.html`. P2P validates each frame on arrival; installation uses unique staging plus rollback backup.

**Security (UI implementers only)**: a modern UI runs in a sandboxed iframe (`allow-scripts allow-same-origin allow-popups allow-modals`, but **not** `allow-forms`) on a separate local port. `allow-same-origin` preserves the UI server's own isolated origin so Chromium can render it reliably; it does not make that origin equal to the broadcast parent. The isolated server exposes no `/api/*`, restricts `frame-ancestors` to the broadcast origin, and the UI must talk to the parent via `postMessage` (never same-origin `fetch`). Older built-in UI already trusted as part of the local client install may use the legacy same-origin fallback (detected when `index.html` lacks `parent.postMessage` or a `.legacy-ui` marker exists). **Remote platform-author and Host P2P UI may not enter that legacy path**: without the bridge, `/api/ui-available` reports `remote_ui_bridge_required` and the client falls back to generic Web/CLI. The full postMessage schema and iframe contract live in the project repository — **most users never author a UI and can skip this section**.

After a platform bundle is verified, the client writes `<protocol_dir>/.aigenora-ui.json` (source, manifest, and per-file hashes); a matching manifest skips redundant installation. A Host P2P bundle and its sidecar live under this session's `state_dir/ui-artifact/`, never becoming an ordinary protocol cache. Old-session files are not silently executed in a later match and cannot be redistributed by another Host with `--share-ui`.

## Business UI Source

Business UI always runs from the Guest/local participant's own localhost service. Local/platform UI comes from the protocol directory; Host P2P UI comes from this session's artifact directory. Sources resolve in this order:

1. Local/built-in UI pre-installed with the wheel.
2. Protocol-author platform bundle accepted with `--accept-ui`.
3. Only if the first two do not exist, a P2P bundle accepted bilaterally with Host `--share-ui` + Guest `--accept-host-ui`.
4. Generic Raw/Debug or CLI when none is usable.

Guest never opens the Host's live Web URL. P2P files land in the Guest's current session directory and pass the same validation/sandbox flow. A UI snapshot is also excluded from `spec.json`, `protocol_id`, and Session Proof canonical data.

Broadcast UI behavior:

- Detects current-session `ui_dir/index.html` or `<protocol_dir>/ui/index.html` → loads in iframe; parent-page Business button enabled
- Doesn't exist, or a remote UI lacks the required `postMessage` bridge → parent page shows "No business UI", Business is disabled, auto-falls back to Raw/Debug

If you are a user Agent and see “No business UI” after `join`, this does not block the session (CLI decisions still work), but protocol-specific controls are absent. Prefer accepting the author's platform bundle; if the platform has none and this Host is trusted, rejoin explicitly with `--accept-host-ui`; or install matching `ui/` from an authoritative source.

### Mode-Aware Human Action UI (v020)

After loading, a business UI must call `GET /api/info` (or the `info` postMessage method from an isolated-origin UI) and render according to local `control_mode`:

- `autonomous`: read-only spectating; hide/disable action buttons. `POST /api/decide` returns 409.
- `hybrid`: keep strategy and Agent controls while allowing temporary human actions.
- `human`: make legal game actions the primary surface, hide strategy editing and Whisper, and never imply that an automatic fallback will play for the user.

`/api/info` also provides `role`, `peer_control_mode`, `supported_control_modes`, `decision_schema`, and `ui_artifact` source/manifest. `/api/ui-available` exposes the same provenance so a page can label Local, Platform Author, or Host P2P. Peer mode is display-only. Cover every action window hooks may publish (including setup/draw/pass/forced pass), show its deadline, and render only local private information present in the snapshot. Submit through `/api/decide`; never construct P2P messages directly or mutate the Protocol.

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
