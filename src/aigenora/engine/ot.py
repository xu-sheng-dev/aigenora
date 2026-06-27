"""v016 M1: Blind-RSA token Oblivious Transfer (Mental Poker 揭牌原语).

ADR §3.2 / §12 硬验收. 半诚实模型. 复用 cryptography 既有的 RSA + ChaCha20Poly1305,
不引入 PyNaCl / 不自研曲线数学.

机制 (Sender 持 n 个 payload, Receiver 私密取其中一个 label):
    0. 会话级: Sender 生成 RSA (e,d,N), 2048-bit, per-game, 不跨局 (决议 #4). 公布 (e,N).
    1. 建组 (一次性): 对每个 label:
         h_label   = FDH(ctx || label) ∈ Z_N*              # ctx 见 build_context (ADR §13.8)
         token     = h_label^d mod N                        # RSA root, 私存
         key_label = HKDF(I2OSP(token, 256) || ctx, 32)
         c_label   = AEAD(key_label, payload, aad=ctx)      # 公开 sealed table
       Sender 只公布 {label, c_label}, 绝不公布 token / payload.
    2. 每次 OT (fresh r/ot_id):
         Receiver: r ∈ Z_N*; z = h_label * r^e mod N        # 盲化请求
         Sender:   y = z^d mod N  (CRT + blinding, 1 次私钥运算)
         Receiver: token = y * r^-1 mod N; key = HKDF(token||ctx); AEAD_Dec(c_label) → payload

安全属性 (半诚实):
    - Sender 隐私: Receiver 每次 blind request 只得一个 token, 解不开其他 sealed payload.
    - Receiver 隐私: z 是 h 的盲化值, Sender 无法推出 label (前提: r ∈ Z_N* 全域).
    - 可审计选择: 赛后 Receiver 公开 (ot_id, label, r), verify_witness 验证 z == h_label * r^e
      钉死其真实 label (伪造需 RSA root).
    - sealed_payload 审计 (攻击 #15): 赛后 Sender 公开 token 列表, verify_token 公钥验证
      token^e ≡ FDH, open_sealed 派生 key 解 sealed 比对 k_H — Sender 投毒在此暴露.

实现红线 (ADR §12 #9): RSA raw 模幂必须从 private_numbers() 提取 (d,p,q,dmp1,dmq1,iqmp)
手写 CRT + Blinding. 禁止用 cryptography.RSAPrivateKey.decrypt() (强制 OAEP/PKCS1 padding).
"""
from __future__ import annotations

import hashlib
import math
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    RSAPublicNumbers,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# KDF / context 常量 (ADR §13.8)
_CTX_MAGIC = b"aigenora.mp.v1"
_HKDF_INFO = b"aigenora.mp.tokenkey.v1"
_AEAD_NONCE_LEN = 12

# 角色码 (ADR §13.8 sender_role)
ROLE_HOST = 0x01
ROLE_GUEST = 0x02
# payload 类型码 (ADR §13.8 payload_type)
PAYLOAD_K_INNER = 0x01   # 内层密钥 k_H (Host 角度的 inner key)
PAYLOAD_K_OUTER = 0x02   # 外层密钥 k_G (Guest 角度的 outer key)


@dataclass(frozen=True)
class Rsapub:
    """RSA 公钥的整数表示 (用于 OT 运算, 不依赖 cryptography 对象)."""
    e: int
    n: int


# ── context 构建 (ADR §13.8) ──

def build_context(
    protocol_id: str,
    session_id: str,
    sender_role: int,
    payload_type: int,
    label: str,
) -> bytes:
    """KDF/FDH 绑定上下文. label 进 context, 故 fdh(ctx) 隐含 label."""
    pid = protocol_id.encode("utf-8")
    sid = session_id.encode("utf-8")
    lbl = label.encode("utf-8")
    return (
        _CTX_MAGIC
        + len(pid).to_bytes(4, "big") + pid
        + len(sid).to_bytes(4, "big") + sid
        + bytes([sender_role])
        + bytes([payload_type])
        + len(lbl).to_bytes(4, "big") + lbl
    )


# ── RSA 会话密钥 ──

