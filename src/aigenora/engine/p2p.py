from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from aigenora.proto.sdk import EventBus, SnapshotBus


CHAT_ALPN = b"/agent-chat/1"


class ChannelClosed(RuntimeError):
    pass


class JsonLineChannel:
    def send(self, msg: dict[str, Any]) -> None:
        raise NotImplementedError

    def recv(self, timeout: float | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def send_wait(self, msg: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        self.send(msg)
        return self.recv(timeout)

    def close(self) -> None:
        return None


class AsyncJsonLineChannel:
    async def send(self, msg: dict[str, Any]) -> None:
        raise NotImplementedError

    async def recv(self, timeout: float | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def send_wait(self, msg: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        await self.send(msg)
        return await self.recv(timeout)

    async def close(self) -> None:
        return None


class MemoryChannel(JsonLineChannel):
    def __init__(self, incoming: "queue.Queue[dict[str, Any] | None]", outgoing: "queue.Queue[dict[str, Any] | None]"):
        self.incoming = incoming
        self.outgoing = outgoing

    def send(self, msg: dict[str, Any]) -> None:
        self.outgoing.put(msg)

    def recv(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            item = self.incoming.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for peer message") from exc
        if item is None:
            raise ChannelClosed("peer closed")
        return item

    def close(self) -> None:
        self.outgoing.put(None)


def memory_duplex() -> tuple[MemoryChannel, MemoryChannel]:
    a_to_b: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
    b_to_a: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
    return MemoryChannel(b_to_a, a_to_b), MemoryChannel(a_to_b, b_to_a)


@dataclass
class IrohTicket:
    ticket: str
    node_id: str | None = None
    relay: str | None = None


class IrohJsonLineChannel(AsyncJsonLineChannel):
    MAX_JSON_LINE_BYTES = 8 * 1024 * 1024

    def __init__(self, send_stream: Any, recv_stream: Any):
        self.send_stream = send_stream
        self.recv_stream = recv_stream
        self._buf = bytearray()

    async def async_send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        await self.send_stream.write_all(line)
        flush = getattr(self.send_stream, "flush", None)
        if flush:
            await flush()

    async def async_recv(self) -> dict[str, Any]:
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                if idx > self.MAX_JSON_LINE_BYTES:
                    raise ChannelClosed("iroh JSON frame exceeds size limit")
                raw = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                if not raw:
                    continue
                return json.loads(raw.decode("utf-8"))
            try:
                chunk = await self.recv_stream.read(4096)
            except Exception:
                # iroh FFI raises IrohError when the stream/connection closes; treat any
                # read failure as a closed channel so _reader exits cleanly instead of
                # leaking a traceback that makes `session list` mislabel a finished game
                # as "crashed".
                raise ChannelClosed("iroh stream read failed")
            if not chunk:
                raise ChannelClosed("iroh stream closed")
            self._buf.extend(chunk)
            if len(self._buf) > self.MAX_JSON_LINE_BYTES and b"\n" not in self._buf:
                raise ChannelClosed("iroh JSON frame exceeds size limit")

    async def send(self, msg: dict[str, Any]) -> None:
        await self.async_send(msg)

    async def recv(self, timeout: float | None = None) -> dict[str, Any]:
        coro = self.async_recv()
        if timeout:
            return await asyncio.wait_for(coro, timeout)
        return await coro

    async def close(self) -> None:
        finish = getattr(self.send_stream, "finish", None)
        if finish:
            await finish()


class AsyncReplayChannel(AsyncJsonLineChannel):
    def __init__(self, inner: AsyncJsonLineChannel, first: dict[str, Any]):
        self.inner = inner
        self.first = first

    async def send(self, msg: dict[str, Any]) -> None:
        await self.inner.send(msg)

    async def recv(self, timeout: float | None = None) -> dict[str, Any]:
        if self.first is not None:
            msg = self.first
            self.first = None
            return msg
        return await self.inner.recv(timeout)

    async def close(self) -> None:
        await self.inner.close()


class IrohRuntime:
    """Thin wrapper around official iroh Python binding.

    The concrete host/guest CLI uses this class. Tests mostly use MemoryChannel,
    because NAT traversal needs real network conditions and separate processes.
    """

    def __init__(self):
        self.iroh = None
        self.iroh_ffi = None

    def _import(self) -> None:
        if self.iroh is not None:
            return
        import iroh  # type: ignore
        import iroh.iroh_ffi as iroh_ffi  # type: ignore

        self.iroh = iroh
        self.iroh_ffi = iroh_ffi

    async def create_node(self, protocols: dict[bytes, Any] | None = None) -> Any:
        self._import()
        self.iroh_ffi._UNIFFI_GLOBAL_EVENT_LOOP = asyncio.get_running_loop()
        if protocols:
            opts = self.iroh.NodeOptions(enable_docs=False, protocols=protocols)
            return await self.iroh.Iroh.memory_with_options(opts)
        return await self.iroh.Iroh.memory()

    def ticket_from_addr(self, addr: Any) -> str:
        self._import()
        return str(self.iroh.NodeTicket(addr))

    def addr_from_ticket(self, ticket: str) -> Any:
        self._import()
        return self.iroh.NodeTicket.parse(ticket).node_addr()


async def create_host_node() -> tuple[IrohRuntime, Any, asyncio.Queue[IrohJsonLineChannel]]:
    runtime = IrohRuntime()
    runtime._import()
    queue_obj: "asyncio.Queue[IrohJsonLineChannel]" = asyncio.Queue()

    class AcceptHandler(runtime.iroh.ProtocolHandler):
        async def accept(self, conn: Any) -> None:
            bi = await conn.accept_bi()
            await queue_obj.put(IrohJsonLineChannel(bi.send(), bi.recv()))

        async def shutdown(self) -> None:
            return None

    class AcceptCreator(runtime.iroh.ProtocolCreator):
        def create(self, alpn: bytes) -> Any:
            return AcceptHandler()

    node = await runtime.create_node({CHAT_ALPN: AcceptCreator()})
    return runtime, node, queue_obj


async def connect_by_ticket(ticket: str) -> tuple[IrohRuntime, Any, IrohJsonLineChannel]:
    runtime = IrohRuntime()
    runtime._import()
    node = await runtime.create_node()
    addr = runtime.addr_from_ticket(ticket)
    await node.net().add_node_addr(addr)
    conn = await node.node().endpoint().connect(addr, CHAT_ALPN)
    bi = await conn.open_bi()
    return runtime, node, IrohJsonLineChannel(bi.send(), bi.recv())


def run_in_threads(
    host_fn,
    guest_fn,
    *,
    join_timeout: float = 30.0,
    terminal_grace: float = 1.0,
) -> tuple[Any, Any]:
    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}

    def wrap(name: str, fn) -> None:
        try:
            results[name] = fn()
        except BaseException as exc:  # pragma: no cover - helper
            errors[name] = exc

    ht = threading.Thread(target=wrap, args=("host", host_fn), daemon=True)
    gt = threading.Thread(target=wrap, args=("guest", guest_fn), daemon=True)
    ht.start()
    gt.start()
    ht.join(join_timeout)
    gt.join(join_timeout)
    alive = [("host", ht), ("guest", gt)]
    alive = [(name, thread) for name, thread in alive if thread.is_alive()]
    # In request/response protocols both threads normally finish well inside the
    # first join.  A real-time match can outlive that first window: Guest returns
    # as soon as it consumes the terminal frame, a few instructions before Host
    # finishes its bounded journal/event-loop teardown.  Give that sole remaining
    # thread a small grace period without extending a genuine two-sided deadlock.
    if len(alive) == 1 and terminal_grace > 0:
        alive[0][1].join(terminal_grace)
        alive = [(name, thread) for name, thread in alive if thread.is_alive()]
    if alive:
        names = ", ".join(name for name, _ in alive)
        raise TimeoutError(f"protocol threads did not finish: {names}")
    if errors:
        name, exc = next(iter(errors.items()))
        raise RuntimeError(f"{name} failed") from exc
    return results.get("host"), results.get("guest")


class AsyncHeartbeatChannel(AsyncJsonLineChannel):
    """Async channel heartbeat, liveness and round-trip latency decorator.

    Responsibilities:
      - Periodically send a `{"_sys": "ping", "ts": ...}` heartbeat frame to the peer in the background.
      - A background read loop filters out the peer's `_sys` frames and only dispatches business frames to the business recv.
      - A watchdog monitors the last activity time; on timeout it notifies the Agent via EventBus / SnapshotBus.
      - Echo bounded ping ids as pong frames and retain local RTT/jitter samples.  Callers can
        actively probe before a protocol handshake without adding game-specific wire messages.
      - Does not raise a timeout exception and does not interrupt the business loop — whether to disconnect is decided by the Agent.
    """

    REEMIT_INTERVAL = 30.0
    MAX_PENDING_PROBES = 64
    MAX_RTT_SAMPLES = 32

    def __init__(
        self,
        inner: AsyncJsonLineChannel,
        interval: float = 10.0,
        timeout: float = 30.0,
        event_bus: "EventBus | None" = None,
        snapshot: "SnapshotBus | None" = None,
    ):
        self._inner = inner
        self._interval = interval
        self._timeout = timeout
        self._event_bus = event_bus
        self._snapshot = snapshot
        self._last_recv_ts: float = time.monotonic()
        self._unresponsive: bool = False
        self._send_lock = asyncio.Lock()
        self._business_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self._reader_error: BaseException | None = None
        self._probe_seq = 0
        self._pending_probes: dict[str, tuple[int, asyncio.Future[float] | None]] = {}
        self._rtt_samples_ms: deque[float] = deque(maxlen=self.MAX_RTT_SAMPLES)
        self._smoothed_rtt_ms: float | None = None

    async def start(self) -> None:
        """Start the reader plus any enabled heartbeat/watchdog tasks.

        The reader always runs, even when periodic heartbeat is disabled, because an
        authoritative real-time engine can still request an explicit latency probe.
        """
        if self._tasks:
            return
        self._last_recv_ts = time.monotonic()
        self._tasks = [asyncio.create_task(self._reader())]
        if self._interval > 0:
            self._tasks.append(asyncio.create_task(self._sender()))
        if self._timeout > 0:
            self._tasks.append(asyncio.create_task(self._watchdog()))

    def _new_probe_id(self) -> str:
        self._probe_seq += 1
        return f"{id(self):x}-{self._probe_seq:x}"

    async def _send_probe(self, *, wait_for_reply: bool) -> asyncio.Future[float] | None:
        probe_id = self._new_probe_id()
        future: asyncio.Future[float] | None = None
        if wait_for_reply:
            future = asyncio.get_running_loop().create_future()
        while len(self._pending_probes) >= self.MAX_PENDING_PROBES:
            stale_id = next(iter(self._pending_probes))
            _, stale_future = self._pending_probes.pop(stale_id)
            if stale_future is not None and not stale_future.done():
                stale_future.cancel()
        self._pending_probes[probe_id] = (time.monotonic_ns(), future)
        try:
            async with self._send_lock:
                await self._inner.send(
                    {"_sys": "ping", "probe_id": probe_id, "ts": time.time()}
                )
        except Exception:
            self._pending_probes.pop(probe_id, None)
            if future is not None and not future.done():
                future.cancel()
            raise
        return future

    def _record_pong(self, probe_id: str) -> None:
        pending = self._pending_probes.pop(probe_id, None)
        if pending is None:
            return
        sent_ns, future = pending
        rtt_ms = max(0.0, (time.monotonic_ns() - sent_ns) / 1_000_000.0)
        self._rtt_samples_ms.append(rtt_ms)
        if self._smoothed_rtt_ms is None:
            self._smoothed_rtt_ms = rtt_ms
        else:
            self._smoothed_rtt_ms = self._smoothed_rtt_ms * 0.75 + rtt_ms * 0.25
        if future is not None and not future.done():
            future.set_result(rtt_ms)

    async def probe_latency(
        self,
        *,
        samples: int = 3,
        timeout: float = 1.0,
        spacing: float = 0.02,
    ) -> dict[str, Any]:
        """Measure peer RTT without exposing heartbeat frames to the business protocol.

        Older peers safely ignore the probe id, in which case the returned metrics have
        ``samples == 0``.  Probe count/timeouts are bounded so a missing peer cannot stall
        the protocol handshake indefinitely.
        """
        count = max(1, min(8, int(samples)))
        per_probe_timeout = max(0.05, min(5.0, float(timeout)))
        for index in range(count):
            if self._closed:
                break
            future = await self._send_probe(wait_for_reply=True)
            assert future is not None
            try:
                await asyncio.wait_for(future, timeout=per_probe_timeout)
            except asyncio.TimeoutError:
                for probe_id, (_, pending_future) in list(self._pending_probes.items()):
                    if pending_future is future:
                        self._pending_probes.pop(probe_id, None)
                        break
            except asyncio.CancelledError:
                if self._closed:
                    break
                raise
            if index + 1 < count and spacing > 0:
                await asyncio.sleep(min(0.25, float(spacing)))
        return self.latency_metrics()

    def latency_metrics(self) -> dict[str, Any]:
        samples = list(self._rtt_samples_ms)
        jitter = 0.0
        if len(samples) > 1:
            jitter = sum(abs(second - first) for first, second in zip(samples, samples[1:])) / (len(samples) - 1)
        return {
            "samples": len(samples),
            "rtt_ms": round(samples[-1], 2) if samples else None,
            "smoothed_rtt_ms": round(self._smoothed_rtt_ms, 2) if self._smoothed_rtt_ms is not None else None,
            "jitter_ms": round(jitter, 2) if samples else None,
        }

    async def _sender(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._interval)
                if self._closed:
                    return
                try:
                    await self._send_probe(wait_for_reply=False)
                except Exception:
                    return
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            # A failed pong write or unexpected transport error must wake business recv;
            # otherwise the reader task could die while callers wait forever on an empty queue.
            self._reader_error = exc
            return

    async def _reader(self) -> None:
        try:
            while not self._closed:
                try:
                    msg = await self._inner.recv()
                except ChannelClosed as exc:
                    self._reader_error = exc
                    return
                if not isinstance(msg, dict):
                    self._touch()
                    await self._business_queue.put(msg)
                    continue
                if msg.get("_sys") == "ping":
                    self._touch()
                    probe_id = msg.get("probe_id")
                    if isinstance(probe_id, str) and 0 < len(probe_id) <= 64:
                        async with self._send_lock:
                            await self._inner.send({"_sys": "pong", "probe_id": probe_id})
                    continue
                if msg.get("_sys") == "pong":
                    self._touch()
                    probe_id = msg.get("probe_id")
                    if isinstance(probe_id, str) and len(probe_id) <= 64:
                        self._record_pong(probe_id)
                    continue
                self._touch()
                await self._business_queue.put(msg)
        except asyncio.CancelledError:
            return

    def _touch(self) -> None:
        self._last_recv_ts = time.monotonic()
        if self._unresponsive:
            self._unresponsive = False
            self._emit("peer_resumed", {})
            if self._snapshot is not None:
                try:
                    self._snapshot.set_phase("in_progress", summary="Peer reconnected")
                except Exception:
                    pass

    async def _watchdog(self) -> None:
        next_reemit_at: float = 0.0
        try:
            while not self._closed:
                await asyncio.sleep(1)
                elapsed = time.monotonic() - self._last_recv_ts
                if not self._unresponsive and elapsed > self._timeout:
                    self._unresponsive = True
                    next_reemit_at = time.monotonic() + self.REEMIT_INTERVAL
                    self._emit("peer_unresponsive", {"elapsed": round(elapsed, 1)})
                    if self._snapshot is not None:
                        try:
                            self._snapshot.set_phase(
                                "peer_unresponsive",
                                summary=f"Peer unresponsive for {int(elapsed)}s",
                                elapsed=round(elapsed, 1),
                            )
                        except Exception:
                            pass
                elif self._unresponsive and time.monotonic() >= next_reemit_at:
                    next_reemit_at = time.monotonic() + self.REEMIT_INTERVAL
                    self._emit("peer_unresponsive", {"elapsed": round(elapsed, 1)})
                    if self._snapshot is not None:
                        try:
                            self._snapshot.update(
                                last_event={
                                    "summary": f"Peer unresponsive for {int(elapsed)}s",
                                    "structured": {"elapsed": round(elapsed, 1)},
                                }
                            )
                        except Exception:
                            pass
        except asyncio.CancelledError:
            return

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        try:
            self._event_bus.emit(event_type, data=data)
        except Exception:
            pass

    async def send(self, msg: dict[str, Any]) -> None:
        async with self._send_lock:
            await self._inner.send(msg)

    async def recv(self, timeout: float | None = None) -> dict[str, Any]:
        # Poll the business queue with a short wait so that a dead reader (the peer
        # disconnected and _reader captured ChannelClosed) surfaces as ChannelClosed
        # instead of blocking the business loop forever. Without this, once _reader
        # exits on ChannelClosed the business recv would hang indefinitely and the
        # session would never terminate or emit a clean abort. v009 P1-6.
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._reader_error is not None:
                raise ChannelClosed("peer channel closed during recv")
            wait = 0.5 if deadline is None else max(0.0, min(0.5, deadline - time.monotonic()))
            try:
                return await asyncio.wait_for(self._business_queue.get(), timeout=wait)
            except asyncio.TimeoutError:
                if self._reader_error is not None:
                    raise ChannelClosed("peer channel closed during recv")
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for peer message")
            # else: still within caller timeout and reader still alive; loop again.

    def is_unresponsive(self) -> bool:
        return self._unresponsive

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for _, future in self._pending_probes.values():
            if future is not None and not future.done():
                future.cancel()
        self._pending_probes.clear()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        await self._inner.close()
