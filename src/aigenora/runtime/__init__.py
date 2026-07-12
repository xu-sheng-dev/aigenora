"""Managed Aigenora Runtime API.

The identity Sidecar deliberately does not import protocol hooks.  First-party
hooks run only in the separate ``aigenora-protocol-worker`` process.
"""

from .generated.v1.contracts import RUNTIME_SCHEMA_DIGEST

__all__ = ["RUNTIME_SCHEMA_DIGEST"]
