"""Trusted cross-device agent collaboration (M1a).

Implements the first deliverable slice of
docs/design/cross-device-agent-collaboration.md: PSK-paired devices, the
in-stream admission handshake, the trusted-agent-task-v1 protocol with
durable task state and batched artifact transfer, worker adapters (zcode /
file / echo), and the A/B-side CLIs.
"""
from __future__ import annotations

__all__ = ["admission", "adapter", "cli", "errors", "guest_app", "host_app", "net", "psk", "service", "store", "transfer", "worker_cli"]
