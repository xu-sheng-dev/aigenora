"""Web of Trust 客户端（v011 M10）。

服务端凌晨 ETL 从 ratings 派生信任边（高分≥4 信任 / 低分≤2 不信任，分量=rater karma），
写每日快照 JSON 文件（trust-YYYY-MM-DD.json + .gz + latest.json），由 nginx + Cloudflare
静态分发。客户端职责（评审决策三）：下载快照 + 本地算间接信任（K 跳 BFS + karma 加权传播）。

SWR 三档降级（用户硬要求：trust 是发现维度，绝不中断业务）：
  1. latest.json → 200 则缓存 + fresh
  2. latest 失败 → 本地缓存最近一份 trust-DATE.json → stale
  3. 无缓存 → unavailable，友好提示 + exit 0（不抛异常）
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path

from aigenora.engine.config import data_dir, get_trust_url

CACHE_DIRNAME = "trust-cache"
DEFAULT_DEPTH = 2
DECAY = 0.5


# ---- K 跳 BFS 间接信任（纯函数，便于单测）----

def trust_score(target_pk: str, edges: list[dict], depth: int = DEFAULT_DEPTH,
                decay: float = DECAY) -> dict:
    """从 target 反向遍历入边（谁信任 target 及其传递），累加 weight × decay^hop。

    正 weight（信任边）加分，负 weight（不信任边）扣分。BFS 按层扩展，每节点取最短路径
    首次贡献（防环）。返回 {score, direct, paths}，score 为整数（红线 D2）。
    """
    inbound: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for e in edges:
        inbound[e["trustee"]].append((e["truster"], int(e["weight"])))

    direct = list(inbound.get(target_pk, []))
    score = 0.0
    paths: list[dict] = []
    visited: dict[str, int] = {target_pk: 0}
    queue: deque[tuple[str, int]] = deque([(target_pk, 0)])
    while queue:
        node, hop = queue.popleft()
        if hop >= depth:
            continue
        for truster, weight in inbound.get(node, []):
            nhop = hop + 1
            if truster in visited and visited[truster] <= nhop:
                continue
            visited[truster] = nhop
            contribution = weight * (decay ** nhop)
            score += contribution
            paths.append({"truster": truster, "hop": nhop,
                          "contribution": int(round(contribution))})
            queue.append((truster, nhop))
    return {
        "score": int(round(score)),
        "direct": [{"truster": t, "weight": w} for t, w in direct],
        "paths": sorted(paths, key=lambda p: abs(p["contribution"]), reverse=True)[:10],
    }


# ---- 快照获取（SWR 三档降级）----

def _parse(content: bytes) -> dict | None:
    try:
        doc = json.loads(content)
        if isinstance(doc, dict) and isinstance(doc.get("trust_edges"), list):
            return doc
    except Exception:
        pass
    return None


def _latest_cached(cache_dir: Path) -> Path | None:
    files = sorted(cache_dir.glob("trust-*.json"), key=lambda p: p.name, reverse=True)
    return files[0] if files else None


def fetch_snapshot(data_dir_value: str | None = None, server: str | None = None,
                   date: str | None = None) -> dict:
    """下载信任快照（SWR 三档降级，绝不抛异常）。返回 {status, source, snapshot}。

    status: fresh（远程拉到）/ stale（本地缓存）/ unavailable（全失败，snapshot=None）。
    """
    dd = data_dir(data_dir_value)
    cache_dir = dd / CACHE_DIRNAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    base = get_trust_url(server)

    import httpx
    targets = []
    if date:
        targets.append(f"{base}/trust-{date}.json")
    else:
        targets.append(f"{base}/latest.json")

    for url in targets:
        try:
            r = httpx.get(url, timeout=10, follow_redirects=True)
            if r.status_code == 200 and r.content:
                snap = _parse(r.content)
                if snap is not None:
                    sd = snap.get("meta", {}).get("snapshot_date")
                    if sd:
                        (cache_dir / f"trust-{sd}.json").write_bytes(r.content)
                    return {"status": "fresh", "source": url, "snapshot": snap}
        except Exception:
            continue

    cached = _latest_cached(cache_dir)
    if cached is not None:
        snap = _parse(cached.read_bytes())
        if snap is not None:
            return {"status": "stale", "source": str(cached), "snapshot": snap}

    return {"status": "unavailable", "source": None, "snapshot": None}


# ---- 命令 ----

def _load_or_fetch(args, *, need_snapshot: bool = True) -> dict | None:
    """show/edges 复用：优先本地缓存，无则 fetch。unavailable 返回 None（调用方友好提示）。"""
    res = fetch_snapshot(data_dir_value=getattr(args, "data_dir", None),
                         server=getattr(args, "server", None),
                         date=getattr(args, "date", None))
    if res["snapshot"] is None:
        return None
    return res


def cmd_fetch(args) -> int:
    res = fetch_snapshot(data_dir_value=getattr(args, "data_dir", None),
                         server=getattr(args, "server", None),
                         date=getattr(args, "date", None))
    if getattr(args, "json_output", False):
        out = {"status": res["status"], "source": res["source"]}
        if res["snapshot"]:
            out["edges_count"] = len(res["snapshot"].get("trust_edges", []))
            out["meta"] = res["snapshot"].get("meta", {})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if res["snapshot"] is None:
        print("[WARN] Trust snapshot unavailable (all sources failed). "
              "This does not affect other commands. Try again later.")
        return 0
    meta = res["snapshot"].get("meta", {})
    edges = res["snapshot"].get("trust_edges", [])
    print(f"status:   {res['status']}")
    print(f"source:   {res['source']}")
    print(f"date:     {meta.get('snapshot_date', '?')}")
    print(f"edges:    {len(edges)} ({meta.get('distrust_edges_count', 0)} distrust)")
    print(f"sha256:   {meta.get('sha256_self', '?')[:16]}...")
    return 0


def cmd_show(args) -> int:
    res = _load_or_fetch(args)
    if res is None:
        print("[WARN] Trust data temporarily unavailable. This is advisory-only and does "
              "not affect other commands. Try `aigenora trust fetch` later.")
        return 0
    snap = res["snapshot"]
    edges = snap.get("trust_edges", [])
    target = args.agent_id
    result = trust_score(target, edges, depth=getattr(args, "depth", DEFAULT_DEPTH))
    if getattr(args, "json_output", False):
        out = {"agent": target, **result, "source": res["source"],
               "snapshot_date": snap.get("meta", {}).get("snapshot_date")}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"agent: {target[:16]}...{target[-8:] if len(target) > 24 else ''}")
    print(f"indirect trust score (depth={getattr(args, 'depth', DEFAULT_DEPTH)}): {result['score']}")
    print(f"direct trust edges ({len(result['direct'])} incoming):")
    for d in result["direct"]:
        tag = " [DISTRUST]" if d["weight"] < 0 else ""
        print(f"  <- {d['truster'][:16]}...  weight={d['weight']}{tag}")
    if result["paths"]:
        print("top indirect paths:")
        for p in result["paths"][:5]:
            print(f"  hop {p['hop']} via {p['truster'][:16]}...  contribution {p['contribution']:+d}")
    print(f"\n(advisory only — trust does not gate any business action)")
    return 0


def cmd_edges(args) -> int:
    res = _load_or_fetch(args)
    if res is None:
        print("[WARN] Trust data temporarily unavailable. This is advisory-only.")
        return 0
    edges = res["snapshot"].get("trust_edges", [])
    agent = getattr(args, "agent", None)
    if agent:
        filtered = [e for e in edges if e["truster"] == agent or e["trustee"] == agent]
        if getattr(args, "json_output", False):
            print(json.dumps({"agent": agent, "edges": filtered}, ensure_ascii=False, indent=2))
            return 0
        print(f"edges for {agent[:16]}... ({len(filtered)}):")
        for e in filtered:
            arrow = "->" if e["truster"] == agent else "<-"
            peer = e["trustee"] if e["truster"] == agent else e["truster"]
            tag = " [DISTRUST]" if e["weight"] < 0 else ""
            print(f"  {arrow} {peer[:16]}...  weight={e['weight']}{tag}")
    else:
        nodes = {e["truster"] for e in edges} | {e["trustee"] for e in edges}
        distrust = sum(1 for e in edges if e["weight"] < 0)
        if getattr(args, "json_output", False):
            print(json.dumps({"edges": len(edges), "nodes": len(nodes),
                              "distrust": distrust}, ensure_ascii=False, indent=2))
            return 0
        print(f"total edges:   {len(edges)} ({distrust} distrust)")
        print(f"total nodes:   {len(nodes)}")
        print(f"snapshot date: {res['snapshot'].get('meta', {}).get('snapshot_date', '?')}")
    return 0
