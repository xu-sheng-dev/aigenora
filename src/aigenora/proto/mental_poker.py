"""v016 M1: Mental Poker 审计纯模块 (DeckState + nullifier + Transcript + 赛后审计).

ADR §4.3 / §5 / §7 / §13.7. 纯逻辑, 不依赖 P2P channel / hooks, 可独立单测.
引擎集成 (_run_mental_poker_*) 在后续里程碑; 本模块只提供可组合的纯构件.

提供:
  - DeckState:        牌权转移记账 (只存 id-B, 不存牌面). 双方各持一份 (event-sourced).
  - validate_play:    出牌合法性纯函数 (nullifier + 牌面密钥验证). hooks 复用, 不 mutate state.
  - Transcript:       canonical length-prefixed 事件流 + sha256 root + Ed25519 双签.
  - 审计:             audit_full_deck / audit_sealed_payload (攻击 #15) / derive_id_map /
                      audit_witnesses (偷看非手牌检测).

红线:
  - 审计必须从 k_outer 解 blob 派生 id 映射, 绝不读 Receiver 披露的 map (ADR §4.3).
  - transcript 双方 (Host/Guest) 用同一份本模块代码编码, 故 Python↔Python 一致, 不涉及 Java.
  - 不支持崩溃恢复: 敏感材料不入盘 (本模块不落盘, 由引擎层遵守).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidTag

from aigenora.engine import aead_deck, keys, ot


# ── 牌权记账 ──

@dataclass
class DeckState:
    """牌权转移记账, 双方各持一份. 只存 id-B, 不存牌面 (保密)."""
    guest_hand: set[str] = field(default_factory=set)
    host_hand: set[str] = field(default_factory=set)
    stock: set[str] = field(default_factory=set)
    played: set[str] = field(default_factory=set)

    def deal_to(self, who: str, id_b: str) -> None:
        """stock → hand (发牌/摸牌, 触发一次 OT). id_b 必须当前在 stock."""
        if id_b not in self.stock:
            raise ValueError(f"deal_to: {id_b!r} not in stock")
        if who == "guest":
            target = self.guest_hand
        elif who == "host":
            target = self.host_hand
        else:
            raise ValueError(f"unknown owner: {who!r}")
        self.stock.discard(id_b)
        target.add(id_b)

    def play(self, who: str, id_b: str) -> bool:
        """hand → played. 校验 id_b ∈ who 手牌 且 ∉ played. 成功返回 True."""
        hand = self.hand_of(who)
        if id_b not in hand or id_b in self.played:
            return False
        hand.discard(id_b)
        self.played.add(id_b)
        return True

    def hand_of(self, who: str) -> set[str]:
        if who == "guest":
            return self.guest_hand
        if who == "host":
            return self.host_hand
        raise ValueError(f"unknown owner: {who!r}")


@dataclass
class ValidationResult:
    ok: bool
    reason: str | None = None


def validate_play(
    hand: set[str],
    played: set[str],
    id_b: str,
    declared_card: bytes,
    k_inner: bytes,
    k_outer: bytes,
    blob_b: bytes,
) -> ValidationResult:
    """出牌合法性纯函数 (ADR §5). hooks 调用判定, 不 mutate state.

    - nullifier: id_b ∈ hand 且 ∉ played.
    - 牌面真实: blob_b 用 k_outer 解 → blob_a; blob_a 用 k_inner 解 → plaintext;
      plaintext[:2] == declared_card (encode_card(rank,suit)).
    """
    if id_b not in hand:
        return ValidationResult(False, "card_not_in_hand")
    if id_b in played:
        return ValidationResult(False, "card_already_played")
    try:
        blob_a = aead_deck.open_outer(k_outer, blob_b)
        plaintext = aead_deck.open_inner(k_inner, blob_a)
    except InvalidTag:
        return ValidationResult(False, "invalid_keys_or_blob")
    if plaintext[: aead_deck.CARD_BYTES] != declared_card:
        return ValidationResult(False, "card_face_mismatch")
    return ValidationResult(True, None)


# ── Transcript (ADR §13.7, 决议 #3) ──

def _canonical_json(obj: Any) -> bytes:
    """确定性 JSON: sort_keys + 无空格 + 非 ASCII 直出. Host/Guest 同代码 → 一致."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _validate_payload(obj: Any) -> None:
    """codex 建议: 拒绝非 wire-safe 类型, 保证 canonical 编码确定性.

    json.dumps 对 float (NaN/Inf 行为不定)、bytes/set (不可序列化)、非 str key 行为异常,
    会破坏 Host/Guest 双方 root_hash 一致. append 时提前校验.
    """
    if obj is None or isinstance(obj, bool) or isinstance(obj, (int, str)):
        return
    if isinstance(obj, float):
        raise ValueError("transcript payload must not contain float (non-deterministic)")
    if isinstance(obj, (bytes, bytearray, set)):
        raise ValueError("transcript payload must not contain bytes/set (non-JSON)")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError("transcript payload dict keys must be str")
            _validate_payload(v)
        return
    if isinstance(obj, list):
        for v in obj:
            _validate_payload(v)
        return
    raise ValueError(f"transcript payload unsupported type: {type(obj).__name__}")


