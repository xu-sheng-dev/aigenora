"""v009 P1-5: protocol malicious-message self-test suite (optional).

Invoked by `aigenora protocol test --adversarial <dir>`. It is OPTIONAL — the default
`protocol test` happy-path run does not include it. Authors run it to self-check that
their hooks are robust against malformed peer messages.

The suite targets the protocol's first guest->host business message schema and injects
malformed variants (unknown field / missing required / out-of-range enum / out-of-range
integer / wrong type). Each variant MUST be rejected by `validate_message_obj` (raise
ValidationError) before reaching hooks. A non-rejection is a robustness gap.

Why first-message only and generic: the engine validates every inbound message against
the spec before hooks (proto/engine.py). Proving the validate gate holds on the first
guest message covers the same code path used for every subsequent message; protocol-
specific commit-reveal cheating is already covered by the simultaneous_round engine
(commit_mismatch_detected) and by tests/test_p2p_adversarial.py.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from aigenora.proto.validate import ValidationError, load_spec


class _InlineAsyncChannel:
    """Minimal in-process async channel for the adversarial harness.

    recv() returns messages put into the in-queue; close() pushes None so a waiting
    recv raises ChannelClosed (mirrors MemoryChannel semantics).
    """

    def __init__(self, in_q: "asyncio.Queue", out_q: "asyncio.Queue"):
        self._in = in_q
        self._out = out_q

    async def send(self, msg: Any) -> None:
        await self._out.put(msg)

    async def recv(self, timeout: float | None = None) -> Any:
        msg = await self._in.get()
        if msg is None:
            from aigenora.engine.p2p import ChannelClosed
            raise ChannelClosed("peer closed")
        return msg

    async def send_wait(self, msg: Any, timeout: float | None = None) -> Any:
        await self.send(msg)
        return await self.recv(timeout)

    async def close(self) -> None:
        await self._out.put(None)


def _placeholder(field: dict[str, Any]) -> Any:
    """A spec-valid placeholder value for a field, used to build a baseline message."""
    t = field.get("type")
    if t == "integer":
        lo = field.get("min")
        hi = field.get("max")
        if lo is not None:
            return lo
        if hi is not None:
            return hi
        return 0
    if t == "enum":
        values = field.get("values") or ["x"]
        return values[0]
    if t == "boolean":
        return False
    if t == "hash":
        return "0" * 64
    if t == "nonce":
        return "0" * 16
    if t == "signature":
        return "0" * 128
    if t in ("id", "ticket", "text"):
        return "placeholder"
    return None


def _first_guest_msg_schema(spec: dict[str, Any]) -> dict[str, Any] | None:
    for m in spec.get("messages", []):
        if m.get("direction") in ("guest_to_host", "both"):
            return m
    return None


def _build_baseline(schema: dict[str, Any]) -> dict[str, Any]:
    fields = schema.get("fields", {})
    base: dict[str, Any] = {}
    action_schema = fields.get("action")
    if isinstance(action_schema, dict) and action_schema.get("values"):
        base["action"] = action_schema["values"][0]
    for key, field in fields.items():
        if key == "action" or not isinstance(field, dict):
            continue
        if field.get("required"):
            base[key] = _placeholder(field)
    return base


def _build_malformed_variants(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return [(label, malformed_msg)] derived from the message schema."""
    fields = schema.get("fields", {})
    base = _build_baseline(schema)
    variants: list[tuple[str, dict[str, Any]]] = []

    # 1. unknown field
    v = dict(base)
    v["__evil_unknown_field__"] = 1
    variants.append(("unknown_field", v))

    # 2. missing the first non-action required field
    for key, field in fields.items():
        if key == "action" or not isinstance(field, dict) or not field.get("required"):
            continue
        v = dict(base)
        v.pop(key, None)
        variants.append((f"missing_required_{key}", v))
        break

    # 3. enum field set to a value outside its whitelist
    for key, field in fields.items():
        if key == "action" or not isinstance(field, dict):
            continue
        if field.get("type") == "enum":
            v = dict(base)
            v[key] = "__not_in_enum__"
            variants.append((f"enum_out_of_range_{key}", v))
            break

    # 4. integer field out of range (max+1, or min-1, or -1 if unbounded)
    for key, field in fields.items():
        if not isinstance(field, dict) or field.get("type") != "integer":
            continue
        if "max" in field:
            bad = field["max"] + 1
        elif "min" in field:
            bad = field["min"] - 1
        else:
            bad = -1
        v = dict(base)
        v[key] = bad
        variants.append((f"integer_out_of_range_{key}", v))
        break

    # 5. integer field wrong type (string instead of int)
    for key, field in fields.items():
        if not isinstance(field, dict) or field.get("type") != "integer":
            continue
        v = dict(base)
        v[key] = "not_an_integer"
        variants.append((f"integer_wrong_type_{key}", v))
        break

    return variants


async def _expect_rejection(protocol_dir: Path, options: dict[str, Any], msg: dict[str, Any]) -> bool:
    """Start a host, inject msg as the first guest message, expect ValidationError."""
    from aigenora.proto.engine import run_host_async

    host_in: "asyncio.Queue" = asyncio.Queue()
    host_out: "asyncio.Queue" = asyncio.Queue()
    ch = _InlineAsyncChannel(host_in, host_out)
    task = asyncio.create_task(
        run_host_async(protocol_dir, ch, options=options, state_base=tempfile.mkdtemp())
    )
    await host_in.put(msg)  # host's first recv() picks up the malformed message
    try:
        await task
        return False  # host returned normally = message was NOT rejected (gap)
    except ValidationError:
        return True
    except Exception:
        # any non-ValidationError failure is also a dirty outcome, treat as not-cleanly-rejected
        return False
    finally:
        if not task.done():
            task.cancel()


def run_adversarial_suite(protocol_dir: str | Path, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the malicious-message suite against a protocol directory.

    Returns a summary dict: {status, passed, failed, total, details:[{case, rejected}]}.
    Prerequisite: the protocol dir must have a real (non-skeleton) hooks.py; callers
    (`protocol test --adversarial`) already enforce assert_hooks_implemented beforehand.
    """
    protocol_dir = Path(protocol_dir)
    spec = load_spec(protocol_dir / "spec.json")
    schema = _first_guest_msg_schema(spec)
    if schema is None:
        return {"status": "skipped", "reason": "no guest_to_host message in spec",
                "passed": 0, "failed": 0, "total": 0, "details": []}

    variants = _build_malformed_variants(schema)
    details = []
    for label, msg in variants:
        rejected = asyncio.run(_expect_rejection(protocol_dir, options or {}, msg))
        details.append({"case": label, "rejected": rejected})

    passed = sum(1 for d in details if d["rejected"])
    failed = len(details) - passed
    return {
        "status": "ok" if failed == 0 else "fail",
        "passed": passed,
        "failed": failed,
        "total": len(details),
        "details": details,
    }
