from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aigenora.engine.p2p import connect_by_ticket
from aigenora.proto.engine import parse_options, run_guest_async
from aigenora.proto.spec_version import check_spec_version


def run(args) -> int:
    protocol_dir = Path(args.protocol_dir)
    spec = json.loads((protocol_dir / "spec.json").read_text(encoding="utf-8"))
    check_spec_version(spec, reject_unknown=True)
    if not args.iroh_ticket:
        raise RuntimeError("--iroh-ticket is required for direct network guest mode")
    return asyncio.run(_network_guest(args))


async def _network_guest(args) -> int:
    options = parse_options(args.options)
    _, node, channel = await connect_by_ticket(args.iroh_ticket)
    try:
        await run_guest_async(Path(args.protocol_dir), channel, options=options, args=args.extra_args)
        print("done")
        return 0
    finally:
        await node.node().shutdown()