def gen_session_rsa() -> RSAPrivateKey:
    """会话级 RSA-2048, e=65537 (决议 #4: 整局复用, 禁跨局)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def rsa_pub_encode(pub: Rsapub) -> dict:
    """ADR §13.1 wire 编码: {"e":e, "n":<hex 512 chars>}."""
    n_hex = format(pub.n, "x").zfill(512)
    return {"e": pub.e, "n": n_hex}


def rsa_pub_decode(d: dict) -> Rsapub:
    """ADR §13.1 反编码 + 语法良构校验 (codex medium fix).

    校验 e==65537 / 2048-bit / 奇数, 防 honest keygen 的意外畸形 (bit_length 错、偶数 N、错 e).

    安全边界 (codex round2, 诚实降级): 这只是语法检查, 不能证明 gcd(e, λ(N))==1 —
    恶意 Sender 仍可构造 N=pq 使 (p-1) 被 e 整除, 令 r^e 退入可检测子群从而反推 Receiver 选择.
    抗恶意 modulus 需 RSA well-formed proof (ZK), 属未来工作; 当前定位为 ADR §2.4 半诚实模型
    + honest keygen (gen_session_rsa 生成的 key 保证 gcd(e,φ(N))=1). 该边界与 ADR-8
    "作弊可检测 (除 abort + selective-failure)" 声明一致, 须在 SKILL/spec.description 诚实标注.
    """
    e = int(d["e"])
    n = int(d["n"], 16)
    if e != 65537:
        raise ValueError(f"RSA public exponent must be 65537, got {e}")
    if n.bit_length() != 2048:
        raise ValueError(f"RSA modulus must be 2048-bit, got {n.bit_length()}")
    if n <= 1 or n % 2 == 0:
        raise ValueError("RSA modulus must be an odd integer > 1")
    return Rsapub(e=e, n=n)


# ── 全域采样与 FDH ──

def rejection_sample_zn_star(n: int) -> int:
    """均匀采样 r ∈ Z_N* (1 ≤ r < n 且 gcd(r,n)=1). 禁 32B 小整数 (攻击面 #6)."""
    n_bits = n.bit_length()
    while True:
        r = secrets.randbits(n_bits)
        if r == 0 or r >= n:
            continue
        if math.gcd(r, n) != 1:
            continue
        return r


def _mgf1_sha256(seed: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:length]


def fdh_to_zn_star(context: bytes, n: int) -> int:
    """Full-Domain Hash → Z_N*. MGF1-SHA256 输出模长字节, 解释为大整数,
    rejection sample 至 [1,n) 且 gcd(h,n)=1. context 已含 label."""
    n_bytes = (n.bit_length() + 7) // 8
    counter = 0
    while True:
        digest = _mgf1_sha256(context + counter.to_bytes(4, "big"), n_bytes)
        h = int.from_bytes(digest, "big")
        counter += 1
        if 0 < h < n and math.gcd(h, n) == 1:
            return h


# ── RSA raw 模幂: CRT + Blinding (ADR §12 #9, 禁 decrypt API) ──

def rsa_crt_blinded_private_op(z: int, priv: rsa.RSAPrivateNumbers, pub: Rsapub) -> int:
    """y = z^d mod N, 用 CRT + RSA Blinding 防 timing/fault 侧信道 (攻击面 #13).

    1. Blinding: b ∈ Z_N*; z' = z * b^e mod N
    2. CRT:      y' = z'^d mod N  via (p, q, dmp1, dmq1, iqmp)
    3. Unblind:  y  = y' * b^-1 mod N   (== z^d mod N)
    """
    n = pub.n
    e = pub.e
    b = rejection_sample_zn_star(n)
    b_inv = pow(b, -1, n)
    z_blind = (z * pow(b, e, n)) % n
    # CRT
    m1 = pow(z_blind % priv.p, priv.dmp1, priv.p)
    m2 = pow(z_blind % priv.q, priv.dmq1, priv.q)
    h = ((m1 - m2) * priv.iqmp) % priv.p
    y_blind = (m2 + h * priv.q) % n
    return (y_blind * b_inv) % n


# ── token key 派生 ──

def _modulus_len(n: int) -> int:
    return (n.bit_length() + 7) // 8


def derive_token_key(token: int, context: bytes, n: int) -> bytes:
    """key_label = HKDF(I2OSP(token, modulus_len) || context, 32)."""
    ikm = token.to_bytes(_modulus_len(n), "big") + context
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO
    ).derive(ikm)


