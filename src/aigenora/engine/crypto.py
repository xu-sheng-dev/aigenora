from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any


def sha256(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def random_nonce(bytes_len: int = 8) -> str:
    return secrets.token_hex(bytes_len)


def commit_hash(choice: str | int, nonce: str | None = None) -> tuple[str, str]:
    n = nonce or random_nonce()
    return n, sha256(f"{choice}:{n}")


def verify_commit(choice: str | int, nonce: str, expected_hash: str) -> bool:
    return sha256(f"{choice}:{nonce}") == expected_hash


def compute_pow(nonce: str, public_key: str, difficulty: int) -> int:
    target = b"\x00" * int(difficulty)
    counter = 0
    while True:
        digest = hashlib.sha256(f"{nonce}{public_key}{counter}".encode("utf-8")).digest()
        if digest[:difficulty] == target:
            return counter
        counter += 1


def _flow_contract(spec: dict[str, Any]) -> dict[str, Any]:
    """Build the flow sub-object of the contract.

    Normalization rules (see docs/design/flow-modes.md §3.2):
    - flow missing or not an object: return {} (write nothing).
    - flow.mode missing or explicitly "session_loop": do not write mode; keep the existing default protocol hash stable.
    - Non-default mode (free / request_response / simultaneous_round / sequenced_turn): write mode.
    - When mode == "simultaneous_round", also write flow.round if present.
    - flow.phases, if present, is still written following the original rules.
    """
    flow = spec.get("flow") if isinstance(spec.get("flow"), dict) else None
    if not flow:
        return {}
    contract_flow: dict[str, Any] = {}
    if flow.get("phases"):
        contract_flow["phases"] = flow["phases"]
    mode = flow.get("mode")
    if isinstance(mode, str) and mode and mode != "session_loop":
        contract_flow["mode"] = mode
        if mode == "simultaneous_round" and isinstance(flow.get("round"), dict):
            contract_flow["round"] = flow["round"]
        elif mode == "authoritative_realtime" and isinstance(flow.get("realtime"), dict):
            # Tick rate, input delay and command limits change observable game
            # semantics.  Pin the complete validated object into protocol_id.
            contract_flow["realtime"] = flow["realtime"]
    return contract_flow


def business_contract(spec: dict[str, Any]) -> dict[str, Any]:
    contract: dict[str, Any] = {}
    for key in ["type", "messages", "commit_reveal"]:
        if key in spec:
            contract[key] = spec[key]
    flow_part = _flow_contract(spec)
    if flow_part:
        contract["flow"] = flow_part
    if isinstance(spec.get("parameters"), dict):
        params: dict[str, Any] = {}
        for key, value in spec["parameters"].items():
            if isinstance(value, dict):
                params[key] = {k: v for k, v in value.items() if k != "default"}
            else:
                params[key] = value
        contract["parameters"] = params
    if isinstance(spec.get("decision"), dict):
        d = dict(spec["decision"])
        mode = d.get("mode", "")
        if mode == "manual":
            d["mode"] = "human_only"
        elif mode == "auto":
            d["mode"] = "collaborative"
        contract["decision"] = d
    return contract


def business_hash_from_obj(spec: dict[str, Any]) -> str:
    canonical = json.dumps(business_contract(spec), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(canonical)


def protocol_contract(spec: dict[str, Any]) -> dict[str, Any]:
    contract: dict[str, Any] = {}
    for key in ["messages", "rules", "choices", "commit_reveal"]:
        if key in spec:
            contract[key] = spec[key]
    flow_part = _flow_contract(spec)
    if flow_part:
        contract["flow"] = flow_part
    if isinstance(spec.get("parameters"), dict):
        params: dict[str, Any] = {}
        for key, value in spec["parameters"].items():
            if isinstance(value, dict):
                params[key] = {k: v for k, v in value.items() if k != "default"}
            else:
                params[key] = value
        contract["parameters"] = params
    if "timing" in spec:
        contract["timing"] = spec["timing"]
    return contract


def protocol_hash_from_obj(spec: dict[str, Any]) -> str:
    canonical = json.dumps(protocol_contract(spec), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(canonical)


def protocol_hash(spec_file: str | Path) -> str:
    with Path(spec_file).open("r", encoding="utf-8") as f:
        return protocol_hash_from_obj(json.load(f))


def session_canonical(post_id: str, host_pub: str, guest_pub: str, protocol_id: str | None, nonce: str) -> str:
    return f"{post_id}:{host_pub}:{guest_pub}:{protocol_id or ''}:{nonce}"


def session_id(post_id: str, host_pub: str, guest_pub: str, protocol_id: str | None, nonce: str) -> str:
    return sha256(session_canonical(post_id, host_pub, guest_pub, protocol_id, nonce))


def transport_binding_canonical(public_key: str, transport: str, iroh_ticket: str, protocol_id: str | None) -> str:
    return (
        f"public_key:{public_key}\n"
        f"transport:{transport}\n"
        f"iroh_ticket:{iroh_ticket}\n"
        f"protocol_id:{protocol_id or ''}"
    )
