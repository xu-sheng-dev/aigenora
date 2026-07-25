"""Host-authoritative real-time protocol engine.

The live path deliberately favors responsiveness over synchronous consensus:

* Host advances a monotonic fixed-rate simulation and is the only referee.
* Guest submits future command frames asynchronously; Host never waits for one.
* Every accepted command is acknowledged and every applied command is echoed in the
  authoritative frame, making omission/delay visible after the fact.
* Guest verifies only the cheap state/hash chain during play and persists the complete
  stream.  Protocol-specific semantic auditing is an optional post-game hook.

This module is separate from ``session_loop`` so high-frequency games never create a
DecisionBus window or rewrite growing JSON documents on every tick.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aigenora.engine.p2p import (
    AsyncHeartbeatChannel,
    AsyncJsonLineChannel,
    ChannelClosed,
    JsonLineChannel,
)

from .loader import load_hooks
from .sdk import EventBus
from .validate import validate_message_obj


ZERO_HASH = "0" * 64
OUTCOMES = frozenset({"none", "host", "guest", "draw"})
REALTIME_WIRE_VERSION = 1


def canonical_json(value: Any) -> str:
    """Canonical finite JSON used by state, command, config and chain hashes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def compute_ruleset_hash(protocol_dir: str | Path) -> str:
    """Hash the executable protocol bundle while deliberately excluding UI assets.

    UI has an independent opt-in manifest and cannot affect authoritative rules.  The
    ruleset hash covers spec.json, hooks.py and any other protocol-local rule/data file;
    cache files and local publication metadata are excluded.
    """
    root = Path(protocol_dir)
    files: list[dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        if not parts or parts[0] == "ui" or "__pycache__" in parts:
            continue
        if any(part.startswith(".") for part in parts) or path.suffix == ".pyc":
            continue
        files.append(
            {
                "path": rel.as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not files:
        raise ValueError(f"real-time protocol bundle is empty: {root}")
    return json_hash({"version": 1, "files": files})


@dataclass(frozen=True)
class RealtimeConfig:
    tick_rate_hz: int
    input_delay_ticks: int
    snapshot_every_ticks: int
    max_command_lead_ticks: int
    max_commands_per_frame: int
    disconnect_policy: str

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "RealtimeConfig":
        realtime = (spec.get("flow") or {}).get("realtime") or {}
        return cls(
            tick_rate_hz=int(realtime["tick_rate_hz"]),
            input_delay_ticks=int(realtime["input_delay_ticks"]),
            snapshot_every_ticks=int(realtime["snapshot_every_ticks"]),
            max_command_lead_ticks=int(realtime["max_command_lead_ticks"]),
            max_commands_per_frame=int(realtime["max_commands_per_frame"]),
            disconnect_policy=str(realtime["disconnect_policy"]),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "tick_rate_hz": self.tick_rate_hz,
            "input_delay_ticks": self.input_delay_ticks,
            "snapshot_every_ticks": self.snapshot_every_ticks,
            "max_command_lead_ticks": self.max_command_lead_ticks,
            "max_commands_per_frame": self.max_commands_per_frame,
            "disconnect_policy": self.disconnect_policy,
        }


def build_transport_profile(
    metrics: dict[str, Any] | None,
    config: RealtimeConfig,
) -> dict[str, Any]:
    """Translate transport RTT into a local real-time command policy.

    A command generated from frame N traverses the full frame-to-Guest-to-Host round
    trip before Host can apply it.  The lead therefore uses RTT rather than one-way
    latency.  This profile is local advice only: it never enters authoritative state or
    match hashes, and protocols remain free to compile either macro or micro intent.
    """
    raw = metrics if isinstance(metrics, dict) else {}
    sample_count = max(0, int(raw.get("samples") or 0))
    latest = raw.get("rtt_ms")
    smoothed = raw.get("smoothed_rtt_ms")
    jitter = raw.get("jitter_ms")
    rtt_ms = float(smoothed if isinstance(smoothed, (int, float)) else latest) if (
        isinstance(smoothed, (int, float)) or isinstance(latest, (int, float))
    ) else None
    jitter_ms = max(0.0, float(jitter)) if isinstance(jitter, (int, float)) else 0.0
    tick_ms = 1000.0 / max(1, config.tick_rate_hz)
    if rtt_ms is None:
        lead_ticks = config.input_delay_ticks
        micro_suitable = False
        recommended = "macro"
        status = "unavailable"
        reason = "RTT is not available; persistent macro orders are the safe default."
    else:
        # Two jitter widths cover normal variance without turning a transient spike into
        # an unbounded queue.  Two guard ticks cover the equality-at-deadline race plus
        # one ordinary local scheduling / serialization interval.  Without the second
        # guard, a fixed 200 ms RTT at 10 Hz can expire when the Host catches up a tick
        # after a short scheduler pause.
        required_ticks = math.ceil((max(0.0, rtt_ms) + jitter_ms * 2.0) / tick_ms) + 2
        lead_ticks = max(config.input_delay_ticks, required_ticks)
        lead_ticks = min(config.max_command_lead_ticks, lead_ticks)
        micro_threshold_ms = max(75.0, min(200.0, tick_ms * 1.5))
        micro_suitable = rtt_ms + jitter_ms <= micro_threshold_ms
        recommended = "micro" if micro_suitable else "macro"
        status = "measured"
        reason = (
            "RTT is within the direct-control budget."
            if micro_suitable
            else "RTT exceeds the direct-control budget; prefer persistent macro orders."
        )
    return {
        "status": status,
        "samples": sample_count,
        "rtt_ms": round(float(latest), 2) if isinstance(latest, (int, float)) else None,
        "smoothed_rtt_ms": round(rtt_ms, 2) if rtt_ms is not None else None,
        "jitter_ms": round(jitter_ms, 2) if sample_count else None,
        "command_lead_ticks": int(lead_ticks),
        "micro_suitable": bool(micro_suitable),
        "recommended_control": recommended,
        "reason": reason,
    }


def _publish_transport_profile(hooks: Any, profile: dict[str, Any]) -> None:
    try:
        hooks.proto_realtime_transport_update(profile)
    except Exception:
        # Transport advice must never break the authoritative game loop.
        pass
    hooks.snapshot.update(realtime={"transport": profile})


class RealtimeJournal:
    """Append-only evidence stream; never re-read by the live engine."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.frames_path = self.state_dir / "realtime-frames.jsonl"
        self.commands_path = self.state_dir / "realtime-commands.jsonl"
        self.audit_path = self.state_dir / "realtime-audit.json"
        self._lock = threading.Lock()

    def _append(self, path: Path, kind: str, payload: dict[str, Any]) -> None:
        record = {
            "kind": kind,
            "recorded_at_ns": time.time_ns(),
            **payload,
        }
        line = canonical_json(record) + "\n"
        with self._lock:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)

    def frame(self, frame: dict[str, Any], *, direction: str) -> None:
        self._append(self.frames_path, "frame", {"direction": direction, "frame": frame})

    def command(self, kind: str, payload: dict[str, Any]) -> None:
        self._append(self.commands_path, kind, payload)

    def audit(self, payload: dict[str, Any]) -> None:
        body = canonical_json(payload)
        tmp = self.audit_path.with_suffix(".tmp")
        with self._lock:
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(self.audit_path)


def _state_dir(base: str | Path | None, role: str) -> Path:
    root = Path(base) if base else Path(tempfile.gettempdir()) / "aigenora-sessions"
    path = root / f"{role}-{int(time.time() * 1000)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _emit(bus: EventBus | None, event_type: str, data: dict[str, Any] | None = None) -> None:
    if bus is not None:
        bus.emit(event_type, data=data or {})


def _validate_wire(
    spec: dict[str, Any],
    msg: dict[str, Any],
    direction: str,
    enabled: bool,
) -> None:
    if enabled:
        validate_message_obj(spec, msg, direction=direction)


def _match_config_hash(bundle_hash: str, options: dict[str, Any], config: RealtimeConfig) -> str:
    return json_hash(
        {
            "wire_version": REALTIME_WIRE_VERSION,
            "ruleset_hash": bundle_hash,
            "options": options,
            "realtime": config.to_json(),
        }
    )


def _frame_core(
    *,
    tick: int,
    state_hash: str,
    prev_state_hash: str,
    prev_chain_hash: str,
    applied_commands: dict[str, Any],
    events: list[dict[str, Any]],
    outcome: str,
    host_time_ns: int,
) -> dict[str, Any]:
    return {
        "tick": tick,
        "state_hash": state_hash,
        "prev_state_hash": prev_state_hash,
        "prev_chain_hash": prev_chain_hash,
        "applied_commands": applied_commands,
        "events": events,
        "outcome": outcome,
        "host_time_ns": host_time_ns,
    }


def make_frame(
    *,
    tick: int,
    state: dict[str, Any],
    previous: dict[str, Any] | None,
    applied_commands: dict[str, Any],
    events: list[dict[str, Any]],
    outcome: str,
) -> dict[str, Any]:
    if outcome not in OUTCOMES:
        raise ValueError(f"invalid real-time outcome: {outcome!r}")
    state_digest = json_hash(state)
    core = _frame_core(
        tick=tick,
        state_hash=state_digest,
        prev_state_hash=previous["state_hash"] if previous else ZERO_HASH,
        prev_chain_hash=previous["chain_hash"] if previous else ZERO_HASH,
        applied_commands=applied_commands,
        events=events,
        outcome=outcome,
        host_time_ns=time.time_ns(),
    )
    return {**core, "state": state, "chain_hash": json_hash(core)}


def verify_frame(frame: dict[str, Any], previous: dict[str, Any] | None) -> None:
    """Cheap in-band integrity check.  It does not re-run game semantics."""
    required = {
        "tick",
        "state",
        "state_hash",
        "prev_state_hash",
        "prev_chain_hash",
        "chain_hash",
        "applied_commands",
        "events",
        "outcome",
        "host_time_ns",
    }
    if set(frame) != required:
        raise ValueError("real-time frame has missing or unknown fields")
    tick = frame["tick"]
    if not isinstance(tick, int) or isinstance(tick, bool) or tick < 0:
        raise ValueError("real-time frame tick is invalid")
    expected_tick = 0 if previous is None else previous["tick"] + 1
    if tick != expected_tick:
        raise ValueError(f"real-time frame tick discontinuity: expected {expected_tick}, got {tick}")
    if not isinstance(frame["state"], dict):
        raise ValueError("real-time frame state must be an object")
    if json_hash(frame["state"]) != frame["state_hash"]:
        raise ValueError("real-time frame state_hash mismatch")
    expected_prev_state = previous["state_hash"] if previous else ZERO_HASH
    expected_prev_chain = previous["chain_hash"] if previous else ZERO_HASH
    if frame["prev_state_hash"] != expected_prev_state:
        raise ValueError("real-time frame prev_state_hash mismatch")
    if frame["prev_chain_hash"] != expected_prev_chain:
        raise ValueError("real-time frame prev_chain_hash mismatch")
    if frame["outcome"] not in OUTCOMES:
        raise ValueError("real-time frame outcome is invalid")
    if not isinstance(frame["events"], list) or not isinstance(frame["applied_commands"], dict):
        raise ValueError("real-time frame events/commands shape is invalid")
    core = _frame_core(
        tick=tick,
        state_hash=frame["state_hash"],
        prev_state_hash=frame["prev_state_hash"],
        prev_chain_hash=frame["prev_chain_hash"],
        applied_commands=frame["applied_commands"],
        events=frame["events"],
        outcome=frame["outcome"],
        host_time_ns=frame["host_time_ns"],
    )
    if json_hash(core) != frame["chain_hash"]:
        raise ValueError("real-time frame chain_hash mismatch")


def _command_message(seq: int, target_tick: int, commands: list[dict[str, Any]]) -> dict[str, Any]:
    digest = json_hash({"seq": seq, "target_tick": target_tick, "commands": commands})
    return {
        "action": "rt_command",
        "seq": seq,
        "target_tick": target_tick,
        "commands": commands,
        "command_hash": digest,
    }


def _empty_applied(side: str, tick: int) -> dict[str, Any]:
    return {
        "side": side,
        "seq": None,
        "target_tick": tick,
        "commands": [],
        "command_hash": json_hash({"seq": None, "target_tick": tick, "commands": []}),
    }


def _applied(side: str, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "side": side,
        "seq": message["seq"],
        "target_tick": message["target_tick"],
        "commands": message["commands"],
        "command_hash": message["command_hash"],
    }


def _normalize_commands(
    hooks: Any,
    side: str,
    commands: Any,
    state: dict[str, Any],
    target_tick: int,
    config: RealtimeConfig,
) -> list[dict[str, Any]]:
    if not isinstance(commands, list):
        raise ValueError("commands must be a list")
    if len(commands) > config.max_commands_per_frame:
        raise ValueError("command frame exceeds max_commands_per_frame")
    normalized = hooks.proto_realtime_validate_commands(side, commands, state, target_tick)
    if not isinstance(normalized, list):
        raise ValueError("proto_realtime_validate_commands must return a list")
    if len(normalized) > config.max_commands_per_frame:
        raise ValueError("normalized command frame exceeds max_commands_per_frame")
    canonical_json(normalized)
    return normalized


def _normalize_step_result(result: Any) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    if not isinstance(result, dict) or not isinstance(result.get("state"), dict):
        raise ValueError("proto_realtime_step must return an object containing state")
    events = result.get("events", [])
    outcome = result.get("outcome", "none")
    if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
        raise ValueError("proto_realtime_step events must be a list of objects")
    if outcome not in OUTCOMES:
        raise ValueError("proto_realtime_step outcome is invalid")
    canonical_json(result["state"])
    canonical_json(events)
    return result["state"], events, outcome


def _snapshot_frame(
    hooks: Any,
    frame: dict[str, Any],
    config: RealtimeConfig,
    role: str,
    *,
    force: bool = False,
) -> None:
    if not force and frame["tick"] % config.snapshot_every_ticks != 0 and frame["outcome"] == "none":
        return
    patch = hooks.proto_realtime_snapshot(frame["state"], frame)
    if not isinstance(patch, dict):
        patch = {"world": frame["state"]}
    base = {
        "phase": "game_over" if frame["outcome"] != "none" else "in_progress",
        "role": role,
        "tick": frame["tick"],
        "game_over": frame["outcome"] != "none",
        "winner": frame["outcome"],
        "state_hash": frame["state_hash"],
        "chain_hash": frame["chain_hash"],
        "realtime": {
            "wire_version": REALTIME_WIRE_VERSION,
            "tick_rate_hz": config.tick_rate_hz,
            "input_delay_ticks": config.input_delay_ticks,
        },
    }
    base.update(patch)
    hooks.snapshot.update(base)


def _init_hooks(
    spec: dict[str, Any],
    protocol_dir: Path,
    options: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    role: str,
) -> tuple[Any, Path, RealtimeJournal, RealtimeConfig, str, str]:
    hooks = load_hooks(protocol_dir)
    state_dir = _state_dir(state_base, role)
    # Real-time micro-control never opens per-tick DecisionBus windows.  Human input
    # persists as macro strategy and is consumed by proto_realtime_commands instead.
    hooks.proto_init(options, role, args or [], state_dir, None)
    hooks.timing = spec.get("timing")
    config = RealtimeConfig.from_spec(spec)
    bundle_hash = compute_ruleset_hash(protocol_dir)
    config_hash = _match_config_hash(bundle_hash, options, config)
    journal = RealtimeJournal(state_dir)
    hooks.snapshot.update(
        phase="waiting_peer",
        role=role,
        protocol_id=spec.get("protocol_id") or spec.get("name") or "",
        protocol_name=spec.get("name") or "",
        match_id=state_dir.name,
        started_at=time.time(),
        ruleset_hash=bundle_hash,
        match_config_hash=config_hash,
        realtime={"wire_version": REALTIME_WIRE_VERSION, **config.to_json()},
        last_event={"summary": "Waiting for real-time peer", "structured": {"role": role}},
    )
    return hooks, state_dir, journal, config, bundle_hash, config_hash


async def _with_heartbeat(
    channel: AsyncJsonLineChannel,
    interval: float,
    timeout: float,
    event_bus: EventBus | None,
    hooks: Any,
) -> AsyncJsonLineChannel:
    # Always wrap authoritative real-time channels: interval=0 disables periodic
    # heartbeat/watchdog but retains the bounded on-demand RTT probe capability.
    wrapped = AsyncHeartbeatChannel(
        channel,
        interval=max(0.0, float(interval or 0.0)),
        timeout=(
            float(timeout)
            if timeout and timeout > 0
            else float(interval) * 3
            if interval and interval > 0
            else 0.0
        ),
        event_bus=event_bus,
        snapshot=getattr(hooks, "snapshot", None),
    )
    await wrapped.start()
    return wrapped


async def _run_host_async(
    spec: dict[str, Any],
    protocol_dir: Path,
    channel: AsyncJsonLineChannel,
    options: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    event_bus: EventBus | None,
    heartbeat_interval: float,
    heartbeat_timeout: float,
) -> dict[str, Any]:
    hooks, state_dir, journal, config, bundle_hash, config_hash = _init_hooks(
        spec, protocol_dir, options, args, state_base, "host"
    )
    metadata = hooks.proto_host_metadata()
    channel = await _with_heartbeat(
        channel, heartbeat_interval, heartbeat_timeout, event_bus, hooks
    )
    try:
        join = await channel.recv()
    except ChannelClosed:
        hooks.snapshot.set_phase("aborted", "Guest disconnected before real-time join")
        return {"metadata": metadata, "state_dir": str(state_dir), "completed": False, "reason": "peer_disconnected"}
    _validate_wire(spec, join, "guest_to_host", validate)
    mismatch = None
    if join.get("wire_version") != REALTIME_WIRE_VERSION:
        mismatch = "wire_version_mismatch"
    elif join.get("ruleset_hash") != bundle_hash:
        mismatch = "ruleset_hash_mismatch"
    elif join.get("match_config_hash") != config_hash:
        mismatch = "match_config_mismatch"
    if mismatch:
        rejection = {"action": "rt_reject", "reason": mismatch}
        _validate_wire(spec, rejection, "host_to_guest", validate)
        await channel.send(rejection)
        hooks.snapshot.set_phase("aborted", "Real-time handshake rejected", reason=mismatch)
        journal.command("handshake_rejected", {"reason": mismatch, "join": join})
        return {
            "metadata": metadata,
            "state_dir": str(state_dir),
            "completed": False,
            "reason": mismatch,
        }

    state = hooks.proto_realtime_initial_state()
    if not isinstance(state, dict):
        raise ValueError("proto_realtime_initial_state must return an object")
    canonical_json(state)
    initial = make_frame(
        tick=0,
        state=state,
        previous=None,
        applied_commands={"host": _empty_applied("host", 0), "guest": _empty_applied("guest", 0)},
        events=[],
        outcome="none",
    )
    ready = {
        "action": "rt_ready",
        "wire_version": REALTIME_WIRE_VERSION,
        "ruleset_hash": bundle_hash,
        "match_config_hash": config_hash,
        "config": config.to_json(),
        "frame": initial,
    }
    _validate_wire(spec, ready, "host_to_guest", validate)
    await channel.send(ready)
    journal.frame(initial, direction="sent")
    _snapshot_frame(hooks, initial, config, "host", force=True)
    _emit(event_bus, "realtime_started", {"role": "host", "tick_rate_hz": config.tick_rate_hz})

    runtime: dict[str, Any] = {
        "tick": 0,
        "state": state,
        "host_seq": 0,
        "host_pending": {},
        "last_guest_seq": -1,
        "pending": {},
    }
    disconnected = asyncio.Event()
    send_lock = asyncio.Lock()

    async def send_message(message: dict[str, Any]) -> bool:
        try:
            async with send_lock:
                await channel.send(message)
            return True
        except ChannelClosed:
            disconnected.set()
            return False

    async def receive_commands() -> None:
        while True:
            try:
                message = await channel.recv()
            except ChannelClosed:
                disconnected.set()
                return
            try:
                _validate_wire(spec, message, "guest_to_host", validate)
                if message.get("action") != "rt_command":
                    raise ValueError("unexpected real-time guest message")
                seq = message["seq"]
                target_tick = message["target_tick"]
                current_tick = runtime["tick"]
                expected_digest = json_hash(
                    {"seq": seq, "target_tick": target_tick, "commands": message["commands"]}
                )
                if message["command_hash"] != expected_digest:
                    raise ValueError("command_hash_mismatch")
                if seq <= runtime["last_guest_seq"]:
                    raise ValueError("non_monotonic_sequence")
                if target_tick <= current_tick:
                    raise ValueError("late_target_tick")
                if target_tick > current_tick + config.max_command_lead_ticks:
                    raise ValueError("target_tick_too_far")
                if target_tick in runtime["pending"]:
                    raise ValueError("target_tick_already_queued")
                normalized = _normalize_commands(
                    hooks,
                    "guest",
                    message["commands"],
                    runtime["state"],
                    target_tick,
                    config,
                )
                accepted = _command_message(seq, target_tick, normalized)
                # Keep the peer's digest only if normalization was identity.  A hook is
                # allowed to normalize aliases; the accepted digest then binds what Host
                # will actually apply and the ack makes that explicit.
                runtime["pending"][target_tick] = accepted
                runtime["last_guest_seq"] = seq
                ack = {
                    "action": "rt_command_ack",
                    "seq": seq,
                    "target_tick": target_tick,
                    "status": "accepted",
                    "received_at_tick": current_tick,
                    "command_hash": accepted["command_hash"],
                }
                journal.command("received", {"message": message, "ack": ack})
            except Exception as exc:
                seq = message.get("seq") if isinstance(message, dict) else None
                target_tick = message.get("target_tick") if isinstance(message, dict) else None
                reason = str(exc)[:200]
                journal.command("rejected", {"message": message, "reason": reason})
                _emit(event_bus, "realtime_command_rejected", {"reason": reason})
                if not isinstance(seq, int) or not isinstance(target_tick, int):
                    continue
                ack = {
                    "action": "rt_command_ack",
                    "seq": seq,
                    "target_tick": target_tick,
                    "status": "rejected",
                    "received_at_tick": runtime["tick"],
                    "command_hash": message.get("command_hash", ZERO_HASH),
                    "reason": reason,
                }
            _validate_wire(spec, ack, "host_to_guest", validate)
            if not await send_message(ack):
                return

    def queue_host_commands(source_state: dict[str, Any], source_tick: int) -> None:
        """Give Host and Guest the same protocol-bound input delay.

        Host still owns the clock, but its local Agent must not observe a newer state
        than Guest when choosing commands for the same target tick.
        """
        target_tick = source_tick + config.input_delay_ticks
        runtime["host_seq"] += 1
        seq = runtime["host_seq"]
        try:
            raw = hooks.proto_realtime_commands(source_state, target_tick)
            commands = _normalize_commands(
                hooks, "host", raw, source_state, target_tick, config
            )
        except Exception as exc:
            commands = []
            journal.command(
                "local_generation_failed",
                {
                    "side": "host",
                    "seq": seq,
                    "target_tick": target_tick,
                    "reason": str(exc)[:200],
                },
            )
        message = _command_message(seq, target_tick, commands)
        runtime["host_pending"][target_tick] = message
        journal.command(
            "generated",
            {"side": "host", "source_tick": source_tick, "message": message},
        )

    receiver = asyncio.create_task(receive_commands(), name="aigenora-realtime-command-receiver")
    queue_host_commands(state, 0)
    previous = initial
    deadline = time.monotonic()
    outcome = "none"
    try:
        while outcome == "none":
            deadline += 1.0 / config.tick_rate_hz
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            # Give an already-ready command receiver one fair scheduling turn before
            # freezing this tick's input.  This never waits for peer input: sleep(0)
            # only yields to runnable local tasks, so the Host remains authoritative
            # and continues on its fixed monotonic deadline.
            await asyncio.sleep(0)
            if disconnected.is_set() and config.disconnect_policy == "abort":
                hooks.snapshot.set_phase("aborted", "Guest disconnected", reason="peer_disconnected")
                return {
                    "metadata": metadata,
                    "state_dir": str(state_dir),
                    "completed": False,
                    "reason": "peer_disconnected",
                }
            tick = runtime["tick"] + 1
            host_message = runtime["host_pending"].pop(tick, None)
            host_commands = host_message["commands"] if host_message else []
            guest_message = runtime["pending"].pop(tick, None)
            guest_commands = guest_message["commands"] if guest_message else []
            step = hooks.proto_realtime_step(
                runtime["state"],
                tick,
                {"host": host_commands, "guest": guest_commands},
            )
            new_state, events, outcome = _normalize_step_result(step)
            applied = {
                "host": _applied("host", host_message) if host_message else _empty_applied("host", tick),
                "guest": _applied("guest", guest_message) if guest_message else _empty_applied("guest", tick),
            }
            frame = make_frame(
                tick=tick,
                state=new_state,
                previous=previous,
                applied_commands=applied,
                events=events,
                outcome=outcome,
            )
            runtime["tick"] = tick
            runtime["state"] = new_state
            if outcome == "none":
                queue_host_commands(new_state, tick)
            journal.frame(frame, direction="sent")
            _snapshot_frame(hooks, frame, config, "host")
            message = {"action": "rt_frame", "frame": frame}
            _validate_wire(spec, message, "host_to_guest", validate)
            if not disconnected.is_set():
                sent = await send_message(message)
                if not sent and config.disconnect_policy == "abort":
                    hooks.snapshot.set_phase("aborted", "Guest disconnected", reason="peer_disconnected")
                    return {
                        "metadata": metadata,
                        "state_dir": str(state_dir),
                        "completed": False,
                        "reason": "peer_disconnected",
                    }
            previous = frame
        _emit(event_bus, "realtime_ended", {"role": "host", "tick": runtime["tick"], "outcome": outcome})
        return {
            "metadata": metadata,
            "state_dir": str(state_dir),
            "completed": True,
            "outcome": outcome,
            "tick": runtime["tick"],
            "ruleset_hash": bundle_hash,
        }
    finally:
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receiver


async def _run_guest_async(
    spec: dict[str, Any],
    protocol_dir: Path,
    channel: AsyncJsonLineChannel,
    options: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    event_bus: EventBus | None,
    heartbeat_interval: float,
    heartbeat_timeout: float,
) -> dict[str, Any]:
    hooks, state_dir, journal, config, bundle_hash, config_hash = _init_hooks(
        spec, protocol_dir, options, args, state_base, "guest"
    )
    channel = await _with_heartbeat(
        channel, heartbeat_interval, heartbeat_timeout, event_bus, hooks
    )
    if isinstance(channel, AsyncHeartbeatChannel):
        measured = await channel.probe_latency(samples=3, timeout=0.75)
    else:  # pragma: no cover - _with_heartbeat currently always wraps
        measured = {}
    transport_profile = build_transport_profile(measured, config)
    _publish_transport_profile(hooks, transport_profile)
    _emit(event_bus, "p2p_latency_measured", transport_profile)
    join = {
        "action": "rt_join",
        "wire_version": REALTIME_WIRE_VERSION,
        "ruleset_hash": bundle_hash,
        "match_config_hash": config_hash,
    }
    _validate_wire(spec, join, "guest_to_host", validate)
    await channel.send(join)
    try:
        ready = await channel.recv()
    except ChannelClosed:
        hooks.snapshot.set_phase("aborted", "Host disconnected before real-time ready")
        return {"state_dir": str(state_dir), "completed": False, "reason": "peer_disconnected"}
    _validate_wire(spec, ready, "host_to_guest", validate)
    if ready.get("action") == "rt_reject":
        reason = ready.get("reason", "handshake_rejected")
        hooks.snapshot.set_phase("aborted", "Real-time handshake rejected", reason=reason)
        return {"state_dir": str(state_dir), "completed": False, "reason": reason}
    if ready.get("action") != "rt_ready":
        raise ValueError("expected rt_ready")
    if ready.get("wire_version") != REALTIME_WIRE_VERSION:
        raise ValueError("wire_version_mismatch")
    if ready.get("ruleset_hash") != bundle_hash or ready.get("match_config_hash") != config_hash:
        raise ValueError("host real-time bundle/config mismatch")
    if ready.get("config") != config.to_json():
        raise ValueError("host real-time config mismatch")
    previous = ready["frame"]
    verify_frame(previous, None)
    journal.frame(previous, direction="received")
    _snapshot_frame(hooks, previous, config, "guest", force=True)
    _emit(event_bus, "realtime_started", {"role": "guest", "tick_rate_hz": config.tick_rate_hz})
    seq = 0

    async def send_commands(frame: dict[str, Any]) -> None:
        nonlocal seq, transport_profile
        if isinstance(channel, AsyncHeartbeatChannel):
            current_profile = build_transport_profile(channel.latency_metrics(), config)
            if current_profile != transport_profile:
                transport_profile = current_profile
                _publish_transport_profile(hooks, transport_profile)
        target_tick = frame["tick"] + max(1, int(transport_profile["command_lead_ticks"]))
        seq += 1
        try:
            raw = hooks.proto_realtime_commands(frame["state"], target_tick)
            commands = _normalize_commands(
                hooks, "guest", raw, frame["state"], target_tick, config
            )
        except Exception as exc:
            commands = []
            journal.command(
                "local_generation_failed",
                {"seq": seq, "target_tick": target_tick, "reason": str(exc)[:200]},
            )
        message = _command_message(seq, target_tick, commands)
        journal.command("sent", {"message": message, "local_send_time_ns": time.time_ns()})
        _validate_wire(spec, message, "guest_to_host", validate)
        await channel.send(message)

    await send_commands(previous)
    while True:
        try:
            message = await channel.recv()
        except ChannelClosed:
            hooks.snapshot.set_phase("aborted", "Host disconnected", reason="peer_disconnected")
            return {"state_dir": str(state_dir), "completed": False, "reason": "peer_disconnected"}
        _validate_wire(spec, message, "host_to_guest", validate)
        action = message.get("action")
        if action == "rt_command_ack":
            journal.command("ack", {"ack": message, "local_receive_time_ns": time.time_ns()})
            hooks.snapshot.update(
                realtime={
                    "last_command_ack": {
                        "seq": message["seq"],
                        "target_tick": message["target_tick"],
                        "status": message["status"],
                        "received_at_tick": message["received_at_tick"],
                        "reason": message.get("reason"),
                    }
                }
            )
            continue
        if action != "rt_frame":
            raise ValueError(f"unexpected real-time host message: {action!r}")
        frame = message["frame"]
        try:
            verify_frame(frame, previous)
        except Exception as exc:
            audit = {
                "status": "integrity_failed",
                "reason": str(exc),
                "last_valid_tick": previous["tick"],
            }
            journal.audit(audit)
            hooks.snapshot.set_phase("aborted", "Real-time integrity check failed", **audit)
            _emit(event_bus, "realtime_integrity_failed", audit)
            return {
                "state_dir": str(state_dir),
                "completed": False,
                "reason": "integrity_failed",
                "audit": audit,
            }
        journal.frame(frame, direction="received")
        _snapshot_frame(hooks, frame, config, "guest")
        previous = frame
        if frame["outcome"] != "none":
            try:
                semantic = hooks.proto_realtime_audit_outcome(frame)
                audit = semantic if isinstance(semantic, dict) else {"status": "deferred"}
            except Exception as exc:
                audit = {"status": "audit_error", "reason": str(exc)[:200]}
            audit = {
                **audit,
                "integrity": "verified",
                "tick": frame["tick"],
                "outcome": frame["outcome"],
                "chain_hash": frame["chain_hash"],
            }
            journal.audit(audit)
            hooks.snapshot.update(realtime={"audit": audit})
            _emit(event_bus, "realtime_ended", {"role": "guest", "tick": frame["tick"], "outcome": frame["outcome"]})
            return {
                "state_dir": str(state_dir),
                "completed": True,
                "outcome": frame["outcome"],
                "tick": frame["tick"],
                "ruleset_hash": bundle_hash,
                "audit": audit,
            }
        await send_commands(frame)


async def run_authoritative_realtime_async(
    spec: dict[str, Any],
    protocol_dir: Path,
    channel: AsyncJsonLineChannel,
    options: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    role: str,
    *,
    event_bus: EventBus | None = None,
    coach: bool = False,
    pace: float = 0,
    heartbeat_interval: float = 0,
    heartbeat_timeout: float = 0,
) -> dict[str, Any]:
    del coach, pace  # Real-time pacing is protocol-bound and never a local CLI override.
    protocol_dir = Path(protocol_dir)
    if role == "host":
        return await _run_host_async(
            spec,
            protocol_dir,
            channel,
            options,
            args,
            state_base,
            validate,
            event_bus,
            heartbeat_interval,
            heartbeat_timeout,
        )
    if role == "guest":
        return await _run_guest_async(
            spec,
            protocol_dir,
            channel,
            options,
            args,
            state_base,
            validate,
            event_bus,
            heartbeat_interval,
            heartbeat_timeout,
        )
    raise ValueError(f"invalid role: {role!r}")


class _AsyncFromSyncChannel(AsyncJsonLineChannel):
    """Run the authoritative async core over the test/offline sync channel API."""

    def __init__(self, inner: JsonLineChannel):
        self.inner = inner

    async def send(self, msg: dict[str, Any]) -> None:
        await asyncio.to_thread(self.inner.send, msg)

    async def recv(self, timeout: float | None = None) -> dict[str, Any]:
        # Use bounded reads so cancelling the Host command receiver cannot leave the
        # event loop's executor blocked forever during sync test teardown.
        slice_timeout = min(timeout, 0.25) if timeout else 0.25
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            try:
                return await asyncio.to_thread(self.inner.recv, slice_timeout)
            except TimeoutError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0)

    async def close(self) -> None:
        await asyncio.to_thread(self.inner.close)


def run_authoritative_realtime_sync(
    spec: dict[str, Any],
    protocol_dir: Path,
    channel: JsonLineChannel,
    options: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    role: str,
    *,
    event_bus: EventBus | None = None,
    coach: bool = False,
    pace: float = 0,
) -> dict[str, Any]:
    return asyncio.run(
        run_authoritative_realtime_async(
            spec,
            Path(protocol_dir),
            _AsyncFromSyncChannel(channel),
            options,
            args,
            state_base,
            validate,
            role,
            event_bus=event_bus,
            coach=coach,
            pace=pace,
        )
    )


__all__ = [
    "RealtimeConfig",
    "RealtimeJournal",
    "canonical_json",
    "compute_ruleset_hash",
    "json_hash",
    "make_frame",
    "verify_frame",
    "run_authoritative_realtime_sync",
    "run_authoritative_realtime_async",
]
