from __future__ import annotations

from aigenora.engine.config import get_server
from aigenora.engine.keys import load_keys
from aigenora.engine.rest import RestClient


def run(args) -> int:
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    client.json("DELETE", f"/api/v1/invitations/{args.post_id}", expected={204})
    print("[OK] invitation cancelled")
    return 0

