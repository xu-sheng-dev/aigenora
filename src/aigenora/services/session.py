from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from aigenora.proto.decide_gateway import submit_decision
from aigenora.proto.sdk import DetailLog, SnapshotBus, StrategyStore

from .context import ServiceContext


_SESSION_ID_RE = re.compile(r"^sess_[A-Za-z0-9][A-Za-z0-9._-]{0,122}$")
_SAFE_DETAIL_FIELDS = {
    "type",
    "round",
    "attempt",
    "winner",
    "game_over",
    "game_winner",
    "hint",
    "result",
    "status",
}


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


class SessionService:
    def __init__(self, context: ServiceContext):
        self._context = context
        self._root = (context.data_dir / "runtime-sessions").resolve()

    def register(
        self,
        session_id: str,
        protocol_id: str,
        role: str,
        *,
        generation: int = 0,
    ) -> Path:
        root = self._session_root(session_id)
        if not re.fullmatch(r"[0-9a-f]{64}", protocol_id):
            raise ValueError("protocol_id is invalid")
        if role not in {"host", "guest"}:
            raise ValueError("role is invalid")
        root.mkdir(parents=True, exist_ok=True)
        metadata = {
            "session_id": session_id,
            "protocol_id": protocol_id,
            "role": role,
            "generation": generation,
            "sequence": 0,
            "strategy_generation": 0,
        }
        _atomic_json(root / "runtime-session.json", metadata)
        return root

    def snapshot(self, session_id: str) -> dict[str, object]:
        root, metadata = self._load_session(session_id)
        snapshot = SessionStateService.snapshot(root)
        last_event = snapshot.get("last_event") if isinstance(snapshot, dict) else None
        summary = last_event.get("summary", "") if isinstance(last_event, dict) else ""
        projection = {
            "session_id": session_id,
            "protocol_id": metadata["protocol_id"],
            "role": metadata["role"],
            "phase": str(snapshot.get("phase", "ready"))[:64],
            "sequence": int(metadata.get("sequence", 0)),
            "generation": int(metadata.get("generation", 0)),
            "last_event_summary": str(summary)[:512],
        }
        return {**projection, "state_digest": _canonical_digest(projection)}

    def details(
        self,
        session_id: str,
        *,
        after_sequence: int = -1,
        limit: int = 64,
    ) -> dict[str, object]:
        root, _ = self._load_session(session_id)
        if after_sequence < -1 or limit < 1 or limit > 64:
            raise ValueError("detail cursor is invalid")
        projected: list[dict[str, object]] = []
        entries = SessionStateService.details(root)
        for sequence, entry in enumerate(entries):
            if sequence <= after_sequence or not isinstance(entry, dict):
                continue
            safe_data = {
                key: value
                for key, value in entry.items()
                if key in _SAFE_DETAIL_FIELDS and isinstance(value, (bool, int, float, str, type(None)))
            }
            projected.append(
                {
                    "sequence": sequence,
                    "event": str(entry.get("type", "protocol.event"))[:128],
                    "occurred_at": str(entry.get("ts", "1970-01-01T00:00:00Z"))[:64],
                    "summary": str(entry.get("summary", ""))[:512],
                    "data_json": json.dumps(
                        safe_data,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )[:2048],
                }
            )
            if len(projected) >= limit:
                break
        last_sequence = projected[-1]["sequence"] if projected else after_sequence
        return {"session_id": session_id, "events": projected, "last_sequence": last_sequence}

    def submit_decision(
        self,
        session_id: str,
        *,
        decision_kind: str,
        expected_sequence: int,
        choice: str | None = None,
        number: int | None = None,
    ) -> dict[str, object]:
        root, metadata = self._load_session(session_id)
        sequence = int(metadata.get("sequence", 0))
        if expected_sequence != sequence:
            return {"status": "rejected", "sequence": sequence, "reason": "sequence_conflict"}
        if decision_kind == "rps_choice" and choice in {"rock", "paper", "scissors"}:
            decision = {"round": sequence, "choice": choice}
        elif decision_kind == "guess_number" and isinstance(number, int):
            decision = {"attempt": sequence + 1, "number": number}
        else:
            return {"status": "rejected", "sequence": sequence, "reason": "decision_invalid"}
        result = SessionStateService.submit_decision(root, decision, origin="runtime")
        if not result.get("ok"):
            return {
                "status": "rejected",
                "sequence": sequence,
                "reason": str(result.get("reason", "decision_rejected"))[:128],
            }
        return {"status": "accepted", "sequence": sequence, "reason": "queued"}

    def strategy_get(self, session_id: str) -> dict[str, object]:
        root, metadata = self._load_session(session_id)
        value = SessionStateService.strategy_read(root)
        result: dict[str, object] = {
            "session_id": session_id,
            "generation": int(metadata.get("strategy_generation", 0)),
            "mode": str(value.get("mode", "random")),
        }
        if value.get("fixed") in {"rock", "paper", "scissors"}:
            result["preferred_choice"] = value["fixed"]
        if isinstance(value.get("number"), int):
            result["preferred_number"] = value["number"]
        if value.get("policy") in {
            "mirror_previous_opponent",
            "counter_previous_opponent",
            "repeat_own_previous",
        }:
            result["policy"] = value["policy"]
        return result

    def strategy_patch(
        self,
        session_id: str,
        *,
        expected_generation: int,
        mode: str,
        preferred_choice: str | None = None,
        preferred_number: int | None = None,
        policy: str | None = None,
        supersedes: str | None = None,
    ) -> dict[str, object]:
        root, metadata = self._load_session(session_id)
        generation = int(metadata.get("strategy_generation", 0))
        receipt_id = "receipt_" + secrets.token_hex(16)
        if expected_generation != generation:
            return {
                "receipt_id": receipt_id,
                "status": "rejected",
                "generation": generation,
                "safe_point_sequence": int(metadata.get("sequence", 0)),
                "reason": "generation_conflict",
            }
        value: dict[str, object] = {"mode": mode}
        if mode == "fixed" and preferred_choice in {"rock", "paper", "scissors"}:
            value["fixed"] = preferred_choice
        elif mode == "numeric" and isinstance(preferred_number, int):
            value["number"] = preferred_number
        elif mode == "policy" and policy in {
            "mirror_previous_opponent",
            "counter_previous_opponent",
            "repeat_own_previous",
        }:
            value["policy"] = policy
        elif mode != "random":
            return {
                "receipt_id": receipt_id,
                "status": "rejected",
                "generation": generation,
                "safe_point_sequence": int(metadata.get("sequence", 0)),
                "reason": "strategy_invalid",
            }
        if supersedes is not None:
            value["supersedes"] = supersedes
        SessionStateService.strategy_write(root, value)
        metadata["strategy_generation"] = generation + 1
        _atomic_json(root / "runtime-session.json", metadata)
        return {
            "receipt_id": receipt_id,
            "status": "applied",
            "generation": generation + 1,
            "safe_point_sequence": int(metadata.get("sequence", 0)),
            "reason": "safe_point",
        }

    def rating_read(self, session_id: str) -> dict[str, object]:
        self._session_root(session_id)
        data = self._context.rest().json(
            "GET", f"/api/v1/sessions/{session_id}", expected={200}
        )
        if not isinstance(data, dict):
            raise ValueError("session rating response must be an object")
        rating_count = data.get("rating_count", 0)
        average = data.get("average_score")
        result: dict[str, object] = {
            "session_id": session_id,
            "server_authority": True,
            "rating_count": rating_count if isinstance(rating_count, int) and rating_count >= 0 else 0,
        }
        if isinstance(average, (int, float)) and 0 <= average <= 5:
            result["average_score"] = float(average)
        return result

    def update_sequence(self, session_id: str, sequence: int) -> None:
        root, metadata = self._load_session(session_id)
        if sequence < int(metadata.get("sequence", 0)):
            raise ValueError("session sequence cannot move backwards")
        metadata["sequence"] = sequence
        _atomic_json(root / "runtime-session.json", metadata)

    def _session_root(self, session_id: str) -> Path:
        if _SESSION_ID_RE.fullmatch(session_id) is None:
            raise ValueError("session_id is invalid")
        root = (self._root / session_id).resolve()
        if root.parent != self._root:
            raise ValueError("session scope is invalid")
        return root

    def _load_session(self, session_id: str) -> tuple[Path, dict[str, Any]]:
        root = self._session_root(session_id)
        metadata_path = root / "runtime-session.json"
        if not metadata_path.is_file():
            raise FileNotFoundError("managed session does not exist")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("session_id") != session_id:
            raise ValueError("managed session metadata is invalid")
        return root, metadata


