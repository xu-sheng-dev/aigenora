"""Whisper → structured command bridge.

把人类在悄悄话框输入的自然语言（"教练说一直出布"）解析成 hooks 已能读懂的
strategy（持久）或 decide（单次），从而让 whisper 真正影响游戏出招。

设计要点：
- 纯函数，无副作用，易测试
- 关键词规则（中英双语），不依赖 LLM
- 持久触发词 → strategy；单次触发词 → decide；默认 → 持久
- 值解析：在协议提供的 choice_keywords 里做子串匹配（如 "rock/石头/r" → rock）
- 数字类协议（guess-number / weak-wins-all）：直接提取数字

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


def parse_whisper_to_command(
    text: str,
    choice_keywords: dict[str, list[str]] | None,
    match_key: str = "round",
    match_value: Any = None,
) -> dict | None:
    """把 whisper 文本解析成结构化命令。

    返回:
        {"type": "strategy", "payload": {...}}  — 持久，写入 strategy.json
        {"type": "decide", "payload": {...}}    — 单次，提交 decision
        None                                     — 无法解析（仅审计）

    参数:
        text: whisper 原文（如 "教练说一直出布"）
        choice_keywords: 协议的选项关键词映射，如 {"rock":["rock","石头"]}
                         None 或空 dict 表示协议未声明（回退数字解析）
        match_key: decide 的匹配键（如 "round"/"turn"/"attempt"）
        match_value: decide 的匹配值（如轮号；None 时调用方需补）
    """
    if not text or not text.strip():
        return None
    scope = detect_scope(text)

    # 1) choice_keywords 匹配（枚举类协议：RPS/Coin Flip/Hero Duel）
    if choice_keywords:
        choice = match_choice(text, choice_keywords)
        if choice is not None:
            if scope == "persist":
                return {"type": "strategy", "payload": {"mode": "fixed", "fixed": choice}}
            return {"type": "decide", "payload": {match_key: match_value, "choice": choice}}

    # 2) 数字提取（押注/猜测类协议：guess-number/weak-wins-all）
    num = extract_number(text)
    if num is not None:
        # 区分 bid（weak-wins-all 的押注）vs guess（guess-number 的猜测）
        # 由调用方通过 choice_keywords 的 key 约定；这里用通用 "value"
        value_field = "bid" if not choice_keywords else next(iter(choice_keywords.keys()), "value")
        if scope == "persist":
            return {"type": "strategy", "payload": {"mode": "fixed", "bid": num}}
        return {"type": "decide", "payload": {match_key: match_value, "bid": num}}

    return None


__all__ = [
    "PERSIST_KEYWORDS",
    "ONCE_KEYWORDS",
    "detect_scope",
    "match_choice",
    "extract_number",
    "parse_whisper_to_command",
]
