from __future__ import annotations

import argparse
from pathlib import Path

from aigenora.runtime.catalog.loader import load_pinned_catalog
from aigenora.services.context import ServiceContext

from .server import RuntimeServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aigenora-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--data-dir", required=True)
    serve.add_argument("--server")
    serve.add_argument("--max-frame-bytes", type=int, default=1_048_576)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "serve":
        raise RuntimeError("unsupported Runtime command")
    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.is_absolute():
        build_parser().error("--data-dir must be absolute")
    context = ServiceContext.create(data_dir.resolve(), args.server)
    catalog = load_pinned_catalog()
    return RuntimeServer(
        context=context,
        catalog=catalog,
        process_role="identity_sidecar",
        max_frame_bytes=args.max_frame_bytes,
    ).serve()


if __name__ == "__main__":
    raise SystemExit(main())
