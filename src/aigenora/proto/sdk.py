from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aigenora.engine.crypto import commit_hash, verify_commit


class StateStore:
    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir)
        self.path.mkdir(parents=True, exist_ok=True)

    def write(self, key: str, value: str | int) -> None:
        (self.path / key).write_text(str(value), encoding="utf-8")

    def read(self, key: str, default: str | None = None) -> str | None:
        path = self.path / key
        if not path.exists():
            return default
        return path.read_text(encoding="utf-8")

    def read_int(self, key: str, default: int = 0) -> int:
        value = self.read(key)
        return default if value is None or value == "" else int(value)


def _replace_with_retry(tmp: Path, target: Path, attempts: int = 6) -> None:
    """os.replace with brief retry on Windows PermissionError.

    On Windows, replacing a file that another handle is reading (e.g. the webui
    broadcast thread reading snapshot.json concurrently with hooks writing it via
    SnapshotBus.update) fails with PermissionError [WinError 5]. The read holds the
    file only briefly, so a short exponential backoff retries through the read
    window. On POSIX os.replace is atomic and lock-free, so the first attempt always
    succeeds and the retry loop is effectively zero-cost.
    """
    delay = 0.05
    last_err: Exception | None = None
    for _ in range(attempts):
        try:
            tmp.replace(target)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(delay)
            delay *= 2
    assert last_err is not None
    raise last_err


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    _replace_with_retry(tmp, path)


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursive shallow merge: dicts are merged, other types are overwritten directly. Returns a new dict."""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class SnapshotBus:
    """Overwrite-style snapshot of the current session state.

    File: <state_dir>/snapshot.json
    Usage:
      - The engine writes a skeleton after proto_init (phase=waiting_peer, started_at, role, protocol_id)
      - hooks incrementally updates business fields via update(...)
      - last_event contains summary (text summary) and structured (structured fields)
    """

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / "snapshot.json"
        self._lock = threading.Lock()

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def write(self, snapshot: dict) -> None:
        snapshot = dict(snapshot)
        snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            _atomic_write_json(self.path, snapshot)

    def update(self, patch: dict | None = None, **kwargs: Any) -> dict:
        """Shallow merge patch into the current snapshot. kwargs are also merged in. Returns the new snapshot."""
        merged = dict(patch or {})
        merged.update(kwargs)
        with self._lock:
            current = self.read()
            new = _deep_merge(current, merged)
            new["updated_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_write_json(self.path, new)
            return new

    def set_phase(self, phase: str, summary: str | None = None, **structured: Any) -> dict:
        event: dict[str, Any] = {}
        if summary is not None:
            event["summary"] = summary
        if structured:
            event["structured"] = structured
        patch: dict[str, Any] = {"phase": phase}
        if event:
            patch["last_event"] = event
        return self.update(patch)

    def record_event(self, summary: str, **structured: Any) -> dict:
        return self.update(last_event={"summary": summary, "structured": structured})


class DetailLog:
    """Append-only log of session details.

    File: <state_dir>/details.jsonl
    One JSON object per line. Optional — only protocols that need a "detail" concept use it.
    Turn-based protocols write per-round details; other protocols may write key events at any granularity.
    """

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / "details.jsonl"
        self._lock = threading.Lock()

    def append(self, entry: dict | None = None, **kwargs: Any) -> dict:
        merged = dict(entry or {})
        merged.update(kwargs)
        merged.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return merged

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        items: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return items


class WhisperLog:
    """Append-only whisper log between the human user and the local Agent (not sent to the peer).

    File: <state_dir>/whispers.jsonl
    One JSON per line: {id, ts, role, text, origin?, agent_id?}
      - role: "user" | "agent" | "system"
      - id: uuid4().hex[:12] (new records) or a sha256-derived virtual id (legacy records)

    v005a upgrade: stable id, origin, agent_id; acks go to a separate whisper_acks.jsonl.
    """

    VALID_ROLES = ("user", "agent", "system")
    _ID_LEN = 12

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / "whispers.jsonl"
        self.acks_path = Path(state_dir) / "whisper_acks.jsonl"
        self._lock = threading.Lock()

    # -- id generation --

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:WhisperLog._ID_LEN]

    @staticmethod
    def _virtual_id(path: Path, line_no: int) -> str:
        return hashlib.sha256(
            f"legacy:{path}:{line_no}".encode()
        ).hexdigest()[:WhisperLog._ID_LEN]

    def _known_ids(self) -> set[str]:
        ids: set[str] = set()
        for i, w in enumerate(self.read_all()):
            wid = w.get("id")
            if wid:
                ids.add(wid)
        return ids

    def _unique_id(self) -> str:
        known = self._known_ids()
        for _ in range(10):
            wid = self._new_id()
            if wid not in known:
                return wid
        return self._new_id()  # In extreme cases, return directly

    # -- write --

    def append(
        self,
        role: str,
        text: str,
        *,
        origin: str = "cli",
        agent_id: str | None = None,
    ) -> dict:
        if role not in self.VALID_ROLES:
            raise ValueError(f"role must be one of {self.VALID_ROLES}, got {role!r}")
        wid = self._unique_id()
        entry: dict[str, Any] = {
            "id": wid,
            "ts": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "text": text,
            "origin": origin,
        }
        if agent_id is not None:
            entry["agent_id"] = agent_id
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return entry

    # -- read --

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        items: list[dict] = []
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" not in rec:
                rec["id"] = self._virtual_id(self.path, i)
            items.append(rec)
        return items

    # -- ack --

    def append_ack(
        self,
        whisper_id: str,
        *,
        status: str,
        agent_id: str | None = None,
        idempotency_key: str | None = None,
        action: str | None = None,
        reason_code: str | None = None,
        detail: dict | None = None,
        ts_client: float | None = None,
    ) -> dict:
        valid = ("executed", "dismissed", "unparsed", "error")
        if status not in valid:
            raise ValueError(f"status must be one of {valid}, got {status!r}")
        eff_agent = agent_id if agent_id else "anonymous"
        if idempotency_key is None:
            seed = f"{whisper_id}:{eff_agent}:{status}:{reason_code or ''}"
            idempotency_key = hashlib.sha256(seed.encode()).hexdigest()[:16]

        # Idempotency: if the same (whisper_id, agent_id, idempotency_key) already exists, do not append
        with self._lock:
            if self.acks_path.exists():
                try:
                    for line in self.acks_path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (rec.get("whisper_id") == whisper_id
                                and rec.get("agent_id") == eff_agent
                                and rec.get("idempotency_key") == idempotency_key):
                            return rec
                except OSError:
                    pass

            ack: dict[str, Any] = {
                "whisper_id": whisper_id,
                "status": status,
                "agent_id": eff_agent,
                "idempotency_key": idempotency_key,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            if action is not None:
                ack["action"] = action
            if reason_code is not None:
                ack["reason_code"] = reason_code
            if detail is not None:
                ack["detail"] = detail
            if ts_client is not None:
                ack["ts_client"] = ts_client

            self.acks_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(ack, ensure_ascii=False, separators=(",", ":"))
            with open(self.acks_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return ack

    def read_acks(self) -> list[dict]:
        if not self.acks_path.exists():
            return []
        items: list[dict] = []
        for line in self.acks_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return items

    def read_with_acks(self) -> list[dict]:
        whispers = self.read_all()
        acks = self.read_acks()
        # Group by (whisper_id, agent_id), take the latest ack per group
        latest: dict[tuple[str, str], dict] = {}
        for ack in acks:
            wid = ack.get("whisper_id", "")
            aid = ack.get("agent_id", "anonymous")
            key = (wid, aid)
            if key not in latest or ack.get("ts", "") > latest[key].get("ts", ""):
                latest[key] = ack
        # Attach latest_ack onto each whisper
        wid_to_ack: dict[str, dict] = {}
        for (wid, _), ack in latest.items():
            if wid not in wid_to_ack or ack.get("ts", "") > wid_to_ack[wid].get("ts", ""):
                wid_to_ack[wid] = ack
        for w in whispers:
            w["latest_ack"] = wid_to_ack.get(w.get("id", ""))
        return whispers


class StrategyStore:
    """Strategy file between the human user and the local Agent.

    File: <state_dir>/strategy.json
    Contents: any JSON object. The engine does not constrain the schema, and protocol authors do not own it either.
    The human writes via `aigenora session strategy --set/--merge`; hooks read it via read() to make decisions.

    New in v005a: strategy_events.jsonl audit event stream; records are appended on write/merge.
    """

    def __init__(self, state_dir: str | Path, default: dict | None = None):
        base = Path(state_dir)
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / "strategy.json"
        self.events_path = base / "strategy_events.jsonl"
        self._lock = threading.Lock()
        if not self.path.exists() and default is not None:
            _atomic_write_json(self.path, default)

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, strategy: dict, *, _meta: dict | None = None) -> None:
        if not isinstance(strategy, dict):
            raise TypeError("strategy must be a JSON object (dict)")
        with self._lock:
            before = self.read()
            _atomic_write_json(self.path, strategy)
            self._append_event("set", before, strategy, _meta)

    def merge(self, patch: dict, *, _meta: dict | None = None) -> dict:
        if not isinstance(patch, dict):
            raise TypeError("patch must be a JSON object (dict)")
        with self._lock:
            current = self.read()
            new = _deep_merge(current, patch)
            _atomic_write_json(self.path, new)
            self._append_event("merge", current, new, _meta)
            return new

    def get(self, key: str, default: Any = None) -> Any:
        return self.read().get(key, default)

    def read_events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        items: list[dict] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return items

    def _append_event(self, op: str, before: dict, after: dict, meta: dict | None) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "before": before,
            "after": after,
        }
        if meta:
            entry["_meta"] = meta
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        try:
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


class DecisionTimeoutError(Exception):
    pass


class DecisionBus:
    """File-based cross-process decision interface.

    File layout (state_dir/decision/):
      state.json      -- hooks write the current state snapshot here; the Agent reads it
      decisions.jsonl  -- the Agent appends decisions (one JSON per line); hooks consume them
    """

    POLL_INTERVAL = 0.2  # seconds

    def __init__(
        self,
        state_dir: str | Path,
        timeout: float = 120.0,
        timeout_action: str = "forfeit",
        fallback_value: dict | None = None,
    ):
        self.dir = Path(state_dir) / "decision"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / "state.json"
        self.decisions_file = self.dir / "decisions.jsonl"
        self.timeout = timeout
        self.timeout_action = timeout_action
        self.fallback_value = fallback_value
        self._lock = threading.Lock()
        self._consumed_offset: int = 0

    def publish_state(self, state_dict: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state_dict, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    def _read_all_decisions(self) -> list[dict]:
        if not self.decisions_file.exists():
            return []
        lines = self.decisions_file.read_text(encoding="utf-8").splitlines()
        result = []
        for line in lines:
            line = line.strip()
            if line:
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return result

    def await_decision(self, match_key: str, match_value, timeout: float | None = None) -> dict:
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            with self._lock:
                all_decisions = self._read_all_decisions()
                for i in range(self._consumed_offset, len(all_decisions)):
                    if all_decisions[i].get(match_key) == match_value:
                        self._consumed_offset = i + 1
                        return all_decisions[i]
            time.sleep(self.POLL_INTERVAL)
        if self.timeout_action == "fallback" and self.fallback_value is not None:
            return self.fallback_value if isinstance(self.fallback_value, dict) else {"value": self.fallback_value}
        raise DecisionTimeoutError(f"Decision timed out waiting for {match_key}={match_value}")

    def read_pending(self) -> list[dict]:
        with self._lock:
            all_decisions = self._read_all_decisions()
            pending = all_decisions[self._consumed_offset :]
            self._consumed_offset = len(all_decisions)
            return pending

    @staticmethod
    def submit(state_dir: str | Path, decision_dict: dict) -> None:
        d = Path(state_dir) / "decision"
        d.mkdir(parents=True, exist_ok=True)
        decisions_file = d / "decisions.jsonl"
        line = json.dumps(decision_dict, ensure_ascii=False, separators=(",", ":"))
        with open(decisions_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # -- v004: hold-and-release / finalized --

    def _finalized_file(self) -> Path:
        return self.dir / "finalized.json"

    def _write_finalized(self, match_key: str, match_value: Any, reason: str) -> None:
        fp = self._finalized_file()
        existing: dict = {}
        if fp.exists():
            try:
                existing = json.loads(fp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}
        key = f"{match_key}:{match_value}"
        existing[key] = {
            "reason": reason,
            "ts": time.monotonic(),
            "match_key": match_key,
            "match_value": match_value,
        }
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(fp)

    def is_finalized(self, match_key: str, match_value: Any) -> bool:
        fp = self._finalized_file()
        if not fp.exists():
            return False
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        return f"{match_key}:{match_value}" in data

    def await_latest_decision(
        self,
        match_key: str,
        match_value: Any,
        release_at: float,
        deadline_at: float,
        fallback_value: dict | None = None,
    ) -> dict:
        """v004 hold-and-release decision wait.

        release_at / deadline_at are absolute monotonic timestamps (time.monotonic()).
        Before release_at, decisions can be overwritten multiple times; once released, the latest one is finalized.
        If there is still no decision at deadline_at, return fallback (after a final drain).
        """
        if deadline_at < release_at:
            raise ValueError(f"deadline_at ({deadline_at}) must be >= release_at ({release_at})")

        bus = EventBus(self.dir.parent)
        latest: dict | None = None
        emitted_ready = False

        def _find_latest() -> dict | None:
            for d in reversed(self._read_all_decisions()):
                if d.get(match_key) == match_value:
                    return d
            return None

        # publish state with timing metadata (B2)
        self.publish_state({
            "match_key": match_key,
            "match_value": match_value,
            "release_at": release_at,
            "deadline_at": deadline_at,
            "waiting_for": "decision",
        })
        bus.emit("local_decision_window_started", data={
            "match_key": match_key, "match_value": match_value,
            "release_at": release_at, "deadline_at": deadline_at,
        })

        while True:
            now = time.monotonic()
            latest = _find_latest()

            # hold phase: update state but don't finalize
            if now < release_at:
                if latest is not None:
                    if not emitted_ready:
                        bus.emit("local_decision_ready", data={"match_key": match_key, "match_value": match_value, "decision": latest})
                        emitted_ready = True
                    else:
                        bus.emit("local_decision_updated", data={"match_key": match_key, "match_value": match_value, "decision": latest})
                time.sleep(self.POLL_INTERVAL)
                continue

            # release phase: finalize if we have a decision
            if latest is not None:
                self._write_finalized(match_key, match_value, "release")
                bus.emit("local_decision_finalized", data={"match_key": match_key, "match_value": match_value, "reason": "release", "decision": latest})
                return latest

            # past deadline: final drain then fallback
            if now >= deadline_at:
                latest = _find_latest()
                if latest is not None:
                    self._write_finalized(match_key, match_value, "release")
                    bus.emit("local_decision_finalized", data={"match_key": match_key, "match_value": match_value, "reason": "release", "decision": latest})
                    return latest
                fb = fallback_value if fallback_value is not None else (self.fallback_value if isinstance(self.fallback_value, dict) else {"value": self.fallback_value} if self.fallback_value is not None else {})
                self._write_finalized(match_key, match_value, "fallback")
                bus.emit("local_decision_fallback", data={"match_key": match_key, "match_value": match_value, "reason": "fallback", "fallback": fb})
                return fb

            # waiting for first decision after release
            time.sleep(self.POLL_INTERVAL)


class EventBus:
    """File-based event stream for an external Agent to track session progress.

    File layout (state_dir/events.jsonl): one JSON event per line.
    """

    def __init__(self, state_dir: str | Path):
        self.events_file = Path(state_dir) / "events.jsonl"
        # events.jsonl 是引擎主循环、心跳通道、续期循环、收信 producer 等多线程/协程的共同写入点，
        # 必须加锁否则并发追加会撕裂行（被 read_events 的 json.loads 静默丢弃，导致事件流缺事件，
        # 进而让 daemon 启动检测、session list 状态判定出错）。与同模块 DetailLog/WhisperLog/SnapshotBus 一致。
        self._lock = threading.Lock()

    def emit(self, event_type: str, data: dict | None = None, summary: str | None = None) -> None:
        entry: dict = {"ts": datetime.now(timezone.utc).isoformat(), "type": event_type}
        if data:
            entry["data"] = data
        if summary:
            entry["summary"] = summary
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self.events_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.events_file, "a", encoding="utf-8") as f:
                f.write(line)

    def read_events(self, after_ts: str | None = None) -> list[dict]:
        if not self.events_file.exists():
            return []
        events: list[dict] = []
        for line in self.events_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if after_ts is None or e.get("ts", "") > after_ts:
                events.append(e)
        return events


__all__ = [
    "StateStore",
    "StrategyStore",
    "SnapshotBus",
    "DetailLog",
    "WhisperLog",
    "DecisionBus",
    "DecisionTimeoutError",
    "EventBus",
    "commit_hash",
    "verify_commit",
]
