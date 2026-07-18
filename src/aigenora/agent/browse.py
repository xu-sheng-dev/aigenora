from __future__ import annotations

import re
from aigenora.services import InvitationService, RegistryService, ServiceContext

_PROTOCOL_ID_RE = re.compile(r"[0-9a-f]{64}")
_TAG_RE = re.compile(r"[A-Za-z0-9_.:-]+")


def _pricing_text(item) -> str:
    if not isinstance(item, dict):
        return ""
    options = item.get("options")
    if isinstance(options, dict):
        pricing = options.get("pricing")
    else:
        pricing = item.get("pricing")
    if not isinstance(pricing, dict):
        return ""
    model = pricing.get("model", "")
    if model == "free":
        return "free"
    amount = pricing.get("amount", "")
    currency = pricing.get("currency", "")
    return f"{amount} {currency}".strip() or model


# options 中对用户决策最关键的键（游戏局数/节奏等），按此顺序格式化为可读摘要
_OPTIONS_DISPLAY_KEYS = (
    "best_of", "rounds_to_win", "termination",
    "round_delay_seconds", "min_think_seconds", "max_think_seconds",
)


def _options_text(item) -> str:
    """格式化邀约 options 为可读摘要，突出局数/节奏等关键参数。

    未携带 options 时返回空串，调用方据此决定是否打印该行。
    """
    if not isinstance(item, dict):
        return ""
    options = item.get("options")
    if not isinstance(options, dict) or not options:
        return ""
    parts = []
    for key in _OPTIONS_DISPLAY_KEYS:
        if key in options:
            parts.append(f"{key}={options[key]}")
    # 兜底：展示剩余非敏感键（如自定义业务参数），跳过 pricing（_pricing_text 已处理）
    for key, value in options.items():
        if key in _OPTIONS_DISPLAY_KEYS or key == "pricing":
            continue
        parts.append(f"{key}={value}")
    return ", ".join(parts)


def _validate_tags_filter(tags_arg: str | None) -> list[str]:
    if tags_arg is None:
        return []
    if not tags_arg:
        raise ValueError("--tags cannot be empty")
    tags: list[str] = []
    for raw in tags_arg.split(","):
        tag = raw.strip()
        if not tag:
            continue
        if len(tag) > 64:
            raise ValueError("--tags entries must be at most 64 characters")
        if not _TAG_RE.fullmatch(tag):
            raise ValueError("--tags entries may contain only A-Za-z0-9_.:-")
        if tag not in tags:
            tags.append(tag)
        if len(tags) > 10:
            raise ValueError("--tags accepts at most 10 tags")
    if not tags:
        raise ValueError("--tags cannot be empty")
    return tags


def _validate_filters(args) -> None:
    if getattr(args, "post_id", None):
        return
    _validate_tags_filter(getattr(args, "tags", None))
    protocol_id = getattr(args, "protocol_id", None)
    if protocol_id is not None:
        if not protocol_id:
            raise ValueError("--protocol-id cannot be empty")
        if not _PROTOCOL_ID_RE.fullmatch(protocol_id):
            raise ValueError("--protocol-id must be a 64-char lowercase protocol hash")


def run(args) -> int:
    try:
        _validate_filters(args)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    context = ServiceContext.create(args.data_dir, args.server)
    if args.post_id:
        data = InvitationService(context).inspect(args.post_id)
        items = [data]
        total = 1
    else:
        items, total = RegistryService(context).browse(
            protocol_id=args.protocol_id,
            invitation_type=args.type,
            tags=args.tags,
            limit=args.limit,
        )
    if args.oneline:
        for item in items:
            tags = item.get("tags", [])
            if isinstance(tags, list):
                tags_text = ",".join(str(t) for t in tags)
            else:
                tags_text = str(tags or "")
            print("\t".join([
                str(item.get("post_id", "")),
                str(item.get("protocol_id", "") or ""),
                str(item.get("type", "") or "chat"),
                str(item.get("message", "") or ""),
                tags_text,
                str(item.get("public_key", "") or ""),
                "true" if item.get("registered", False) else "false",
                str(item.get("nickname", "") or ""),
                str(item.get("agent_id", "") or ""),
                _pricing_text(item),
                _options_text(item),
                str(item.get("host_control_mode", "hybrid") or "hybrid"),
            ]))
        return 0
    print(f"Total: {total}")
    for item in items:
        print(f"{item.get('post_id')} [{item.get('type', 'chat')}] {item.get('message', '')}")
        print(f"  protocol_id: {item.get('protocol_id', '')}")
        print(f"  public_key: {item.get('public_key', '')}")
        print(f"  host_control_mode: {item.get('host_control_mode', 'hybrid') or 'hybrid'}")
        options_text = _options_text(item)
        if options_text:
            print(f"  options: {options_text}")
    return 0
