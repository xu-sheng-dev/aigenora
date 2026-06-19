"""Unified decision submission path, shared by the CLI `session decide` and the Web `POST /api/decide`.

Introduced in v005a: ensures the web side also runs finalized checks, match_key validation,
idempotent deduplication, and audit _meta.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aigenora.proto.sdk import DecisionBus


def _find_match_key(state_dir: str | Path, decision: dict) -> tuple[str | None, Any]:
    """Extract match_key/match_value from state.json or the decision.

    Ignores _meta and all top-level keys starting with an underscore.
    """
    p = Path(state_dir)
    state_file = p / "decision" / "state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            mk = state.get("match_key")
            mv = state.get("match_value")
            if mk is not None and mv is not None:
                return mk, mv
        except (json.JSONDecodeError, OSError):
            pass
    for key, value in decision.items():
        if key.startswith("_"):
            continue
        if key in ("round", "attempt"):
            return key, value
    return None, None


def submit_decision(
    state_dir: str | Path,
    decision: dict,
    *,
    origin: str,
    agent_id: str | None = None,
    idempotency_key: str | None = None,
    caused_by_whisper_id: str | None = None,
    require_match_key: bool = False,
) -> dict:
    """Unified decision submission.

    Returns {ok, status, reason, match_key, match_value}
      status: "ok" | "rejected"
      reason: None | "match_key_required" | "decision_finalized" | "duplicate_idempotency"
    """
    state_dir_str = str(state_dir)
    match_key, match_value = _find_match_key(state_dir_str, decision)

    if require_match_key and match_key is None:
        return {
            "ok": False,
            "status": "rejected",
            "reason": "match_key_required",
            "match_key": None,
            "match_value": None,
        }

    if match_key is not None:
        bus = DecisionBus(state_dir_str)
        if bus.is_finalized(match_key, match_value):
            return {
                "ok": False,
                "status": "rejected",
                "reason": "decision_finalized",
                "match_key": match_key,
                "match_value": match_value,
            }

    if idempotency_key and agent_id:
        dedup_path = Path(state_dir_str) / "decision" / "decisions.jsonl"
        if dedup_path.exists():
            try:
                for line in reversed(dedup_path.read_text(encoding="utf-8").splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    meta = rec.get("_meta", {})
                    if (meta.get("agent_id") == agent_id
                            and meta.get("idempotency_key") == idempotency_key):
                        return {
                            "ok": True,
                            "status": "ok",
                            "reason": "duplicate_idempotency",
                            "match_key": match_key,
                            "match_value": match_value,
                        }
            except OSError:
                pass

    decision = dict(decision)
    decision["_meta"] = {
        "origin": origin,
        "agent_id": agent_id,
        "idempotency_key": idempotency_key,
        "caused_by_whisper_id": caused_by_whisper_id,
        "request_ts": datetime.now(timezone.utc).isoformat(),
    }

    DecisionBus.submit(state_dir_str, decision)

    return {
        "ok": True,
        "status": "ok",
        "reason": None,
        "match_key": match_key,
        "match_value": match_value,
    }
