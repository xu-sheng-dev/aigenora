"""Networking + protocol-location helpers for the collab plane.

Own accept/connect wrappers around the iroh FFI so the admission handshake
can read the REAL remote node id from the connection object (design §4.3:
``transport_info.endpoint_id`` is a self-reported community key and must not
be treated as the iroh endpoint).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aigenora.engine.config import builtin_protocols_root
from aigenora.engine.crypto import protocol_hash
from aigenora.engine.p2p import CHAT_ALPN, IrohJsonLineChannel, IrohRuntime

COLLAB_PROTOCOL_FAMILY = "trusted-agent-task-v1"

# §4.3 pre-authentication global guards: bound the candidate connection slots
# so node-id-rotation floods cannot exhaust the host (authenticated devices
# are not constrained by any quota).
MAX_CONCURRENT_ADMISSIONS = 4
MAX_QUEUED_CANDIDATES = 16


def locate_collab_protocol() -> tuple[Path, str]:
    """Find the built-in trusted-agent-task-v1 protocol dir and its hash id."""
    root = builtin_protocols_root()
    for spec_file in sorted(root.glob("*/*/spec.json")):
        try:
            spec = json.loads(spec_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if spec.get("family") == COLLAB_PROTOCOL_FAMILY:
            return spec_file.parent, protocol_hash(spec_file)
    raise FileNotFoundError("built-in trusted-agent-task-v1 protocol not found")


@dataclass
class AcceptedConnection:
    channel: IrohJsonLineChannel
    conn: Any
    close: Any  # callable to fully close the connection


async def create_collab_host_node():
    """Host node whose accept queue keeps the Connection object per candidate.

    Note: the pinned iroh 0.35 Python FFI does not persist the endpoint
    secret (``Iroh.persistent`` still rotates node ids across restarts), so
    the stable-node-id part of design §4.6 remains M2 work. Host restarts
    therefore produce a fresh ticket; guests rediscover via the community
    board (accepted M1a behavior per §11).
    """
    runtime = IrohRuntime()
    runtime._import()
    queue_obj: "asyncio.Queue[AcceptedConnection]" = asyncio.Queue(maxsize=MAX_QUEUED_CANDIDATES)

    class AcceptHandler(runtime.iroh.ProtocolHandler):
        async def accept(self, conn: Any) -> None:
            bi = await conn.accept_bi()
            channel = IrohJsonLineChannel(bi.send(), bi.recv())

            async def _close() -> None:
                try:
                    await conn.close()
                except Exception:
                    pass

            await queue_obj.put(AcceptedConnection(channel=channel, conn=conn, close=_close))

        async def shutdown(self) -> None:
            return None

    class AcceptCreator(runtime.iroh.ProtocolCreator):
        def create(self, alpn: bytes) -> Any:
            return AcceptHandler()

    node = await runtime.create_node({CHAT_ALPN: AcceptCreator()})
    return runtime, node, queue_obj


@dataclass
class DialedConnection:
    channel: IrohJsonLineChannel
    conn: Any
    runtime: IrohRuntime
    node: Any


async def collab_connect(ticket: str) -> DialedConnection:
    """Guest dial that keeps the Connection object for remote node id binding."""
    runtime = IrohRuntime()
    runtime._import()
    node = await runtime.create_node()
    addr = runtime.addr_from_ticket(ticket)
    await node.net().add_node_addr(addr)
    conn = await node.node().endpoint().connect(addr, CHAT_ALPN)
    bi = await conn.open_bi()
    channel = IrohJsonLineChannel(bi.send(), bi.recv())
    return DialedConnection(channel=channel, conn=conn, runtime=runtime, node=node)