def _aead_seal(key: bytes, payload: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(_AEAD_NONCE_LEN)
    ct = ChaCha20Poly1305(key).encrypt(nonce, payload, aad)
    return nonce + ct


def _aead_open(key: bytes, sealed: bytes, aad: bytes) -> bytes:
    nonce = sealed[:_AEAD_NONCE_LEN]
    ct = sealed[_AEAD_NONCE_LEN:]
    return ChaCha20Poly1305(key).decrypt(nonce, ct, aad)


# ── OTSender / OTReceiver ──

class OTSender:
    """Sender 持 payloads_by_label + RSA 私钥. publish_table 发布 sealed (不发 token).
    respond 对任意盲化 z 做 1 次私钥运算, 不泄露 label."""

    def __init__(
        self,
        payloads_by_label: dict[str, bytes],
        rsa_priv: RSAPrivateKey,
        protocol_id: str,
        session_id: str,
        sender_role: int,
        payload_type: int,
    ):
        self.payloads = payloads_by_label
        self._key = rsa_priv
        self._priv = rsa_priv.private_numbers()
        self.pub = Rsapub(e=self._priv.public_numbers.e, n=self._priv.public_numbers.n)
        self.protocol_id = protocol_id
        self.session_id = session_id
        self.sender_role = sender_role
        self.payload_type = payload_type
        self._tokens: dict[str, int] = {}     # label → token, 私存 (终局才公开)
        self.sealed: dict[str, str] = {}      # label → hex(nonce||ct), 公开
        self._build()

    def _ctx(self, label: str) -> bytes:
        return build_context(
            self.protocol_id, self.session_id, self.sender_role, self.payload_type, label
        )

    def _build(self) -> None:
        for label, payload in self.payloads.items():
            ctx = self._ctx(label)
            h = fdh_to_zn_star(ctx, self.pub.n)
            token = rsa_crt_blinded_private_op(h, self._priv, self.pub)   # h^d
            self._tokens[label] = token
            key = derive_token_key(token, ctx, self.pub.n)
            self.sealed[label] = _aead_seal(key, payload, ctx).hex()

    def publish_table(self) -> dict:
        """公开材料: rsa_pub + sealed table (无 token 明文)."""
        return {"rsa_pub": rsa_pub_encode(self.pub), "sealed": dict(self.sealed)}

    def respond(self, z: int) -> int:
        """y = z^d mod N. Sender 无法推出 label (z 被全域盲化)."""
        return rsa_crt_blinded_private_op(z, self._priv, self.pub)

    def export_tokens(self) -> dict[str, int]:
        """终局公开 token 列表 (决议 #4 锁定 RSA 不跨局, 赛后公开无害). 用于 sealed_payload 审计."""
        return dict(self._tokens)


class OTReceiver:
    """Receiver 私密取一个 label 的 payload. request 发盲化 z; recover 用 y 还原 token 解 sealed."""

    def __init__(
        self,
        label: str,
        pub: Rsapub,
        protocol_id: str,
        session_id: str,
        sender_role: int,
        payload_type: int,
    ):
        self.label = label
        self.pub = pub
        self.protocol_id = protocol_id
        self.session_id = session_id
        self.sender_role = sender_role
        self.payload_type = payload_type
        self._r: int | None = None
        self._ctx = build_context(
            protocol_id, session_id, sender_role, payload_type, label
        )

    def request(self) -> tuple[int, int]:
        """返回 (z, r). z 发给 Sender; r 私存作赛后 witness. z = h_label * r^e mod N."""
        h = fdh_to_zn_star(self._ctx, self.pub.n)
        self._r = rejection_sample_zn_star(self.pub.n)
        z = (h * pow(self._r, self.pub.e, self.pub.n)) % self.pub.n
        return (z, self._r)

    def recover(self, y: int, sealed_table: dict) -> bytes:
        """token = y * r^-1; 派生 key; 解 sealed[label] → payload."""
        if self._r is None:
            raise RuntimeError("request() must be called before recover()")
        token = (y * pow(self._r, -1, self.pub.n)) % self.pub.n
        key = derive_token_key(token, self._ctx, self.pub.n)
        sealed = bytes.fromhex(sealed_table[self.label])
        return _aead_open(key, sealed, self._ctx)


# ── 赛后审计 ──

def verify_witness(
    z: int,
    label: str,
    r: int,
    pub: Rsapub,
    protocol_id: str,
    session_id: str,
    sender_role: int,
    payload_type: int,
) -> bool:
    """钉死 Receiver 真实选择: z == FDH(ctx||label) * r^e mod N. 伪造需 RSA root."""
    ctx = build_context(protocol_id, session_id, sender_role, payload_type, label)
    h = fdh_to_zn_star(ctx, pub.n)
    if not (1 <= r < pub.n) or math.gcd(r, pub.n) != 1:
        return False
    expected = (h * pow(r, pub.e, pub.n)) % pub.n
    return z == expected


def verify_token(
    label: str,
    token: int,
    pub: Rsapub,
    protocol_id: str,
    session_id: str,
    sender_role: int,
    payload_type: int,
) -> bool:
    """公钥验证 token 真伪: token^e ≡ FDH(ctx||label) mod N. 钉死 Sender 不能造假 token.

    codex round2 fix: 先校验 1 <= token < n. 否则 token >= n 时 pow 仍可能在模 N 下等于 h,
    但 derive_token_key 的 I2OSP(token, modulus_len) 会 OverflowError — 直接拒绝更干净.
    """
    if not (1 <= token < pub.n):
        return False
    ctx = build_context(protocol_id, session_id, sender_role, payload_type, label)
    h = fdh_to_zn_star(ctx, pub.n)
    return pow(token, pub.e, pub.n) == h


def open_sealed(
    label: str,
    token: int,
    sealed_hex: str,
    pub: Rsapub,
    protocol_id: str,
    session_id: str,
    sender_role: int,
    payload_type: int,
) -> bytes:
    """审计第 2 步: 用 token 派生 key 解 sealed[label] → payload. 调用方再断言 payload == 公开的 k_H."""
    ctx = build_context(protocol_id, session_id, sender_role, payload_type, label)
    key = derive_token_key(token, ctx, pub.n)
    sealed = bytes.fromhex(sealed_hex)
    return _aead_open(key, sealed, ctx)
