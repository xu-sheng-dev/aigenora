from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from aigenora.engine.p2p import AsyncJsonLineChannel, ChannelClosed, JsonLineChannel
from aigenora.engine.crypto import commit_hash, random_nonce, sha256

from .hooks import HookResult, ProtocolHooks
from .loader import load_hooks
from .sdk import EventBus
from .validate import (
    ValidationError,
    load_spec,
    resolve_flow_mode,
    validate_message_obj,
    validate_options,
)


def _state_dir(base: str | Path | None, key: str) -> Path:
    if base:
        path = Path(base)
    else:
        path = Path(tempfile.gettempdir()) / "aigenora-sessions"
    path = path / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def _as_result(value: HookResult | dict[str, Any] | None) -> HookResult:
    if isinstance(value, HookResult):
        return value
    if isinstance(value, dict):
        return HookResult(response=value)
    return HookResult()


def _display(hooks: Any, msg: dict[str, Any], direction: str) -> None:
    text = hooks.proto_display(msg, direction)
    if text:
        print(text)


def _emit(bus: EventBus | None, event_type: str, data: dict | None = None, summary: str | None = None) -> None:
    if bus is not None:
        bus.emit(event_type, data=data, summary=summary)


def _validate(spec: dict[str, Any], msg: dict[str, Any], direction: str) -> None:
    validate_message_obj(spec, msg, direction=direction)


def _is_control_end(msg: Any) -> bool:
    return isinstance(msg, dict) and msg == {"action": "end"}


def _winner_of(*candidates: Any) -> str | None:
    """提取游戏胜者（host/guest/draw）供 close 时声明 ELO winner（v010 M5 ELO 触发修复）。

    从触发 game_over 的消息里取 winner：host 侧是裁判产生的 round_result（result.response），
    guest 侧是刚处理的 host round_result（loop 的 msg，因 guest game_over 时返回的
    HookResult response=None 走 else 分支）。只接受 host/guest/draw；'none'/空/缺失返回 None
    ——胜负优先透传触发 ELO；平局的协议差异（gomoku round_result.winner='none' vs
    end.winner='draw'）暂不映射，后续如需平局计 ELO 再统一。
    """
    for c in candidates:
        if isinstance(c, dict):
            # session_loop 棋类用 "winner"；commit-reveal(RPS/coin/weak-wins)整局赢家用 "game_winner"
            for key in ("winner", "game_winner"):
                w = c.get(key)
                if w in ("host", "guest", "draw"):
                    return w
    return None


def _snapshot_init(hooks: Any, role: str, spec: dict[str, Any], state_dir: Path) -> None:
    """Engine fallback initialization for snapshot.json, so it is not empty when hooks have not written to it.

    Writes phase=waiting_peer + started_at + role + protocol_id + summary.
    """
    snap = getattr(hooks, "snapshot", None)
    if snap is None:
        return
    snap.update(
        phase="waiting_peer",
        role=role,
        protocol_id=spec.get("protocol_id") or spec.get("name") or "",
        protocol_name=spec.get("name") or "",
        started_at=time.time(),
        last_event={"summary": "Waiting for peer", "structured": {"role": role}},
    )


def _snapshot_phase(hooks: Any, phase: str, summary: str, **structured: Any) -> None:
    snap = getattr(hooks, "snapshot", None)
    if snap is None:
        return
    snap.set_phase(phase, summary=summary, **structured)


def _handle_peer_disconnect(hooks: Any, event_bus: EventBus | None, **result_extra: Any) -> dict[str, Any]:
    """Clean termination when the peer channel closes mid-session (v009 P1-6).

    Emits session_ended(peer_disconnected), marks the snapshot aborted, and returns a
    game_over=False result so no session proof is produced and no score is polluted.
    """
    _emit(event_bus, "session_ended", {"game_over": False, "reason": "peer_disconnected"})
    _snapshot_phase(hooks, "aborted", "Peer disconnected", reason="peer_disconnected")
    return {"game_over": False, "reason": "peer_disconnected", **result_extra}


async def _maybe_wrap_heartbeat(
    channel: AsyncJsonLineChannel,
    interval: float,
    timeout: float,
    event_bus: EventBus | None,
    hooks: Any,
) -> AsyncJsonLineChannel:
    """Enable AsyncHeartbeatChannel based on arguments.

    When interval <= 0, returns the original channel directly without wrapping (used for in-memory channels/tests).
    When timeout is not configured (timeout <= 0), defaults to timeout = interval * 3.
    """
    if interval is None or interval <= 0:
        return channel
    if timeout is None or timeout <= 0:
        timeout = interval * 3
    from aigenora.engine.p2p import AsyncHeartbeatChannel
    wrapped = AsyncHeartbeatChannel(
        channel,
        interval=interval,
        timeout=timeout,
        event_bus=event_bus,
        snapshot=getattr(hooks, "snapshot", None),
    )
    await wrapped.start()
    return wrapped


def _resolve_decision_config(spec: dict[str, Any], coach: bool) -> dict[str, Any] | None:
    if coach:
        return {"mode": "manual", "timeout_seconds": 0, "timeout_action": "fallback"}
    return spec.get("decision")


def _resolve_value_field(spec: dict[str, Any]) -> str:
    """Read the business value field name from spec.flow.round.value_field, default 'value'."""
    flow = spec.get("flow") or {}
    round_spec = flow.get("round") or {}
    return round_spec.get("value_field") or "value"