class SessionStateService:
    """Path-bound protocol state operations shared by legacy CLI and Sidecar.

    The Runtime-facing ``SessionService`` supplies the managed path; the legacy
    CLI supplies its already-resolved state directory.  Requests never carry a
    path across the Runtime boundary.
    """

    @staticmethod
    def snapshot(state_dir: str | Path) -> dict[str, Any]:
        value = SnapshotBus(str(state_dir)).read() or {}
        if not isinstance(value, dict):
            raise ValueError("session snapshot must be an object")
        return value

    @staticmethod
    def details(state_dir: str | Path) -> list[dict[str, Any]]:
        value = DetailLog(str(state_dir)).read_all()
        return [dict(item) for item in value if isinstance(item, dict)]

    @staticmethod
    def submit_decision(
        state_dir: str | Path,
        decision: dict[str, Any],
        *,
        origin: str,
    ) -> dict[str, Any]:
        return submit_decision(
            str(state_dir), decision, origin=origin, require_match_key=False
        )

    @staticmethod
    def strategy_read(state_dir: str | Path) -> dict[str, Any]:
        value = StrategyStore(str(state_dir)).read() or {}
        if not isinstance(value, dict):
            raise ValueError("session strategy must be an object")
        return value

    @staticmethod
    def strategy_write(state_dir: str | Path, value: dict[str, Any]) -> None:
        StrategyStore(str(state_dir)).write(value)

    @staticmethod
    def strategy_merge(state_dir: str | Path, patch: dict[str, Any]) -> None:
        StrategyStore(str(state_dir)).merge(patch)
