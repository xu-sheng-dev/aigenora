"""v014-M2: webui embedded LLM coach (agent-agnostic).

The coach is a chat panel in the broadcast webui where the human user talks tactics with
their OWN agent CLI (claude-code / codex / opencode), declared in PERSONAL.md and driven by a
command template. It is strictly distinct from whispers (whisper = human -> hooks one-way
tactical hint, no LLM; coach = human <-> LLM two-way conversation).

Key design (see docs/devplan/v014-cli-first-web-and-coach.md ADR-4..7 and
docs/implplan/v014-m2-embedded-coach.md):

- Coach session = daemon-level CROSS-GAME session pool (NOT one-session-per-game). session-id
  lives in <data_dir>/coach_workspace/coach_session.json; daemon restart resumes; only an
  explicit "reset coach" clears it. Switching games only refreshes the injected snapshot, it
  does NOT clear the conversation.
- Process model = one Popen per turn + session-id resume (no resident stdin -> no stderr
  deadlock + natural crash isolation). stdout is read line-by-line (default text output,
  real-time flushed ~250ms/line per 2026-06-23 measurement; NOT parsed as JSON).
- Independent SSE /api/coach/stream (not merged into the main /sse/stream).
- Coach -> whisper bridge: an "adopt as tactical hint" button on a coach reply reuses
  /api/whisper (role=user) — implemented in the web layer, not here.
- Security: list-form Popen (shell=True forbidden; the prompt is a single argv element after
  placeholder substitution, injection-safe); coach_workspace holds only a slimmed
  COACH_SKILL.md and is NEVER symlinked to state_dir (the situation reaches the coach only via
  prompt injection, so raw game data never pollutes the coach's context).
"""
from __future__ import annotations

import json
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from aigenora.proto.sdk import CoachDialog, CoachInbox, EventBus, SnapshotBus


# ---------- defaults ----------

DEFAULT_TIMEOUT = 180             # seconds per coach turn
DEFAULT_MAX_CONTEXT_EVENTS = 12   # how many recent events to inject into the prompt

# Decision-window bookkeeping events that the engine emits at high frequency every round
# (commit→reveal→round_result triggers a fresh window each turn). They carry no tactical
# information for the coach -- the actual opponent moves live in `protocol_message` events
# (msg.type=reveal / round_result). Before v014.x the prompt-injected tail was pure-time,
# so a busy decision-window cycle could push every reveal / round_result / peer_joined out
# of the window and leave the coach blind to what the opponent just played. We drop them
# from `summarize_events` so combat events survive the tail.
#   Background: project-v014-coach-snapshot-events-truncation.
_COACH_NOISE_EVENT_TYPES = frozenset({
    "local_decision_window_started",
    "local_decision_updated",
    "local_decision_fallback",
    "local_decision_finalized",
    "local_decision_ready",
})
DEFAULT_USER_AGENT = "claude-code"
PERSONAL_FILENAME = "PERSONAL.md"
COACH_SKILL_FILENAME = "COACH_SKILL.md"
TRACKER_PATH = Path.home() / ".aigenora" / "skill_targets.json"

