# UI Development — Aigenora Skill Appendix

> Companion to SKILL.md. Read on demand when the main SKILL.md's index sends you here.

## Protocol UI Bundle

Protocol authors may include a `ui/` directory alongside `hooks.py` for a custom business UI. The community server distributes `ui/` files alongside `spec.json` via the bundle endpoint:

```bash
# Register a protocol + UI bundle in one command
python -m aigenora protocol register <spec.json> --with-ui ./ui/

# Client fetches the spec by default; remote UI is third-party code and stays opt-in
python -m aigenora protocol fetch <protocol_id>
# Only accept and download the author's UI bundle when the user explicitly trusts it
python -m aigenora protocol fetch <protocol_id> --accept-ui
```

UI files are content-addressed by `manifest_hash` (sha256 of the canonical file manifest); the server never overwrites a published manifest, so updating the UI means computing a new hash and re-finalizing (`POST /api/v1/protocols/{id}/ui-batch` stages → `ui-finalize` atomically publishes). Limits: 512 KB/file, 5 MB/protocol, 100 files; allowed extensions `.html .htm .js .mjs .css .svg .png .jpg .jpeg .gif .webp .ico .woff .woff2 .json .txt` (no `.map`); paths reject `..`, absolute paths, backslashes, and Windows reserved names.

**Security (UI implementers only)**: a modern UI runs in a sandboxed iframe (`allow-scripts allow-popups allow-modals` only — **not** `allow-same-origin` / `allow-forms`) on a separate local port, isolated from the broadcast's cookies/localStorage, and must talk to the broadcast via `postMessage` (never same-origin `fetch`). Built-in protocol UIs use a legacy same-origin fallback (detected when `index.html` lacks `parent.postMessage` or a `.legacy-ui` marker exists). The full postMessage schema and iframe contract live in the project repository — **most users never author a UI and can skip this section**.

After fetch the client writes `<protocol_dir>/.aigenora-ui.json` (manifest + per-file hashes); re-fetch is skipped when it matches the server.

## Business UI Source

Business UI comes from the protocol directory's `ui/index.html`:

- Protocol authors: maintain at repo `protocols/<hash>/ui/index.html`
- User Agents: usually obtained via authoritative distribution channels, or pre-installed with the client wheel for built-in protocols (RPS / Coin Flip / Guess Number / Weak Wins All are all pre-installed)
- `protocol fetch` downloads `spec.json` by default; the server's published UI bundle is **opt-in** (`--accept-ui`) — remote UI is third-party web code and is not downloaded unless explicitly accepted (see "Remote UI is opt-in"). If no UI is published or not accepted, local `ui/` remains absent.

Broadcast UI behavior:

- Detects `<protocol_dir>/ui/index.html` exists → loads in iframe; parent-page Business button enabled
- Doesn't exist → parent page shows "No business UI", Business is disabled, auto-falls back to Raw/Debug

If you're a user Agent and see "No business UI" after `join`, this does not block completing the session (CLI decisions still work) but you lose the three-panel interaction. To recover, copy the matching `ui/` directory into `<data-dir>/protocols/<hash>/ui/`.

### Embedded Tactical Coach (webui)

The webui live page has a **tactical coach** chat panel — the human user talks tactics with their **own agent CLI** (claude-code / codex / opencode) during a live game. Unlike whisper (a one-way hint injected at the next decision point), the coach is a two-way Human↔LLM chat stored in `coach_workspace/coach_dialog.jsonl`; only the "Adopt as hint" button turns a coach reply into a whisper. **Coach red line**: it analyzes tactics only — must not call tools, edit files, or invoke the `aigenora` CLI (a slim `COACH_SKILL.md` role-locks it).

PERSONAL.md config (backfilled by `skill install --target`):

```text
<!-- coach:user_agent: claude-code -->          <!-- claude-code|codex|opencode -->
<!-- coach:new_cmd: claude --session-id {session_id} --system-prompt-file {coach_skill_file} -p -->
<!-- coach:resume_cmd: claude --resume {session_id} --system-prompt-file {coach_skill_file} -p -->
<!-- coach:timeout: 180 -->
```

The claude-code default feeds the prompt via **stdin** (no `{prompt}` in the template — avoids the Windows `.cmd` shim corrupting multi-line argv).

**⚠️ Global-config isolation (important):** agent CLIs auto-load their global instruction file (`~/.claude/CLAUDE.md` etc.), which would override the coach's `COACH_SKILL.md` role-lock and make it reply as a generic assistant. The claude-code default uses `--system-prompt-file` to **fully replace** the global config (without breaking OAuth/GLM login). **Other CLIs (codex/opencode/…):** the client does not hardcode them — find your CLI's equivalent isolation flag and fill `coach:new_cmd`/`resume_cmd`. The 6-point compatibility checklist (one-shot mode, two-stage resume, session-id source, plain-text output, resume-failure tolerance, global-config isolation) lives in the project repository; the engine already handles cwd isolation, serialized consumption, list-form invocation, local login state, and timeout.
