from __future__ import annotations

import sys
from typing import Any


SUPPORTED_SPEC_VERSIONS = {"1.0"}
DEFAULT_SPEC_VERSION = "1.0"


def get_spec_version(spec: dict[str, Any]) -> str:
    return spec.get("spec_version") or DEFAULT_SPEC_VERSION


def check_spec_version(spec: dict[str, Any], reject_unknown: bool = True) -> str | None:
    version = get_spec_version(spec)
    if version in SUPPORTED_SPEC_VERSIONS:
        return None
    if reject_unknown:
        raise ValueError(
            f"unsupported spec_version: {version!r} "
            f"(supported: {', '.join(sorted(SUPPORTED_SPEC_VERSIONS))})"
        )
    print(
        f"[warning] unsupported spec_version: {version!r} "
        f"(supported: {', '.join(sorted(SUPPORTED_SPEC_VERSIONS))})",
        file=sys.stderr,
    )
    return version