def _run_session_loop_sync(
    spec: dict[str, Any],
    proto_dir: Path,
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
    """1v1 ping-pong default main loop (sync version)."""
    if role == "host":
        return _run_session_loop_sync_host(
            spec, proto_dir, channel, options, args, state_base, validate,
            event_bus=event_bus, coach=coach, pace=pace,
        )
    if role == "guest":
        return _run_session_loop_sync_guest(
            spec, proto_dir, channel, options, args, state_base, validate,
            event_bus=event_bus, coach=coach, pace=pace,
        )
    raise ValueError(f"invalid role: {role!r}")


def _run_session_loop_sync_host(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: JsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"host-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "host", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "host", spec, state_dir)
    metadata = hooks.proto_host_metadata()
    join_msg = channel.recv()
    if validate:
        _validate(spec, join_msg, "guest_to_host")
    _display(hooks, join_msg, "received")
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": join_msg})
    ready_result = _as_result(hooks.proto_host_handle_join(join_msg))
    if ready_result.response is None:
        raise RuntimeError("host join hook did not return ready response")
    if validate:
        _validate(spec, ready_result.response, "host_to_guest")
    _display(hooks, ready_result.response, "sent")
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": ready_result.response})
    if ready_result.game_over or ready_result.abort:
        channel.send(ready_result.response)
        _emit(event_bus, "session_ended", {"game_over": True, "reason": "abort" if ready_result.abort else "game_over"})
        _snapshot_phase(hooks, "aborted" if ready_result.abort else "game_over",
                        "Session ended during handshake", reason="abort" if ready_result.abort else "game_over")
        return {"metadata": metadata, "state_dir": str(state_dir), "game_over": ready_result.game_over, "winner": _winner_of(ready_result.response)}
    msg = channel.send_wait(ready_result.response)
    while True:
        if validate:
            _validate(spec, msg, "guest_to_host")
        _display(hooks, msg, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": msg})
        result = _as_result(hooks.proto_host_handle(msg))
        if result.response:
            if validate:
                _validate(spec, result.response, "host_to_guest")
            _display(hooks, result.response, "sent")
            _emit(event_bus, "protocol_message", {"direction": "sent", "msg": result.response},
                  summary=hooks.proto_display(result.response, "sent"))
            if result.game_over or result.abort:
                channel.send(result.response)
                _emit(event_bus, "session_ended", {"game_over": True, "reason": "abort" if result.abort else "game_over"})
                _snapshot_phase(hooks, "aborted" if result.abort else "game_over",
                                "Session ended", reason="abort" if result.abort else "game_over")
                return {"metadata": metadata, "state_dir": str(state_dir), "game_over": result.game_over, "winner": _winner_of(result.response)}
            if pace > 0:
                time.sleep(pace)
            msg = channel.send_wait(result.response)
        else:
            _snapshot_phase(hooks, "game_over", "Session completed")
            return {"metadata": metadata, "state_dir": str(state_dir), "game_over": result.game_over, "winner": _winner_of(result.response)}


def _run_session_loop_sync_guest(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: JsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"guest-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "guest", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "guest", spec, state_dir)
    join_msg = hooks.proto_guest_join_message()
    if validate:
        _validate(spec, join_msg, "guest_to_host")
    _display(hooks, join_msg, "sent")
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": join_msg})
    ready_msg = channel.send_wait(join_msg)
    if validate:
        _validate(spec, ready_msg, "host_to_guest")
    _display(hooks, ready_msg, "received")
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": ready_msg})
    hooks.proto_guest_handle_ready(ready_msg)
    first = hooks.proto_guest_first_action()
    if not first:
        _snapshot_phase(hooks, "game_over", "Guest ended after handshake")
        return {"state_dir": str(state_dir), "game_over": False}
    if validate:
        _validate(spec, first, "guest_to_host")
    _display(hooks, first, "sent")
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": first})
    msg = channel.send_wait(first)
    while True:
        if validate:
            _validate(spec, msg, "host_to_guest")
        _display(hooks, msg, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": msg})
        result = _as_result(hooks.proto_guest_handle(msg))
        if result.response:
            if validate:
                _validate(spec, result.response, "guest_to_host")
            _display(hooks, result.response, "sent")
            _emit(event_bus, "protocol_message", {"direction": "sent", "msg": result.response},
                  summary=hooks.proto_display(result.response, "sent"))
            if result.game_over or result.abort:
                channel.send(result.response)
                _emit(event_bus, "session_ended", {"game_over": True, "reason": "abort" if result.abort else "game_over"})
                _snapshot_phase(hooks, "aborted" if result.abort else "game_over",
                                "Session ended", reason="abort" if result.abort else "game_over")
                return {"state_dir": str(state_dir), "game_over": result.game_over, "winner": _winner_of(result.response, msg)}
            if pace > 0:
                time.sleep(pace)
            msg = channel.send_wait(result.response)
        else:
            _snapshot_phase(hooks, "game_over", "Session completed")
            return {"state_dir": str(state_dir), "game_over": result.game_over, "winner": _winner_of(result.response, msg)}


async def _run_session_loop_async(
    spec: dict[str, Any],
    proto_dir: Path,
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
    """1v1 ping-pong default main loop (async version)."""
    if role == "host":
        return await _run_session_loop_async_host(
            spec, proto_dir, channel, options, args, state_base, validate,
            event_bus=event_bus, coach=coach, pace=pace,
            heartbeat_interval=heartbeat_interval, heartbeat_timeout=heartbeat_timeout,
        )
    if role == "guest":
        return await _run_session_loop_async_guest(
            spec, proto_dir, channel, options, args, state_base, validate,
            event_bus=event_bus, coach=coach, pace=pace,
            heartbeat_interval=heartbeat_interval, heartbeat_timeout=heartbeat_timeout,
        )
    raise ValueError(f"invalid role: {role!r}")


async def _run_session_loop_async_host(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: AsyncJsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
    heartbeat_interval: float,
    heartbeat_timeout: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"host-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "host", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "host", spec, state_dir)
    metadata = hooks.proto_host_metadata()
    channel = await _maybe_wrap_heartbeat(
        channel, heartbeat_interval, heartbeat_timeout, event_bus, hooks,
    )
    try:
        join_msg = await channel.recv()
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        _display(hooks, join_msg, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": join_msg})
        ready_result = _as_result(hooks.proto_host_handle_join(join_msg))
        if ready_result.response is None:
            raise RuntimeError("host join hook did not return ready response")
        if validate:
            _validate(spec, ready_result.response, "host_to_guest")
        _display(hooks, ready_result.response, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": ready_result.response})
        if ready_result.game_over or ready_result.abort:
            await channel.send(ready_result.response)
            _emit(event_bus, "session_ended", {"game_over": True, "reason": "abort" if ready_result.abort else "game_over"})
            _snapshot_phase(hooks, "aborted" if ready_result.abort else "game_over",
                            "Session ended during handshake", reason="abort" if ready_result.abort else "game_over")
            return {"metadata": metadata, "state_dir": str(state_dir), "game_over": ready_result.game_over, "winner": _winner_of(ready_result.response)}
        msg = await channel.send_wait(ready_result.response)
        while True:
            if validate:
                _validate(spec, msg, "guest_to_host")
            _display(hooks, msg, "received")
            _emit(event_bus, "protocol_message", {"direction": "received", "msg": msg})
            result = _as_result(hooks.proto_host_handle(msg))
            if result.response:
                if validate:
                    _validate(spec, result.response, "host_to_guest")
                _display(hooks, result.response, "sent")
                _emit(event_bus, "protocol_message", {"direction": "sent", "msg": result.response},
                      summary=hooks.proto_display(result.response, "sent"))
                if result.game_over or result.abort:
                    await channel.send(result.response)
                    _emit(event_bus, "session_ended", {"game_over": True, "reason": "abort" if result.abort else "game_over"})
                    _snapshot_phase(hooks, "aborted" if result.abort else "game_over",
                                    "Session ended", reason="abort" if result.abort else "game_over")
                    return {"metadata": metadata, "state_dir": str(state_dir), "game_over": result.game_over, "winner": _winner_of(result.response)}
                if pace > 0:
                    await asyncio.sleep(pace)
                msg = await channel.send_wait(result.response)
            else:
                _snapshot_phase(hooks, "game_over", "Session completed")
                return {"metadata": metadata, "state_dir": str(state_dir), "game_over": result.game_over, "winner": _winner_of(result.response)}
    except ChannelClosed:
        return _handle_peer_disconnect(hooks, event_bus, metadata=metadata, state_dir=str(state_dir))


async def _run_session_loop_async_guest(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: AsyncJsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
    heartbeat_interval: float,
    heartbeat_timeout: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"guest-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "guest", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "guest", spec, state_dir)
    channel = await _maybe_wrap_heartbeat(
        channel, heartbeat_interval, heartbeat_timeout, event_bus, hooks,
    )
    try:
        join_msg = hooks.proto_guest_join_message()
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        _display(hooks, join_msg, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": join_msg})
        ready_msg = await channel.send_wait(join_msg)
        if validate:
            _validate(spec, ready_msg, "host_to_guest")
        _display(hooks, ready_msg, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": ready_msg})
        hooks.proto_guest_handle_ready(ready_msg)
        first = hooks.proto_guest_first_action()
        if not first:
            _snapshot_phase(hooks, "game_over", "Guest ended after handshake")
            return {"state_dir": str(state_dir), "game_over": False}
        if validate:
            _validate(spec, first, "guest_to_host")
        _display(hooks, first, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": first})
        msg = await channel.send_wait(first)
        while True:
            if validate:
                _validate(spec, msg, "host_to_guest")
            _display(hooks, msg, "received")
            _emit(event_bus, "protocol_message", {"direction": "received", "msg": msg})
            result = _as_result(hooks.proto_guest_handle(msg))
            if result.response:
                if validate:
                    _validate(spec, result.response, "guest_to_host")
                _display(hooks, result.response, "sent")
                _emit(event_bus, "protocol_message", {"direction": "sent", "msg": result.response},
                      summary=hooks.proto_display(result.response, "sent"))
                if result.game_over or result.abort:
                    await channel.send(result.response)
                    _emit(event_bus, "session_ended", {"game_over": True, "reason": "abort" if result.abort else "game_over"})
                    _snapshot_phase(hooks, "aborted" if result.abort else "game_over",
                                    "Session ended", reason="abort" if result.abort else "game_over")
                    return {"state_dir": str(state_dir), "game_over": result.game_over, "winner": _winner_of(result.response, msg)}
                if pace > 0:
                    await asyncio.sleep(pace)
                msg = await channel.send_wait(result.response)
            else:
                _snapshot_phase(hooks, "game_over", "Session completed")
                return {"state_dir": str(state_dir), "game_over": result.game_over, "winner": _winner_of(result.response, msg)}
    except ChannelClosed:
        return _handle_peer_disconnect(hooks, event_bus, state_dir=str(state_dir))


def _run_free(
    spec: dict[str, Any],
    proto_dir: Path,
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
    """Sync implementation of free mode.

    v006 P5: the sender consumes both stdin and <state_dir>/inbox.jsonl.
    The inbox is appended to by the webui (POST /api/chat/send); the sender remembers the consumed offset by file position.
    stdin behavior stays backward compatible; /quit from either source terminates the session.
    """
    import queue as _queue

    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"{role}-{int(time.time() * 1000)}")
    hooks.proto_init(options, role, args or [], state_dir)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, role, spec, state_dir)
    metadata = hooks.proto_host_metadata()
    end_event = threading.Event()
    seq = [0]

    input_q: "_queue.Queue[tuple[str, str]]" = _queue.Queue()  # (source, text)
    inbox_path = state_dir / "inbox.jsonl"

    def receiver():
        while not end_event.is_set():
            try:
                msg = channel.recv(timeout=0.5)
            except TimeoutError:
                continue
            except ChannelClosed:
                # 通道已断，结束 receiver，避免对已关闭通道空转轮询（原 bare-Exception 会僵尸循环）。
                end_event.set()
                return
            except Exception as e:
                print(f"[aigenora] free-mode receiver recv error: {e}", file=sys.stderr)
                continue
            try:
                if validate:
                    _validate(spec, msg, "both")
                if _is_control_end(msg):
                    hooks.proto_on_end()
                    end_event.set()
                    return
                hooks.proto_on_message(msg)
            except ValidationError as e:
                # 对方发了违反 spec 的消息：容错跳过（继续聊），但记录便于排查，不再静默吞。
                print(f"[aigenora] free-mode receiver dropped invalid message: {e}", file=sys.stderr)
                continue
            except Exception as e:
                # hooks 内部 bug 或未知错误：记录后继续，避免 receiver 静默僵尸。
                print(f"[aigenora] free-mode receiver hook error: {e}", file=sys.stderr)
                continue

    def stdin_producer():
        while not end_event.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return
            input_q.put(("stdin", line.rstrip("\n")))

    def inbox_producer():
        offset = 0
        while not end_event.is_set():
            try:
                if inbox_path.exists():
                    with open(inbox_path, "r", encoding="utf-8") as f:
                        f.seek(offset)
                        for raw in f:
                            line = raw.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            text = str(rec.get("text", ""))
                            if text:
                                input_q.put(("inbox", text))
                        offset = f.tell()
            except Exception:
                pass
            end_event.wait(timeout=0.1)

    def sender():
        while not end_event.is_set():
            try:
                source, text = input_q.get(timeout=0.2)
            except _queue.Empty:
                continue
            if text.strip() == "/quit":
                end_msg = {"action": "end"}
                if validate:
                    _validate(spec, end_msg, "both")
                channel.send(end_msg)
                hooks.proto_on_end()
                end_event.set()
                return
            seq[0] += 1
            msg = {"action": "chat", "text": text, "seq": seq[0]}
            if validate:
                _validate(spec, msg, "both")
            channel.send(msg)
            try:
                hooks.proto_on_send(msg)
            except Exception:
                pass

    if role == "host":
        join_msg = channel.recv()
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        ready = {"action": "ready"}
        if validate:
            _validate(spec, ready, "host_to_guest")
        channel.send(ready)
    else:
        join_msg = {"action": "join"}
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        channel.send(join_msg)
        ready_msg = channel.recv()
        if validate:
            _validate(spec, ready_msg, "host_to_guest")

    t_recv = threading.Thread(target=receiver, daemon=True)
    t_send = threading.Thread(target=sender, daemon=True)
    t_stdin = threading.Thread(target=stdin_producer, daemon=True)
    t_inbox = threading.Thread(target=inbox_producer, daemon=True)
    t_recv.start()
    t_send.start()
    t_stdin.start()
    t_inbox.start()
    t_recv.join()
    t_send.join()
    _snapshot_phase(hooks, "game_over", "Free mode session ended")
    return {"metadata": metadata, "state_dir": str(state_dir), "game_over": True}


async def _run_free_async(
    spec: dict[str, Any],
    proto_dir: Path,
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
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"{role}-{int(time.time() * 1000)}")
    hooks.proto_init(options, role, args or [], state_dir)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, role, spec, state_dir)
    metadata = hooks.proto_host_metadata()
    seq = [0]

    if role == "host":
        join_msg = await channel.recv()
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        ready = {"action": "ready"}
        if validate:
            _validate(spec, ready, "host_to_guest")
        await channel.send(ready)
    else:
        join_msg = {"action": "join"}
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        await channel.send(join_msg)
        ready_msg = await channel.recv()
        if validate:
            _validate(spec, ready_msg, "host_to_guest")

    loop = asyncio.get_event_loop()
    inbox_path = state_dir / "inbox.jsonl"
    input_q: asyncio.Queue = asyncio.Queue()
    end_event = asyncio.Event()

    async def receiver():
        while not end_event.is_set():
            msg = await channel.recv()
            if validate:
                _validate(spec, msg, "both")
            if _is_control_end(msg):
                hooks.proto_on_end()
                end_event.set()
                return
            hooks.proto_on_message(msg)

    async def stdin_producer():
        while not end_event.is_set():
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                return
            await input_q.put(("stdin", line.rstrip("\n")))

    async def inbox_producer():
        offset = 0
        while not end_event.is_set():
            try:
                if inbox_path.exists():
                    def _read():
                        nonlocal offset
                        items: list[str] = []
                        with open(inbox_path, "r", encoding="utf-8") as f:
                            f.seek(offset)
                            for raw in f:
                                line = raw.strip()
                                if not line:
                                    continue
                                try:
                                    rec = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                txt = str(rec.get("text", ""))
                                if txt:
                                    items.append(txt)
                            offset = f.tell()
                        return items
                    items = await loop.run_in_executor(None, _read)
                    for txt in items:
                        await input_q.put(("inbox", txt))
            except Exception:
                pass
            try:
                await asyncio.wait_for(end_event.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

    async def sender():
        while not end_event.is_set():
            try:
                source, text = await asyncio.wait_for(input_q.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if text.strip() == "/quit":
                end_msg = {"action": "end"}
                if validate:
                    _validate(spec, end_msg, "both")
                await channel.send(end_msg)
                hooks.proto_on_end()
                end_event.set()
                return
            seq[0] += 1
            msg = {"action": "chat", "text": text, "seq": seq[0]}
            if validate:
                _validate(spec, msg, "both")
            await channel.send(msg)
            try:
                hooks.proto_on_send(msg)
            except Exception:
                pass

    tasks = [
        asyncio.create_task(receiver()),
        asyncio.create_task(sender()),
        asyncio.create_task(stdin_producer()),
        asyncio.create_task(inbox_producer()),
    ]
    done, pending = await asyncio.wait(
        [tasks[0], tasks[1]],
        return_when=asyncio.FIRST_COMPLETED,
    )
    end_event.set()
    for t in tasks:
        if not t.done():
            t.cancel()
    _snapshot_phase(hooks, "game_over", "Free mode session ended")
    return {"metadata": metadata, "state_dir": str(state_dir), "game_over": True}


def _run_request_response_sync(
    spec: dict[str, Any],
    proto_dir: Path,
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
    """request_response sync engine: join/ready handshake -> Guest request -> Host response -> forced end."""
    if role == "host":
        return _run_rr_sync_host(
            spec, proto_dir, channel, options, args, state_base, validate,
            event_bus=event_bus, coach=coach, pace=pace,
        )
    return _run_rr_sync_guest(
        spec, proto_dir, channel, options, args, state_base, validate,
        event_bus=event_bus, coach=coach, pace=pace,
    )


def _run_rr_sync_host(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: JsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"host-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "host", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "host", spec, state_dir)
    metadata = hooks.proto_host_metadata()

    # join/ready handshake
    join_msg = channel.recv()
    if validate:
        _validate(spec, join_msg, "guest_to_host")
    _display(hooks, join_msg, "received")
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": join_msg})
    ready_result = _as_result(hooks.proto_host_handle_join(join_msg))
    if ready_result.response is None:
        raise RuntimeError("host join hook did not return ready response")
    if validate:
        _validate(spec, ready_result.response, "host_to_guest")
    _display(hooks, ready_result.response, "sent")
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": ready_result.response})
    channel.send(ready_result.response)

    # Wait for Guest request
    request_msg = channel.recv()
    if validate:
        _validate(spec, request_msg, "guest_to_host")
    _display(hooks, request_msg, "received")
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": request_msg})

    # Host handles request and returns response
    result = _as_result(hooks.proto_host_handle(request_msg))
    if result.response is None:
        _emit(event_bus, "session_ended", {"game_over": False, "reason": "abort"})
        _snapshot_phase(hooks, "aborted", "Host did not return response")
        return {"metadata": metadata, "state_dir": str(state_dir), "game_over": False}
    if validate:
        _validate(spec, result.response, "host_to_guest")
    _display(hooks, result.response, "sent")
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": result.response})
    channel.send(result.response)

    # Forced end, regardless of game_over returned by hooks
    _emit(event_bus, "session_ended", {"game_over": True, "reason": "request_response_complete"})
    _snapshot_phase(hooks, "game_over", "Request-response completed")
    return {"metadata": metadata, "state_dir": str(state_dir), "game_over": True}


def _run_rr_sync_guest(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: JsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"guest-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "guest", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "guest", spec, state_dir)

    # join/ready handshake
    join_msg = hooks.proto_guest_join_message()
    if validate:
        _validate(spec, join_msg, "guest_to_host")
    _display(hooks, join_msg, "sent")
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": join_msg})
    ready_msg = channel.send_wait(join_msg)
    if validate:
        _validate(spec, ready_msg, "host_to_guest")
    _display(hooks, ready_msg, "received")
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": ready_msg})
    hooks.proto_guest_handle_ready(ready_msg)

    # Guest sends request
    request = hooks.proto_guest_first_action()
    if not request:
        _emit(event_bus, "session_ended", {"game_over": False, "reason": "abort"})
        _snapshot_phase(hooks, "aborted", "Guest did not send request")
        return {"state_dir": str(state_dir), "game_over": False}
    if validate:
        _validate(spec, request, "guest_to_host")
    _display(hooks, request, "sent")
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": request})

    # Wait for Host response
    response_msg = channel.send_wait(request)
    if validate:
        _validate(spec, response_msg, "host_to_guest")
    _display(hooks, response_msg, "received")
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": response_msg})

    # Guest may optionally handle response (proto_guest_handle), but does not drive subsequent messages
    hooks.proto_guest_handle(response_msg)

    # Forced end
    _emit(event_bus, "session_ended", {"game_over": True, "reason": "request_response_complete"})
    _snapshot_phase(hooks, "game_over", "Request-response completed")
    return {"state_dir": str(state_dir), "game_over": True}


async def _run_request_response_async(
    spec: dict[str, Any],
    proto_dir: Path,
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
    """request_response async engine."""
    if role == "host":
        return await _run_rr_async_host(
            spec, proto_dir, channel, options, args, state_base, validate,
            event_bus=event_bus, coach=coach, pace=pace,
            heartbeat_interval=heartbeat_interval, heartbeat_timeout=heartbeat_timeout,
        )
    return await _run_rr_async_guest(
        spec, proto_dir, channel, options, args, state_base, validate,
        event_bus=event_bus, coach=coach, pace=pace,
        heartbeat_interval=heartbeat_interval, heartbeat_timeout=heartbeat_timeout,
    )


async def _run_rr_async_host(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: AsyncJsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
    heartbeat_interval: float,
    heartbeat_timeout: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"host-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "host", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "host", spec, state_dir)
    metadata = hooks.proto_host_metadata()
    channel = await _maybe_wrap_heartbeat(
        channel, heartbeat_interval, heartbeat_timeout, event_bus, hooks,
    )

    # join/ready handshake
    try:
        join_msg = await channel.recv()
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        _display(hooks, join_msg, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": join_msg})
        ready_result = _as_result(hooks.proto_host_handle_join(join_msg))
        if ready_result.response is None:
            raise RuntimeError("host join hook did not return ready response")
        if validate:
            _validate(spec, ready_result.response, "host_to_guest")
        _display(hooks, ready_result.response, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": ready_result.response})
        await channel.send(ready_result.response)

        # Wait for Guest request
        request_msg = await channel.recv()
        if validate:
            _validate(spec, request_msg, "guest_to_host")
        _display(hooks, request_msg, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": request_msg})

        # Host handles request -> response
        result = _as_result(hooks.proto_host_handle(request_msg))
        if result.response is None:
            _emit(event_bus, "session_ended", {"game_over": False, "reason": "abort"})
            _snapshot_phase(hooks, "aborted", "Host did not return response")
            return {"metadata": metadata, "state_dir": str(state_dir), "game_over": False}
        if validate:
            _validate(spec, result.response, "host_to_guest")
        _display(hooks, result.response, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": result.response})
        await channel.send(result.response)

        _emit(event_bus, "session_ended", {"game_over": True, "reason": "request_response_complete"})
        _snapshot_phase(hooks, "game_over", "Request-response completed")
        return {"metadata": metadata, "state_dir": str(state_dir), "game_over": True}
    except ChannelClosed:
        return _handle_peer_disconnect(hooks, event_bus, metadata=metadata, state_dir=str(state_dir))


async def _run_rr_async_guest(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: AsyncJsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
    heartbeat_interval: float,
    heartbeat_timeout: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"guest-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "guest", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "guest", spec, state_dir)
    channel = await _maybe_wrap_heartbeat(
        channel, heartbeat_interval, heartbeat_timeout, event_bus, hooks,
    )

    # join/ready handshake
    try:
        join_msg = hooks.proto_guest_join_message()
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        _display(hooks, join_msg, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": join_msg})
        ready_msg = await channel.send_wait(join_msg)
        if validate:
            _validate(spec, ready_msg, "host_to_guest")
        _display(hooks, ready_msg, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": ready_msg})
        hooks.proto_guest_handle_ready(ready_msg)

        # Guest sends request
        request = hooks.proto_guest_first_action()
        if not request:
            _emit(event_bus, "session_ended", {"game_over": False, "reason": "abort"})
            _snapshot_phase(hooks, "aborted", "Guest did not send request")
            return {"state_dir": str(state_dir), "game_over": False}
        if validate:
            _validate(spec, request, "guest_to_host")
        _display(hooks, request, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": request})

        # Wait for Host response
        response_msg = await channel.send_wait(request)
        if validate:
            _validate(spec, response_msg, "host_to_guest")
        _display(hooks, response_msg, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": response_msg})
        hooks.proto_guest_handle(response_msg)

        _emit(event_bus, "session_ended", {"game_over": True, "reason": "request_response_complete"})
        _snapshot_phase(hooks, "game_over", "Request-response completed")
        return {"state_dir": str(state_dir), "game_over": True}
    except ChannelClosed:
        return _handle_peer_disconnect(hooks, event_bus, state_dir=str(state_dir))

def _run_simultaneous_round_sync(
    spec: dict[str, Any],
    proto_dir: Path,
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
    """simultaneous_round sync engine: the engine takes over the commit-reveal barrier."""
    if role == "host":
        return _run_sr_sync_host(
            spec, proto_dir, channel, options, args, state_base, validate,
            event_bus=event_bus, coach=coach, pace=pace,
        )
    return _run_sr_sync_guest(
        spec, proto_dir, channel, options, args, state_base, validate,
        event_bus=event_bus, coach=coach, pace=pace,
    )


def _run_sr_sync_host(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: JsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"host-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "host", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "host", spec, state_dir)
    metadata = hooks.proto_host_metadata()
    state: dict[str, Any] = {}
    round_num = 0
    _value_field = _resolve_value_field(spec)

    # join/ready handshake
    join_msg = channel.recv()
    if validate:
        _validate(spec, join_msg, "guest_to_host")
    _display(hooks, join_msg, "received")
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": join_msg})
    ready_result = _as_result(hooks.proto_host_handle_join(join_msg))
    if ready_result.response is None:
        raise RuntimeError("host join hook did not return ready response")
    if validate:
        _validate(spec, ready_result.response, "host_to_guest")
    _display(hooks, ready_result.response, "sent")
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": ready_result.response})
    channel.send(ready_result.response)

    while True:
        host_value = hooks.proto_round_value(round_num, state)
        nonce = random_nonce()
        h = sha256(f"{host_value}:{nonce}")

        # 2. Send Host commit
        commit_msg = {"action": "commit", "round": round_num, "hash": h}
        if validate:
            _validate(spec, commit_msg, "host_to_guest")
        _display(hooks, commit_msg, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": commit_msg})
        channel.send(commit_msg)

        # 3. Receive Guest commit
        guest_commit = channel.recv()
        if validate:
            _validate(spec, guest_commit, "guest_to_host")
        _display(hooks, guest_commit, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": guest_commit})

        # 4. Send Host reveal
        reveal_msg = {"action": "reveal", "round": round_num, _value_field: host_value, "nonce": nonce}
        if validate:
            _validate(spec, reveal_msg, "host_to_guest")
        _display(hooks, reveal_msg, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": reveal_msg})
        channel.send(reveal_msg)

        # 5. Receive Guest reveal
        guest_reveal = channel.recv()
        if validate:
            _validate(spec, guest_reveal, "guest_to_host")
        _display(hooks, guest_reveal, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": guest_reveal})

        # 6. Validate Guest commit hash
        guest_value = guest_reveal.get(_value_field)
        guest_nonce = guest_reveal.get("nonce")
        guest_hash = guest_commit.get("hash")
        expected_hash = sha256(f"{guest_value}:{guest_nonce}")
        if expected_hash != guest_hash:
            _emit(event_bus, "commit_mismatch_detected", {
                "round": round_num, "role": "guest",
                "expected": expected_hash, "actual": guest_hash,
            })
            _snapshot_phase(hooks, "aborted", "Commit mismatch detected", round=round_num)
            abort_msg = {"action": "error", "round": round_num, "reason": "commit_mismatch"}
            channel.send(abort_msg)
            return {"metadata": metadata, "state_dir": str(state_dir), "game_over": False}

        # 7. Judge
        judge_result = _as_result(hooks.proto_round_judge(round_num, host_value, guest_value, state))
        if judge_result.response:
            if validate:
                _validate(spec, judge_result.response, "host_to_guest")
            _display(hooks, judge_result.response, "sent")
            _emit(event_bus, "protocol_message", {"direction": "sent", "msg": judge_result.response})
            channel.send(judge_result.response)

        round_num += 1
        if judge_result.game_over or judge_result.abort:
            # After Host game_over, send an end signal so Guest exits
            end_msg = {"action": "end"}
            channel.send(end_msg)
            _emit(event_bus, "session_ended", {
                "game_over": True,
                "reason": "abort" if judge_result.abort else "game_over",
            })
            _snapshot_phase(hooks, "aborted" if judge_result.abort else "game_over",
                            "Session ended", reason="abort" if judge_result.abort else "game_over")
            return {"metadata": metadata, "state_dir": str(state_dir), "game_over": judge_result.game_over, "winner": _winner_of(judge_result.response)}

        if pace > 0:
            time.sleep(pace)


def _run_sr_sync_guest(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: JsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"guest-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "guest", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "guest", spec, state_dir)
    state: dict[str, Any] = {}
    round_num = 0
    _value_field = _resolve_value_field(spec)

    # join/ready handshake
    join_msg = hooks.proto_guest_join_message()
    if validate:
        _validate(spec, join_msg, "guest_to_host")
    _display(hooks, join_msg, "sent")
    _emit(event_bus, "protocol_message", {"direction": "sent", "msg": join_msg})
    ready_msg = channel.send_wait(join_msg)
    if validate:
        _validate(spec, ready_msg, "host_to_guest")
    _display(hooks, ready_msg, "received")
    _emit(event_bus, "protocol_message", {"direction": "received", "msg": ready_msg})
    hooks.proto_guest_handle_ready(ready_msg)

    pending_host_commit = None
    result_msg: dict[str, Any] = {}
    while True:
        guest_value = hooks.proto_round_value(round_num, state)
        nonce = random_nonce()
        h = sha256(f"{guest_value}:{nonce}")

        # Receive Host commit (or use the message prefetched from the previous round)
        if pending_host_commit is not None:
            host_commit = pending_host_commit
            pending_host_commit = None
        else:
            host_commit = channel.recv()
        if _is_control_end(host_commit):
            _emit(event_bus, "session_ended", {"game_over": True, "reason": "game_over"})
            _snapshot_phase(hooks, "game_over", "Session completed")
            # result_msg 是上一轮收到的 round_result（含 game_winner），透传给 host 触发 ELO result 上报
            return {"state_dir": str(state_dir), "game_over": True, "winner": _winner_of(result_msg)}
        if validate:
            _validate(spec, host_commit, "host_to_guest")
        _display(hooks, host_commit, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": host_commit})

        # Send Guest commit
        commit_msg = {"action": "commit", "round": round_num, "hash": h}
        if validate:
            _validate(spec, commit_msg, "guest_to_host")
        _display(hooks, commit_msg, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": commit_msg})
        channel.send(commit_msg)

        # Receive Host reveal
        host_reveal = channel.recv()
        if validate:
            _validate(spec, host_reveal, "host_to_guest")
        _display(hooks, host_reveal, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": host_reveal})

        # Validate Host commit hash
        host_value = host_reveal.get(_value_field)
        host_nonce = host_reveal.get("nonce")
        host_hash = host_commit.get("hash")
        expected_hash = sha256(f"{host_value}:{host_nonce}")
        if expected_hash != host_hash:
            _emit(event_bus, "commit_mismatch_detected", {
                "round": round_num, "role": "host",
                "expected": expected_hash, "actual": host_hash,
            })
            _snapshot_phase(hooks, "aborted", "Commit mismatch detected", round=round_num)
            return {"state_dir": str(state_dir), "game_over": False}

        # Send Guest reveal
        reveal_msg = {"action": "reveal", "round": round_num, _value_field: guest_value, "nonce": nonce}
        if validate:
            _validate(spec, reveal_msg, "guest_to_host")
        _display(hooks, reveal_msg, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": reveal_msg})

        # Receive round_result
        result_msg = channel.send_wait(reveal_msg)
        if validate:
            _validate(spec, result_msg, "host_to_guest")
        _display(hooks, result_msg, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": result_msg})
        hooks.proto_guest_handle(result_msg)

        round_num += 1
        if pace > 0:
            time.sleep(pace)
        # Prefetch next message to decide whether it is end or the next round commit
        pending_host_commit = channel.recv()


async def _run_simultaneous_round_async(
    spec: dict[str, Any],
    proto_dir: Path,
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
    """simultaneous_round async engine."""
    if role == "host":
        return await _run_sr_async_host(
            spec, proto_dir, channel, options, args, state_base, validate,
            event_bus=event_bus, coach=coach, pace=pace,
            heartbeat_interval=heartbeat_interval, heartbeat_timeout=heartbeat_timeout,
        )
    return await _run_sr_async_guest(
        spec, proto_dir, channel, options, args, state_base, validate,
        event_bus=event_bus, coach=coach, pace=pace,
        heartbeat_interval=heartbeat_interval, heartbeat_timeout=heartbeat_timeout,
    )


async def _run_sr_async_host(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: AsyncJsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
    heartbeat_interval: float,
    heartbeat_timeout: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"host-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "host", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "host", spec, state_dir)
    metadata = hooks.proto_host_metadata()
    channel = await _maybe_wrap_heartbeat(
        channel, heartbeat_interval, heartbeat_timeout, event_bus, hooks,
    )
    state: dict[str, Any] = {}
    round_num = 0
    _value_field = _resolve_value_field(spec)

    # join/ready
    try:
        join_msg = await channel.recv()
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        _display(hooks, join_msg, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": join_msg})
        ready_result = _as_result(hooks.proto_host_handle_join(join_msg))
        if ready_result.response is None:
            raise RuntimeError("host join hook did not return ready response")
        if validate:
            _validate(spec, ready_result.response, "host_to_guest")
        _display(hooks, ready_result.response, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": ready_result.response})
        await channel.send(ready_result.response)

        while True:
            host_value = hooks.proto_round_value(round_num, state)
            nonce = random_nonce()
            h = sha256(f"{host_value}:{nonce}")

            commit_msg = {"action": "commit", "round": round_num, "hash": h}
            if validate:
                _validate(spec, commit_msg, "host_to_guest")
            _display(hooks, commit_msg, "sent")
            _emit(event_bus, "protocol_message", {"direction": "sent", "msg": commit_msg})

            guest_commit = await channel.send_wait(commit_msg)
            if validate:
                _validate(spec, guest_commit, "guest_to_host")
            _display(hooks, guest_commit, "received")
            _emit(event_bus, "protocol_message", {"direction": "received", "msg": guest_commit})

            reveal_msg = {"action": "reveal", "round": round_num, _value_field: host_value, "nonce": nonce}
            if validate:
                _validate(spec, reveal_msg, "host_to_guest")
            _display(hooks, reveal_msg, "sent")
            _emit(event_bus, "protocol_message", {"direction": "sent", "msg": reveal_msg})

            guest_reveal = await channel.send_wait(reveal_msg)
            if validate:
                _validate(spec, guest_reveal, "guest_to_host")
            _display(hooks, guest_reveal, "received")
            _emit(event_bus, "protocol_message", {"direction": "received", "msg": guest_reveal})

            guest_value = guest_reveal.get(_value_field)
            guest_nonce = guest_reveal.get("nonce")
            guest_hash = guest_commit.get("hash")
            expected_hash = sha256(f"{guest_value}:{guest_nonce}")
            if expected_hash != guest_hash:
                _emit(event_bus, "commit_mismatch_detected", {
                    "round": round_num, "role": "guest",
                    "expected": expected_hash, "actual": guest_hash,
                })
                _snapshot_phase(hooks, "aborted", "Commit mismatch detected", round=round_num)
                abort_msg = {"action": "error", "round": round_num, "reason": "commit_mismatch"}
                await channel.send(abort_msg)
                return {"metadata": metadata, "state_dir": str(state_dir), "game_over": False}

            judge_result = _as_result(hooks.proto_round_judge(round_num, host_value, guest_value, state))
            if judge_result.response:
                if validate:
                    _validate(spec, judge_result.response, "host_to_guest")
                _display(hooks, judge_result.response, "sent")
                _emit(event_bus, "protocol_message", {"direction": "sent", "msg": judge_result.response})
                await channel.send(judge_result.response)

            round_num += 1
            if judge_result.game_over or judge_result.abort:
                # After Host game_over, send an end signal so Guest exits
                end_msg = {"action": "end"}
                await channel.send(end_msg)
                _emit(event_bus, "session_ended", {
                    "game_over": True,
                    "reason": "abort" if judge_result.abort else "game_over",
                })
                _snapshot_phase(hooks, "aborted" if judge_result.abort else "game_over",
                                "Session ended", reason="abort" if judge_result.abort else "game_over")
                return {"metadata": metadata, "state_dir": str(state_dir), "game_over": judge_result.game_over, "winner": _winner_of(judge_result.response)}

            if pace > 0:
                await asyncio.sleep(pace)
    except ChannelClosed:
        return _handle_peer_disconnect(hooks, event_bus, metadata=metadata, state_dir=str(state_dir))


async def _run_sr_async_guest(
    spec: dict[str, Any],
    proto_dir: Path,
    channel: AsyncJsonLineChannel,
    opts: dict[str, Any],
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    *,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
    heartbeat_interval: float,
    heartbeat_timeout: float,
) -> dict[str, Any]:
    hooks = load_hooks(proto_dir)
    state_dir = _state_dir(state_base, f"guest-{int(time.time() * 1000)}")
    decision_config = _resolve_decision_config(spec, coach)
    hooks.proto_init(opts, "guest", args or [], state_dir, decision_config)
    hooks.timing = spec.get("timing")
    _snapshot_init(hooks, "guest", spec, state_dir)
    channel = await _maybe_wrap_heartbeat(
        channel, heartbeat_interval, heartbeat_timeout, event_bus, hooks,
    )
    state: dict[str, Any] = {}
    round_num = 0
    _value_field = _resolve_value_field(spec)

    try:
        join_msg = hooks.proto_guest_join_message()
        if validate:
            _validate(spec, join_msg, "guest_to_host")
        _display(hooks, join_msg, "sent")
        _emit(event_bus, "protocol_message", {"direction": "sent", "msg": join_msg})
        ready_msg = await channel.send_wait(join_msg)
        if validate:
            _validate(spec, ready_msg, "host_to_guest")
        _display(hooks, ready_msg, "received")
        _emit(event_bus, "protocol_message", {"direction": "received", "msg": ready_msg})
        hooks.proto_guest_handle_ready(ready_msg)

        pending_host_commit = None
        result_msg: dict[str, Any] = {}
        while True:
            guest_value = hooks.proto_round_value(round_num, state)
            nonce = random_nonce()
            h = sha256(f"{guest_value}:{nonce}")

            if pending_host_commit is not None:
                host_commit = pending_host_commit
                pending_host_commit = None
            else:
                host_commit = await channel.recv()
            if _is_control_end(host_commit):
                _emit(event_bus, "session_ended", {"game_over": True, "reason": "game_over"})
                _snapshot_phase(hooks, "game_over", "Session completed")
                # result_msg 是上一轮收到的 round_result（含 game_winner），透传给 guest 触发 ELO result 上报
                return {"state_dir": str(state_dir), "game_over": True, "winner": _winner_of(result_msg)}
            if validate:
                _validate(spec, host_commit, "host_to_guest")
            _display(hooks, host_commit, "received")
            _emit(event_bus, "protocol_message", {"direction": "received", "msg": host_commit})

            commit_msg = {"action": "commit", "round": round_num, "hash": h}
            if validate:
                _validate(spec, commit_msg, "guest_to_host")
            _display(hooks, commit_msg, "sent")
            _emit(event_bus, "protocol_message", {"direction": "sent", "msg": commit_msg})
            await channel.send(commit_msg)

            host_reveal = await channel.recv()
            if validate:
                _validate(spec, host_reveal, "host_to_guest")
            _display(hooks, host_reveal, "received")
            _emit(event_bus, "protocol_message", {"direction": "received", "msg": host_reveal})

            host_value = host_reveal.get(_value_field)
            host_nonce = host_reveal.get("nonce")
            host_hash = host_commit.get("hash")
            expected_hash = sha256(f"{host_value}:{host_nonce}")
            if expected_hash != host_hash:
                _emit(event_bus, "commit_mismatch_detected", {
                    "round": round_num, "role": "host",
                    "expected": expected_hash, "actual": host_hash,
                })
                _snapshot_phase(hooks, "aborted", "Commit mismatch detected", round=round_num)
                return {"state_dir": str(state_dir), "game_over": False}

            reveal_msg = {"action": "reveal", "round": round_num, _value_field: guest_value, "nonce": nonce}
            if validate:
                _validate(spec, reveal_msg, "guest_to_host")
            _display(hooks, reveal_msg, "sent")
            _emit(event_bus, "protocol_message", {"direction": "sent", "msg": reveal_msg})

            result_msg = await channel.send_wait(reveal_msg)
            if validate:
                _validate(spec, result_msg, "host_to_guest")
            _display(hooks, result_msg, "received")
            _emit(event_bus, "protocol_message", {"direction": "received", "msg": result_msg})
            hooks.proto_guest_handle(result_msg)

            round_num += 1
            if pace > 0:
                await asyncio.sleep(pace)
            pending_host_commit = await channel.recv()
    except ChannelClosed:
        return _handle_peer_disconnect(hooks, event_bus, state_dir=str(state_dir))


# Engine registry
ENGINES_SYNC: dict[str, Callable[..., dict[str, Any]]] = {
    "session_loop": _run_session_loop_sync,
    "free": _run_free,
    "request_response": _run_request_response_sync,
    "simultaneous_round": _run_simultaneous_round_sync,
}

ENGINES_ASYNC: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "session_loop": _run_session_loop_async,
    "free": _run_free_async,
    "request_response": _run_request_response_async,
    "simultaneous_round": _run_simultaneous_round_async,
}


def parse_options(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("--options must be a JSON object")
    return data


def _dispatch_sync(
    role: str,
    protocol_dir: str | Path,
    channel: JsonLineChannel,
    options: dict[str, Any] | None,
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
) -> dict[str, Any]:
    proto_dir = Path(protocol_dir)
    spec = load_spec(proto_dir / "spec.json")
    opts = options or {}
    validate_options(spec, opts)
    mode = resolve_flow_mode(spec)
    engine = ENGINES_SYNC.get(mode)
    if engine is None:
        raise ValidationError(f"flow.mode {mode!r} has no sync engine implementation")
    return engine(
        spec, proto_dir, channel, opts, args, state_base, validate, role,
        event_bus=event_bus, coach=coach, pace=pace,
    )


async def _dispatch_async(
    role: str,
    protocol_dir: str | Path,
    channel: AsyncJsonLineChannel,
    options: dict[str, Any] | None,
    args: list[str] | None,
    state_base: str | Path | None,
    validate: bool,
    event_bus: EventBus | None,
    coach: bool,
    pace: float,
    heartbeat_interval: float,
    heartbeat_timeout: float,
) -> dict[str, Any]:
    proto_dir = Path(protocol_dir)
    spec = load_spec(proto_dir / "spec.json")
    opts = options or {}
    validate_options(spec, opts)
    mode = resolve_flow_mode(spec)
    engine = ENGINES_ASYNC.get(mode)
    if engine is None:
        raise ValidationError(f"flow.mode {mode!r} has no async engine implementation")
    return await engine(
        spec, proto_dir, channel, opts, args, state_base, validate, role,
        event_bus=event_bus, coach=coach, pace=pace,
        heartbeat_interval=heartbeat_interval, heartbeat_timeout=heartbeat_timeout,
    )


def run_host(
    protocol_dir: str | Path,
    channel: JsonLineChannel,
    options: dict[str, Any] | None = None,
    args: list[str] | None = None,
    state_base: str | Path | None = None,
    validate: bool = True,
    event_bus: EventBus | None = None,
    coach: bool = False,
    pace: float = 0,
) -> dict[str, Any]:
    return _dispatch_sync(
        "host", protocol_dir, channel, options, args, state_base, validate,
        event_bus, coach, pace,
    )


def run_guest(
    protocol_dir: str | Path,
    channel: JsonLineChannel,
    options: dict[str, Any] | None = None,
    args: list[str] | None = None,
    state_base: str | Path | None = None,
    validate: bool = True,
    event_bus: EventBus | None = None,
    coach: bool = False,
    pace: float = 0,
) -> dict[str, Any]:
    return _dispatch_sync(
        "guest", protocol_dir, channel, options, args, state_base, validate,
        event_bus, coach, pace,
    )


async def run_host_async(
    protocol_dir: str | Path,
    channel: AsyncJsonLineChannel,
    options: dict[str, Any] | None = None,
    args: list[str] | None = None,
    state_base: str | Path | None = None,
    validate: bool = True,
    event_bus: EventBus | None = None,
    coach: bool = False,
    pace: float = 0,
    heartbeat_interval: float = 0,
    heartbeat_timeout: float = 0,
) -> dict[str, Any]:
    return await _dispatch_async(
        "host", protocol_dir, channel, options, args, state_base, validate,
        event_bus, coach, pace, heartbeat_interval, heartbeat_timeout,
    )


async def run_guest_async(
    protocol_dir: str | Path,
    channel: AsyncJsonLineChannel,
    options: dict[str, Any] | None = None,
    args: list[str] | None = None,
    state_base: str | Path | None = None,
    validate: bool = True,
    event_bus: EventBus | None = None,
    coach: bool = False,
    pace: float = 0,
    heartbeat_interval: float = 0,
    heartbeat_timeout: float = 0,
) -> dict[str, Any]:
    return await _dispatch_async(
        "guest", protocol_dir, channel, options, args, state_base, validate,
        event_bus, coach, pace, heartbeat_interval, heartbeat_timeout,
    )


__all__ = [
    "run_host",
    "run_guest",
    "run_host_async",
    "run_guest_async",
    "parse_options",
    "ValidationError",
    "ENGINES_SYNC",
    "ENGINES_ASYNC",
]
