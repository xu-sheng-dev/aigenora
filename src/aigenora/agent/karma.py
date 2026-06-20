from __future__ import annotations

import json

from aigenora.agent.registry import _resolve_agent_id
from aigenora.engine.config import get_server
from aigenora.engine.keys import load_keys
from aigenora.engine.rest import RestClient


def cmd_show(args) -> int:
    """公开只读：查某 Agent 的信誉积分（karma / level）。

    --agent-id 直接用内部 id；--public-key 先解析 id；两者都缺省查自己。
    karma 为百分制整数（0-500 = weightedScore×100），level 复用 confidenceLevel 口径
    （high/medium/low/none）。GET 端点公开只读，无需签名。
    """
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    if getattr(args, "agent_id", None) is not None:
        agent_id = int(args.agent_id)
    else:
        public_key = args.public_key or kp.public_key
        agent_id = _resolve_agent_id(client, public_key)

    data = client.json("GET", f"/api/v1/agents/{agent_id}/karma", expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        pk = data.get("public_key", "") if isinstance(data, dict) else ""
        karma = data.get("karma", 0) if isinstance(data, dict) else 0
        level = data.get("level", "none") if isinstance(data, dict) else "none"
        updated = data.get("updated_at") if isinstance(data, dict) else None
        print(f"agent: {pk[:16]}...")
        print(f"karma: {karma}/500  (level: {level})")
        if updated:
            print(f"updated_at: {updated}")
    return 0


def cmd_leaderboard(args) -> int:
    """公开只读：karma 排行榜（keyset 分页）。

    --limit 每页条数（默认 20，上限 100）；--cursor 翻页（上一页响应里的 next_cursor）。
    按 karma DESC, public_key DESC 排序。
    """
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    path = "/api/v1/karma/leaderboard"
    params = []
    if getattr(args, "limit", None) is not None:
        params.append(f"limit={int(args.limit)}")
    if getattr(args, "cursor", None):
        params.append(f"cursor={args.cursor}")
    if params:
        path = path + "?" + "&".join(params)

    data = client.json("GET", path, expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        entries = data.get("entries", []) if isinstance(data, dict) else []
        next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
        print(f"karma leaderboard ({len(entries)} entries):")
        for i, e in enumerate(entries, 1):
            pk = e.get("public_key", "")
            print(f"  {i:>3}. {pk[:16]}...  karma={e.get('karma', 0)}/500  ({e.get('level', 'none')})")
        if next_cursor:
            print(f"\nnext_cursor: {next_cursor}")
    return 0
