from __future__ import annotations

import importlib.util
import platform

from aigenora import __version__
from aigenora.engine.config import check_version, get_server
from aigenora.engine.keys import key_path


def run(args) -> int:
    print(f"client: {__version__}")
    print(f"python: {platform.python_version()} ({platform.platform()})")
    print(f"server: {get_server(args.server)}")
    print(f"key: {key_path(args.data_dir)} {'OK' if key_path(args.data_dir).exists() else 'MISSING'}")
    for mod in ["cryptography", "httpx", "iroh"]:
        print(f"{mod}: {'OK' if importlib.util.find_spec(mod) else 'MISSING'}")
    if args.offline:
        return 0
    info = check_version(args.server)
    if info:
        print(f"min_client_version: {info.get('min_client_version')}")
        print(f"latest_version: {info.get('latest_version')}")
    print("[OK] doctor completed")
    return 0

