"""v016 M1: layered AEAD deck primitives (Mental Poker).

分层 AEAD 加密牌堆原语 (ADR §3.1 / §4.1 / §4.4). 纯函数, 无副作用, 不依赖 P2P / hooks,
可独立单测. 复用 box.py 既有的 ChaCha20Poly1305, 不引入新依赖.

构造 (双方各做一层 AEAD 加密 + 打乱 → 双方都无法单独解密的双层密文牌堆):
    内层 (Host):   blob_A = nonce(12) || AEAD_Enc(k_H, nonce, p)
                   p = card(2B) + pad(14B) = 16B 定长 → blob_A = 44B
    外层 (Guest):  blob_B = nonce(12) || AEAD_Enc(k_G, nonce, blob_A) → blob_B = 72B

不变量 (防水印, ADR §4.4): 所有 blob_A 等长 44B, 所有 blob_B 等长 72B; 明文定长 16B;
nonce 在内层, 被外层加密覆盖, Host 无法用 nonce 给好牌做标记.

card 编码: encode_card(rank_index, suit_index) → 定长 2B 整数编码. 原语层不耦合扑克枚举
语义, 协议层 (M2 hooks) 负责 rank/suit 字符串 ↔ index 映射.
"""
from __future__ import annotations

import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

# 定长常量 (ADR §4.0 / §4.4). 改这些会破坏 blob 等长不变量与 watermarking 防御.
CARD_BYTES = 2
PAD_LEN = 14
INNER_PLAINTEXT_LEN = CARD_BYTES + PAD_LEN          # 16
NONCE_LEN = 12
TAG_LEN = 16
BLOB_A_LEN = NONCE_LEN + INNER_PLAINTEXT_LEN + TAG_LEN   # 44 (inner ciphertext)
BLOB_B_LEN = NONCE_LEN + BLOB_A_LEN + TAG_LEN            # 72 (outer ciphertext)

KEY_LEN = 32


def encode_card(rank_index: int, suit_index: int) -> bytes:
    """Fixed 2-byte card encoding. Indices must fit one byte each (0-255)."""
    if not (0 <= rank_index <= 255 and 0 <= suit_index <= 255):
        raise ValueError("card indices must fit one byte each")
    return bytes([rank_index, suit_index])


def decode_card(b: bytes) -> tuple[int, int]:
    """Inverse of encode_card."""
    if len(b) != CARD_BYTES:
        raise ValueError(f"card encoding must be {CARD_BYTES} bytes, got {len(b)}")
    return (b[0], b[1])


def pack_inner(card: bytes, pad: bytes | None = None) -> bytes:
    """Inner plaintext = card(2B) + pad(14B) = fixed 16B. pad defaults to random."""
    if len(card) != CARD_BYTES:
        raise ValueError(f"card must be {CARD_BYTES} bytes, got {len(card)}")
    if pad is None:
        pad = secrets.token_bytes(PAD_LEN)
    if len(pad) != PAD_LEN:
        raise ValueError(f"pad must be {PAD_LEN} bytes, got {len(pad)}")
    return card + pad


def random_key() -> bytes:
    """32-byte symmetric key (one per card, ADR §3.1)."""
    return secrets.token_bytes(KEY_LEN)


def seal_inner(k: bytes, plaintext: bytes) -> bytes:
    """Inner layer: nonce(12) || AEAD_Enc(k, nonce, plaintext). No AAD."""
    _check_key(k)
    nonce = secrets.token_bytes(NONCE_LEN)
    ct = ChaCha20Poly1305(k).encrypt(nonce, plaintext, None)
    return nonce + ct


def seal_outer(k: bytes, inner_blob: bytes) -> bytes:
    """Outer layer: re-encrypt the whole inner_blob (including its nonce). No AAD."""
    _check_key(k)
    nonce = secrets.token_bytes(NONCE_LEN)
    ct = ChaCha20Poly1305(k).encrypt(nonce, inner_blob, None)
    return nonce + ct


def open_inner(k: bytes, blob: bytes) -> bytes:
    """Decrypt inner layer. Raises InvalidTag on tamper / wrong key."""
    _check_key(k)
    if len(blob) < NONCE_LEN + TAG_LEN:
        raise ValueError("blob too short to contain nonce + tag")
    nonce = blob[:NONCE_LEN]
    ct = blob[NONCE_LEN:]
    return ChaCha20Poly1305(k).decrypt(nonce, ct, None)


def open_outer(k: bytes, blob: bytes) -> bytes:
    """Decrypt outer layer (same wire shape as inner). Raises InvalidTag on tamper."""
    return open_inner(k, blob)


def brute_force_open(blob: bytes, candidates) -> tuple[bytes, bytes] | None:
    """Exhaustive decryption: try each candidate key, return (key, plaintext) on the
    first whose AEAD tag verifies, else None.

    Used by the post-game audit (ADR §4.3): derive the id-B↔id-A map by decrypting
    every blob_B with the disclosed k_G table and matching against Host's transcript
    blob_A — without trusting Receiver-disclosed mappings. AEAD tag is 16 bytes;
    collision probability is negligible.
    """
    if len(blob) < NONCE_LEN + TAG_LEN:
        return None
    nonce = blob[:NONCE_LEN]
    ct = blob[NONCE_LEN:]
    for k in candidates:
        try:
            pt = ChaCha20Poly1305(k).decrypt(nonce, ct, None)
            return (k, pt)
        except InvalidTag:
            continue
    return None


def _check_key(k: bytes) -> None:
    if len(k) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes, got {len(k)}")
