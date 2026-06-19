"""Web UI auto-launch mode resolution.

Three modes:
- auto    : start the relay subprocess and open the browser automatically (default behavior)
- headless: start the relay subprocess without opening a browser (print the URL for the user to open manually)
- off     : do not start the relay subprocess (pure CLI)

Priority (high -> low):
1. CLI argument: --web {auto,headless,off} (mutually-exclusive aliases of --no-web / --no-browser)
2. Environment variable: AIGENORA_WEB
3. Default value: auto
"""
from __future__ import annotations

import os
from typing import Literal

WebMode = Literal["auto", "headless", "off"]
VALID_MODES: tuple[WebMode, ...] = ("auto", "headless", "off")
DEFAULT_MODE: WebMode = "auto"
ENV_VAR = "AIGENORA_WEB"


def normalize(value: str | None) -> WebMode | None:
    """Normalize to a valid WebMode; return None if invalid or empty."""
    if value is None:
        return None
    v = value.strip().lower()
    if v in VALID_MODES:
        return v  # type: ignore[return-value]
    return None


def resolve_web_mode(args) -> WebMode:
    """Resolve the final web mode by CLI > env > default.

    args is expected to come from argparse and may contain the following optional attributes:
      - web      : explicit --web value (auto/headless/off)
      - no_web   : --no-web flag
      - no_browser: --no-browser flag
    """
    explicit = normalize(getattr(args, "web", None))
    if explicit is not None:
        return explicit
    if getattr(args, "no_web", False):
        return "off"
    if getattr(args, "no_browser", False):
        return "headless"
    env = normalize(os.environ.get(ENV_VAR))
    if env is not None:
        return env
    return DEFAULT_MODE
