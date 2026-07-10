"""Whisper → structured command bridge.

把人类在悄悄话框输入的自然语言（"教练说一直出布"）解析成 hooks 已能读懂的
strategy（持久）或 decide（单次），从而让 whisper 真正影响游戏出招。

设计要点：
- 纯函数，无副作用，易测试
- 关键词规则（中英双语），不依赖 LLM
- 持久触发词 → strategy；单次触发词 → decide；默认 → 持久
- 值解析：在协议提供的 choice_keywords 里做子串匹配（如 "rock/石头/r" → rock）
- 数字类协议（guess-number / weak-wins-all）：直接提取数字

v019-M2: 新增 parse_whisper_to_intent() / materialize_intent()，按协议 DECISION_SCHEMA
解析，识别 scope/target/value/policy，不再产生 {round: None}。parse_whisper_to_command()
保留为 wrapper，满足旧测试。

调用点：web.py 的 /api/whisper、CLI session whisper、hooks 基类的
_resolve_whisper_override。三者复用同一套解析逻辑，行为一致。
"""
from __future__ import annotations

import re
from typing import Any

# 持久触发词：出现这些词 → 生成 strategy（持续生效直到下一条）
# 顺序：先中文后英文，长词优先（"接下来都" 优先于 "都"）
PERSIST_KEYWORDS: tuple[str, ...] = (
    "一直", "总是", "永远", "以后", "接下来都", "接下来", "持续", "保持",
    "固定", "循环", "重复", "每轮都", "每手都", "全都",
    "always", "keep", "forever", "from now on", "all future", "every",
    "persist", "sticky",
)

# 单次触发词：出现这些词 → 生成 decide（只生效一次）
ONCE_KEYWORDS: tuple[str, ...] = (
    "这手", "这轮", "这一手", "这一轮", "下一手", "下一轮", "下次",
    "只", "仅", "就这次", "这一次",
    "this round", "this turn", "next", "once", "just this", "one time",
)

# v019-M2: 动态策略触发词（中英双语）
POLICY_KEYWORDS: dict[str, str] = {
    # policy_id: 触发词列表（任一命中即识别为该 policy）
    "mirror_previous_opponent": (
        "模仿对方上一轮", "模仿对方上一次", "跟着对方上一轮", "跟着对方出",
        "学对方上一轮", "学对方出", "对方出啥我出啥", "mirror", "copy opponent",
    ),
    "counter_previous_opponent": (
        "克制对方上一轮", "克制对方上一次", "打败对方上一轮", "克制上一轮",
        "打败上一轮", "克制对方", "counter", "beat opponent",
    ),
    "repeat_own_previous": (
        "重复自己上一轮", "重复上一轮的", "继续上一次", "repeat own",
    ),
}

# v019-M2: 一次性策略触发词（once + 策略计算，需落 IntentStore）
ONCE_POLICY_KEYWORDS: dict[str, str] = {
    "counter_once": (
        "下一轮克制", "下一手克制", "下次克制", "下一轮打败", "下一手打败",
    ),
    "mirror_once": (
        "下一轮模仿", "下一手模仿", "下次模仿", "下一轮跟着", "下一手跟着",
    ),
}


def _normalize(text: str) -> str:
    """小写化 + 去多余空白，保留中文。"""
    return re.sub(r"\s+", " ", text.strip().lower())


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    for n in needles:
        if n in haystack:
            return True
    return False


def detect_scope(text: str) -> str:
    """判断 whisper 是持久还是单次。返回 'persist' | 'once'。

    默认 persist（更符合"教练喊战术"直觉：喊了就一直执行，直到再喊新的）。
    """
    norm = _normalize(text)
    if _contains_any(norm, ONCE_KEYWORDS):
        return "once"
    return "persist"


def match_choice(text: str, choice_keywords: dict[str, list[str]]) -> str | None:
    """在 choice_keywords 里做子串匹配，返回匹配到的 choice key。

    choice_keywords 形如 {"rock": ["rock", "石头", "r"], ...}。
    匹配规则：对每个 choice，遍历其别名，若任一别名（小写）出现在 text（小写）里则命中。
    长别名优先（避免 "r" 抢先匹配到含 r 的句子）。
    """
    norm = _normalize(text)
    # 收集所有 (alias, choice) 对，按 alias 长度降序匹配
    pairs: list[tuple[str, str]] = []
    for choice, aliases in choice_keywords.items():
        for alias in aliases:
            pairs.append((alias.lower(), choice))
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    for alias, choice in pairs:
        if alias and alias in norm:
            return choice
    return None


