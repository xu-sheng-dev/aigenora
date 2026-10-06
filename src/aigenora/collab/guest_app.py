"""``aigenora collab submit/status/chat/cancel/pull`` — the A-side guest client.

Each CLI command opens one admitted, session-bound connection to the pinned
peer host: cached-ticket reattach when possible, otherwise community-board
discovery + formal join with a server-verified Session Proof. Long waits
poll ``status_query`` on the same connection and transparently reattach
after drops (idempotent resubmission makes reconnects exact).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from aigenora.collab import admission, net, transfer
from aigenora.collab.errors import AdmissionError
from aigenora.collab.psk import PeerRegistry, load_psk
from aigenora.engine.config import data_dir as resolve_data_dir, get_server
from aigenora.engine.crypto import (
    protocol_hash,
    session_canonical,
    session_id as compute_session_id,
    transport_binding_canonical,
)
from aigenora.engine.keys import KeyPair, load_keys, verify_raw
from aigenora.engine.p2p import ChannelClosed
from aigenora.engine.rest import RestClient
from aigenora.proto.session import (
    SessionProof,
    new_session_nonce,
    sign_session,
    submit_session,
)
from aigenora.proto.validate import load_spec, validate_message_obj

MANIFEST_PAGE_SIZE = 128
UPLOAD_BATCH = 4
DEFAULT_DEADLINE_MS = 10 * 60 * 1000
POLL_INTERVAL_SECONDS = 4.0
TERMINAL = {"completed", "failed", "rejected", "cancelled", "expired"}


class GuestError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class GuestSession:
    """One admitted connection bound to a formal (or reattached) session."""

    def __init__(self, channel: Any, conn: Any, runtime: Any, node: Any, *, session_id: str, host_pub: str, protocol_spec: dict) -> None:
        self.channel = channel
        self.conn = conn
        self.runtime = runtime
        self.node = node
        self.session_id = session_id
        self.host_pub = host_pub
        self.spec = protocol_spec

    async def rpc(self, msg: dict[str, Any]) -> dict[str, Any]:
        validate_message_obj(self.spec, msg, "guest_to_host")
        await self.channel.send(msg)
        while True:
            response = await self.channel.recv()
            if not isinstance(response, dict):
                raise GuestError("internal_error", "non-object response frame")
            validate_message_obj(self.spec, response, "host_to_guest")
            if response.get("action") == "error":
                raise GuestError(str(response.get("error_code")), str(response.get("detail", "")))
            return response

    async def close(self) -> None:
        try:
            await self.node.node().shutdown()
        except Exception:
            pass


class GuestClient:
    def __init__(self, args, peer_alias: str) -> None:
        self.args = args
        self.data_dir_value = args.data_dir
        self.kp: KeyPair = load_keys(self.data_dir_value)
        self.psk = load_psk(self.data_dir_value)
        peers = PeerRegistry(self.data_dir_value)
        peer = peers.by_alias(peer_alias)
        if peer is None:
            known = ", ".join(p.alias for p in peers.all()) or "(none)"
            raise SystemExit(f"unknown peer alias {peer_alias!r}; trusted peers: {known}")
        if peer.public_key == self.kp.public_key:
            raise SystemExit("peer alias resolves to this device's own key; pair the OTHER device")
        self.peer = peer
        self.protocol_dir, self.protocol_id = net.locate_collab_protocol()
        self.spec = load_spec(self.protocol_dir / "spec.json")
        self.client = RestClient(get_server(args.server), self.kp)
        self._cache_dir = resolve_data_dir(self.data_dir_value) / "collab" / "sessions"
        self._tasks_cache = resolve_data_dir(self.data_dir_value) / "collab" / "client-tasks.json"

    # -- discovery / connection -------------------------------------------------

    def _cache_path(self) -> Path:
        return self._cache_dir / f"{self.peer.public_key[:16]}.json"

    def _read_cache(self) -> dict[str, Any] | None:
        path = self._cache_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_cache(self, payload: dict[str, Any]) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _discover_post(self) -> dict[str, Any]:
        data = self.client.json(
            "GET", f"/api/v1/invitations?protocol_id={self.protocol_id}&limit=50", expected={200}
        )
        items = data.get("results") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise GuestError("internal_error", "unexpected invitation list response")
        candidates: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("public_key", "")).lower() != self.peer.public_key:
                continue
            if str(item.get("status", "active")) != "active":
                continue
            post_id = item.get("post_id") or item.get("id")
            if not post_id:
                continue
            detail = self.client.json("GET", f"/api/v1/invitations/{post_id}", expected={200})
            if isinstance(detail, dict) and detail.get("post_id"):
                candidates.append(detail)
        if not candidates:
            raise GuestError(
                "unauthorized", f"no active invitation from peer {self.peer.alias}; is the host running?"
            )
        return candidates[0]

    async def connect(self) -> GuestSession:
        cache = self._read_cache()
        if cache and cache.get("ticket") and cache.get("session_id"):
            try:
                return await self._reattach(cache["ticket"], cache["session_id"])
            except Exception:
                pass  # stale ticket (host restart) → fall through to discovery
        return await self._formal_join()

    async def _run_admission(self, dialed: net.DialedConnection, session_nonce: str) -> None:
        await admission.run_guest_admission(
            dialed.channel,
            psk=self.psk,
            expected_host_public_key=self.peer.public_key,
            protocol_id=self.protocol_id,
            local_node=dialed.node,
            conn=dialed.conn,
            session_nonce=session_nonce,
            guest_private_key_hex=self.kp.private_key,
            guest_public_key=self.kp.public_key,
        )

    async def _reattach(self, ticket: str, session_id: str) -> GuestSession:
        dialed = await net.collab_connect(ticket)
        try:
            nonce = new_session_nonce()
            await self._run_admission(dialed, nonce)
            await dialed.channel.send(
                {
                    "_session_init": True,
                    "guest_public_key": self.kp.public_key,
                    "session_nonce": nonce,
                    "guest_control_mode": "autonomous",
                    "reattach": True,
                    "session_id": session_id,
                }
            )
            reply = await dialed.channel.recv()
            if not isinstance(reply, dict) or reply.get("_session_reattach_ok") is not True:
                raise GuestError("internal_error", "host refused reattach")
        except (ChannelClosed, AdmissionError, GuestError):
            await dialed.node.node().shutdown()
            raise
        return GuestSession(
            dialed.channel, dialed.conn, dialed.runtime, dialed.node,
            session_id=session_id, host_pub=self.peer.public_key, protocol_spec=self.spec,
        )

    async def _formal_join(self) -> GuestSession:
        post = self._discover_post()
        ticket = str((post.get("transport_info") or {}).get("ticket") or post.get("iroh_ticket") or "")
        post_id = str(post["post_id"])
        if not ticket:
            raise GuestError("internal_error", "invitation carries no iroh ticket")
        binding = transport_binding_canonical(self.peer.public_key, "iroh", ticket, self.protocol_id)
        verify_raw(
            self.peer.public_key,
            binding.encode("utf-8"),
            str(post.get("transport_binding_signature", "")),
        )
        dialed = await net.collab_connect(ticket)
        try:
            nonce = new_session_nonce()
            await self._run_admission(dialed, nonce)
            await dialed.channel.send(
                {
                    "_session_init": True,
                    "guest_public_key": self.kp.public_key,
                    "session_nonce": nonce,
                    "guest_control_mode": "autonomous",
                }
            )
            proof_msg = await dialed.channel.recv()
            if not isinstance(proof_msg, dict) or proof_msg.get("_session_proof") is not True:
                raise GuestError("internal_error", "host did not return a session proof")
            host_pub = str(proof_msg.get("host_public_key", ""))
            if host_pub != self.peer.public_key:
                raise GuestError("internal_error", "session proof host key differs from pinned peer")
            if str(proof_msg.get("protocol_id", "")) != self.protocol_id:
                raise GuestError("internal_error", "session proof protocol mismatch")
            verify_raw(
                host_pub,
                session_canonical(post_id, host_pub, self.kp.public_key, self.protocol_id, nonce).encode("utf-8"),
                str(proof_msg.get("host_signature", "")),
            )
            canonical = session_canonical(post_id, host_pub, self.kp.public_key, self.protocol_id, nonce)
            guest_signature = sign_session(
                self.kp, post_id, host_pub, self.kp.public_key, self.protocol_id, nonce
            )
            proof = SessionProof(
                post_id=post_id,
                host_public_key=host_pub,
                guest_public_key=self.kp.public_key,
                protocol_id=self.protocol_id,
                session_nonce=nonce,
                host_signature=str(proof_msg.get("host_signature", "")),
                guest_signature=guest_signature,
            )
            try:
                session_id = await asyncio.to_thread(submit_session, self.client, proof)
            except Exception as exc:
                # Post may have been consumed by a racing peer; force rediscovery.
                raise GuestError("internal_error", f"session proof rejected by community: {type(exc).__name__}") from exc
            await dialed.channel.send({"_session_ready": True, "session_id": session_id})
        except BaseException:
            try:
                await dialed.node.node().shutdown()
            except Exception:
                pass
            raise
        self._write_cache(
            {
                "host_alias": self.peer.alias,
                "host_public_key": self.peer.public_key,
                "post_id": post_id,
                "protocol_id": self.protocol_id,
                "ticket": ticket,
                "session_id": session_id,
                "bound_at": int(time.time() * 1000),
            }
        )
        return GuestSession(
            dialed.channel, dialed.conn, dialed.runtime, dialed.node,
            session_id=session_id, host_pub=host_pub, protocol_spec=self.spec,
        )

    # -- task cache ---------------------------------------------------------------

    def cache_task(self, task_id: str, **updates: Any) -> None:
        data: dict[str, Any] = {}
        if self._tasks_cache.exists():
            try:
                data = json.loads(self._tasks_cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        entry = data.get(task_id) or {"host_alias": self.peer.alias, "task_id": task_id}
        entry.update(updates, updated_at=int(time.time() * 1000))
        data[task_id] = entry
        self._tasks_cache.parent.mkdir(parents=True, exist_ok=True)
        self._tasks_cache.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# -- input collection -----------------------------------------------------------


def collect_inputs(paths: list[str]) -> list[tuple[Path, dict[str, Any]]]:
    """Flatten --input files/dirs into (local_root, manifest_entry) pairs."""
    pairs: list[tuple[Path, dict[str, Any]]] = []
    seen: set[str] = set()
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames if d not in {".git", "__pycache__", "node_modules", ".venv", ".aigenora"})
                for name in sorted(filenames):
                    full = Path(dirpath) / name
                    rel = full.relative_to(p).as_posix()
                    if rel in seen:
                        continue
                    seen.add(rel)
                    pairs.append((p, transfer.build_manifest_entry(p, rel)))
        elif p.is_file():
            if p.name in seen:
                raise SystemExit(f"duplicate input file name: {p.name}")
            seen.add(p.name)
            pairs.append((p.parent, transfer.build_manifest_entry(p.parent, p.name)))
        else:
            raise SystemExit(f"input path not found: {raw}")
    return pairs


def paginate(entries: list[tuple[Path, dict[str, Any]]]) -> list[list[tuple[Path, dict[str, Any]]]]:
    pages: list[list[tuple[Path, dict[str, Any]]]] = []
    for i in range(0, len(entries), MANIFEST_PAGE_SIZE):
        pages.append(entries[i : i + MANIFEST_PAGE_SIZE])
    return pages


def request_digest(goal: str, execution_class: str, entries: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        {"goal": goal, "class": execution_class, "files": sorted(entries, key=lambda e: e["path"])},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# -- RPC flows --------------------------------------------------------------------


async def upload_artifacts(session: GuestSession, task_id: str, pages: list[list[tuple[Path, dict[str, Any]]]]) -> None:
    """Offer + batched-put every manifest page until each artifact verifies complete."""
    for idx, page in enumerate(pages):
        if not page:
            continue
        artifact_id = f"input-{idx}"
        entries = [entry for _, entry in page]
        ack = await session.rpc(
            {
                "action": "artifact_offer",
                "task_id": task_id,
                "artifact_id": artifact_id,
                "artifact_kind": "input",
                "files": entries,
            }
        )
        received_map = {f["path"]: set(f.get("received_chunks", [])) for f in ack.get("files", [])}
        complete_map = {f["path"]: f["complete"] for f in ack.get("files", [])}
        if complete_map and all(complete_map.values()):
            continue
        while True:
            batch = _next_missing_batch(page, received_map)
            if not batch:
                break
            ack = await session.rpc(
                {"action": "artifact_put", "task_id": task_id, "artifact_id": artifact_id, "chunks": batch}
            )
            if ack.get("artifact_state") == "verified_failed":
                raise GuestError("internal_error", "host reports a corrupt artifact upload")
            done = True
            for f in ack.get("files", []):
                received_map[f["path"]] = set(f.get("received_chunks", []))
                if not f["complete"]:
                    done = False
            if done:
                break


def _next_missing_batch(page: list[tuple[Path, dict[str, Any]]], received_map: dict[str, set[int]]) -> list[dict[str, Any]]:
    batch: list[dict[str, Any]] = []
    for root, entry in page:
        total = transfer.chunk_count(int(entry["size"]))
        got = received_map.get(entry["path"], set())
        for idx in range(total):
            if idx in got:
                continue
            chunk = _read_local_chunk(root, entry, idx)
            if chunk is not None:
                batch.append(chunk)
            if len(batch) >= UPLOAD_BATCH:
                return batch
    return batch


def _read_local_chunk(root: Path, entry: dict[str, Any], idx: int) -> dict[str, Any] | None:
    candidate = root / entry["path"]
    if not candidate.is_file():
        return None
    offset = idx * transfer.CHUNK_SIZE
    if offset >= int(entry["size"]):
        return None
    with candidate.open("rb") as stream:
        stream.seek(offset)
        raw = stream.read(transfer.CHUNK_SIZE)
    if not raw:
        return None
    return {
        "path": entry["path"],
        "offset": offset,
        "size": len(raw),
        "sha256": transfer.sha256_bytes(raw),
        "data": base64.b64encode(raw).decode("ascii"),
    }


async def pull_results(session: GuestSession, task_id: str, manifest: dict[str, Any], out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = manifest.get("files", [])
    paths = [f["path"] for f in files]
    cursor: dict[str, int] = {}
    written: list[str] = []
    while True:
        response = await session.rpc(
            {
                "action": "artifact_pull",
                "task_id": task_id,
                "artifact_id": "result",
                "paths": paths,
                "cursor": cursor,
            }
        )
        for chunk in response.get("chunks", []):
            path = transfer.safe_rel_path(str(chunk.get("path", "")))
            raw = base64.b64decode(str(chunk.get("data", "")), validate=True)
            offset = int(chunk.get("offset", 0))
            if transfer.sha256_bytes(raw) != chunk.get("sha256"):
                raise GuestError("internal_error", f"chunk digest mismatch on {path}")
            target = out_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("r+b" if target.exists() else "w+b") as stream:
                stream.seek(offset)
                stream.write(raw)
        cursor = {str(k): int(v) for k, v in (response.get("cursor") or {}).items()}
        if response.get("done") is True:
            break
    # Size-0 files never receive chunks; create them so verification passes.
    for entry in files:
        if int(entry.get("size", -1)) == 0:
            target = out_dir / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(b"")
    for entry in files:
        target = out_dir / entry["path"]
        if target.is_file() and transfer.sha256_file(target) == entry["sha256"]:
            written.append(entry["path"])
        else:
            raise GuestError("internal_error", f"file digest mismatch after pull: {entry['path']}")
    return written


async def wait_terminal(session_factory, client: GuestClient, task_id: str, timeout_s: float, pull_out: Path | None, quiet: bool = False) -> dict[str, Any]:
    """Poll until terminal (or timeout), transparently reattaching after drops."""
    deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
    last_event = 0
    session = await session_factory()
    last_status: dict[str, Any] = {}
    try:
        while True:
            try:
                response = await session.rpc({"action": "status_query", "task_id": task_id, "after_event_id": last_event})
            except (ChannelClosed, GuestError, ConnectionError, OSError):
                await asyncio.sleep(1.5)
                session = await session_factory()
                continue
            last_status = response
            for event in response.get("events", []):
                last_event = max(last_event, int(event.get("event_id", 0)))
                if not quiet:
                    print(f"[event] {event.get('kind')}: {event.get('summary')}")
            for note in response.get("notes", []):
                print(f"[worker] {note.get('text')}")
            input_request = response.get("input_request")
            if isinstance(input_request, dict) and input_request.get("question"):
                print(f"[host asks] {input_request.get('question')} (input_id={input_request.get('input_id')})")
            client.cache_task(task_id, last_state=response.get("state"))
            state = str(response.get("state", ""))
            if state in TERMINAL:
                break
            if deadline is not None and time.monotonic() > deadline:
                print(f"[timeout] task still {state!r}; detach (task keeps running on the peer)")
                return response
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await session.close()
    state = str(last_status.get("state", ""))
    execution = last_status.get("execution") or {}
    if execution.get("summary"):
        print(f"[summary] {str(execution['summary'])[:2000]}")
    if state == "completed" and pull_out is not None:
        manifest = last_status.get("result_manifest") or {}
        if manifest.get("files"):
            session = await session_factory()
            try:
                written = await pull_results(session, task_id, manifest, pull_out)
                for path in written:
                    print(f"[pulled] {(pull_out / path)}")
                receipt = await session.rpc({"action": "result_ack", "task_id": task_id})
                if receipt.get("summary"):
                    print(f"[receipt] {str(receipt['summary'])[:2000]}")
            finally:
                await session.close()
    return last_status
