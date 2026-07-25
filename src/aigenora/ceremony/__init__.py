"""Native ceremony primitives.

This package is intentionally independent from the Host/Guest protocol engine.
It must not import ``aigenora.proto`` or execute protocol hooks.
"""

from .canonical import canonical_json_bytes, domain_hash_hex, parse_canonical_json
from .bus import InMemoryCeremonyBus, QuorumRule
from .errors import VsdpError
from .manifest import PROFILE_ID, build_final_manifest, build_setup_manifest
from .roles import RoleAssignment, RoleKind

__all__ = [
    "PROFILE_ID",
    "InMemoryCeremonyBus",
    "QuorumRule",
    "RoleAssignment",
    "RoleKind",
    "VsdpError",
    "build_final_manifest",
    "build_setup_manifest",
    "canonical_json_bytes",
    "domain_hash_hex",
    "parse_canonical_json",
]
