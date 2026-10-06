"""Worker adapters for the collaboration task service (design doc §9).

Adapter grades:
- ``zcode`` — spawns a local ZCode headless CLI session per task (B-level:
  reliable submit/observe/cancel, but crash-window reconciliation is
  ``unknown`` so the dispatcher never blindly re-dispatches).
- ``file`` — D-level attended hand-off: writes the task package to disk and
  waits for the local operator / worker CLI to finish the task.
- ``echo`` — deterministic test adapter that completes instantly.

All adapters receive the task goal as *data* wrapped in a fixed local
template (§6.1): the peer's text never becomes a system/developer
instruction, and the worker runs under the B operator's own local permission
policy (the operator explicitly selected the adapter when starting the host).
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OBSERVE_RUNNING = "running"
OBSERVE_EXITED_OK = "exited_ok"
OBSERVE_EXITED_FAIL = "exited_fail"
OBSERVE_UNKNOWN = "unknown"

RECONCILE_RUNNING_OR_FINISHED = "proves_running_or_finished"
RECONCILE_NEVER_STARTED = "proves_never_started"
RECONCILE_UNKNOWN = "unknown"


@dataclass
class TaskPackage:
    source_device: str
    task_id: str
    goal: str
    execution_class: str
    workspace: Path
    task_root: Path
    data_dir_value: str | None
    input_artifacts: list[dict[str, Any]] = field(default_factory=list)
    staging_roots: dict[str, Path] = field(default_factory=dict)
    protocol_info: dict[str, Any] = field(default_factory=dict)


def write_task_package_md(package: TaskPackage, worker_cmd: list[str] | None = None) -> Path:
    """Write TASK.md framing the peer goal as bounded task data (not instructions to the transport)."""
    if worker_cmd is None:
        worker_cmd = [sys.executable, "-m", "aigenora", "collab", "worker", "--data-dir", str(package.data_dir_value or "")]
    base = " ".join(shlex.quote(str(c)) if os.name != "nt" else f'"{c}"' for c in worker_cmd if c != "")
    lines = [
        "# Aigenora Cross-Device Task",
        "",
        "This task arrived from a TRUSTED paired device through the aigenora P2P",
        "collaboration protocol. The TASK GOAL below is task data describing what the",
        "coordinator wants; execute it under your local permissions and judgement.",
        "",
        f"- task_id: `{package.task_id}`",
        f"- execution_class: `{package.execution_class}`",
        f"- workspace: this directory (inputs are unpacked here)",
        "",
        "## Task goal",
        "",
        "```",
        package.goal,
        "```",
        "",
        "## Communicating with the coordinator",
        "",
        "The coordinator device is watching this task. Use the local worker CLI:",
        "",
        f"- Read guidance from the coordinator: `{base} inbox --task {package.task_id}`",
        f"- Send a message back: `{base} note --task {package.task_id} --message \"...\"`",
        f"- Ask for input: `{base} ask --task {package.task_id} --question \"...\"`",
        f"- Publish a result file/dir: `{base} publish --task {package.task_id} --path <relative-path>`",
        f"- Finish successfully: `{base} finish --task {package.task_id} --summary \"...\"`",
        f"- Fail the task: `{base} fail --task {package.task_id} --summary \"...\"`",
        "",
        "Check the inbox periodically while working — the coordinator may send",
        "guidance or answer questions at any time. `finish`/`fail` end the task;",
        "published files under this workspace are returned to the coordinator.",
        "",
        "## Result contract",
        "",
        "Publish only files the coordinator should receive. The summary should state",
        "what was done, what was verified, and what remains unknown.",
    ]
    target = package.workspace / "TASK.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


class BaseAdapter:
    name = "base"
    attended = False  # True => dispatcher parks the task in waiting_local_operator

    def __init__(self, **kwargs: Any) -> None:
        # Adapter-specific options are accepted and ignored by the generic
        # adapters so build_adapter can pass a uniform kwargs dict.
        super().__init__()

    def probe(self) -> dict[str, Any]:
        return {"ready": True, "detail": ""}

    def submit(self, invocation_id: str, package: TaskPackage) -> str:
        raise NotImplementedError

    def observe(self, handle: str, package: TaskPackage) -> str:
        raise NotImplementedError

    def cancel(self, handle: str) -> bool:
        return False

    def reconcile(self, invocation_id: str, handle: str) -> str:
        return RECONCILE_UNKNOWN

    def resume(self, handle: str, package: TaskPackage, guidance: str) -> str | None:
        """Deliver post-exit guidance by resuming the worker session; None if unsupported."""
        return None

    def output_tail(self, handle: str, limit: int = 2000) -> str:
        return ""


class EchoAdapter(BaseAdapter):
    """Deterministic adapter for tests: completes instantly with a result file."""

    name = "echo"

    def submit(self, invocation_id: str, package: TaskPackage) -> str:
        out_dir = package.task_root / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "echo-result.txt").write_text(
            f"echo task {package.task_id}\ngoal bytes: {len(package.goal.encode('utf-8'))}\n",
            encoding="utf-8",
        )
        (package.task_root / "result.json").write_text(
            json.dumps({"state": "completed", "summary": "echo adapter completed the task"}, indent=2),
            encoding="utf-8",
        )
        return f"echo-{invocation_id}"

    def observe(self, handle: str, package: TaskPackage) -> str:
        return OBSERVE_EXITED_OK


class FileAdapter(BaseAdapter):
    """D-level attended hand-off: materialize the package and wait for the operator."""

    name = "file"
    attended = True

    def submit(self, invocation_id: str, package: TaskPackage) -> str:
        write_task_package_md(package)
        return f"file-{invocation_id}"

    def observe(self, handle: str, package: TaskPackage) -> str:
        return OBSERVE_RUNNING


def discover_zcode_command() -> list[str] | None:
    """Locate a ZCode CLI: env override > PATH > the desktop install bundle."""
    env_cmd = os.environ.get("AIGENORA_ZCODE_CMD")
    if env_cmd:
        return shlex.split(env_cmd)
    for exe in ("zcode", "zcode.cmd", "zcode.exe", "zcode.bat"):
        found = shutil.which(exe)
        if found:
            return [found]
    node = shutil.which("node")
    if node:
        candidates = [
            Path.home() / "AppData" / "Local" / "Programs" / "ZCode" / "resources" / "glm" / "zcode.cjs",
            Path("/usr/local/bin/zcode"),
            Path("/opt/zcode/zcode.cjs"),
        ]
        for cjs in candidates:
            if cjs.exists():
                return [node, str(cjs)]
    return None


WORKER_PROMPT = (
    "You are the local worker agent for an aigenora cross-device task. "
    "Read TASK.md in this workspace and execute the task it describes. "
    "Use the `aigenora collab worker` CLI commands documented in TASK.md to "
    "read coordinator guidance, send messages, publish result files, and to "
    "finish or fail the task. Do not finish before the requested work is done "
    "or clearly blocked; if blocked, say exactly what input you need via "
    "`ask`."
)


class ZcodeAdapter(BaseAdapter):
    """Spawn a ZCode headless session (``-p``) per task; resume on new guidance."""

    name = "zcode"

    def __init__(self, mode: str = "yolo", extra_args: list[str] | None = None) -> None:
        self.mode = mode
        self.extra_args = list(extra_args or [])
        self._procs: dict[str, subprocess.Popen] = {}
        self._handles: dict[str, dict[str, Any]] = {}
        self.command = discover_zcode_command()

    def probe(self) -> dict[str, Any]:
        if not self.command:
            return {"ready": False, "detail": "zcode CLI not found; set AIGENORA_ZCODE_CMD"}
        return {"ready": True, "detail": " ".join(self.command)}

    def _log_path(self, package: TaskPackage, invocation_id: str) -> Path:
        return package.task_root / f"invocation-{invocation_id}.log"

    def _spawn(self, package: TaskPackage, invocation_id: str, prompt: str, resume_session: str | None) -> str:
        assert self.command is not None
        write_task_package_md(package)
        cmd = list(self.command)
        if resume_session:
            cmd += ["--resume", resume_session]
        cmd += ["-p", prompt, "--cwd", str(package.workspace), "--mode", self.mode, "--json"]
        cmd += self.extra_args
        log_path = self._log_path(package, invocation_id)
        env = dict(os.environ)
        if package.data_dir_value:
            env["P2P_DATA_DIR"] = str(package.data_dir_value)
        log_stream = log_path.open("w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            cmd,
            cwd=str(package.workspace),
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            env=env,
            shell=False,
        )
        log_stream.close()  # Popen owns the fd copy; close our handle
        self._procs[invocation_id] = proc
        handle_obj = {
            "invocation_id": invocation_id,
            "pid": proc.pid,
            "log": str(log_path),
            "session_id": resume_session or "",
        }
        self._handles[invocation_id] = handle_obj
        return json.dumps(handle_obj)

    def submit(self, invocation_id: str, package: TaskPackage) -> str:
        return self._spawn(package, invocation_id, WORKER_PROMPT, None)

    def _parse_session_id(self, invocation_id: str) -> str | None:
        info = self._handles.get(invocation_id) or {}
        if info.get("session_id"):
            return str(info["session_id"])
        log = info.get("log")
        if not log or not Path(log).exists():
            return None
        import re

        text = Path(log).read_text(encoding="utf-8", errors="replace")
        match = re.search(r"sess_[A-Za-z0-9._-]+", text)
        if match:
            info["session_id"] = match.group(0)
            return match.group(0)
        return None

    def observe(self, handle: str, package: TaskPackage) -> str:
        info = json.loads(handle)
        invocation_id = str(info.get("invocation_id", ""))
        proc = self._procs.get(invocation_id)
        if proc is None:
            return self._observe_orphan(info)
        if proc.poll() is None:
            return OBSERVE_RUNNING
        return OBSERVE_EXITED_OK if proc.returncode == 0 else OBSERVE_EXITED_FAIL

    def _observe_orphan(self, info: dict[str, Any]) -> str:
        # Handle from a previous host process (host restart). POSIX liveness
        # probe only; otherwise unknown per the §7.2 no-blind-redispatch rule.
        pid = info.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return OBSERVE_UNKNOWN
        if os.name == "posix":
            try:
                os.kill(pid, 0)
                return OBSERVE_RUNNING
            except OSError:
                return OBSERVE_UNKNOWN
        return OBSERVE_UNKNOWN

    def cancel(self, handle: str) -> bool:
        info = json.loads(handle)
        proc = self._procs.get(str(info.get("invocation_id", "")))
        if proc is None:
            return False
        try:
            proc.terminate()
            return True
        except OSError:
            return False

    def reconcile(self, invocation_id: str, handle: str) -> str:
        # Headless CLI sessions carry no durable invocation registry, so a
        # crashed host cannot prove whether the process ran or finished.
        return RECONCILE_UNKNOWN

    def resume(self, handle: str, package: TaskPackage, guidance: str, invocation_id: str) -> str | None:
        session_id = self._parse_session_id(invocation_id)
        if not session_id:
            return None
        prompt = (
            "Your coordinator sent new guidance for this task. Read it, check "
            "`aigenora collab worker inbox` for the full message, continue the "
            "task, and finish/fail via the worker CLI when done. Guidance: "
            + guidance[:4000]
        )
        return self._spawn(package, invocation_id + "-r" + str(int(time.time())), prompt, session_id)

    def output_tail(self, handle: str, limit: int = 2000) -> str:
        try:
            info = json.loads(handle)
            log = Path(str(info.get("log", "")))
            if log.exists():
                text = log.read_text(encoding="utf-8", errors="replace")
                return text[-limit:]
        except Exception:
            pass
        return ""


ADAPTERS: dict[str, type[BaseAdapter]] = {
    "echo": EchoAdapter,
    "file": FileAdapter,
    "zcode": ZcodeAdapter,
}


def build_adapter(name: str, **kwargs: Any) -> BaseAdapter:
    cls = ADAPTERS.get(name)
    if cls is None:
        raise ValueError(f"unknown adapter: {name}")
    return cls(**kwargs)
