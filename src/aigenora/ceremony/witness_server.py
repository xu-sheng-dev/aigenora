from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import shlex
import sys
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .board import (
    CommandProofVerifier,
    RejectingProofVerifier,
    WitnessStore,
    generate_witness_key,
    load_witness_key,
)
from .canonical import canonical_json_bytes, parse_canonical_json, require_hex
from .errors import NON_CANONICAL, VsdpError
from .manifest import decision_id, validate_final_manifest


MAX_REQUEST_BYTES = 4 * 1024 * 1024


class WitnessHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: WitnessStore,
        control_token: str,
    ):
        super().__init__(address, WitnessRequestHandler)
        self.store = store
        self.control_token = control_token


class WitnessRequestHandler(BaseHTTPRequestHandler):
    server: WitnessHttpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Do not log source IP, payload hashes, nullifiers, or exact request paths.
        return

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        raw = canonical_json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise VsdpError(NON_CANONICAL, "Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise VsdpError(NON_CANONICAL, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise VsdpError(NON_CANONICAL, "invalid Content-Length") from exc
        if length < 1 or length > MAX_REQUEST_BYTES:
            raise VsdpError(NON_CANONICAL, "request body length is outside the limit")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise VsdpError(NON_CANONICAL, "truncated request body")
        value = parse_canonical_json(raw)
        if not isinstance(value, dict):
            raise VsdpError(NON_CANONICAL, "request body must be an object")
        return value

    def _require_control_authorization(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.control_token}"
        if hmac.compare_digest(supplied, expected):
            return True
        self.close_connection = True
        self._send_json(
            HTTPStatus.FORBIDDEN,
            {
                "code": "VSDP_CONTROL_UNAUTHORIZED",
                "message": "Witness control authorization is required",
                "schema": "vsdp-error/1",
            },
        )
        return False

    def _handle(self) -> None:
        path = urlsplit(self.path).path
        if self.command == "GET" and path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "decision_id": decision_id(self.server.store.final_manifest),
                    "public_key": self.server.store.key.public_key,
                    "schema": "vsdp-witness-health/1",
                    "status": "ready",
                    "witness_id": self.server.store.key.witness_id,
                },
            )
            return

        if self.command == "GET" and path.startswith("/v1/records/"):
            record_id = path.removeprefix("/v1/records/")
            record = self.server.store.load_record(record_id)
            self._send_json(
                HTTPStatus.OK,
                {"record": record, "schema": "vsdp-record-response/1"},
            )
            return

        if self.command == "POST" and path == "/v1/ballots":
            body = self._read_json()
            if set(body) != {"record", "schema"} or body["schema"] != "vsdp-ballot-submit/1":
                raise VsdpError(NON_CANONICAL, "invalid ballot submit envelope")
            record = body["record"]
            if not isinstance(record, dict):
                raise VsdpError(NON_CANONICAL, "record must be an object")
            acceptance = self.server.store.submit(canonical_json_bytes(record))
            self._send_json(
                HTTPStatus.OK,
                {"acceptance": acceptance, "schema": "vsdp-ballot-submit-response/1"},
            )
            return

        if self.command == "POST" and path == "/v1/receipts":
            body = self._read_json()
            allowed = {"receipt", "record", "schema"}
            if set(body) - allowed or body.get("schema") != "vsdp-receipt-gossip/1":
                raise VsdpError(NON_CANONICAL, "invalid receipt gossip envelope")
            receipt = body.get("receipt")
            if not isinstance(receipt, dict):
                raise VsdpError(NON_CANONICAL, "receipt must be an object")
            record = body.get("record")
            raw_record = None
            if record is not None:
                if not isinstance(record, dict):
                    raise VsdpError(NON_CANONICAL, "record must be an object")
                raw_record = canonical_json_bytes(record)
            receipt_id = self.server.store.store_receipt(receipt, raw_record=raw_record)
            self._send_json(
                HTTPStatus.OK,
                {
                    "receipt_id": receipt_id,
                    "schema": "vsdp-receipt-gossip-response/1",
                    "stored": True,
                },
            )
            return

        if self.command == "POST" and path == "/v1/checkpoints/sign":
            if not self._require_control_authorization():
                return
            body = self._read_json()
            expected = {
                "cutoff_statement_hash",
                "height",
                "previous_checkpoint_hash",
                "schema",
            }
            if set(body) != expected or body["schema"] != "vsdp-checkpoint-request/1":
                raise VsdpError(NON_CANONICAL, "invalid checkpoint request")
            self.server.store.assert_voting_closed()
            signed = self.server.store.checkpoint(
                cutoff_statement_hash=body["cutoff_statement_hash"],
                height=body["height"],
                previous_checkpoint_hash=body["previous_checkpoint_hash"],
            )
            self._send_json(
                HTTPStatus.OK,
                {"schema": "vsdp-checkpoint-response/1", "signed_checkpoint": signed},
            )
            return

        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"code": "NOT_FOUND", "message": "route not found", "schema": "vsdp-error/1"},
        )

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            self._handle()
        except VsdpError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"code": exc.code, "message": exc.message, "schema": "vsdp-error/1"},
            )
        except FileNotFoundError:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "code": "VSDP_ARTIFACT_NOT_FOUND",
                    "message": "requested artifact was not found",
                    "schema": "vsdp-error/1",
                },
            )
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "code": "VSDP_INTERNAL_ERROR",
                    "message": "internal Witness error",
                    "schema": "vsdp-error/1",
                },
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m aigenora.ceremony.witness_server")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--final-manifest", required=True)
    parser.add_argument("--key-file")
    parser.add_argument("--init-key", action="store_true")
    parser.add_argument("--force-key", action="store_true")
    parser.add_argument("--control-token-file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--proof-verifier-command",
        help="verify-only worker command; parsed without a shell",
    )
    return parser


def load_or_create_control_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return require_hex(path.read_text(encoding="ascii"), 64, "control token")
    token = secrets.token_hex(32)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".witness-control.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(token.encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return token


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise VsdpError(
            NON_CANONICAL,
            "the L1 Witness server may only bind to a loopback address",
        )
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_value = parse_canonical_json(Path(args.final_manifest).read_bytes())
    if not isinstance(manifest_value, dict):
        raise VsdpError(NON_CANONICAL, "Final Manifest must be an object")
    validate_final_manifest(manifest_value)

    key_path = Path(args.key_file) if args.key_file else state_dir / "witness-key.json"
    if args.init_key or not key_path.exists():
        key = generate_witness_key(key_path, force=args.force_key)
    else:
        key = load_witness_key(key_path)
    control_token_path = (
        Path(args.control_token_file)
        if args.control_token_file
        else state_dir / "control-token"
    )
    control_token = load_or_create_control_token(control_token_path)
    if args.proof_verifier_command:
        command = shlex.split(args.proof_verifier_command, posix=sys.platform != "win32")
        verifier = CommandProofVerifier(command)
    else:
        verifier = RejectingProofVerifier()
    store = WitnessStore(
        state_dir,
        final_manifest=manifest_value,
        key=key,
        proof_verifier=verifier,
    )
    server = WitnessHttpServer((args.host, args.port), store, control_token)
    host, port = server.server_address
    print(
        json.dumps(
            {
                "decision_id": decision_id(manifest_value),
                "control_auth_required": True,
                "host": host,
                "network_privacy": "linkable_by_first_hop",
                "port": port,
                "schema": "vsdp-witness-started/1",
                "witness_id": key.witness_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
