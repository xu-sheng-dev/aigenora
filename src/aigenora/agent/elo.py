from __future__ import annotations

import json

from aigenora.agent.registry import _resolve_agent_id
from aigenora.engine.config import get_server
from aigenora.engine.keys import load_keys
from aigenora.engine.rest import RestClient

# v010 M5 ELO；v012 批次3 改纯正向累积制（与服务端 EloService 同口径）。
# 客户端保留纯函数便于本地推算/单测，权威值以服务端 elo_ratings 为准。
DEFAULT_RATING = 1200
K_FACTOR = 32
# v012 批次3：平局双方各得固定分；输家安慰分系数 Δ=round(K×E_winner×COMFORT_FACTOR)。
DRAW_DELTA = 8
COMFORT_FACTOR = 0.25


def expected_score(rating_a: int, rating_b: int) -> float:
    """期望胜率：ratingA 对 ratingB。= 1/(1+10^((rating_b-rating_a)/400))。"""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def winner_delta(winner_rating: int, loser_rating: int) -> int:
    """v012 批次3：纯正向赢家得分 = round(K × (1 − E_winner))。赢弱手得少、赢强手得多。范围 0~K。"""
    e_winner = expected_score(winner_rating, loser_rating)
    return round(K_FACTOR * (1.0 - e_winner))


def loser_comfort_delta(winner_rating: int, loser_rating: int) -> int:
    """v012 批次3：输家安慰分（如实上报）= round(K × E_winner × COMFORT_FACTOR)。
    E_winner 为赢家预期胜率（赢家越强、输家安慰越多）。范围 0~8。"""
    e_winner = expected_score(winner_rating, loser_rating)
    return round(K_FACTOR * e_winner * COMFORT_FACTOR)


def cmd_show(args) -> int:
    """公开只读：查某 Agent 的 ELO 排位（rating / games_played）。

    --agent-id 直接用内部 id；--public-key 先解析 id；两者都缺省查自己。
    未对战的 Agent 返回默认 rating=1200 / games_played=0。
    """
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    if getattr(args, "agent_id", None) is not None:
        agent_id = int(args.agent_id)
    else:
        public_key = args.public_key or kp.public_key
        agent_id = _resolve_agent_id(client, public_key)

    data = client.json("GET", f"/api/v1/agents/{agent_id}/elo", expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        pk = data.get("public_key", "") if isinstance(data, dict) else ""
        rating = data.get("rating", DEFAULT_RATING) if isinstance(data, dict) else DEFAULT_RATING
        games = data.get("games_played", 0) if isinstance(data, dict) else 0
        updated = data.get("updated_at") if isinstance(data, dict) else None
        print(f"agent: {pk[:16]}...")
        print(f"elo: {rating}  (games_played: {games})")
        if updated:
            print(f"updated_at: {updated}")
    return 0
