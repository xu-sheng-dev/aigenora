from __future__ import annotations

import importlib.util
import hashlib
from pathlib import Path

from .hooks import ProtocolHooks


def load_hooks(protocol_dir: str | Path) -> ProtocolHooks:
    proto_dir = Path(protocol_dir)
    hooks_file = proto_dir / "hooks.py"
    if not hooks_file.exists():
        raise FileNotFoundError(f"hooks.py not found in protocol dir: {proto_dir}")
    module_name = "aigenora_user_protocol_" + hashlib.sha256(str(hooks_file.resolve()).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(module_name, hooks_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load hooks module: {hooks_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = getattr(module, "Hooks", None)
    if cls is None:
        raise TypeError(f"{hooks_file} must define class Hooks")
    obj = cls()
    if not isinstance(obj, ProtocolHooks):
        raise TypeError("Hooks must inherit aigenora.proto.hooks.ProtocolHooks")
    return obj

