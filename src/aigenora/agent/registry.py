from __future__ import annotations

import json

from aigenora.agent.session import _parse_governance_string_array as _parse_capabilities
from aigenora.engine.config import get_server
from aigenora.engine.keys import load_keys
from aigenora.engine.rest import RestClient


def _resolve_agent_id(client: RestClient, public_key: str) -> int:
    """v010 M3：capabilities 端点用内部 agent id（/{id}/capabilities），
    但用户只知道 public_key。通过 GET /api/v1/agents?public_key=... 解析内部 id。"""
    data = client.json("GET", f"/api/v1/agents?public_key={public_key}", expected={200})
    agent_id = data.get("id") if isinstance(data, dict) else None
    if agent_id is None:
        raise RuntimeError(
            f"cannot resolve agent id for public_key={public_key[:16]}... (not registered?)"
        )
    return int(agent_id)


def cmd_set(args) -> int:
    """upsert 当前 Agent（caller 本人）的能力声明。

    --capabilities 是 JSON 字符串数组（与 governance --capabilities 同口径），
    本地预校验正则 / 64 项 / 64 字符，早失败，避免无效签名请求。
    """
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    try:
        capabilities = _parse_capabilities(args.capabilities, "--capabilities")
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    agent_id = _resolve_agent_id(client, kp.public_key)
    data = client.json(
        "POST",
        f"/api/v1/agents/{agent_id}/capabilities",
        {"capabilities": capabilities},
        expected={200},
    )
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        caps = data.get("capabilities", []) if isinstance(data, dict) else []
        print(f"[OK] capabilities set for {kp.public_key[:16]}... ({len(caps)} item(s))")
    return 0


def cmd_get(args) -> int:
    """公开只读：查某 Agent 的能力声明。

    --agent-id 直接用内部 id；--public-key 先解析 id；两者都缺省查自己。
    """
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    if getattr(args, "agent_id", None) is not None:
        agent_id = int(args.agent_id)
    else:
        public_key = args.public_key or kp.public_key
        agent_id = _resolve_agent_id(client, public_key)

    data = client.json("GET", f"/api/v1/agents/{agent_id}/capabilities", expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        caps = data.get("capabilities", []) if isinstance(data, dict) else []
        updated = data.get("updated_at") if isinstance(data, dict) else None
        pk = data.get("public_key", "") if isinstance(data, dict) else ""
        print(f"agent: {pk[:16]}...")
        print(f"capabilities ({len(caps)}): {', '.join(caps) if caps else '(none)'}")
        if updated:
            print(f"updated_at: {updated}")
    return 0
