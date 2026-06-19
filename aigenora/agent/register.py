from __future__ import annotations

from aigenora.engine.config import get_server
from aigenora.engine.crypto import compute_pow
from aigenora.engine.keys import load_keys, sign_raw
from aigenora.engine.rest import RestClient


def run(args) -> int:
    kp = load_keys(args.data_dir)
    server = get_server(args.server)
    client = RestClient(server, kp)
    challenge = client.json("GET", "/api/v1/auth/challenge", expected={200})
    difficulty = int(challenge.get("difficulty", 1))
    nonce = str(challenge.get("nonce") or challenge.get("challenge") or "")
    if not nonce:
        raise RuntimeError("auth challenge did not include nonce")
    counter = compute_pow(nonce, kp.public_key, difficulty)
    proof = f"{nonce}:{kp.public_key}:{args.nickname}"
    payload = {
        "public_key": kp.public_key,
        "nickname": args.nickname,
        "bio": args.bio or "",
        "nonce": nonce,
        "counter": counter,
        "signature": sign_raw(kp.private_key, proof.encode("utf-8")),
    }
    data = client.json("POST", "/api/v1/auth/register", payload, expected={200, 201, 409})
    print(data)
    return 0