class Transcript:
    """canonical length-prefixed 事件流. 双方在 P2P terminal receipt 签同一 root hash.

    编码 (每事件): u64_be(seq) || u32_be(type_len) || type_utf8 || u32_be(payload_len) || canonical_json(payload)
    禁止 out-of-band OT: 任何无 seq 的事件不进 transcript (引擎层强制走 append).
    """

    def __init__(self) -> None:
        self._events: list[tuple[int, str, dict]] = []

    def append(self, event_type: str, payload: dict | None = None) -> int:
        """追加事件, 返回其序号. payload 必须是 dict (None → 空 dict); 经 _validate_payload 校验.

        codex round2 fix: 用 'payload is None' 而非 'payload or {}', 避免把 []/0/False/''
        静默转成 {} 绕过类型校验; 顶层强制 dict.
        """
        p = {} if payload is None else payload
        if not isinstance(p, dict):
            raise ValueError("transcript payload must be a dict")
        _validate_payload(p)
        seq = len(self._events)
        self._events.append((seq, event_type, p))
        return seq

    @property
    def seq(self) -> int:
        return len(self._events)

    def root_hash_digest(self) -> bytes:
        h = hashlib.sha256()
        for seq, etype, payload in self._events:
            type_bytes = etype.encode("utf-8")
            payload_bytes = _canonical_json(payload)
            h.update(seq.to_bytes(8, "big"))
            h.update(len(type_bytes).to_bytes(4, "big"))
            h.update(type_bytes)
            h.update(len(payload_bytes).to_bytes(4, "big"))
            h.update(payload_bytes)
        return h.digest()

    def root_hash(self) -> str:
        return self.root_hash_digest().hex()

    def sign(self, kp: keys.KeyPair) -> str:
        """Ed25519 签 root hash (双签同一 hash)."""
        return keys.sign_raw(kp.private_key, self.root_hash_digest())

    def verify_signature(self, pub_hex: str, sig_hex: str) -> bool:
        try:
            keys.verify_raw(pub_hex, self.root_hash_digest(), sig_hex)
            return True
        except Exception:
            return False


# ── 赛后审计 (ADR §7.3 / §4.3) ──

@dataclass
class AuditResult:
    ok: bool
    reason: str | None = None
    evidence: dict | None = None


class AuditError(Exception):
    """审计中数据不可用/不一致 (如无法派生 id 映射)."""


