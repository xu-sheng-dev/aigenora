from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aigenora.engine.keys import load_keys
from aigenora.proto.rule_artifacts import (
    create_rule_endorsement,
    create_rule_proposal,
    freeze_rule_set,
    verify_rule_artifact,
)


def run(args) -> int:
    if args.rules_cmd == "propose":
        spec = _read_object(args.spec)
        rules_text = (
            Path(args.rules).read_text(encoding="utf-8")
            if args.rules
            else str(spec.get("rules") or spec.get("description") or "")
        )
        artifact = create_rule_proposal(
            spec,
            rules_text=rules_text,
            keypair=load_keys(args.data_dir),
        )
        _write_object(args.output, artifact)
        _print_result(args, artifact, "proposal_id")
        return 0
    if args.rules_cmd == "endorse":
        proposal = _read_object(args.proposal)
        artifact = create_rule_endorsement(
            proposal,
            decision=args.decision,
            reason=args.reason or "",
            keypair=load_keys(args.data_dir),
        )
        _write_object(args.output, artifact)
        _print_result(args, artifact, "endorsement_id")
        return 0
    if args.rules_cmd == "freeze":
        proposal = _read_object(args.proposal)
        endorsements = [_read_object(path) for path in args.endorsement]
        artifact = freeze_rule_set(
            proposal,
            endorsements,
            quorum=args.quorum,
            coordinator_keypair=load_keys(args.data_dir),
        )
        _write_object(args.output, artifact)
        _print_result(args, artifact, "ruleset_id")
        return 0
    if args.rules_cmd == "verify":
        artifact = verify_rule_artifact(_read_object(args.artifact))
        if args.json_output:
            print(json.dumps(artifact, ensure_ascii=False, indent=2))
        else:
            identifier = next(
                (
                    artifact.get(key)
                    for key in ("ruleset_id", "proposal_id", "endorsement_id")
                    if artifact.get(key)
                ),
                "unknown",
            )
            print("[OK] rule artifact verified")
            print(f"kind: {artifact['artifact_kind']}")
            print(f"id: {identifier}")
        return 0
    raise RuntimeError(f"unknown protocol rules command: {args.rules_cmd}")


def _read_object(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _write_object(path: str | Path, value: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _print_result(args, artifact: dict[str, Any], identifier: str) -> None:
    if args.json_output:
        print(json.dumps(artifact, ensure_ascii=False, indent=2))
    else:
        print(f"[OK] {artifact['artifact_kind']}: {Path(args.output).resolve()}")
        print(f"{identifier}: {artifact[identifier]}")