def extract_number(text: str) -> int | None:
    """从文本提取数字（用于 guess-number 的猜测值、weak-wins-all 的 bid）。"""
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def detect_policy(text: str) -> str | None:
    """检测动态策略触发词，返回 policy_id 或 None。

    先检测持久策略（mirror/counter/repeat），再检测一次性策略（counter_once/mirror_once）。
    """
    norm = _normalize(text)
    # 一次性策略优先（更具体的匹配）
    for policy_id, keywords in ONCE_POLICY_KEYWORDS.items():
        for kw in keywords:
            if kw in norm:
                return policy_id
    for policy_id, keywords in POLICY_KEYWORDS.items():
        for kw in keywords:
            if kw in norm:
                return policy_id
    return None


def detect_target_policy(text: str) -> str:
    """识别目标窗口语义：current / next / persist / future。

    - persist: 持久策略
    - next: 下一轮/下一手
    - current: 这轮/这手
    - future: 显式 round N（本期暂按 next 处理，具体 N 由 materializer 解析）
    """
    norm = _normalize(text)
    if _contains_any(norm, PERSIST_KEYWORDS):
        return "persist"
    # 下一类
    next_kws = ("下一轮", "下一手", "下次", "next", "下一局")
    if _contains_any(norm, next_kws):
        return "next"
    # 当前类
    current_kws = ("这轮", "这一轮", "这手", "这一手", "this round", "this turn")
    if _contains_any(norm, current_kws):
        return "current"
    return "persist"  # 默认持久


def parse_whisper_to_intent(
    text: str,
    schema: dict | None = None,
    *,
    choice_keywords: dict[str, list[str]] | None = None,
    match_key: str = "round",
    match_value: Any = None,
) -> dict | None:
    """把 whisper 文本解析成 intent（v019-M2）。

    返回:
        {
          "scope": "persist" | "once",
          "target_policy": "current" | "next" | "future" | "persist",
          "value": {"choice": "rock"} | None,
          "policy": {"mode": "policy", "policy": "counter_previous_opponent"} | None,
          "raw_text": "...",
          "confidence": 0.9,
        }
        None — 无法解析

    schema 优先；无 schema 时从 choice_keywords 兼容生成。不再产生 {round: None}。
    """
    if not text or not text.strip():
        return None

    # 合并 schema 与 choice_keywords
    if schema is None:
        schema = {}
    if choice_keywords and not schema.get("choices"):
        schema = {**schema, "choices": choice_keywords}
    choices = schema.get("choices")
    value_field = schema.get("value_field") or ("choice" if choices else "bid")
    numeric = schema.get("numeric", False)

    scope = detect_scope(text)
    target = detect_target_policy(text)

    # 1) 动态策略检测（mirror/counter/repeat/once 策略）
    policy_id = detect_policy(text)
    if policy_id is not None:
        is_once_policy = policy_id in ONCE_POLICY_KEYWORDS
        eff_scope = "once" if is_once_policy else scope
        eff_target = "next" if is_once_policy else ("persist" if scope == "persist" else target)
        return {
            "scope": eff_scope,
            "target_policy": eff_target,
            "value": None,
            "policy": {"mode": "policy", "policy": policy_id, "params": {}},
            "raw_text": text,
            "confidence": 0.85,
        }

    # 2) 枚举值匹配（RPS/Coin Flip 等）
    if choices:
        choice = match_choice(text, choices)
        if choice is not None:
            return {
                "scope": scope,
                "target_policy": target,
                "value": {value_field: choice},
                "policy": None,
                "raw_text": text,
                "confidence": 0.9,
            }

    # 3) 数字提取（Weak Wins All bid / Guess Number number）
    num = extract_number(text)
    if num is not None:
        return {
            "scope": scope,
            "target_policy": target,
            "value": {value_field: num},
            "policy": None,
            "raw_text": text,
            "confidence": 0.9,
        }

    return None


