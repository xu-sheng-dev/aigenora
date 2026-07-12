from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aigenora.engine.config import builtin_protocols_root
from aigenora.proto.loader import load_hooks
from aigenora.runtime.catalog.loader import CatalogEntry, PinnedCatalog
from aigenora.runtime.catalog.policy import require_pinned_bundle
from aigenora.runtime.errors import RuntimeMethodError
from aigenora.runtime.registry import RuntimeHandler


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass
class WorkerSession:
    worker_id: str
    session_id: str
    entry: CatalogEntry
    role: str
    profile: str
    generation: int
    sequence: int
    state_digest: str
    status: str
    hooks: Any
    session_root: Path
    options_json: str
    history: list[dict[str, Any]]


class ProtocolWorkerService:
    """Runs one pinned protocol Session and exposes only narrow typed steps."""

    def __init__(self, catalog: PinnedCatalog, state_root: Path):
        self._catalog = catalog
        self._state_root = state_root.resolve()
        self._state_root.mkdir(parents=True, exist_ok=True)
        if (self._state_root / "key.json").exists():
            raise ValueError("Protocol Worker state root must not contain identity material")
        self._session: WorkerSession | None = None

    def handlers(self) -> dict[str, RuntimeHandler]:
        return {
            "protocol.worker.open": self.open,
            "protocol.worker.step": self.step,
            "protocol.worker.close": self.close,
        }

    def open(self, params: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        self._assert_meta(params, meta)
        if self._session is not None:
            raise RuntimeMethodError(
                "protocol.worker_role_mismatch", "Protocol Worker already owns another Session"
            )
        entry = require_pinned_bundle(
            self._catalog,
            protocol_id=params["protocol_id"],
            bundle_digest=params["bundle_digest"],
            profile=params["profile"],
        )
        try:
            options = json.loads(params["options_json"])
        except json.JSONDecodeError as exc:
            raise ValueError("worker options are invalid JSON") from exc
        expected_options = entry.profile_options(params["profile"])
        if options != expected_options:
            raise RuntimeMethodError(
                "protocol.bundle_mismatch", "Worker options differ from the pinned profile"
            )
        session_root = (self._state_root / params["session_id"]).resolve()
        if session_root.parent != self._state_root:
            raise ValueError("worker Session path is invalid")
        binding_path = session_root / "worker-binding.v1.json"
        recovered: dict[str, Any] | None = None
        if session_root.exists():
            recovered = self._load_recovery(binding_path, params)
        else:
            session_root.mkdir(parents=True, exist_ok=False)
        generation = int(recovered["generation"]) + 1 if recovered is not None else 1
        generation_root = session_root / f"generation-{generation}"
        generation_root.mkdir(parents=False, exist_ok=False)
        hook_options = dict(options)
        if entry.family == "guess-number":
            range_min = int(hook_options.get("range_min", 1))
            range_max = int(hook_options.get("range_max", 100))
            entropy = hashlib.sha256(params["session_id"].encode("utf-8")).digest()
            hook_options["secret"] = range_min + int.from_bytes(entropy[:8], "big") % (
                range_max - range_min + 1
            )
        bundle_root = builtin_protocols_root().joinpath(*entry.path.split("/"))
        hooks = load_hooks(bundle_root)
        hooks.proto_init(hook_options, params["role"], [], generation_root, None)
        worker_id = "worker_" + secrets.token_hex(16)
        previous_digest = recovered["state_digest"] if recovered is not None else None
        sequence = int(recovered["sequence"]) if recovered is not None else 0
        status = str(recovered["status"]) if recovered is not None else "ready"
        history = [dict(item) for item in recovered["history"]] if recovered is not None else []
        session = WorkerSession(
            worker_id=worker_id,
            session_id=params["session_id"],
            entry=entry,
            role=params["role"],
            profile=params["profile"],
            generation=generation,
            sequence=0,
            state_digest="",
            status="ready",
            hooks=hooks,
            session_root=session_root,
            options_json=params["options_json"],
            history=history,
        )
        if recovered is not None:
            for index, action in enumerate(history):
                session.sequence = index
                if entry.family == "rps" and action.get("action_kind") == "rps_round":
                    _outcome, replay_status, _summary = self._step_rps(session, action)
                elif entry.family == "guess-number" and action.get("action_kind") == "guess_attempt":
                    _outcome, replay_status, _summary = self._step_guess(session, action)
                else:
                    raise RuntimeMethodError(
                        "protocol.worker_output_rejected", "Worker recovery history is invalid"
                    )
            if history and replay_status != status:
                raise RuntimeMethodError(
                    "protocol.worker_output_rejected", "Worker recovery status differs"
                )
        session.sequence = sequence
        session.status = status
        state_digest = _digest(
            {
                "worker_id": worker_id,
                "session_id": params["session_id"],
                "protocol_id": entry.protocol_id,
                "bundle_digest": entry.bundle_digest,
                "generation": generation,
                "sequence": sequence,
                "status": status,
                "recovered_from": previous_digest,
            }
        )
        session.state_digest = state_digest
        self._session = session
        self._persist(session, closed=False)
        return {
            "worker_id": worker_id,
            "session_id": params["session_id"],
            "protocol_id": entry.protocol_id,
            "bundle_digest": entry.bundle_digest,
            "generation": generation,
            "state_digest": state_digest,
            "expected_sequence": sequence,
            "status": "ready",
        }

    def step(self, params: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        self._assert_meta(params, meta)
        session = self._require_session(params)
        if session.status == "completed":
            raise RuntimeMethodError(
                "protocol.worker_output_rejected", "Protocol Worker Session is already complete"
            )
        if params["sequence"] != session.sequence or params["pre_state_digest"] != session.state_digest:
            raise RuntimeMethodError(
                "protocol.worker_output_rejected", "Protocol Worker pre-state binding differs"
            )
        if session.entry.family == "rps" and params["action_kind"] == "rps_round":
            outcome, status, summary = self._step_rps(session, params)
        elif session.entry.family == "guess-number" and params["action_kind"] == "guess_attempt":
            outcome, status, summary = self._step_guess(session, params)
        else:
            raise RuntimeMethodError(
                "protocol.worker_output_rejected", "Protocol Worker action is incompatible"
            )
        next_sequence = session.sequence + 1
        public_proposal = {
            "worker_id": session.worker_id,
            "session_id": session.session_id,
            "protocol_id": session.entry.protocol_id,
            "bundle_digest": session.entry.bundle_digest,
            "generation": session.generation,
            "sequence": session.sequence,
            "pre_state_digest": session.state_digest,
            "next_sequence": next_sequence,
            "status": status,
            "outcome": outcome,
            "summary": summary,
        }
        proposal_digest = _digest(public_proposal)
        post_state_digest = _digest(
            {
                "previous": session.state_digest,
                "proposal": proposal_digest,
                "next_sequence": next_sequence,
                "status": status,
            }
        )
        result = {
            **public_proposal,
            "proposal_digest": proposal_digest,
            "post_state_digest": post_state_digest,
        }
        session.sequence = next_sequence
        session.state_digest = post_state_digest
        session.status = status
        session.history.append(
            {
                key: params[key]
                for key in (
                    "action_kind",
                    "self_choice",
                    "peer_choice",
                    "self_number",
                    "peer_number",
                )
                if key in params
            }
        )
        self._persist(session, closed=False)
        return result

    def close(self, params: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        self._assert_meta(params, meta)
        session = self._require_session(params)
        if params["expected_state_digest"] != session.state_digest:
            raise RuntimeMethodError(
                "protocol.worker_output_rejected", "Protocol Worker final state differs"
            )
        result = {
            "worker_id": session.worker_id,
            "session_id": session.session_id,
            "generation": session.generation,
            "final_state_digest": session.state_digest,
            "closed": True,
        }
        self._persist(session, closed=True)
        self._session = None
        return result

    @staticmethod
    def _load_recovery(binding_path: Path, params: dict[str, Any]) -> dict[str, Any]:
        if not binding_path.is_file() or binding_path.is_symlink():
            raise RuntimeMethodError(
                "session.version_conflict", "Worker Session has no trusted recovery binding"
            )
        value = json.loads(binding_path.read_text(encoding="utf-8"))
        fields = {
            "binding_version",
            "session_id",
            "protocol_id",
            "bundle_digest",
            "role",
            "profile",
            "options_json",
            "generation",
            "sequence",
            "state_digest",
            "status",
            "history",
            "closed",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise RuntimeMethodError(
                "session.version_conflict", "Worker recovery binding is invalid"
            )
        expected = {
            "session_id": params["session_id"],
            "protocol_id": params["protocol_id"],
            "bundle_digest": params["bundle_digest"],
            "role": params["role"],
            "profile": params["profile"],
            "options_json": params["options_json"],
        }
        if (
            value.get("binding_version") != "1"
            or any(value.get(key) != expected_value for key, expected_value in expected.items())
            or value.get("closed") is not False
            or not isinstance(value.get("generation"), int)
            or not isinstance(value.get("sequence"), int)
            or value.get("status") not in {"ready", "running", "completed"}
            or not isinstance(value.get("state_digest"), str)
            or not isinstance(value.get("history"), list)
            or len(value["history"]) > 256
            or value["sequence"] != len(value["history"])
            or not all(isinstance(item, dict) for item in value["history"])
        ):
            raise RuntimeMethodError(
                "session.version_conflict", "Worker recovery binding differs"
            )
        return value

    @staticmethod
    def _persist(session: WorkerSession, *, closed: bool) -> None:
        value = {
            "binding_version": "1",
            "session_id": session.session_id,
            "protocol_id": session.entry.protocol_id,
            "bundle_digest": session.entry.bundle_digest,
            "role": session.role,
            "profile": session.profile,
            "options_json": session.options_json,
            "generation": session.generation,
            "sequence": session.sequence,
            "state_digest": session.state_digest,
            "status": session.status,
            "history": session.history,
            "closed": closed,
        }
        target = session.session_root / "worker-binding.v1.json"
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, target)

    @staticmethod
    def _step_rps(
        session: WorkerSession, params: dict[str, Any]
    ) -> tuple[str, str, str]:
        self_choice = params["self_choice"]
        peer_choice = params["peer_choice"]
        host_choice = self_choice if session.role == "host" else peer_choice
        guest_choice = peer_choice if session.role == "host" else self_choice
        hook_result = session.hooks.proto_round_judge(
            session.sequence, host_choice, guest_choice, {}
        )
        response = hook_result.response or {}
        outcome = str(
            response.get("game_winner")
            if hook_result.completed
            else response.get("round_winner", "none")
        )
        if outcome not in {"host", "guest", "draw"}:
            outcome = "none"
        status = "completed" if hook_result.completed else "running"
        summary = (
            f"R{session.sequence + 1}: {host_choice} vs {guest_choice}; "
            f"outcome={outcome}; status={status}"
        )
        return outcome, status, summary

    @staticmethod
    def _step_guess(
        session: WorkerSession, params: dict[str, Any]
    ) -> tuple[str, str, str]:
        if session.role == "guest":
            guess = params.get("self_number")
            if not isinstance(guess, int):
                raise RuntimeMethodError(
                    "protocol.worker_output_rejected", "Guest Worker requires a bounded guess"
                )
            return "none", "running", f"attempt {session.sequence + 1}: guess submitted"
        guess = params.get("peer_number")
        if not isinstance(guess, int):
            raise RuntimeMethodError(
                "protocol.worker_output_rejected", "Host Worker requires the guest guess"
            )
        hook_result = session.hooks.proto_host_handle(
            {"action": "guess", "number": guess, "attempt": session.sequence + 1}
        )
        response = hook_result.response or {}
        outcome = str(response.get("winner", "none")) if hook_result.completed else "none"
        if outcome not in {"host", "guest", "draw", "none"}:
            outcome = "none"
        status = "completed" if hook_result.completed else "running"
        result = str(response.get("result", "complete" if hook_result.completed else "hint"))
        return outcome, status, f"attempt {session.sequence + 1}: {result}; status={status}"

    @staticmethod
    def _assert_meta(params: dict[str, Any], meta: dict[str, Any]) -> None:
        if meta.get("session_id") != params.get("session_id") or meta.get("origin_id") is None:
            raise RuntimeMethodError(
                "session.scope_mismatch", "Protocol Worker Session binding is invalid"
            )

    def _require_session(self, params: dict[str, Any]) -> WorkerSession:
        session = self._session
        if session is None:
            raise RuntimeMethodError("session.not_found", "Protocol Worker Session is absent")
        checks = {
            "worker_id": session.worker_id,
            "session_id": session.session_id,
            "generation": session.generation,
        }
        if "protocol_id" in params:
            checks["protocol_id"] = session.entry.protocol_id
        if "bundle_digest" in params:
            checks["bundle_digest"] = session.entry.bundle_digest
        if any(params.get(key) != value for key, value in checks.items()):
            raise RuntimeMethodError(
                "protocol.worker_output_rejected", "Protocol Worker binding differs"
            )
        return session
