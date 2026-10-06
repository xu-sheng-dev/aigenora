"""``aigenora collab host`` — the B-side collaboration task service.

Owns the whole host lifecycle:
- community invitation (auto protocol registration, renewal while waiting),
- a bounded multi-connection accept loop (§4.3 candidate guards),
- in-stream ``admission_psk_v1`` before any session frame,
- the formal ``_session_init``/``_session_proof``/``_session_ready`` handshake
  plus reattach of previously-bound guests (observation sessions),
- the trusted-agent-task-v1 RPC loop over the TaskService.

Runs in the foreground (Ctrl+C stops it). Every step is mirrored into
``<data_dir>/collab/host-events.jsonl`` for local operators.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from aigenora.collab import admission, net
from aigenora.collab.adapter import build_adapter
from aigenora.collab.errors import ERROR_INTERNAL, ERROR_INVALID_MESSAGE, CollabError
from aigenora.collab.psk import PeerRegistry, load_psk
from aigenora.collab.service import TaskService
from aigenora.collab.store import TaskStore, TERMINAL_STATES
from aigenora.engine.config import data_dir as resolve_data_dir, get_server
from aigenora.engine.crypto import (
    protocol_hash,
    session_canonical,
    session_id as compute_session_id,
    transport_binding_canonical,
)
from aigenora.engine.keys import KeyPair, load_keys, sign_raw
from aigenora.engine.p2p import ChannelClosed
from aigenora.engine.rest import RestClient
from aigenora.proto.session import submit_session
from aigenora.proto.validate import load_spec, validate_message_obj
from aigenora.proto.validate import ValidationError


class HostEventLog:
    def __init__(self, data_dir_value: str | None) -> None:
        self.path = resolve_data_dir(data_dir_value) / "collab" / "host-events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, **data: Any) -> None:
        record = {"at": int(time.time() * 1000), "kind": kind, **data}
        try:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass
        print(f"[collab-host] {kind} " + (json.dumps(data, ensure_ascii=False)[:400] if data else ""), flush=True)


def _error_frame(code: str, text: str) -> dict[str, Any]:
    return {"action": "error", "error_code": code, "detail": text}


class CollabHost:
    def __init__(self, args) -> None:
        self.args = args
        self.data_dir_value = args.data_dir
        self.kp: KeyPair = load_keys(self.data_dir_value)
        self.psk = load_psk(self.data_dir_value)
        self.peers = PeerRegistry(self.data_dir_value)
        self.protocol_dir, self.protocol_id = net.locate_collab_protocol()
        self.spec = load_spec(self.protocol_dir / "spec.json")
        if protocol_hash(self.protocol_dir / "spec.json") != self.protocol_id:
            raise RuntimeError("protocol hash mismatch")
        adapter_kwargs: dict[str, Any] = {}
        if getattr(args, "zcode_mode", None):
            adapter_kwargs["mode"] = args.zcode_mode
        self.adapter = build_adapter(getattr(args, "adapter", "file"), **adapter_kwargs)
        self.store = TaskStore(self.data_dir_value)
        self.service = TaskService(
            self.data_dir_value, adapter=self.adapter, store=self.store, autostart_dispatcher=False
        )
        self.events = HostEventLog(self.data_dir_value)
        self.client = RestClient(get_server(args.server), self.kp)
        self.current_post_id: str | None = None
        self.node: Any = None
        self.runtime: Any = None
        self.accept_queue: Any = None
        self._stop = asyncio.Event()
        self._seen_guest_nonces: dict[str, float] = {}
        self._admission_semaphore: asyncio.Semaphore | None = None
        self._connection_tasks: set[asyncio.Task] = set()
        self._session_bindings = self._load_session_bindings()

    # -- session bindings ------------------------------------------------------

    def _load_session_bindings(self) -> dict[str, str]:
        return self.store.kv_get("session-bindings", {}) or {}

    def _bind_session(self, guest_pub: str, session_id: str) -> None:
        self._session_bindings[guest_pub] = session_id
        self.store.kv_set("session-bindings", self._session_bindings)

    # -- invitation --------------------------------------------------------------

    def _register_protocol_if_needed(self) -> None:
        try:
            self.client.json("GET", f"/api/v1/protocols/{self.protocol_id}", expected={200})
            return
        except Exception:
            pass
        payload = {
            "protocol_id": self.protocol_id,
            "name": self.spec.get("name") or "Trusted Agent Task",
            "description": self.spec.get("description") or "",
            "type": self.spec.get("type") or "service",
            "spec_json": self.spec,
        }
        data = self.client.json("POST", "/api/v1/protocols", payload, expected={200, 201, 409})
        self.events.emit("protocol_registered", protocol_id=self.protocol_id, response=str(data)[:200])

    async def create_invitation(self) -> str:
        node_addr = await self.node.net().node_addr()
        ticket = self.runtime.ticket_from_addr(node_addr)
        binding = transport_binding_canonical(self.kp.public_key, "iroh", ticket, self.protocol_id)
        body = {
            "message": "Trusted cross-device agent task service",
            "tags": ["collab", "trusted-agent-task"],
            "iroh_ticket": ticket,
            "transport": "iroh",
            "transport_info": {"version": 1, "endpoint_id": self.kp.public_key, "ticket": ticket},
            "transport_binding_signature": sign_raw(self.kp.private_key, binding.encode("utf-8")),
            "protocol_id": self.protocol_id,
            "host_control_mode": "autonomous",
            "type": "supply",
        }
        data = self.client.json("POST", "/api/v1/invitations", body, expected={201})
        post_id = str(data["post_id"])
        self.current_post_id = post_id
        self.events.emit("invite_created", post_id=post_id, protocol_id=self.protocol_id)
        return post_id

    async def _renew_loop(self) -> None:
        """Keep the current (unbound) invitation alive; exits when a new post replaces it."""
        last_post: str | None = None
        while not self._stop.is_set():
            post_id = self.current_post_id
            if post_id is None:
                await asyncio.sleep(5)
                continue
            if post_id != last_post:
                last_post = post_id
                deadline = time.monotonic() + 25 * 60
            elif time.monotonic() > deadline:
                # Older than the renew horizon and still unbound: refresh anyway.
                deadline = time.monotonic() + 25 * 60
            try:
                await asyncio.to_thread(
                    self.client.json, "POST", f"/api/v1/invitations/{post_id}/renew", None, {200}
                )
            except Exception:
                pass
            await asyncio.sleep(120)

    # -- main loop -----------------------------------------------------------------

    async def run(self) -> int:
        probe = self.adapter.probe()
        self.events.emit(
            "host_starting",
            adapter=self.adapter.name,
            adapter_ready=bool(probe.get("ready")),
            adapter_detail=str(probe.get("detail", ""))[:200],
            protocol_id=self.protocol_id,
            peers=[p.alias for p in self.peers.all()],
        )
        if not probe.get("ready"):
            print(f"[collab-host] WARNING adapter not ready: {probe.get('detail', '')}", file=sys.stderr)
        self._register_protocol_if_needed()
        self.service.start_dispatcher()
        self.runtime, self.node, self.accept_queue = await net.create_collab_host_node()
        post_id = await self.create_invitation()
        node_id = await self.node.net().node_id()
        print(
            json.dumps(
                {
                    "status": "hosting",
                    "post_id": post_id,
                    "protocol_id": self.protocol_id,
                    "iroh_node_id": node_id,
                    "adapter": self.adapter.name,
                    "data_dir": str(resolve_data_dir(self.data_dir_value)),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        self._admission_semaphore = asyncio.Semaphore(net.MAX_CONCURRENT_ADMISSIONS)
        renew = asyncio.create_task(self._renew_loop())
        try:
            while not self._stop.is_set():
                try:
                    accepted = await asyncio.wait_for(self.accept_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                task = asyncio.create_task(self._handle_candidate(accepted))
                self._connection_tasks.add(task)
                task.add_done_callback(self._connection_tasks.discard)
        except asyncio.CancelledError:
            pass
        finally:
            renew.cancel()
            for task in list(self._connection_tasks):
                task.cancel()
            self.service.stop_dispatcher()
            try:
                await self.node.node().shutdown()
            except Exception:
                pass
            self.events.emit("host_stopped")
        return 0

    async def _handle_candidate(self, accepted: net.AcceptedConnection) -> None:
        guest_pub = ""
        bound_session = False
        formal = False
        try:
            async with self._admission_semaphore:
                try:
                    result = await admission.run_host_admission(
                        accepted.channel,
                        psk=self.psk,
                        peers=self.peers,
                        protocol_id=self.protocol_id,
                        local_node=self.node,
                        conn=accepted.conn,
                        host_private_key_hex=self.kp.private_key,
                        host_public_key=self.kp.public_key,
                        seen_guest_nonces=self._seen_guest_nonces,
                    )
                except admission.AdmissionError as exc:
                    self.events.emit("admission_rejected", code=exc.code)
                    await admission.reject_admission(accepted.channel, exc.code)
                    await accepted.close()
                    return
                except (ChannelClosed, Exception):
                    await accepted.close()
                    return
            guest_pub = result.peer_public_key
            self.events.emit("admission_ok", guest=guest_pub[:16])
            session_id, formal, bound = await self._session_handshake(accepted.channel, guest_pub)
            if formal and session_id:
                bound_session = True
            await self._rpc_loop(accepted.channel, guest_pub, session_id)
        except ChannelClosed:
            pass
        except Exception as exc:
            self.events.emit("connection_error", error=type(exc).__name__)
        finally:
            if formal and not bound_session:
                # A guest that completed admission and started the formal
                # handshake but never bound may have consumed the post at the
                # server; publish a fresh invitation for the next joiner.
                self.events.emit("formal_session_unbound", guest=guest_pub[:16])
                try:
                    await self.create_invitation()
                except Exception as exc:
                    self.events.emit("invite_refresh_failed", error=type(exc).__name__)
            try:
                await accepted.close()
            except Exception:
                pass

    async def _session_handshake(self, channel: Any, guest_pub: str) -> tuple[str, bool, bool]:
        """Returns (session_id, formal, bound). Raises on protocol violation."""
        first = await channel.recv()
        if not isinstance(first, dict) or first.get("_session_init") is not True:
            raise RuntimeError("expected _session_init after admission")
        claimed_guest = str(first.get("guest_public_key", ""))
        if claimed_guest != guest_pub:
            raise RuntimeError("session init identity differs from admission identity")
        if first.get("reattach") is True:
            original = str(first.get("session_id", ""))
            bound = self._session_bindings.get(guest_pub)
            if not original or bound != original:
                await channel.send({"_session_reject": "unknown_session"})
                raise RuntimeError("reattach for an unbound session")
            await channel.send({"_session_reattach_ok": True, "session_id": original})
            self.events.emit("guest_reattached", session_id=original)
            return original, False, True
        nonce = str(first.get("session_nonce", ""))
        post_id = self.current_post_id or ""
        if not nonce or not post_id:
            await channel.send({"_session_reject": "no_active_invitation"})
            raise RuntimeError("formal join without an active invitation")
        canonical = session_canonical(post_id, self.kp.public_key, guest_pub, self.protocol_id, nonce)
        host_signature = sign_raw(self.kp.private_key, canonical.encode("utf-8"))
        await channel.send(
            {
                "_session_proof": True,
                "host_public_key": self.kp.public_key,
                "host_signature": host_signature,
                "protocol_id": self.protocol_id,
                "post_id": post_id,
            }
        )
        ready = await channel.recv()
        if not isinstance(ready, dict) or ready.get("_session_ready") is not True:
            raise RuntimeError("guest did not complete the session handshake")
        session_id = str(ready.get("session_id", ""))
        if session_id != compute_session_id(post_id, self.kp.public_key, guest_pub, self.protocol_id, nonce):
            raise RuntimeError("guest session_id does not match the signed canonical")
        self._bind_session(guest_pub, session_id)
        self.events.emit("session_bound", session_id=session_id, guest=guest_pub[:16])
        # Post is consumed at the server once the proof is submitted; publish
        # a fresh invitation so the next formal joiner can discover us.
        try:
            await self.create_invitation()
        except Exception as exc:
            self.events.emit("invite_refresh_failed", error=type(exc).__name__)
        return session_id, True, True

    async def _rpc_loop(self, channel: Any, source_device: str, session_id: str) -> None:
        await rpc_loop(channel, self.service, source_device, self.spec, self.events)


async def rpc_loop(channel: Any, service: TaskService, source_device: str, spec: dict, events: "HostEventLog | None" = None) -> None:
    """One connection's guest→host RPC loop: validate before dispatch, safe errors out."""
    while True:
        msg = await channel.recv()
        if not isinstance(msg, dict):
            raise RuntimeError("business frame must be an object")
        try:
            validate_message_obj(spec, msg, "guest_to_host")
        except ValidationError as exc:
            if events is not None:
                events.emit("invalid_message", phase="spec", reason=str(exc)[:300])
            await channel.send(_error_frame(ERROR_INVALID_MESSAGE, "message failed protocol spec validation"))
            continue
        try:
            response = service.handle(msg, source_device=source_device)
        except CollabError as exc:
            if events is not None:
                events.emit("task_error", code=exc.code, detail=exc.detail[:200])
            await channel.send(exc.wire())
            continue
        except Exception as exc:
            service.diag(f"handler crash on {msg.get('action')}: {type(exc).__name__}: {exc}")
            if events is not None:
                events.emit("handler_error", action=str(msg.get("action")), error=type(exc).__name__)
            await channel.send(_error_frame(ERROR_INTERNAL, "internal error"))
            continue
        try:
            validate_message_obj(spec, response, "host_to_guest")
        except ValidationError as exc:
            service.diag(f"response validation failed for {msg.get('action')}: {exc}")
            await channel.send(_error_frame(ERROR_INTERNAL, "internal error"))
            continue
        await channel.send(response)


async def run_host(args) -> int:
    host = CollabHost(args)
    try:
        return await host.run()
    except KeyboardInterrupt:
        return 0


def run(args) -> int:
    try:
        return asyncio.run(run_host(args))
    except KeyboardInterrupt:
        return 0
