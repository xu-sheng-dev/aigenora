"""v019-M2: InterventionIntentStore — pending intent 暂存通道。

承载目标窗口未到位的指令：跨局指挥、筹备期、条件触发、异步 producer、一次性策略指令
（目标窗口确定但 value 要等窗口打开时算）。

文件：<state_dir>/intervention_intents.jsonl，append-only。
每条 intent 带 intent_id、scope、target_policy、value/policy、created_window、ttl_windows、
origin、agent_id、caused_by_whisper_id、status。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class InterventionIntentStore:
    """Pending intent 暂存：append/read/materialize/expire。"""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.state_dir / "intervention_intents.jsonl"

    def append(
        self,
        intent: dict,
        *,
        origin: str = "whisper_bridge",
        agent_id: str | None = None,
        caused_by_whisper_id: str | None = None,
    ) -> dict:
        """追加一条 pending intent，返回完整记录（含 intent_id）。"""
        intent_id = f"int_{uuid.uuid4().hex[:16]}"
        rec = {
            "intent_id": intent_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            "scope": intent.get("scope", "once"),
            "target_policy": intent.get("target_policy", "next"),
            "value": intent.get("value"),
            "policy": intent.get("policy"),
            "created_match_key": intent.get("created_match_key"),
            "created_match_value": intent.get("created_match_value"),
            "ttl_windows": intent.get("ttl_windows", 3),
            "_meta": {
                "origin": origin,
                "agent_id": agent_id,
                "caused_by_whisper_id": caused_by_whisper_id,
            },
        }
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with open(self.file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return rec

    def _read_all(self) -> list[dict]:
        if not self.file.exists():
            return []
        result = []
        for line in self.file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result

    def pending_for_window(self, match_key: str, match_value: Any) -> list[dict]:
        """返回适用于当前窗口的未过期 pending intent。

        适用于当前窗口的条件：
        - effective_status == "pending"（未被 materialized/expired/failed）
        - target_policy == "next" 且 created_match_value + offset 能对上当前 match_value
          （offset 从 1 开始递增，每轮 +1）
        - 或 target_policy == "current" 且 created_match_value == match_value
        - ttl_windows 未耗尽
        """
        # 先算出每个 intent_id 的最终状态
        all_recs = self._read_all()
        latest_status: dict[str, str] = {}
        original: dict[str, dict] = {}
        for rec in all_recs:
            iid = rec.get("intent_id")
            if not iid:
                continue
            # 第一条（含 scope/value/policy 等完整字段）作为 original
            if iid not in original and rec.get("scope"):
                original[iid] = rec
            if rec.get("status"):
                latest_status[iid] = rec["status"]

        result = []
        for iid, orig in original.items():
            if latest_status.get(iid, "pending") != "pending":
                continue
            created_mv = orig.get("created_match_value")
            tp = orig.get("target_policy")
            ttl = orig.get("ttl_windows", 3)

            if tp == "current":
                if created_mv == match_value:
                    result.append(orig)
            elif tp == "next":
                if created_mv is not None and isinstance(created_mv, int) and isinstance(match_value, int):
                    offset = match_value - created_mv
                    if 1 <= offset <= ttl:
                        result.append(orig)
                else:
                    result.append(orig)
        return result

    def mark(self, intent_id: str, status: str, *, reason: str | None = None, detail: dict | None = None) -> None:
        """追加状态事件记录（materialized/expired/failed）。

        状态事件也写 intervention_intents.jsonl，格式为 {"intent_id", "status_event": ...}。
        读取 pending 时通过 status 字段判断，status_event 记录只做审计。
        实际的 status 更新采用"追加覆盖记录"方式：写一条新的 status 字段记录。
        """
        rec = {
            "intent_id": intent_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": status,  # materialized / expired / failed
            "status_event": status,
        }
        if reason:
            rec["reason"] = reason
        if detail:
            rec["detail"] = detail
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with open(self.file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read_all(self) -> list[dict]:
        """读取全部 intent 记录（含状态事件），用于审计/复盘。"""
        return self._read_all()

    def effective_status(self, intent_id: str) -> str:
        """返回某 intent 的最终状态（取最后一条 status 记录）。"""
        last = None
        for rec in self._read_all():
            if rec.get("intent_id") == intent_id:
                last = rec
        return last.get("status", "pending") if last else "unknown"