def materialize_intent(
    intent: dict,
    *,
    state_dir: str,
    origin: str = "whisper_bridge",
    agent_id: str | None = None,
    whisper_id: str | None = None,
    current_match_key: str | None = None,
    current_match_value: Any = None,
) -> dict:
    """把 intent 落到 StrategyStore / DecisionBus / InterventionIntentStore。

    返回 {"ack_status": ..., "applied": ..., "detail": ...}

    分工规则：
    - persist + 固定值 → StrategyStore（strategy_active）
    - persist + policy → StrategyStore mode=policy（policy_active）
    - once + current → DecisionBus（decision_queued），目标窗口已 finalized 则 rejected_finalized
    - once + next + 目标可算(match_value+1) → future decision（decision_queued）
    - once + next + 策略(需窗口打开时算) → IntentStore（intent_queued）
    - once + future/跨局 → IntentStore（intent_queued）
    """
    from aigenora.proto.sdk import StrategyStore, DecisionBus
    from aigenora.proto.decide_gateway import submit_decision

    scope = intent.get("scope", "persist")
    target = intent.get("target_policy", "persist")
    value = intent.get("value")
    policy = intent.get("policy")
    mk = current_match_key or "round"

    # 持久策略/固定值 → StrategyStore
    if scope == "persist":
        strat = StrategyStore(state_dir)
        if policy is not None:
            # 动态策略：mode=policy
            payload = dict(policy)
            payload["_meta"] = {
                "origin": origin,
                "agent_id": agent_id,
                "caused_by_whisper_id": whisper_id,
            }
            strat.merge(payload)
            return {"ack_status": "policy_active", "applied": {"type": "policy", "policy": policy.get("policy")}, "detail": payload}
        if value is not None:
            payload = {"mode": "fixed", "_meta": {"origin": origin, "agent_id": agent_id, "caused_by_whisper_id": whisper_id}}
            payload.update(value)
            strat.merge(payload)
            return {"ack_status": "strategy_active", "applied": {"type": "strategy", "payload": payload}, "detail": payload}
        return {"ack_status": "unparsed", "applied": None, "detail": None}

    # once + 策略 → IntentStore（目标窗口可能确定，但 value 要等窗口打开时算）
    if scope == "once" and policy is not None:
        try:
            from aigenora.proto.intervention_intent import InterventionIntentStore
            store = InterventionIntentStore(state_dir)
            intent_rec = store.append({
                "scope": "once",
                "target_policy": target,
                "value": None,
                "policy": policy,
                "created_match_key": mk,
                "created_match_value": current_match_value,
                "ttl_windows": 3,
            }, origin=origin, agent_id=agent_id, caused_by_whisper_id=whisper_id)
            return {"ack_status": "intent_queued", "applied": {"type": "intent", "intent_id": intent_rec["intent_id"]}, "detail": intent_rec}
        except Exception:
            return {"ack_status": "error", "applied": None, "detail": "intent_store_failed"}

    # once + 固定值
    if scope == "once" and value is not None:
        # current → 当前窗口
        if target == "current":
            if current_match_value is None:
                # 无法确定当前窗口，落 IntentStore
                return _queue_intent(state_dir, intent, origin, agent_id, whisper_id, mk, current_match_value)
            # 检查 finalized
            bus = DecisionBus(state_dir)
            if bus.is_finalized(mk, current_match_value):
                return {"ack_status": "rejected_finalized", "applied": None, "detail": f"window {mk}={current_match_value} finalized"}
            res = submit_decision(state_dir, {mk: current_match_value, **value}, origin=origin, agent_id=agent_id, caused_by_whisper_id=whisper_id, require_match_key=True)
            if res.get("ok"):
                return {"ack_status": "decision_queued", "applied": {"type": "decide", "match_key": mk, "match_value": current_match_value, "decision_id": res.get("decision_id")}, "detail": res}
            return {"ack_status": "rejected_finalized" if res.get("reason") == "decision_finalized" else "error", "applied": None, "detail": res}

        # next → 目标可算则 future decision，否则 IntentStore
        if target == "next":
            if current_match_value is not None and isinstance(current_match_value, int):
                next_mv = current_match_value + 1
                res = submit_decision(state_dir, {mk: next_mv, **value}, origin=origin, agent_id=agent_id, caused_by_whisper_id=whisper_id, require_match_key=True)
                if res.get("ok"):
                    return {"ack_status": "decision_queued", "applied": {"type": "decide", "match_key": mk, "match_value": next_mv, "decision_id": res.get("decision_id")}, "detail": res}
                return {"ack_status": "error", "applied": None, "detail": res}
            # 不可算 → IntentStore
            return _queue_intent(state_dir, intent, origin, agent_id, whisper_id, mk, current_match_value)

        # future/跨局 → IntentStore
        return _queue_intent(state_dir, intent, origin, agent_id, whisper_id, mk, current_match_value)

    return {"ack_status": "unparsed", "applied": None, "detail": None}


