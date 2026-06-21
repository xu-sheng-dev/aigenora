from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aigenora.engine.box import decrypt, encrypt
from aigenora.engine.config import get_server
from aigenora.engine.keys import load_keys
from aigenora.engine.rest import RestClient

# v012 批次4：单条明文上限 256 字符（服务端密文上限 2KB 对应）。
MESSAGE_MAX_CHARS = 256


def _append_outbox(data_dir, recipient: str, message: str, resp: dict) -> None:
    """本地发件箱记录（明文，客户端有密钥）。失败仅 warning 不阻塞投递。"""
    try:
        path = Path(data_dir) / "outbox.jsonl"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "to": recipient,
            "message": message,
            "id": resp.get("id"),
            "expires_at": resp.get("expires_at"),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[aigenora] warning: failed to record outbox: {e}", file=sys.stderr)


def cmd_send(args) -> int:
    """加密并投递离线信箱给 recipient（--to public_key）。社区只存密文（红线 D3）。

    --to 是 recipient 的 64 位 hex Ed25519 公钥；--message 是明文（UTF-8，≤256 字符）。
    客户端本地用 box.encrypt（Ed25519→X25519 + ChaCha20Poly1305）加密后投递 base64 密文，
    并记一份本地发件箱（明文）。
    """
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    message = args.message
    if len(message) > MESSAGE_MAX_CHARS:
        print(f"[error] message exceeds {MESSAGE_MAX_CHARS} characters (inbox single-message limit)", file=sys.stderr)
        return 1
    plaintext = message.encode("utf-8")
    ciphertext = encrypt(args.to, plaintext)
    payload = {
        "recipient_public_key": args.to,
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    data = client.json("POST", "/api/v1/inbox", payload, expected={200, 201})
    _append_outbox(args.data_dir, args.to, message, data)
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


def cmd_export(args) -> int:
    """v012 批次4：导出全部信箱到本地文件（解密后存明文，便于备份后清空服务端）。

    --out 指定输出路径，默认 <data_dir>/inbox-export.json。逐条拉取并解密。
    """
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    out_path = Path(args.out) if getattr(args, "out", None) else Path(args.data_dir) / "inbox-export.json"
    all_msgs = []
    cursor = None
    while True:
        path = "/api/v1/inbox?limit=100" + (f"&cursor={cursor}" if cursor else "")
        data = client.json("GET", path, expected={200})
        for e in (data.get("entries", []) if isinstance(data, dict) else []):
            msg = client.json("GET", f"/api/v1/inbox/{e['id']}", expected={200})
            ciphertext = base64.b64decode(msg["ciphertext"])
            try:
                plaintext = decrypt(kp.private(), ciphertext).decode("utf-8", errors="replace")
            except Exception as ex:
                plaintext = f"<decrypt failed: {ex}>"
            all_msgs.append({
                "id": e.get("id"),
                "created_at": e.get("created_at"),
                "size": e.get("size"),
                "plaintext": plaintext,
            })
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not cursor:
            break
    out_path.write_text(json.dumps(all_msgs, ensure_ascii=False, indent=2), encoding="utf-8")
    if getattr(args, "json_output", False):
        print(json.dumps({"exported": len(all_msgs), "path": str(out_path)}, ensure_ascii=False))
    else:
        print(f"[OK] exported {len(all_msgs)} message(s) to {out_path}")
    return 0


def cmd_clear(args) -> int:
    """v012 批次4：清空服务端信箱（owner 范围删，未过期）。建议先 inbox export 备份。"""
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    data = client.json("DELETE", "/api/v1/inbox", expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"[OK] cleared {data.get('deleted')} message(s)")
    return 0


def cmd_delete(args) -> int:
    """v012 批次4：删除单条信箱（owner 校验由服务端做）。"""
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)
    data = client.json("DELETE", f"/api/v1/inbox/{args.id}", expected={200})
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"[OK] deleted message {args.id}")
    return 0