def audit_full_deck(cards: list[bytes], expected_cards) -> AuditResult:
    """强制 full-deck opening 审计 (ADR §7.3 / codex 漏洞 #5 + 复核 high fix).

    codex high fix: 不仅查数量/去重, 必须验证 set(cards) == expected_cards (目标牌集
    universe). 否则恶意方可提交 N 个互异但非标准牌集的 bytes (如缺黑桃 A、混入非法
    (255,255)) 骗过审计。初始偏置 (加密重复/缺失/非法牌) 在此暴露.
    """
    expected_set = set(expected_cards)
    if len(cards) != len(expected_set):
        return AuditResult(False, "deck_size_mismatch",
                           {"actual": len(cards), "expected": len(expected_set)})
    if set(cards) != expected_set:
        return AuditResult(False, "deck_universe_mismatch",
                           {"missing": sorted(expected_set - set(cards)),
                            "extra": sorted(set(cards) - expected_set)})
    return AuditResult(True)


def audit_sealed_payload(
    tokens: dict[str, int],
    sealed_table: dict[str, str],
    expected_payloads: dict[str, bytes],
    pub: ot.Rsapub,
    protocol_id: str,
    session_id: str,
    sender_role: int,
    payload_type: int,
) -> AuditResult:
    """sealed_payload 审计链 (攻击 #15, ADR §3.2 / §7.3 / §12 #11)。

    对每个 label: verify_token (公钥钉死 token 真伪) → open_sealed (派生 key 解 sealed) →
    比对 expected_payload (公开的 k_H). 任一步失败 → 定位违规 label.

    codex high fix: 强制 keyset 完全一致 (tokens == sealed == expected_payloads), 否则
    恶意 Sender 可只公开未投毒 label 的 opening 隐瞒投毒 label. expected 缺失也判失败
    (不跳过). 捕获 InvalidTag / ValueError (畸形 hex/短密文), 全部转为审计失败而非异常逃逸.
    """
    if set(tokens) != set(sealed_table) or set(tokens) != set(expected_payloads):
        return AuditResult(
            False, "keyset_mismatch",
            {"tokens": sorted(set(tokens)),
             "sealed": sorted(set(sealed_table)),
             "expected": sorted(set(expected_payloads))},
        )
    for label, token in tokens.items():
        if not ot.verify_token(label, token, pub, protocol_id, session_id, sender_role, payload_type):
            return AuditResult(False, "forged_token", {"label": label})
        try:
            payload = ot.open_sealed(
                label, token, sealed_table[label], pub, protocol_id, session_id, sender_role, payload_type
            )
        except (InvalidTag, ValueError, OverflowError):
            # token 真但 sealed 解不开 (投毒 / 畸形 / token 越界致 I2OSP 失败) → 攻击 #15
            return AuditResult(False, "sealed_payload_poisoned", {"label": label})
        if payload != expected_payloads[label]:   # keyset 已保证存在
            return AuditResult(False, "payload_mismatch", {"label": label})
    return AuditResult(True)


