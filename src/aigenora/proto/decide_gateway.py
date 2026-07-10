"""Unified decision submission path, shared by the CLI `session decide` and the Web `POST /api/decide`.

Introduced in v005a: ensures the web side also runs finalized checks, match_key validation,
idempotent deduplication, and audit _meta.

v019-M1: match key 解析改为 body 显式优先于 state.json；支持 round/turn/attempt/action_seq；
为每条 record 补 _meta.decision_id。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aigenora.proto.sdk import DecisionBus

# v019-M1: 支持的 match key 字段（schema 声明可覆盖，这里给默认集合）
_MATCH_KEY_FIELDS = ("round", "turn", "attempt", "action_seq")


def _find_match_key(state_dir: str | Path, decision: dict) -> tuple[str | None, Any]:
    """Extract match_key/match_value.

    v019-M1: body 显式含 round/turn/attempt/action_seq 时，body 优先；
    body 无 match key 时，才读 decision/state.json。
    忽略 _meta 和所有以 _ 开头的顶层键。
    """
    # 1. body 显式 match key 优先
    for key in _MATCH_KEY_FIELDS:
        if key in decision and not key.startswith("_") and decision[key] is not None:
            return key, decision[key]
    # 2. 回退到 state.json
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
    target_policy: str | None = None,
) -> dict:
    """Unified decision submission.

    Returns {ok, status, reason, match_key, match_value, decision_id}
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
            "decision_id": None,
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
                "decision_id": None,
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
                            "decision_id": meta.get("decision_id"),
                        }
            except OSError:
                pass

    # v019-M1: 为每条 record 生成 decision_id
    decision_id = f"dec_{uuid.uuid4().hex[:16]}"
    decision = dict(decision)
    decision["_meta"] = {
        "decision_id": decision_id,
        "origin": origin,
        "agent_id": agent_id,
        "idempotency_key": idempotency_key,
        "caused_by_whisper_id": caused_by_whisper_id,
        "target_policy": target_policy,
        "request_ts": datetime.now(timezone.utc).isoformat(),
    }

    DecisionBus.submit(state_dir_str, decision)

    return {
        "ok": True,
        "status": "ok",
        "reason": None,
        "match_key": match_key,
        "match_value": match_value,
        "decision_id": decision_id,
    }
