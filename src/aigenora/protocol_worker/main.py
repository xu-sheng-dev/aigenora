from __future__ import annotations

import argparse
from pathlib import Path

from aigenora.runtime.catalog.loader import load_pinned_catalog
from aigenora.runtime.server import RuntimeServer

from .runner import ProtocolWorkerService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aigenora-protocol-worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--state-root", required=True)
    serve.add_argument("--max-frame-bytes", type=int, default=1_048_576)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_root = Path(args.state_root).expanduser()
    if not state_root.is_absolute():
        build_parser().error("--state-root must be absolute")
    catalog = load_pinned_catalog()
    worker = ProtocolWorkerService(catalog, state_root.resolve())
    return RuntimeServer(
        context=None,
        catalog=catalog,
        process_role="protocol_worker",
        max_frame_bytes=args.max_frame_bytes,
        worker_handlers=worker.handlers(),
    ).serve()


if __name__ == "__main__":
    raise SystemExit(main())