def derive_id_map(
    k_outer_by_idb: dict[str, bytes],
    blob_b_by_idb: dict[str, bytes],
    blob_a_by_ida: dict[str, bytes],
) -> dict[str, str]:
    """数据派生 id-B↔id-A 映射 (ADR §4.3, codex 第 2 轮: 不信任 Receiver 披露).

    用 Receiver 公开的全量 k_outer 解每个 blob_B → blob_A, 匹配 Host 初始 transcript 的
    (id-A, blob_A) 表, 派生 id_b → id_a. 任一 blob_B 解不开或 blob_A 不在 transcript → AuditError.

    codex medium fix: 强制 bijection + surjective — Host transcript blob_A 无重复, 每个
    id_a 最多被一个 id_b 使用, 且所有 id_a 都被覆盖. 否则 Receiver 可用重复/子集 blob 误导.
    """
    blob_a_to_ida: dict[bytes, str] = {}
    for id_a, blob_a in blob_a_by_ida.items():
        if blob_a in blob_a_to_ida:
            raise AuditError(f"duplicate blob_A in Host transcript (id_a {id_a!r})")
        blob_a_to_ida[blob_a] = id_a
    result: dict[str, str] = {}
    used_ida: set[str] = set()
    for id_b, blob_b in blob_b_by_idb.items():
        k = k_outer_by_idb.get(id_b)
        if k is None:
            raise AuditError(f"missing k_outer for id_b {id_b!r}")
        try:
            blob_a = aead_deck.open_outer(k, blob_b)
        except InvalidTag:
            raise AuditError(f"cannot open blob_B for id_b {id_b!r}")
        id_a = blob_a_to_ida.get(blob_a)
        if id_a is None:
            raise AuditError(f"blob_A for id_b {id_b!r} not in Host transcript")
        if id_a in used_ida:
            raise AuditError(f"many-to-one: id_a {id_a!r} claimed by multiple id_b")
        used_ida.add(id_a)
        result[id_b] = id_a
    if used_ida != set(blob_a_by_ida):
        raise AuditError(
            f"id map not surjective: uncovered id_a {sorted(set(blob_a_by_ida) - used_ida)}")
    return result


@dataclass
class Witness:
    """Receiver 赛后公开的单次 OT witness (ADR §4.3 / §13.4).

    codex 复核 critical fix: 不含 z — z 必须取自 transcript (公共记录), 不能由 Receiver
    自带. 否则 Receiver 可对偷看的 OT 重新选 r 构造合法 (label, r, z) 骗过审计.
    """
    ot_id: str
    label: str
    r: int


def audit_witnesses(
    witnesses: list[Witness],
    transcript_ot_pairs: list[tuple[str, int]],
    pub: ot.Rsapub,
    protocol_id: str,
    session_id: str,
    sender_role: int,
    payload_type: int,
    label_to_idb: dict[str, str],
    hand_at_ot: dict[str, set[str]],
) -> AuditResult:
    """钉死 Receiver 每次 OT 的真实选择 (ADR §4.3, codex 漏洞 #1 + round1/round2 fix).

    codex round2 fix: transcript_ot_pairs 是 transcript 内所有 OT 事件的 (ot_id, z) 有序列表;
    审计独立验证 transcript ot_id 无重复 (dict 会静默丢弃重复 ot_id, 隐藏"同 ot_id 先偷看
    再合法"攻击). witness 的 ot_id 集合必须与 transcript ot_id 完全一致 (exact set match).
    z 始终取自 transcript, 不信 witness 自带.
    """
    t_ids = [ot_id for ot_id, _ in transcript_ot_pairs]
    if len(t_ids) != len(set(t_ids)):
        return AuditResult(False, "duplicate_transcript_ot_id")
    transcript_z = dict(transcript_ot_pairs)
    w_ids = [w.ot_id for w in witnesses]
    if len(w_ids) != len(set(w_ids)):
        return AuditResult(False, "duplicate_witness_ot_id")
    if set(w_ids) != set(t_ids):
        return AuditResult(
            False, "witness_transcript_mismatch",
            {"missing": sorted(set(t_ids) - set(w_ids)),
             "extra": sorted(set(w_ids) - set(t_ids))},
        )
    for w in witnesses:
        z = transcript_z[w.ot_id]   # z 来自 transcript, 不信 witness
        if not ot.verify_witness(z, w.label, w.r, pub, protocol_id, session_id, sender_role, payload_type):
            return AuditResult(False, "bad_witness", {"ot_id": w.ot_id})
        id_b = label_to_idb.get(w.label)
        if id_b is None:
            return AuditResult(False, "label_not_in_map", {"label": w.label, "ot_id": w.ot_id})
        if id_b not in hand_at_ot.get(w.ot_id, set()):
            return AuditResult(
                False, "peeked_non_hand",
                {"ot_id": w.ot_id, "id_b": id_b, "label": w.label},
            )
    return AuditResult(True)