def _queue_intent(state_dir, intent, origin, agent_id, whisper_id, mk, current_match_value):
    """落 InterventionIntentStore 的内部 helper。"""
    try:
        from aigenora.proto.intervention_intent import InterventionIntentStore
        store = InterventionIntentStore(state_dir)
        intent_rec = store.append({
            "scope": intent.get("scope", "once"),
            "target_policy": intent.get("target_policy", "next"),
            "value": intent.get("value"),
            "policy": intent.get("policy"),
            "created_match_key": mk,
            "created_match_value": current_match_value,
            "ttl_windows": 3,
        }, origin=origin, agent_id=agent_id, caused_by_whisper_id=whisper_id)
        return {"ack_status": "intent_queued", "applied": {"type": "intent", "intent_id": intent_rec["intent_id"]}, "detail": intent_rec}
    except Exception:
        return {"ack_status": "error", "applied": None, "detail": "intent_store_failed"}


def parse_whisper_to_command(
    text: str,
    choice_keywords: dict[str, list[str]] | None,
    match_key: str = "round",
    match_value: Any = None,
) -> dict | None:
    """[wrapper, 旧接口] 把 whisper 文本解析成结构化命令。

    v019-M2: 内部委托 parse_whisper_to_intent()，再转成旧 {type, payload} 格式。
    保留是为了兼容旧测试和第三方调用。旧格式约定：
    - 枚举类：strategy payload 用 `fixed` 字段；decide payload 用 `choice` 字段
    - 数字类：strategy/decide payload 用 `bid` 字段

    返回:
        {"type": "strategy", "payload": {...}}  — 持久，写入 strategy.json
        {"type": "decide", "payload": {...}}    — 单次，提交 decision
        None                                     — 无法解析（仅审计）
    """
    if not text or not text.strip():
        return None
    intent = parse_whisper_to_intent(text, None, choice_keywords=choice_keywords, match_key=match_key, match_value=match_value)
    if intent is None:
        return None
    scope = intent["scope"]
    value = intent.get("value") or {}
    policy = intent.get("policy")
    if policy is not None and scope == "persist":
        return {"type": "strategy", "payload": policy}
    if value:
        # 判断是枚举类还是数字类：枚举用 fixed/choice，数字用 bid
        is_enum = bool(choice_keywords)
        if scope == "persist":
            if is_enum:
                choice_val = value.get("choice") or next(iter(value.values()))
                return {"type": "strategy", "payload": {"mode": "fixed", "fixed": choice_val}}
            # 数字类
            num_val = value.get("bid") or next(iter(value.values()))
            return {"type": "strategy", "payload": {"mode": "fixed", "bid": num_val}}
        # decide
        if is_enum:
            choice_val = value.get("choice") or next(iter(value.values()))
            return {"type": "decide", "payload": {match_key: match_value, "choice": choice_val}}
        num_val = value.get("bid") or next(iter(value.values()))
        return {"type": "decide", "payload": {match_key: match_value, "bid": num_val}}
    if policy is not None:
        # once + policy：旧接口无法表达 IntentStore，返回 None 让旧路径走 operator_hint 兜底
        return None
    return None


__all__ = [
    "PERSIST_KEYWORDS",
    "ONCE_KEYWORDS",
    "POLICY_KEYWORDS",
    "ONCE_POLICY_KEYWORDS",
    "detect_scope",
    "detect_policy",
    "detect_target_policy",
    "match_choice",
    "extract_number",
    "parse_whisper_to_intent",
    "materialize_intent",
    "parse_whisper_to_command",
]
