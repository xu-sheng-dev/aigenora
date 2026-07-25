from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from aigenora.ceremony.artifacts import make_public_artifact
from aigenora.ceremony.board import (
    CommandProofVerifier,
    combine_acceptances,
    combine_checkpoints,
    generate_witness_key,
)
from aigenora.ceremony.canonical import (
    canonical_json_bytes,
    parse_canonical_json,
    require_hex,
    sha256_hex,
)
from aigenora.ceremony.decision_proof import build_provisional_decision_proof
from aigenora.ceremony.errors import NON_CANONICAL, VsdpError
from aigenora.ceremony.manifest import (
    build_final_manifest,
    build_setup_manifest,
    ceremony_id,
    decision_id,
    validate_final_manifest,
)
from aigenora.ceremony.tally import CommandTallyVerifier
from aigenora.engine.config import get_server
from aigenora.engine.keys import load_keys
from aigenora.engine.rest import RestClient
from aigenora.verifier.vsdp import (
    BOARD_BUNDLE_SCHEMA,
    DECISION_BUNDLE_SCHEMA,
    verify_board_bundle_file,
    verify_decision_bundle_file,
)


EXPERIMENTAL_NOTICE = (
    "EXPERIMENTAL L0/L1 RESEARCH ONLY; NOT EXTERNALLY AUDITED; "
    "NOT FOR REAL DECISIONS; NETWORK PARTICIPATION MAY BE LINKABLE."
)


def _notice() -> None:
    print(EXPERIMENTAL_NOTICE, file=sys.stderr)