# Default command templates per user_agent. {session_id} and {prompt} are placeholders replaced
# by coach.py AFTER shlex.split (NEVER by a shell). claude-code validated 2026-06-23 (see
# implplan): --session-id <new_uuid> creates, --resume <uuid> continues; same uuid cannot be
# re-created ("already in use"), so new/resume are two distinct templates.
DEFAULT_CMDS: dict[str, dict[str, str]] = {
    "claude-code": {
        # COACH_SKILL.md is injected via --system-prompt-file so it becomes the session system
        # prompt and FULLY REPLACES the user's global ~/.claude/CLAUDE.md. Without this, claude
        # auto-loads the global CLAUDE.md (self-evolution / CodeGraph / MCP persona), whose
        # system-prompt priority dominates any role-lock and makes the coach reply as a generic
        # assistant (verified 2026-06-25 half-open test). The prompt itself is fed via STDIN, not
        # as a -p {prompt} argv element: the Windows npm .cmd shim corrupts multi-line argv and
        # would drop the situation/user-question text; -p with no argument reads the prompt from
        # stdin. Auth is unaffected (unlike --bare, which forces ANTHROPIC_API_KEY and breaks
        # OAuth / GLM-provider login). See SKILL.md "Embedded Coach" for background.
        "new_cmd": "claude --session-id {session_id} --system-prompt-file {coach_skill_file} -p",
        "resume_cmd": "claude --resume {session_id} --system-prompt-file {coach_skill_file} -p",
    },
    # 2b/2c: codex / opencode. Like claude, these CLIs read the user's GLOBAL instruction/config
    # file (~/.codex, opencode global config), which pollutes the coach role the same way. There is
    # no single cross-agent isolation flag, so these are UNISOLATED baselines: override
    # new_cmd/resume_cmd in PERSONAL.md with your agent's "custom system-prompt / disable global
    # config" flag. See SKILL.md "Embedded Coach" -> per-agent isolation guidance.
    "codex": {
        "new_cmd": "codex exec {prompt}",
        "resume_cmd": "codex exec --resume {session_id} {prompt}",
    },
    "opencode": {
        "new_cmd": "opencode -p {prompt}",
        "resume_cmd": "opencode -p {prompt}",
    },
}

# PERSONAL.md field regex: <!-- coach:<key>: <value> -->
_PERSONAL_FIELD_RE = re.compile(r"<!--\s*coach:(\w+)\s*:\s*(.*?)\s*-->")


# ---------- config ----------

@dataclass
class CoachConfig:
    """Resolved coach configuration parsed from PERSONAL.md (with sensible defaults)."""

    user_agent: str = DEFAULT_USER_AGENT
    new_cmd: str = ""
    resume_cmd: str = ""
    timeout: int = DEFAULT_TIMEOUT
    max_context_events: int = DEFAULT_MAX_CONTEXT_EVENTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_agent": self.user_agent,
            "timeout": self.timeout,
            "max_context_events": self.max_context_events,
        }


