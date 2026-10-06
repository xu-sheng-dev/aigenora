"""Hooks for trusted-agent-task-v1.

The M1a collaboration host runs its own multi-connection accept loop
(``aigenora.collab.host_app``) and dispatches messages through
``aigenora.collab.service.TaskService``; this hooks module makes the protocol
directory drivable through the generic single-session engine
(``aigenora host/join --protocol-dir ...``) for smoke tests and the builtin
protocol completeness suite.

Generic-engine behavior: one status_query (task id from the ``collab_task_id``
option, default ``demo-task``) answered with a snapshot/unknown-task error;
the session ends after that exchange. Host-side handling delegates to the
real TaskService only when the ``collab_data_dir`` option is provided.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aigenora.proto.hooks import HookResult, ProtocolHooks


class Hooks(ProtocolHooks):
    SUPPORTED_CONTROL_MODES = ("autonomous", "hybrid", "human")

    def proto_init(self, options, role, args, state_dir: Path, decision_config: dict[str, Any] | None = None):
        super().proto_init(options, role, args, state_dir, decision_config)
        self._service = None
        self._source_device = "engine-guest"
        if role == "host" and isinstance(options, dict) and options.get("collab_data_dir"):
            from aigenora.collab.service import TaskService

            self._service = TaskService(str(options["collab_data_dir"]))

    def proto_host_metadata(self):
        return ("Trusted Agent Task", "collab,trusted-agent-task", "supply", {})

    def _demo_task_id(self) -> str:
        if isinstance(self.options, dict):
            value = self.options.get("collab_task_id")
            if isinstance(value, str) and value:
                return value
        return "demo-task"

    def proto_guest_join_message(self):
        return {"action": "status_query", "task_id": self._demo_task_id(), "after_event_id": 0}

    def proto_guest_first_action(self):
        return {"action": "status_query", "task_id": self._demo_task_id(), "after_event_id": 0}

    def proto_guest_handle(self, msg):
        # Generic-engine smoke path: after one query/response round trip the
        # session completes. The real guest driver is aigenora.collab.guest_app.
        return HookResult(None, completed=True)

    def proto_host_handle_join(self, msg):
        task_id = str(msg.get("task_id", "") or self._demo_task_id())
        if self._service is not None:
            try:
                response = self._service.handle(dict(msg), source_device=self._source_device)
            except Exception:
                response = {
                    "action": "error",
                    "error_code": "internal_error",
                    "detail": "task service failed to answer the query",
                }
            if response.get("action") == "error":
                return HookResult(response, completed=True)
            return HookResult(response)
        return HookResult(
            {
                "action": "task_status",
                "task_id": task_id,
                "state": "received",
                "revision": 0,
                "events": [],
                "notes": [],
            }
        )

    def proto_host_handle(self, msg):
        if self._service is None:
            task_id = str(msg.get("task_id", "") or self._demo_task_id())
            return HookResult(
                {
                    "action": "task_status",
                    "task_id": task_id,
                    "state": "received",
                    "revision": 0,
                    "events": [],
                    "notes": [],
                },
                completed=True,
            )
        response = self._service.handle(msg, source_device=self._source_device)
        completed = str(msg.get("action")) in ("result_ack",)
        return HookResult(response, completed=completed)
