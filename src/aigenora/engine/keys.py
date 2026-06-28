from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .config import data_dir


@dataclass(frozen=True)
class KeyPair:
    public_key: str
    private_key: str

    def private(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.private_key))

    def public(self) -> Ed25519PublicKey:
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.public_key))


def key_path(dir_value: str | None = None) -> Path:
    return data_dir(dir_value) / "key.json"


def keygen(dir_value: str | None = None, force: bool = False) -> KeyPair:
    path = key_path(dir_value)
    if path.exists() and not force:
        return load_keys(dir_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    kp = KeyPair(public_key=pub_bytes.hex(), private_key=priv_bytes.hex())
    with path.open("w", encoding="utf-8") as f:
        json.dump({"public_key": kp.public_key, "private_key": kp.private_key}, f, separators=(",", ":"))
    # 收紧私钥文件权限：POSIX 上设为 0600 防止其他用户读取根信任私钥；Windows 上 chmod 是 noop，依赖文件系统 ACL。
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return kp


def load_keys(dir_value: str | None = None) -> KeyPair:
    path = key_path(dir_value)
    if not path.exists():
        raise FileNotFoundError(f"key.json not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    pub = data.get("public_key") or data.get("peer_id")
    priv = data.get("private_key")
    if not isinstance(pub, str) or len(pub) != 64:
        raise ValueError("key.json public_key must be 64 hex chars")
    if not isinstance(priv, str) or len(priv) != 64:
        raise ValueError("key.json private_key must be 64 hex chars")
    return KeyPair(public_key=pub.lower(), private_key=priv.lower())


def sign_raw(private_key_hex: str, payload: bytes) -> str:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex)).sign(payload).hex()


def verify_raw(public_key_hex: str, payload: bytes, signature_hex: str) -> None:
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(
        bytes.fromhex(signature_hex), payload
    )


def signed_request_headers(
    kp: KeyPair,
    body: bytes = b"",
    *,
    method: str = "POST",
    path: str = "/",
    timestamp: int | None = None,
    request_id: str | None = None,
) -> dict[str, str]:
    ts = str(timestamp or int(time.time()))
    rid = request_id or secrets.token_hex(16)
    signed = f"{ts}\n{method.upper()}\n{path}\n{rid}\n".encode("utf-8") + body
    signature = sign_raw(kp.private_key, signed)
    return {
        "X-Public-Key": kp.public_key,
        "X-Signature": signature,
        "X-Timestamp": ts,
        "X-Request-Id": rid,
        "Content-Type": "application/json",
    }
