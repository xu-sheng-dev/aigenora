from __future__ import annotations

import json
import os
from pathlib import Path


DEFAULT_SERVER = "http://agent.aigenora.com"


def skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def data_dir(value: str | None = None) -> Path:
    raw = value or os.environ.get("P2P_DATA_DIR") or os.environ.get("AGENT_DIR")
    if raw:
        return Path(raw).expanduser()
    return Path.cwd() / ".aigenora"


def builtin_protocols_root() -> Path:
    # Read-only built-in sample protocols bundled with the package. Resolves to the
    # same location in source (aigenora/protocols) and wheel (site-packages/aigenora/
    # protocols) via parents[1]. Used only as the init seed source; runtime protocol
    # discovery reads the user library (data_protocols_root), never here.
    return Path(__file__).resolve().parents[1] / "protocols"


def data_protocols_root(data_dir_value: str | None = None) -> Path:
    # The user's writable protocol library under the data dir (default cwd/.aigenora/
    # protocols). init seeds samples here; fetch/create land here too. All runtime
    # discovery (path_for/search/preflight) reads this, not the bundled source.
    return data_dir(data_dir_value) / "protocols"


def protocols_root() -> Path:
    # Backward-compatible alias for the bundled sample source. New code should use
    # builtin_protocols_root() (seed source) or data_protocols_root() (user library).
    return builtin_protocols_root()


def templates_root(data_dir_value: str | None = None) -> Path:
    # Templates: prefer the user library (seeded by init), fall back to the bundled
    # sample source so `protocol create` works even before init has run.
    user = data_protocols_root(data_dir_value) / "templates"
    if user.exists():
        return user
    return builtin_protocols_root() / "templates"


def load_config() -> dict:
    candidates = [
        Path(os.environ["AIGENORA_CONFIG"]).expanduser()
        if os.environ.get("AIGENORA_CONFIG")
        else None,
        skill_root() / "aigenora.conf",
    ]
    for path in candidates:
        if path and path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    return {}


def check_version(server: str | None = None):
    try:
        import httpx
        r = httpx.get(f"{get_server(server)}/api/v1/version", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def get_server(explicit: str | None = None) -> str:
    if explicit:
        return explicit.rstrip("/")
    if os.environ.get("P2P_SERVER"):
        return os.environ["P2P_SERVER"].rstrip("/")
    return str(load_config().get("server") or DEFAULT_SERVER).rstrip("/")


def get_trust_url(explicit: str | None = None) -> str:
    """信任快照分发 base URL（v011 M10）。优先级：显式参数 > AIGENORA_TRUST_URL 环境变量 >
    aigenora.conf trust_url > 主 server（兜底，假设同域 /trust 子路径由 nginx serve）。

    生产独立子域配 AIGENORA_TRUST_URL=https://trust.aigenora.com；staging 子路径配
    http://test.aigenora.com/trust。trust 是公开只读静态文件，无需签名。
    """
    if explicit:
        return explicit.rstrip("/")
    if os.environ.get("AIGENORA_TRUST_URL"):
        return os.environ["AIGENORA_TRUST_URL"].rstrip("/")
    cfg = load_config()
    if cfg.get("trust_url"):
        return str(cfg["trust_url"]).rstrip("/")
    return get_server().rstrip("/")
