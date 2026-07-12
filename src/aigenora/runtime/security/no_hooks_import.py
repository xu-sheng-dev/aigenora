from __future__ import annotations

import importlib.abc
import sys


_FORBIDDEN_EXACT = {
    "aigenora.proto.loader",
    "aigenora.agent.host",
    "aigenora.agent.guest",
    "aigenora.agent.join",
}


def _forbidden(fullname: str) -> bool:
    if fullname in _FORBIDDEN_EXACT:
        return True
    parts = fullname.split(".")
    return "hooks" in parts or "protocol_worker" in parts


class _NoHooksFinder(importlib.abc.MetaPathFinder):
    _aigenora_no_hooks_gate = True

    def find_spec(self, fullname: str, path=None, target=None):  # type: ignore[no-untyped-def]
        if _forbidden(fullname):
            raise ImportError("identity Sidecar cannot import protocol execution modules")
        return None


def install_no_hooks_import_gate() -> None:
    if not any(getattr(finder, "_aigenora_no_hooks_gate", False) for finder in sys.meta_path):
        sys.meta_path.insert(0, _NoHooksFinder())
    assert_no_hooks_loaded()


def assert_no_hooks_loaded() -> None:
    forbidden = sorted(name for name in sys.modules if _forbidden(name))
    if forbidden:
        raise RuntimeError("identity Sidecar imported a protocol execution module")
