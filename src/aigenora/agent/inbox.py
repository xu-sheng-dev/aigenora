from __future__ import annotations

import base64
import json

from aigenora.engine.box import decrypt, encrypt
from aigenora.engine.config import get_server
from aigenora.engine.keys import load_keys
from aigenora.engine.rest import RestClient


def cmd_send(args) -> int:
    """加密并投递离线信箱给 recipient（--to public_key）。社区只存密文（红线 D3）。

    --to 是 recipient 的 64 位 hex Ed25519 公钥；--message 是明文（UTF-8）。
    客户端本地用 box.encrypt（Ed25519→X25519 + ChaCha20Poly1305）加密后投递 base64 密文。
    """
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    plaintext = args.message.encode("utf-8")
    ciphertext = encrypt(args.to, plaintext)
    payload = {
        "recipient_public_key": args.to,
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    data = client.json("POST", "/api/v1/inbox", payload, expected={200, 201})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"[OK] inbox delivered to {args.to[:16]}... (id={data.get('id')}, expires_at={data.get('expires_at')})")
    return 0


def cmd_list(args) -> int:
    """列出自己的信箱元数据（id/size/created_at/expires_at，不含密文）。"""
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    path = "/api/v1/inbox"
    params = []
    if getattr(args, "limit", None) is not None:
        params.append(f"limit={int(args.limit)}")
    if getattr(args, "cursor", None):
        params.append(f"cursor={args.cursor}")
    if params:
        path = path + "?" + "&".join(params)
    data = client.json("GET", path, expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        entries = data.get("entries", []) if isinstance(data, dict) else []
        next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
        print(f"inbox ({len(entries)} message(s)):")
        for e in entries:
            print(f"  id={e.get('id')}  size={e.get('size')}  created_at={e.get('created_at')}  expires_at={e.get('expires_at')}")
        if next_cursor:
            print(f"\nnext_cursor: {next_cursor}")
    return 0


def cmd_read(args) -> int:
    """读取单条信箱并本地解密（owner 校验由服务端做）。密钥不匹配/被篡改时抛 InvalidTag。"""
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    data = client.json("GET", f"/api/v1/inbox/{args.id}", expected={200})
    ciphertext = base64.b64decode(data["ciphertext"])
    plaintext = decrypt(kp.private(), ciphertext)
    text = plaintext.decode("utf-8", errors="replace")
    if getattr(args, "json_output", False):
        print(json.dumps({"id": args.id, "plaintext": text}, ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0