def _read_tracker_targets() -> list[str]:
    """Recently-installed skill targets (~/.aigenora/skill_targets.json), newest last."""
    if not TRACKER_PATH.is_file():
        return []
    try:
        data = json.loads(TRACKER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [t.get("target") for t in data.get("targets", []) if isinstance(t, dict) and t.get("target")]


def _resolve_personal_md() -> Path | None:
    """Locate PERSONAL.md as the sibling of the most recently installed SKILL.md."""
    targets = _read_tracker_targets()  # names only; need paths -> re-read raw
    if not TRACKER_PATH.is_file():
        return None
    try:
        data = json.loads(TRACKER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    rows = [t for t in data.get("targets", []) if isinstance(t, dict) and t.get("path")]
    if not rows:
        return None
    personal = Path(rows[-1]["path"]).parent / PERSONAL_FILENAME
    return personal if personal.is_file() else None


def parse_personal_md(personal_path: str | Path | None) -> CoachConfig:
    """Parse coach:* fields from PERSONAL.md.

    Fallback chain for user_agent: PERSONAL.md coach:user_agent -> most recent target in
    ~/.aigenora/skill_targets.json -> DEFAULT_USER_AGENT (claude-code).
    Missing new_cmd/resume_cmd fall back to DEFAULT_CMDS[user_agent].
    """
    fields: dict[str, str] = {}
    path = Path(personal_path) if personal_path else _resolve_personal_md()
    if path and path.is_file():
        try:
            text = path.read_text(encoding="utf-8-sig")
            for key, value in _PERSONAL_FIELD_RE.findall(text):
                fields[key] = value
        except OSError:
            pass

    user_agent = fields.get("user_agent", "").strip()
    if user_agent not in DEFAULT_CMDS:
        targets = _read_tracker_targets()
        user_agent = next((t for t in reversed(targets) if t in DEFAULT_CMDS), DEFAULT_USER_AGENT)

    defaults = DEFAULT_CMDS.get(user_agent, DEFAULT_CMDS[DEFAULT_USER_AGENT])

    def _int_field(key: str, default: int) -> int:
        raw = fields.get(key)
        if not raw:
            return default
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return default

    return CoachConfig(
        user_agent=user_agent,
        new_cmd=fields.get("new_cmd") or defaults["new_cmd"],
        resume_cmd=fields.get("resume_cmd") or defaults["resume_cmd"],
        timeout=_int_field("timeout", DEFAULT_TIMEOUT),
        max_context_events=_int_field("max_context_events", DEFAULT_MAX_CONTEXT_EVENTS),
    )


# ---------- situation summarization (prompt injection, never symlink state_dir) ----------

def summarize_snapshot(state_dir: str | Path) -> str:
    """Compact one-line snapshot of the current game state for prompt injection."""
    try:
        snap = SnapshotBus(state_dir).read()
    except Exception:
        return ""
    if not isinstance(snap, dict) or not snap:
        return ""
    parts: list[str] = []
    for k in ("phase", "role", "round", "score", "summary"):
        v = snap.get(k)
        if v not in (None, "", [], {}):
            parts.append(f"{k}={v}")
    le = snap.get("last_event")
    if isinstance(le, dict):
        s = le.get("summary")
        if s:
            parts.append(f"last={s}")
    return ", ".join(parts)


def summarize_events(state_dir: str | Path, max_events: int) -> str:
    """Tail of recent events (one per line) for prompt injection.

    Filters out high-frequency decision-window bookkeeping (`local_decision_*`) before
    taking the tail so combat events (peer_joined / protocol_message reveal+round_result /
    game_over / session_ended) survive even during busy decision cycles. See
    `_COACH_NOISE_EVENT_TYPES` for the rationale.
    """
    try:
        events = EventBus(state_dir).read_events()
    except Exception:
        return ""
    if not events:
        return ""
    filtered = [
        e for e in events
        if isinstance(e, dict) and e.get("type") not in _COACH_NOISE_EVENT_TYPES
    ]
    if not filtered:
        return ""
    tail = filtered[-max_events:] if max_events > 0 else filtered
    lines: list[str] = []
    for e in tail:
        if not isinstance(e, dict):
            continue
        s = e.get("summary") or e.get("event") or e.get("type") or ""
        if not s:
            continue
        ts = e.get("ts", "")
        lines.append(f"- [{ts}] {s}" if ts else f"- {s}")
    return "\n".join(lines)


# ---------- command building (list-form Popen, shell=True forbidden) ----------

def build_cmd_list(
    template: str,
    *,
    session_id: str,
    prompt: str,
    coach_skill_text: str | None = None,
    coach_skill_file: str | None = None,
) -> list[str]:
    """Turn a PERSONAL.md command template into a list-form argv.

    {session_id}, {prompt}, {coach_skill_text}, {coach_skill_file} are substituted AFTER
    shlex.split, as individual argv elements, so the prompt and skill text (which may contain
    shell metacharacters / newlines) are never re-interpreted by a shell. This is the injection
    guard (ADR-5 security). {coach_skill_file} is preferred over {coach_skill_text} on Windows,
    where the npm .cmd shim corrupts multi-line argv (a file path is a single safe element).
    """
    tokens = shlex.split(template, posix=True)
    out: list[str] = []
    for tok in tokens:
        if tok == "{session_id}":
            out.append(session_id)
        elif tok == "{prompt}":
            out.append(prompt)
        elif tok == "{coach_skill_text}" and coach_skill_text is not None:
            out.append(coach_skill_text)
        elif tok == "{coach_skill_file}" and coach_skill_file is not None:
            out.append(coach_skill_file)
        else:
            repl = tok.replace("{session_id}", session_id).replace("{prompt}", prompt)
            if coach_skill_text is not None:
                repl = repl.replace("{coach_skill_text}", coach_skill_text)
            if coach_skill_file is not None:
                repl = repl.replace("{coach_skill_file}", coach_skill_file)
            out.append(repl)
    return out


def _resolve_bin(name: str) -> list[str] | None:
    """Resolve a command binary to an executable argv prefix (Windows-aware).

    npm-style CLIs ship as `.cmd`/`.bat` shims; Windows CreateProcess cannot execute them
    directly and does not consult PATHEXT, so they are wrapped with `cmd /c <full_path>`. The
    rest of the argv (including the {prompt}) remain separate list elements, so list2cmdline's
    quoting still protects against shell injection. Returns None if not on PATH.
    """
    resolved = shutil.which(name)
    if not resolved:
        return None
    if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", resolved]
    return [resolved]


# ---------- independent SSE streamer ----------

class CoachStreamer:
    """Independent SSE fan-out for coach events (NOT merged into the main /sse/stream).

    Each subscriber gets its own pending queue (list of (event, payload_json) tuples), matching
    the _Broadcaster pattern but simpler: no file polling — CoachWorker publishes directly.
    """

    def __init__(self, dialog: CoachDialog):
        self._dialog = dialog
        self._subs: list[list[tuple[str, str]]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> list[tuple[str, str]]:
        q: list[tuple[str, str]] = []
        with self._lock:
            self._subs.append(q)
        # Replay full dialog history so a fresh browser tab rebuilds the conversation.
        for d in self._dialog.read_all():
            q.append(("coach_history", json.dumps(d, ensure_ascii=False)))
        return q

    def unsubscribe(self, q: list[tuple[str, str]]) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: str, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            for q in self._subs:
                q.append((event, data))


# ---------- worker ----------

class CoachWorker:
    """Single-threaded serial consumer of the coach inbox.

    One Popen per turn + session-id resume. claude/codex session state machines are NOT
    concurrency-safe, so turns are strictly serialized (multi-tab concurrency is handled by a
    queue + "N ahead of you" feedback via coach_turn_start.queue_depth).

    Lifecycle:
        worker = CoachWorker(state_dir=<daemon root>, data_dir=<data_dir>, personal_md_path=...)
        worker.start()         # spawns the consumer thread
        worker.send("...")     # enqueue a user message; returns turn_id
        worker.streamer        # CoachStreamer for /api/coach/stream
        worker.reset()         # explicit "reset coach": cancel current/pending turns + clear dialog/session-id
        worker.stop()          # signal stop
        worker.wait_stopped()  # join the consumer thread (daemon exit)

    state_dir is the daemon root (parent); the effective business dir is re-resolved per turn
    via resolve_state_dir so injected snapshots follow parent -> child game switches.
    data_dir is the stable user data dir; coach_workspace (session + dialog + COACH_SKILL.md)
    lives under it so the cross-game conversation survives game switches and daemon restarts.
    """

    POLL_TIMEOUT = 0.5  # inbox.get timeout, so _stop is checked promptly

    def __init__(
        self,
        state_dir: str | Path,
        data_dir: str | Path,
        personal_md_path: str | Path | None = None,
    ):
        self.root_dir = Path(state_dir)
        self.data_dir = Path(data_dir)
        self.workspace = self.data_dir / "coach_workspace"
        self.session_file = self.workspace / "coach_session.json"
        self._config = parse_personal_md(personal_md_path)
        # Dialog lives in the workspace (cross-game stable), NOT in the per-game state_dir.
        self.dialog = CoachDialog(self.workspace)
        self.inbox = CoachInbox(self.workspace)
        self.streamer = CoachStreamer(self.dialog)
        self._inbox: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._session_lock = threading.Lock()  # guards session_file read/write + reset
        self._generation_lock = threading.Lock()
        self._generation = self.inbox.current_generation()
        self._proc_lock = threading.Lock()
        self._current_proc: subprocess.Popen | None = None
        self._coach_skill_text = self._load_coach_skill()

    # -- config / workspace --

    @property
    def config(self) -> CoachConfig:
        return self._config

    def config_view(self) -> dict[str, Any]:
        return self._config.to_dict()

    @property
    def coach_skill_path(self) -> Path:
        """Absolute path of the slimmed COACH_SKILL.md inside the workspace.

        Injected into the agent CLI via --system-prompt-file (claude-code default) /
        --append-system-prompt, or as a prompt-prefix fallback for agents without a
        system-prompt equivalent.
        """
        return self.workspace / COACH_SKILL_FILENAME

    def _load_coach_skill(self) -> str:
        """Ensure the slimmed COACH_SKILL.md exists in the workspace and return its text.

        The coach still works without it (just less constrained); a missing packaged file is
        non-fatal.
        """
        self.workspace.mkdir(parents=True, exist_ok=True)
        target = self.workspace / COACH_SKILL_FILENAME
        if not target.exists():
            try:
                res = resources.files("aigenora.skill").joinpath(COACH_SKILL_FILENAME)
                target.write_text(res.read_text(encoding="utf-8-sig"), encoding="utf-8")
            except (FileNotFoundError, TypeError, OSError):
                return ""
        try:
            return target.read_text(encoding="utf-8")
        except OSError:
            return ""

    # -- session id --

    def _read_session(self) -> dict[str, Any]:
        if not self.session_file.exists():
            return {}
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_session(self, data: dict[str, Any]) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        try:
            self.session_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def _current_session_id(self) -> str | None:
        with self._session_lock:
            return self._read_session().get("session_id")

    def _ensure_new_session_id(self) -> str:
        """Generate (and persist) a fresh session-id for a NEW conversation."""
        with self._session_lock:
            sid = str(uuid.uuid4())
            self._write_session(
                {"session_id": sid, "user_agent": self._config.user_agent, "created_ts": time.time()}
            )
            return sid

    def _clear_session(self) -> None:
        with self._session_lock:
            try:
                self.session_file.unlink()
            except OSError:
                pass

    # -- lifecycle --

    def start(self) -> None:
        if self._thread is None:
            for item in self.inbox.pending():
                self._inbox.put((item["turn_id"], item["text"], int(item.get("generation", 0))))
            self._thread = threading.Thread(target=self._loop, name="aigenora-coach", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._terminate_current_proc()

    def wait_stopped(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    # -- public actions --

    def send(self, user_text: str) -> str:
        """Enqueue a user message. Returns the turn_id (also surfaced via SSE)."""
        turn_id = uuid.uuid4().hex[:12]
        with self._generation_lock:
            generation = self._generation
        self.inbox.append(turn_id, user_text, generation)
        self._inbox.put((turn_id, user_text, generation))
        return turn_id

    def dialog_history(self) -> list[dict]:
        return self.dialog.read_all()

    def reset(self) -> None:
        """Explicit reset: cancel current/pending turns, clear dialog, and drop the session-id."""
        with self._generation_lock:
            self._generation += 1
            generation = self._generation
        self._terminate_current_proc()
        self._drain_inbox()
        self.inbox.reset(generation)
        self.dialog.clear()
        self._clear_session()
        self.streamer.publish("coach_reset", {"ts": time.time(), "generation": generation})

    # -- consumer loop --

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._inbox.get(timeout=self.POLL_TIMEOUT)
            except queue.Empty:
                continue
            if item is None:
                break
            turn_id, user_text, generation = item
            try:
                self._handle_turn(turn_id, user_text, generation)
            except Exception as exc:  # never let one turn kill the consumer
                self._fail(turn_id, f"coach worker error: {exc}", generation=generation)
                self.inbox.mark_done(turn_id)

    # -- one turn --

    def _effective_state_dir(self) -> Path:
        # delayed import: aigenora.agent.web imports this module at top level (circular otherwise)
        from aigenora.agent.web import resolve_state_dir
        return resolve_state_dir(self.root_dir)

    def _build_prompt(self, user_text: str) -> str:
        parts: list[str] = []
        # If the command template injects COACH_SKILL into the system-prompt layer
        # ({coach_skill_text} via --append-system-prompt, or {coach_skill_file} via
        # --system-prompt-file as claude-code does by default), don't duplicate it as a prompt
        # prefix. Otherwise (e.g. codex/opencode without a system-prompt equivalent) fall back to
        # a prompt prefix so the role-lock still reaches the model (NOTE: a prompt prefix is weaker
        # than a true system prompt and may be dominated by the agent's global config — see
        # SKILL.md "Embedded Coach").
        uses_system_inject = "{coach_skill_text}" in self._config.new_cmd or "{coach_skill_file}" in self._config.new_cmd
        if self._coach_skill_text and not uses_system_inject:
            parts.append(self._coach_skill_text.strip())
        eff = self._effective_state_dir()
        snap = summarize_snapshot(eff)
        evs = summarize_events(eff, self._config.max_context_events)
        situation_bits: list[str] = []
        if snap:
            situation_bits.append(snap)
        if evs:
            situation_bits.append("Recent events:\n" + evs)
        if situation_bits:
            parts.append("## Current situation\n" + "\n\n".join(situation_bits))
        parts.append("## User question\n" + user_text.strip())
        return "\n\n".join(parts)

    def _handle_turn(self, turn_id: str, user_text: str, generation: int) -> None:
        if not self._is_current_generation(generation):
            return
        # Record the user side and signal start (so the UI shows a loading state immediately).
        if not self._dialog_has(turn_id, "user"):
            self.dialog.append("user", user_text, turn_id=turn_id)
        self.streamer.publish(
            "coach_turn_start",
            {"turn_id": turn_id, "ts": time.time(), "queue_depth": self._inbox.qsize()},
        )

        prompt = self._build_prompt(user_text)
        sid = self._current_session_id()
        is_resume = bool(sid)
        if not sid:
            sid = self._ensure_new_session_id()

        template = self._config.resume_cmd if is_resume else self._config.new_cmd
        cmd = build_cmd_list(
            template,
            session_id=sid,
            prompt=prompt,
            coach_skill_text=self._coach_skill_text or None,
            coach_skill_file=str(self.coach_skill_path),
        )

        rc, err = self._run_streaming(turn_id, generation, cmd, prompt=prompt, template=template)
        if rc == 0:
            self.inbox.mark_done(turn_id)
            return  # _run_streaming already recorded the coach reply + coach_turn_done
        if rc == 130:
            # Reset/stop cancelled this turn; do not resurrect it as an error.
            return
        # Resume failed because the persisted session is gone (e.g. user cleared ~/.claude).
        if is_resume and err and "No conversation found" in err and self._is_current_generation(generation):
            self._clear_session()
            new_sid = self._ensure_new_session_id()
            cmd2 = build_cmd_list(
                self._config.new_cmd,
                session_id=new_sid,
                prompt=prompt,
                coach_skill_text=self._coach_skill_text or None,
                coach_skill_file=str(self.coach_skill_path),
            )
            self.streamer.publish(
                "coach_context_reset",
                {"turn_id": turn_id, "reason": "session_lost", "ts": time.time()},
            )
            rc2, err2 = self._run_streaming(turn_id, generation, cmd2, prompt=prompt, template=self._config.new_cmd)
            if rc2 != 0:
                if not (rc2 == 130 and not self._is_current_generation(generation)):
                    self._fail(turn_id, err2 or "coach subprocess failed", generation=generation)
                    self.inbox.mark_done(turn_id)
            else:
                self.inbox.mark_done(turn_id)
            return
        self._fail(turn_id, err or f"coach subprocess exited with code {rc}", generation=generation)
        self.inbox.mark_done(turn_id)

    def _run_streaming(
        self,
        turn_id: str,
        generation: int,
        cmd: list[str],
        *,
        prompt: str | None = None,
        template: str = "",
    ) -> tuple[int, str]:
        """Popen the coach CLI, stream stdout line-by-line, record the reply.

        Returns (returncode, stderr_text). On success the coach reply is appended to the dialog
        and coach_turn_done is published. On failure the caller decides (retry-as-new or fail).

        If `template` has no {prompt} placeholder, the prompt is fed via STDIN instead of argv
        (claude-code on Windows: the .cmd shim corrupts multi-line argv and would drop the
        situation/user-question text; -p with no arg reads the prompt from stdin).
        """
        start = time.monotonic()
        timeout = max(5.0, float(self._config.timeout))
        uses_stdin_prompt = bool(prompt) and "{prompt}" not in template
        # Resolve the binary: Windows .cmd/.bat shims (npm CLIs) cannot be run by CreateProcess
        # directly and CreateProcess ignores PATHEXT, so wrap with `cmd /c <full_path>`. When the
        # prompt is an argv element, list2cmdline quoting preserves injection safety.
        resolved = _resolve_bin(cmd[0])
        if resolved is None:
            return (127, "binary not found")
        cmd = resolved + cmd[1:]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.workspace),
                stdin=subprocess.PIPE if uses_stdin_prompt else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            return (127, "binary not found")
        except OSError as exc:
            return (1, str(exc))
        # Feed the prompt via stdin (EOF on close) when the template has no {prompt} placeholder.
        if uses_stdin_prompt and proc.stdin is not None:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        with self._proc_lock:
            self._current_proc = proc

        # Drain stderr on a separate thread so a large stderr never deadlocks the stdout pipe.
        err_buf: list[str] = []

        def _drain_err() -> None:
            assert proc.stderr is not None
            for line in iter(proc.stderr.readline, ""):
                err_buf.append(line)

        threading.Thread(target=_drain_err, name="aigenora-coach-stderr", daemon=True).start()

        # stdout is drained on a thread too, so the main loop can enforce a wall-clock timeout
        # even while the model is thinking (no stdout for many seconds). queue.get with a 1s
        # timeout keeps stop/timeout responsive on Windows (select() cannot poll pipes here).
        out_q: queue.Queue = queue.Queue()

        def _drain_out() -> None:
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ""):
                out_q.put(line)
            out_q.put(None)  # EOF sentinel

        out_thread = threading.Thread(target=_drain_out, name="aigenora-coach-stdout", daemon=True)
        out_thread.start()

        collected: list[str] = []
        timed_out = False
        stopped = False
        while True:
            if self._stop.is_set():
                stopped = True
                self._safe_terminate(proc)
                break
            if not self._is_current_generation(generation):
                self._safe_terminate(proc)
                break
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                timed_out = True
                self._safe_terminate(proc)
                break
            try:
                line = out_q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                break  # EOF
            collected.append(line)
            if self._is_current_generation(generation):
                self.streamer.publish("coach_chunk", {"turn_id": turn_id, "text": line})

        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._safe_terminate(proc, kill=True)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        finally:
            with self._proc_lock:
                if self._current_proc is proc:
                    self._current_proc = None
        out_thread.join(timeout=2.0)
        err = "".join(err_buf).strip()

        if stopped or not self._is_current_generation(generation):
            return (130, "cancelled")
        if timed_out:
            return (124, "timeout")
        if proc.returncode != 0:
            return (proc.returncode, err)

        reply = "".join(collected).strip()
        sid = self._current_session_id()
        self.dialog.append("coach", reply, turn_id=turn_id, session_id=sid)
        self.streamer.publish(
            "coach_turn_done",
            {"turn_id": turn_id, "text": reply, "session_id": sid, "ts": time.time()},
        )
        return (0, err)

    def _is_current_generation(self, generation: int) -> bool:
        with self._generation_lock:
            return generation == self._generation

    def _drain_inbox(self) -> None:
        while True:
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                break

    def _terminate_current_proc(self) -> None:
        with self._proc_lock:
            proc = self._current_proc
        if proc is not None and proc.poll() is None:
            self._safe_terminate(proc)

    def _dialog_has(self, turn_id: str, role: str) -> bool:
        for row in self.dialog.read_all():
            if row.get("turn_id") == turn_id and row.get("role") == role:
                return True
        return False

    @staticmethod
    def _safe_terminate(proc: subprocess.Popen, *, kill: bool = False) -> None:
        try:
            (proc.kill if kill else proc.terminate)()
        except OSError:
            pass

    def _fail(self, turn_id: str, message: str, *, generation: int | None = None) -> None:
        if generation is not None and not self._is_current_generation(generation):
            return
        self.dialog.append("error", message, turn_id=turn_id)
        self.streamer.publish(
            "coach_turn_error",
            {"turn_id": turn_id, "error": message, "ts": time.time()},
        )
