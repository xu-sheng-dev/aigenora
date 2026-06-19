from __future__ import annotations

from aigenora.engine.config import get_server
from aigenora.engine.keys import load_keys
from aigenora.engine.rest import RestClient


def feedback(args) -> int:
    kp = load_keys(args.data_dir)
    payload = {"session_id": args.session_id}
    if args.amount is not None:
        payload["amount"] = args.amount
    if args.currency:
        payload["currency"] = args.currency
    if args.description:
        payload["description"] = args.description
    print(RestClient(get_server(args.server), kp).json("POST", "/api/v1/feedback", payload, expected={200, 201}))
    return 0


def rating(args) -> int:
    kp = load_keys(args.data_dir)
    payload = {"session_id": args.session_id, "score": args.score}
    if args.comment:
        payload["comment"] = args.comment
    print(RestClient(get_server(args.server), kp).json("POST", "/api/v1/ratings", payload, expected={200, 201}))
    return 0


def ratings(args) -> int:
    kp = load_keys(args.data_dir)
    print(RestClient(get_server(args.server), kp).json("GET", f"/api/v1/agents/{args.agent_id}/ratings", expected={200}))
    return 0