def _load_object(path: str | Path, label: str) -> dict[str, Any]:
    value = parse_canonical_json(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise VsdpError(NON_CANONICAL, f"{label} must be an object")
    return value


def _write_object(path: str | Path, value: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_json_bytes(value)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _parse_choice(value: str) -> tuple[str, str]:
    option_id, separator, summary = value.partition("=")
    if not separator or not option_id or not summary:
        raise VsdpError(NON_CANONICAL, "choice must use OPTION_ID=SUMMARY")
    return option_id, summary


def _json_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        try:
            error = response.json()
            code = error.get("code", f"HTTP_{response.status_code}")
            message = error.get("message", "Witness rejected the request")
        except Exception:
            code = f"HTTP_{response.status_code}"
            message = "Witness rejected the request"
        raise VsdpError(str(code), str(message))
    value = parse_canonical_json(response.content)
    if not isinstance(value, dict):
        raise VsdpError(NON_CANONICAL, "Witness response must be an object")
    return value


def _cmd_manifest_setup(args: argparse.Namespace) -> int:
    setup = build_setup_manifest(
        question=args.question,
        choices=[_parse_choice(value) for value in args.choice],
        guardian_public_keys=args.guardian_public_key,
        witness_public_keys=args.witness_public_key,
        roster_snapshot_commitment=args.roster_snapshot_commitment,
        minimum_anonymity_set=args.minimum_anonymity_set,
        enrollment_duration_seconds=args.enrollment_duration_seconds,
        voting_duration_seconds=args.voting_duration_seconds,
        dispute_duration_seconds=args.dispute_duration_seconds,
        challenge_source=args.challenge_source,
    )
    _write_object(args.output, setup)
    print(
        json.dumps(
            {
                "ceremony_id": ceremony_id(setup),
                "output": str(Path(args.output)),
                "profile_id": setup["profile_id"],
                "schema": "vsdp-manifest-created/1",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _cmd_manifest_final(args: argparse.Namespace) -> int:
    setup = _load_object(args.setup, "Setup Manifest")
    final = build_final_manifest(
        setup_manifest=setup,
        eligibility_root=args.eligibility_root,
        enrollment_count=args.enrollment_count,
        tally_public_key=args.tally_public_key,
        dkg_transcript_root=args.dkg_transcript_root,
        board_epoch=args.board_epoch,
        board_genesis_hash=args.board_genesis_hash,
        setup_artifact_root=args.setup_artifact_root,
        vote_opens_at=args.vote_opens_at,
        vote_closes_at=args.vote_closes_at,
        dispute_closes_at=args.dispute_closes_at,
    )
    _write_object(args.output, final)
    print(
        json.dumps(
            {
                "decision_id": decision_id(final),
                "output": str(Path(args.output)),
                "schema": "vsdp-manifest-created/1",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _cmd_witness_keygen(args: argparse.Namespace) -> int:
    key = generate_witness_key(args.output, force=args.force)
    print(
        json.dumps(
            {
                "output": str(Path(args.output)),
                "public_key": key.public_key,
                "schema": "vsdp-witness-key-created/1",
                "witness_id": key.witness_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _cmd_witness_serve(args: argparse.Namespace) -> int:
    from aigenora.ceremony.witness_server import main as witness_main

    argv = [
        "--state-dir",
        args.state_dir,
        "--final-manifest",
        args.final_manifest,
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.key_file:
        argv.extend(["--key-file", args.key_file])
    if args.init_key:
        argv.append("--init-key")
    if args.force_key:
        argv.append("--force-key")
    if args.control_token_file:
        argv.extend(["--control-token-file", args.control_token_file])
    if args.proof_verifier_command:
        argv.extend(["--proof-verifier-command", args.proof_verifier_command])
    return witness_main(argv)


def _unique_urls(values: list[str]) -> list[str]:
    urls = sorted({value.rstrip("/") for value in values})
    if len(urls) < 3:
        raise VsdpError(NON_CANONICAL, "at least three distinct Witness URLs are required")
    return urls


def _control_tokens(values: list[str], urls: list[str]) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for value in values:
        url, separator, path = value.partition("=")
        normalized_url = url.rstrip("/")
        if (
            not separator
            or normalized_url not in urls
            or normalized_url in tokens
            or not path
        ):
            raise VsdpError(
                NON_CANONICAL,
                "control token files must use unique WITNESS_URL=PATH entries",
            )
        try:
            token = Path(path).read_text(encoding="ascii")
        except (OSError, UnicodeError) as exc:
            raise VsdpError(
                NON_CANONICAL,
                "Witness control token file cannot be read",
            ) from exc
        token = require_hex(token, 64, "Witness control token")
        tokens[normalized_url] = token
    if set(tokens) != set(urls):
        raise VsdpError(NON_CANONICAL, "every Witness URL requires a control token file")
    return tokens


def _cmd_board_post(args: argparse.Namespace) -> int:
    final_manifest = _load_object(args.final_manifest, "Final Manifest")
    record = _load_object(args.record, "BallotRecord")
    urls = _unique_urls(args.witness_url)
    acceptances: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    request = {"record": record, "schema": "vsdp-ballot-submit/1"}
    with httpx.Client(timeout=args.timeout) as client:
        for url in urls:
            try:
                response = _json_response(
                    client.post(
                        f"{url}/v1/ballots",
                        content=canonical_json_bytes(request),
                        headers={"Content-Type": "application/json"},
                    )
                )
                acceptance = response.get("acceptance")
                if not isinstance(acceptance, dict):
                    raise VsdpError(NON_CANONICAL, "Witness omitted its acceptance")
                acceptances.append(acceptance)
            except (httpx.HTTPError, VsdpError) as exc:
                errors.append({"error": str(exc), "witness": url})
        receipt = combine_acceptances(acceptances, final_manifest=final_manifest)
        gossip = {
            "receipt": receipt,
            "record": record,
            "schema": "vsdp-receipt-gossip/1",
        }
        stored = 0
        for url in urls:
            try:
                _json_response(
                    client.post(
                        f"{url}/v1/receipts",
                        content=canonical_json_bytes(gossip),
                        headers={"Content-Type": "application/json"},
                    )
                )
                stored += 1
            except (httpx.HTTPError, VsdpError):
                continue
    _write_object(args.output, receipt)
    print(
        json.dumps(
            {
                "errors": errors,
                "gossip_stored": stored,
                "output": str(Path(args.output)),
                "receipt_id": receipt["receipt_id"],
                "schema": "vsdp-board-posted/1",
                "signature_count": len(receipt["signatures"]),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _cmd_board_seal(args: argparse.Namespace) -> int:
    final_manifest = _load_object(args.final_manifest, "Final Manifest")
    urls = _unique_urls(args.witness_url)
    control_tokens = _control_tokens(args.witness_control_token_file, urls)
    request = {
        "cutoff_statement_hash": args.cutoff_statement_hash,
        "height": args.height,
        "previous_checkpoint_hash": args.previous_checkpoint_hash,
        "schema": "vsdp-checkpoint-request/1",
    }
    values: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with httpx.Client(timeout=args.timeout) as client:
        for url in urls:
            try:
                response = _json_response(
                    client.post(
                        f"{url}/v1/checkpoints/sign",
                        content=canonical_json_bytes(request),
                        headers={
                            "Authorization": f"Bearer {control_tokens[url]}",
                            "Content-Type": "application/json",
                        },
                    )
                )
                signed = response.get("signed_checkpoint")
                if not isinstance(signed, dict):
                    raise VsdpError(NON_CANONICAL, "Witness omitted its checkpoint signature")
                values.append(signed)
            except (httpx.HTTPError, VsdpError) as exc:
                errors.append({"error": str(exc), "witness": url})
    checkpoint = combine_checkpoints(values, final_manifest=final_manifest)
    _write_object(args.output, checkpoint)
    print(
        json.dumps(
            {
                "checkpoint_hash": checkpoint["checkpoint_hash"],
                "errors": errors,
                "output": str(Path(args.output)),
                "record_count": checkpoint["checkpoint"]["record_count"],
                "schema": "vsdp-board-sealed/1",
                "signature_count": len(checkpoint["signatures"]),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _cmd_bundle_build(args: argparse.Namespace) -> int:
    final_manifest = _load_object(args.final_manifest, "Final Manifest")
    records = [_load_object(path, "BallotRecord") for path in args.record]
    receipts = [_load_object(path, "QuorumReceipt") for path in args.receipt]
    checkpoint = _load_object(args.checkpoint, "QuorumCheckpoint")
    bundle = {
        "checkpoint": checkpoint,
        "final_manifest": final_manifest,
        "minimum_anonymity_set": args.minimum_anonymity_set,
        "receipts": receipts,
        "records": records,
        "schema": BOARD_BUNDLE_SCHEMA,
    }
    _write_object(args.output, bundle)
    print(
        json.dumps(
            {
                "output": str(Path(args.output)),
                "receipt_count": len(receipts),
                "record_count": len(records),
                "schema": "vsdp-board-bundle-created/1",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _cmd_bundle_decision(args: argparse.Namespace) -> int:
    setup_manifest = _load_object(args.setup_manifest, "Setup Manifest")
    board_bundle = _load_object(args.board_bundle, "Board bundle")
    authorization = _load_object(args.authorization, "AggregateAuthorization")
    tally_result = _load_object(args.tally_result, "tally result")
    final_manifest = board_bundle.get("final_manifest")
    checkpoint = board_bundle.get("checkpoint")
    if not isinstance(final_manifest, dict) or not isinstance(checkpoint, dict):
        raise VsdpError(NON_CANONICAL, "Board bundle is missing its Final Manifest or checkpoint")
    checkpoint_hash = checkpoint.get("checkpoint_hash")
    provisional = build_provisional_decision_proof(
        setup_manifest=setup_manifest,
        final_manifest=final_manifest,
        checkpoint_hash=checkpoint_hash,
        authorization=authorization,
        tally_result=tally_result,
    )
    bundle = {
        "aggregate_authorization": authorization,
        "board_bundle": board_bundle,
        "provisional_decision_proof": provisional,
        "schema": DECISION_BUNDLE_SCHEMA,
        "setup_manifest": setup_manifest,
        "tally_result": tally_result,
    }
    _write_object(args.output, bundle)
    print(
        json.dumps(
            {
                "decision_id": provisional["decision_id"],
                "output": str(Path(args.output)),
                "proof_hash": provisional["proof_hash"],
                "schema": "vsdp-decision-bundle-created/1",
                "status": "provisional",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    if not args.proof_verifier_command:
        raise VsdpError(
            "VSDP_BALLOT_PROOF_INVALID",
            "--proof-verifier-command is required; verification never falls back to structural checks",
        )
    command = shlex.split(args.proof_verifier_command, posix=sys.platform != "win32")
    verifier = CommandProofVerifier(command, timeout_seconds=args.timeout)
    bundle = _load_object(args.bundle, "verification bundle")
    if bundle.get("schema") == DECISION_BUNDLE_SCHEMA:
        if not args.tally_verifier_command:
            raise VsdpError(
                "VSDP_TALLY_MISMATCH",
                "--tally-verifier-command is required for a Decision bundle",
            )
        tally_command = shlex.split(
            args.tally_verifier_command,
            posix=sys.platform != "win32",
        )
        tally_verifier = CommandTallyVerifier(
            tally_command,
            timeout_seconds=args.timeout,
        )
        result = verify_decision_bundle_file(
            args.bundle,
            proof_verifier=verifier,
            tally_verifier=tally_verifier,
        )
    elif bundle.get("schema") == BOARD_BUNDLE_SCHEMA:
        result = verify_board_bundle_file(args.bundle, proof_verifier=verifier)
    else:
        raise VsdpError(NON_CANONICAL, "unsupported verification bundle schema")
    print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
    return 0


def _cmd_mirror_register(args: argparse.Namespace) -> int:
    setup = _load_object(args.setup_manifest, "Setup Manifest")
    final = _load_object(args.final_manifest, "Final Manifest")
    validate_final_manifest(final, setup_manifest=setup)
    payload = {
        "ceremony_id": ceremony_id(setup),
        "decision_id": decision_id(final),
        "final_manifest_hash": sha256_hex(canonical_json_bytes(final)),
        "profile_id": final["profile_id"],
        "setup_manifest_hash": sha256_hex(canonical_json_bytes(setup)),
        "status": "experimental",
    }
    client = RestClient(
        get_server(args.server),
        load_keys(args.data_dir),
        timeout=args.timeout,
    )
    response = client.json(
        "POST",
        "/api/v1/decisions",
        payload,
        expected={200, 201},
    )
    print(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


def _cmd_mirror_publish(args: argparse.Namespace) -> int:
    bundle = _load_object(args.bundle, "Decision bundle")
    if bundle.get("schema") != DECISION_BUNDLE_SCHEMA:
        raise VsdpError(NON_CANONICAL, "only a complete Decision bundle can be mirrored")
    setup = bundle.get("setup_manifest")
    board_bundle = bundle.get("board_bundle")
    if not isinstance(setup, dict) or not isinstance(board_bundle, dict):
        raise VsdpError(NON_CANONICAL, "Decision bundle is missing its Manifests")
    final = board_bundle.get("final_manifest")
    if not isinstance(final, dict):
        raise VsdpError(NON_CANONICAL, "Decision bundle is missing its Final Manifest")
    validate_final_manifest(final, setup_manifest=setup)
    frozen_decision_id = decision_id(final)
    artifact = make_public_artifact(
        kind="decision_bundle",
        body=bundle,
        decision_id=frozen_decision_id,
    )
    content_json = canonical_json_bytes(artifact).decode("utf-8")
    payload = {
        "artifact_id": artifact["artifact_id"],
        "ceremony_id": ceremony_id(setup),
        "content_hash": sha256_hex(content_json.encode("utf-8")),
        "content_json": content_json,
        "kind": "decision_bundle",
        "schema_version": bundle["schema"],
    }
    path = f"/api/v1/decisions/{frozen_decision_id}/artifacts"
    client = RestClient(
        get_server(args.server),
        load_keys(args.data_dir),
        timeout=args.timeout,
    )
    response = client.json("POST", path, payload, expected={200, 201})
    print(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


def _cmd_mirror_get(args: argparse.Namespace) -> int:
    base = get_server(args.server).rstrip("/")
    frozen_decision_id = require_hex(args.decision_id, 64, "decision_id")
    path = f"/api/v1/decisions/{frozen_decision_id}"
    if args.artifact_id:
        artifact_id = require_hex(args.artifact_id, 64, "artifact_id")
        path += f"/artifacts/{artifact_id}"
    try:
        response = httpx.get(
            f"{base}{path}",
            timeout=args.timeout,
            trust_env=False,
        )
    except httpx.HTTPError as exc:
        raise VsdpError("VSDP_MIRROR_UNAVAILABLE", str(exc)) from exc
    if response.status_code >= 400:
        raise VsdpError(
            f"HTTP_{response.status_code}",
            "experimental mirror rejected the read",
        )
    print(json.dumps(response.json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


def run(args: argparse.Namespace) -> int:
    _notice()
    command = (args.decision_cmd, getattr(args, "decision_subcmd", None))
    handlers = {
        ("manifest", "setup"): _cmd_manifest_setup,
        ("manifest", "final"): _cmd_manifest_final,
        ("witness", "keygen"): _cmd_witness_keygen,
        ("witness", "serve"): _cmd_witness_serve,
        ("board", "post"): _cmd_board_post,
        ("board", "seal"): _cmd_board_seal,
        ("bundle", "build"): _cmd_bundle_build,
        ("bundle", "decision"): _cmd_bundle_decision,
        ("mirror", "register"): _cmd_mirror_register,
        ("mirror", "publish"): _cmd_mirror_publish,
        ("mirror", "get"): _cmd_mirror_get,
        ("verify", None): _cmd_verify,
    }
    try:
        handler = handlers[command]
    except KeyError as exc:
        raise RuntimeError(f"unsupported decision command: {command}") from exc
    try:
        return handler(args)
    except VsdpError as exc:
        print(
            json.dumps(
                {
                    "code": exc.code,
                    "message": exc.message,
                    "schema": "vsdp-error/1",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
