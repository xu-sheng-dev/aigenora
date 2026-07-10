"""v019-M3: 通用脚本沙箱（引擎层，无游戏语义）。

加载 <state_dir>/policy_scripts/<script_id>.py，通过 JSON stdin 传入
{schema, context, strategy, params}，收 JSON stdout，硬 timeout。

沙箱约束：
- 不允许脚本直接写 P2P 消息、import hooks、访问 channel/secret。
- hard timeout 默认 1000ms。
- 输出非法 JSON、字段不合法、超时都 fallback。
- 不含任何策略逻辑、不读 beats、不认识游戏规则。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _find_script(script_id: str, state_dir: str | Path) -> Path | None:
    """查找脚本文件。

    查找顺序：
    1. 用户本地 <state_dir>/policy_scripts/<script_id>.py（用户/agent 自己放的）
    2. 包内置示例 aigenora.policy_scripts/<script_id>.py（随包安装分发）

    script_id 只允许字母/数字/下划线/连字符，防止路径穿越。
    """
    import re
    if not re.match(r"^[a-zA-Z0-9_-]+$", script_id):
        return None
    # 1. 用户本地
    local = Path(state_dir) / "policy_scripts" / f"{script_id}.py"
    if local.exists():
        return local
    # 2. 包内置示例
    try:
        import aigenora.policy_scripts as pkg_scripts
        builtin = Path(pkg_scripts.__file__).parent / f"{script_id}.py"
        if builtin.exists():
            return builtin
    except (ImportError, TypeError):
        pass
    return None


def run_script(
    strategy: dict,
    context: dict,
    *,
    state_dir: str | Path,
    schema: dict | None = None,
    timeout_ms: int = 1000,
) -> dict:
    """运行脚本 producer，返回 {ok, decision?, reason?}。

    strategy: {"mode":"script","script_id":"...","params":{...},...}
    context: 协议 build_decision_context() 的输出
    schema: 协议 DECISION_SCHEMA
    """
    script_id = strategy.get("script_id")
    if not script_id:
        return {"ok": False, "reason": "no_script_id"}

    # 查找脚本：优先用户本地 <state_dir>/policy_scripts/<script_id>.py，
    # 再回退到包内置示例 aigenora.policy_scripts/<script_id>.py（安装时随包分发）。
    script_path = _find_script(script_id, state_dir)
    if script_path is None:
        return {"ok": False, "reason": "script_not_found", "script_id": script_id}

    params = strategy.get("params") or {}
    stdin_payload = {
        "schema": schema or {},
        "context": context,
        "strategy": strategy,
        "params": params,
    }

    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            input=json.dumps(stdin_payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_ms / 1000.0,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "policy_timeout", "script_id": script_id, "timeout_ms": timeout_ms}
    except Exception as e:
        return {"ok": False, "reason": "script_error", "detail": f"{type(e).__name__}: {e}"}

    if proc.returncode != 0:
        return {"ok": False, "reason": "script_error", "returncode": proc.returncode, "stderr": proc.stderr[-500:] if proc.stderr else ""}

    stdout = proc.stdout.strip()
    if not stdout:
        return {"ok": False, "reason": "empty_output"}

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as e:
        return {"ok": False, "reason": "invalid_json", "detail": str(e)}

    # 脚本可返回 {ok: false, reason: ...} 表示无上下文/不支持
    if result.get("ok") is False:
        return result

    decision = result.get("decision")
    if not decision:
        return {"ok": False, "reason": "no_decision_in_output"}

    return {"ok": True, "decision": decision, "confidence": result.get("confidence"), "reason": result.get("reason")}


__all__ = ["run_script"]
