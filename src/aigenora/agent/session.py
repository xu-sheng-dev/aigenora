from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from aigenora.engine.config import get_server
from aigenora.engine.keys import load_keys
from aigenora.engine.rest import RestClient
from aigenora.proto.decide_gateway import submit_decision
from aigenora.proto.sdk import DecisionBus, DetailLog, EventBus, SnapshotBus, StrategyStore


_GOVERNANCE_STRING_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness probe (side-effect free, never signals target process).

    POSIX: os.kill(pid, 0) is query-only; PermissionError is treated as alive
            (access denied = process exists).
    Windows: ctypes calls OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) +
            GetExitCodeProcess, comparing against STILL_ACTIVE=259. On Windows,
            os.kill(pid, 0) actually sends signals like CTRL_C_EVENT/CTRL_BREAK_EVENT
            and may kill the target by mistake — **must never be used**.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = wintypes.DWORD(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except OSError:
            return False
    # POSIX
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True  # process exists but no permission to signal
    except (OSError, ProcessLookupError):
        return False
    return True


def _parse_governance_string_array(raw: str, option_name: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{option_name} must be a JSON string array") from exc
    if not isinstance(value, list):
        raise ValueError(f"{option_name} must be a JSON string array")
    if len(value) > 64:
        raise ValueError(f"{option_name} must contain at most 64 items")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{option_name}[{index}] must be a string")
        if not item or len(item) > 64 or _GOVERNANCE_STRING_RE.fullmatch(item) is None:
            raise ValueError(
                f"{option_name}[{index}] must be 1-64 chars matching [A-Za-z0-9_.:-]+"
            )
    return value


def _format_governance_string_array(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def session_get(args) -> int:
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    data = client.json("GET", f"/api/v1/sessions/{args.session_id}", expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"session_id: {data.get('session_id', '')}")
        print(f"status: {data.get('status', '')}")
        print(f"protocol_id: {data.get('protocol_id', '')[:16]}...")
        print(f"host: {data.get('host_public_key', '')[:16]}...")
        print(f"guest: {data.get('guest_public_key', '')[:16]}...")
    return 0


def session_status(args) -> int:
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    payload = {"status": args.status}
    # v010 M5 ELO：游戏类 session close 时声明 winner（host/guest/draw），触发双方排位更新。
    if getattr(args, "winner", None):
        payload["winner"] = args.winner
    data = client.json("POST", f"/api/v1/sessions/{args.session_id}/status", payload, expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"[OK] session {args.session_id} status updated to {args.status}")
    return 0


def session_transport_get(args) -> int:
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    data = client.json("GET", f"/api/v1/sessions/{args.session_id}/transport", expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"transport: {json.dumps(data, ensure_ascii=False)}")
    return 0


def session_transport_update(args) -> int:
    from aigenora.engine.crypto import transport_binding_canonical
    from aigenora.engine.keys import sign_raw

    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    # Get session to find protocol_id and participants
    session = client.json("GET", f"/api/v1/sessions/{args.session_id}", expected={200})
    canonical = transport_binding_canonical(kp.public_key, "iroh", args.iroh_ticket, session.get("protocol_id", ""))
    signature = sign_raw(kp.private_key, canonical.encode("utf-8"))
    payload = {
        "iroh_ticket": args.iroh_ticket,
        "transport_binding_signature": signature,
    }
    data = client.json("PATCH", f"/api/v1/sessions/{args.session_id}/transport", payload, expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"[OK] transport updated for session {args.session_id}")
    return 0


def governance_get(args) -> int:
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    data = client.json("GET", f"/api/v1/protocols/{args.protocol_id}/governance", expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"protocol_id: {args.protocol_id[:16]}...")
        print(f"family: {data.get('family', '')}")
        print(f"status: {data.get('status', '')}")
        if data.get("parent_protocol_id"):
            print(f"parent: {data['parent_protocol_id'][:16]}...")
        if data.get("capabilities"):
            print(f"capabilities: {_format_governance_string_array(data['capabilities'])}")
        if data.get("tags"):
            print(f"tags: {_format_governance_string_array(data['tags'])}")
    return 0


def governance_set(args) -> int:
    payload: dict = {
        "family": args.family,
        "status": args.status,
    }
    if getattr(args, "parent_protocol_id", None):
        payload["parent_protocol_id"] = args.parent_protocol_id
    if getattr(args, "capabilities", None):
        try:
            payload["capabilities"] = _parse_governance_string_array(args.capabilities, "--capabilities")
        except ValueError as exc:
            print(f"error: {exc}")
            return 2
    if getattr(args, "tags", None):
        try:
            payload["tags"] = _parse_governance_string_array(args.tags, "--tags")
        except ValueError as exc:
            print(f"error: {exc}")
            return 2
    if getattr(args, "created_reason", None):
        payload["created_reason"] = args.created_reason
    if getattr(args, "deprecated_reason", None):
        payload["deprecated_reason"] = args.deprecated_reason
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    data = client.json("POST", f"/api/v1/protocols/{args.protocol_id}/governance", payload, expected={200, 201})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"[OK] governance set for {args.protocol_id[:16]}...")
    return 0


def protocol_stats(args) -> int:
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    data = client.json("GET", f"/api/v1/protocols/{args.protocol_id}/stats", expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"protocol_id: {args.protocol_id[:16]}...")
        print(f"posts: {data.get('post_count', '?')}")
        print(f"sessions: {data.get('session_count', '?')}")
        print(f"avg_rating: {data.get('avg_rating', '?')}")
    return 0


def agent_stats(args) -> int:
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    data = client.json("GET", f"/api/v1/agents/{args.agent_id}/stats", expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"agent_id: {args.agent_id}")
        print(f"total_sessions: {data.get('total_sessions', '?')}")
        print(f"successful_sessions: {data.get('successful_sessions', '?')}")
        print(f"success_rate: {data.get('success_rate', '?')}")
        print(f"weighted_score: {data.get('weighted_score', '?')}")
        print(f"confidence_level: {data.get('confidence_level', '?')}")
    return 0


def _format_event(e: dict) -> str:
    ts = e.get("ts", "")[11:19]  # HH:MM:SS
    etype = e.get("type", "?")
    summary = e.get("summary")
    data = e.get("data", {})
    if etype == "invite_created":
        return f"[{ts}] invite_created: post_id={data.get('post_id', '?')[:12]}..."
    if etype == "peer_joined":
        return f"[{ts}] peer_joined: session_id={data.get('session_id', '?')[:12]}..."
    if etype == "protocol_message":
        d = data.get("direction", "?")
        msg = data.get("msg", {})
        action = msg.get("action", "?")
        if summary:
            return f"[{ts}] {d}: {action} | {summary}"
        return f"[{ts}] {d}: {action}"
    if etype == "session_ended":
        reason = data.get("reason", "")
        return f"[{ts}] session_ended: {reason}"
    if etype == "peer_unresponsive":
        return f"[{ts}] peer_unresponsive: {data.get('elapsed', '?')}s no response"
    if etype == "peer_resumed":
        return f"[{ts}] peer_resumed: peer reconnected"
    return f"[{ts}] {etype}: {json.dumps(data, ensure_ascii=False) if data else ''}"


def _resolve_state_dir(state_dir: str) -> Path:
    """Resolve the session state directory.

    In daemon mode there are two layers of directories:
      parent/  ← session.json, events.jsonl (daemon outputs this path)
      parent/host-TIMESTAMP/  ← snapshot.json, details.jsonl, strategy.json (used by hooks)

    If the passed path is parent (contains session.json but no snapshot.json),
    automatically locate the newest child subdirectory (host-*/ or guest-*/).
    """
    p = Path(state_dir)
    if (p / "snapshot.json").exists() or (p / "details.jsonl").exists():
        return p
    if (p / "session.json").exists():
        children = sorted(
            [d for d in p.iterdir() if d.is_dir() and (d.name.startswith("host-") or d.name.startswith("guest-"))],
            key=lambda d: d.name,
        )
        if children:
            return children[-1]
    return p


def cmd_events(args) -> int:
    bus = EventBus(args.state_dir)
    last_ts = None
    if not args.follow:
        events = bus.read_events()
        for e in events:
            if getattr(args, "json_output", False):
                print(json.dumps(e, ensure_ascii=False))
            else:
                print(_format_event(e))
        return 0
    while True:
        events = bus.read_events(after_ts=last_ts)
        for e in events:
            if getattr(args, "json_output", False):
                print(json.dumps(e, ensure_ascii=False), flush=True)
            else:
                print(_format_event(e), flush=True)
            last_ts = e.get("ts", "")
        time.sleep(0.5)


def cmd_decide(args) -> int:
    decision = json.loads(args.decision)
    state_dir = str(_resolve_state_dir(args.state_dir))
    result = submit_decision(
        state_dir, decision,
        origin="cli",
        require_match_key=False,
    )
    if not result["ok"]:
        print(json.dumps({
            "status": result["status"],
            "reason": result["reason"],
            "match_key": result["match_key"],
            "match_value": result["match_value"],
        }, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "ok", "decision": decision}, ensure_ascii=False))
    return 0


def cmd_snapshot(args) -> int:
    snap = SnapshotBus(str(_resolve_state_dir(args.state_dir))).read()
    if getattr(args, "json_output", False):
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0
    if not snap:
        print("(snapshot not initialized)")
        return 0
    phase = snap.get("phase", "?")
    role = snap.get("role", "?")
    print(f"phase: {phase}")
    print(f"role: {role}")
    if snap.get("protocol_name"):
        print(f"protocol: {snap['protocol_name']}")
    if "score" in snap:
        print(f"score: {json.dumps(snap['score'], ensure_ascii=False)}")
    if "round" in snap:
        print(f"round: {snap['round']}")
    last = snap.get("last_event") or {}
    if last.get("summary"):
        print(f"last_event: {last['summary']}")
    if snap.get("updated_at"):
        print(f"updated_at: {snap['updated_at']}")
    return 0


def cmd_details(args) -> int:
    log = DetailLog(str(_resolve_state_dir(args.state_dir)))
    if not args.follow:
        for entry in log.read_all():
            if getattr(args, "json_output", False):
                print(json.dumps(entry, ensure_ascii=False))
            else:
                ts = entry.get("ts", "")[11:19]
                summary = entry.get("summary") or json.dumps(
                    {k: v for k, v in entry.items() if k not in ("ts", "summary")},
                    ensure_ascii=False,
                )
                print(f"[{ts}] {summary}")
        return 0
    last_ts = None
    while True:
        items = log.read_all()
        for entry in items:
            ts = entry.get("ts", "")
            if last_ts is not None and ts <= last_ts:
                continue
            if getattr(args, "json_output", False):
                print(json.dumps(entry, ensure_ascii=False), flush=True)
            else:
                summary = entry.get("summary") or json.dumps(
                    {k: v for k, v in entry.items() if k not in ("ts", "summary")},
                    ensure_ascii=False,
                )
                print(f"[{ts[11:19]}] {summary}", flush=True)
            last_ts = ts
        time.sleep(0.5)


def cmd_strategy(args) -> int:
    store = StrategyStore(str(_resolve_state_dir(args.state_dir)))
    set_value = getattr(args, "set_value", None)
    merge_value = getattr(args, "merge_value", None)
    if set_value and merge_value:
        print("error: --set and --merge are mutually exclusive", flush=True)
        return 2
    if set_value is not None:
        payload = json.loads(set_value)
        if not isinstance(payload, dict):
            print("error: --set value must be a JSON object", flush=True)
            return 2
        store.write(payload)
    elif merge_value is not None:
        patch = json.loads(merge_value)
        if not isinstance(patch, dict):
            print("error: --merge value must be a JSON object", flush=True)
            return 2
        store.merge(patch)
    data = store.read()
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if not data:
            print("(strategy is empty)")
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_abort(args) -> int:
    """Actively disconnect a running daemon session.

    Steps:
      1. Resolve state_dir (accepts daemon parent directory), read pid from session.json
      2. Kill the process (Windows: taskkill /F /PID; POSIX: os.kill(pid, SIGTERM))
      3. Append a session_ended event with reason=<reason> to events.jsonl
      4. Update session.json status to aborted
    """
    import signal
    import subprocess
    import sys as _sys

    state_dir = Path(args.state_dir)
    session_file = state_dir / "session.json"
    if not session_file.exists():
        print(json.dumps({"status": "error", "error": "session.json not found",
                          "state_dir": str(state_dir)}, ensure_ascii=False))
        return 2
    meta = json.loads(session_file.read_text(encoding="utf-8"))
    pid = meta.get("pid")
    reason = getattr(args, "reason", "aborted_by_agent") or "aborted_by_agent"
    killed = False
    err: str | None = None
    if pid:
        try:
            if _sys.platform == "win32":
                proc = subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, text=True, timeout=10,
                )
                killed = proc.returncode == 0
                if not killed:
                    err = (proc.stderr or proc.stdout or "").strip()
            else:
                os.kill(int(pid), signal.SIGTERM)
                killed = True
        except (OSError, ProcessLookupError) as exc:
            err = str(exc)
    # events written to parent state_dir (consistent with daemon behavior)
    try:
        EventBus(str(state_dir)).emit(
            "session_ended",
            data={"reason": reason, "pid": pid, "killed": killed},
            summary=f"Session aborted by agent ({reason})",
        )
    except Exception as exc:
        err = err or str(exc)
    meta["status"] = "aborted"
    meta["aborted_at"] = time.time()
    meta["abort_reason"] = reason
    session_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    out = {
        "status": "aborted",
        "pid": pid,
        "killed": killed,
        "reason": reason,
        "state_dir": str(state_dir),
    }
    if err:
        out["warning"] = err
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _check_daemon_alive(state_dir: Path, meta: dict) -> dict:
    """Probe whether the daemon PID is alive; if dead, infer status from daemon.err.log:
      - log contains traceback keyword → 'crashed', and write the last 500 bytes of
        the log to last_error_excerpt
      - otherwise → 'stopped'
    The new status is written back to session.json, and a daemon_died event is emitted
    to events.jsonl.
    Returns the updated meta (dict).
    """
    pid = meta.get("pid")
    status = meta.get("status", "?")
    if not pid or status not in {"running", "starting"}:
        return meta
    if _pid_alive(int(pid)):
        return meta  # still alive
    # process is dead, determine crashed vs stopped
    err_log = state_dir / "daemon.err.log"
    excerpt = ""
    crashed = False
    if err_log.exists() and err_log.stat().st_size > 0:
        try:
            with open(err_log, "rb") as f:
                size = err_log.stat().st_size
                if size > 500:
                    f.seek(size - 500)
                excerpt_bytes = f.read()
            excerpt = excerpt_bytes.decode("utf-8", errors="replace")
            if any(kw in excerpt for kw in ("Traceback", "Error", "Exception")):
                crashed = True
        except OSError:
            pass
    new_status = "crashed" if crashed else "stopped"
    if meta.get("status") != new_status:
        meta["status"] = new_status
        if excerpt:
            meta["last_error_excerpt"] = excerpt
        try:
            (state_dir / "session.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        try:
            EventBus(str(state_dir)).emit(
                "daemon_died",
                data={
                    "pid": pid,
                    "reason": "crashed_with_log" if crashed else "missing_no_log",
                    "last_error_excerpt": excerpt or None,
                },
                summary=f"Daemon process {pid} died ({new_status})",
            )
        except OSError:
            pass
    return meta


def cmd_logs(args) -> int:
    """Tail daemon stderr/stdout logs.

    The daemon subprocess writes stdout/stderr to <state_dir>/daemon.out.log /
    daemon.err.log. Defaults to --err --tail 50.
    """
    state_dir = Path(args.state_dir)
    which = "err" if getattr(args, "err", False) or not getattr(args, "out", False) else "out"
    log_file = state_dir / f"daemon.{which}.log"
    if not log_file.exists():
        print(f"(no daemon.{which}.log at {state_dir})")
        return 0
    n = getattr(args, "tail", 50)
    if n is None:
        n = 50
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error reading {log_file}: {exc}")
        return 2
    lines = text.splitlines()
    if n > 0:
        lines = lines[-n:]
    for line in lines:
        print(line)
    return 0


def cmd_list(args) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else Path.cwd() / ".aigenora"
    sessions_dir = data_dir / "sessions"
    if not sessions_dir.exists():
        print("No sessions found.")
        return 0
    results = []
    for d in sorted(sessions_dir.iterdir()):
        meta_file = d / "session.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pid = meta.get("pid")
        # check if process still alive — if dead, update status and emit daemon_died event
        meta = _check_daemon_alive(d, meta)
        status = meta.get("status", "?")
        role = meta.get("role", "?")
        post_id = meta.get("post_id", "")
        started = meta.get("started_at", 0)
        results.append({"dir": str(d), "role": role, "status": status, "pid": pid, "post_id": post_id, "started_at": started})
    if getattr(args, "json_output", False):
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            ts = time.strftime("%H:%M:%S", time.localtime(r["started_at"])) if r["started_at"] else "?"
            pid_str = str(r["pid"]) if r["pid"] else "?"
            print(f"{r['role']:5s}  {r['status']:8s}  pid={pid_str:<8s}  {ts}  {r['dir']}")
    return 0
