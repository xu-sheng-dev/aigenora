from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# v010 M5 信箱端到端加密（红线 D3：社区只存密文不可解）。
#
# sealed box（ECIES 风格，发送方匿名）：复用 Agent 的 Ed25519 身份密钥做 X25519 协商，
# 不引入 PyNaCl（依赖已有 cryptography）。Ed25519→X25519 转换用标准算法：
#   - 私钥：Ed25519 seed → SHA-512[:32] + clamping（RFC 8032→Curve25519 私钥）
#   - 公钥：Ed25519 公钥 y 坐标 → (1+y)/(1-y) mod p（Edwards→Montgomery u）
#
# 密文格式：MAGIC(5) + ephemeral_x25519_pub(32) + nonce(12) + ciphertext(len+16 AEAD tag)。
# MAGIC 前缀版本化，便于未来升级（如换 KDF / 换 AEAD）。

MAGIC = b"AGBX1"
_KDF_INFO = b"aigenora-inbox-v1"
_P = 2 ** 255 - 19  # Curve25519 域


def _ed25519_sk_to_x25519(ed_priv: Ed25519PrivateKey) -> X25519PrivateKey:
    """Ed25519 私钥 → X25519 私钥：SHA-512(seed)[:32] + clamping。"""
    seed = ed_priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    h = hashes.Hash(hashes.SHA512())
    h.update(seed)
    digest = bytearray(h.finalize()[:32])
    digest[0] &= 248
    digest[31] &= 127
    digest[31] |= 64
    return X25519PrivateKey.from_private_bytes(bytes(digest))


def _ed25519_pk_to_x25519(ed_pub: Ed25519PublicKey) -> X25519PublicKey:
    """Ed25519 公钥 → X25519 公钥：(1+y)/(1-y) mod (2^255-19)。"""
    pub_bytes = ed_pub.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    y = int.from_bytes(pub_bytes, "little") & ((1 << 255) - 1)
    u = ((1 + y) * pow((1 - y) % _P, -1, _P)) % _P
    return X25519PublicKey.from_public_bytes(u.to_bytes(32, "little"))


def _derive_key(shared: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_KDF_INFO
    ).derive(shared)


def encrypt(recipient_ed25519_pubkey_hex: str, plaintext: bytes) -> bytes:
    """用 recipient 的 Ed25519 公钥加密（sealed box，发送方匿名）。

    生成临时 X25519 密钥对，与 recipient 的 X25519 公钥 ECDH 协商共享密钥，
    HKDF 派生后用 ChaCha20Poly1305 加密。返回 MAGIC+eph_pub+nonce+ct。
    """
    ed_pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(recipient_ed25519_pubkey_hex))
    recipient_x_pub = _ed25519_pk_to_x25519(ed_pub)

    eph_priv = X25519PrivateKey.generate()
    eph_pub_bytes = eph_priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    shared = eph_priv.exchange(recipient_x_pub)
    key = _derive_key(shared)
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, None)
    return MAGIC + eph_pub_bytes + nonce + ct


def decrypt(my_ed25519_priv: Ed25519PrivateKey, ciphertext: bytes) -> bytes:
    """用本人 Ed25519 私钥解密。密文被篡改或密钥不匹配时抛 cryptography InvalidTag。"""
    if len(ciphertext) < 5 + 32 + 12 + 16 or ciphertext[:5] != MAGIC:
        raise ValueError("invalid inbox ciphertext (bad magic or truncated)")
    eph_pub_bytes = ciphertext[5:37]
    nonce = ciphertext[37:49]
    ct = ciphertext[49:]
    eph_pub = X25519PublicKey.from_public_bytes(eph_pub_bytes)
    my_x_priv = _ed25519_sk_to_x25519(my_ed25519_priv)
    shared = my_x_priv.exchange(eph_pub)
    key = _derive_key(shared)
    return ChaCha20Poly1305(key).decrypt(nonce, ct, None)
