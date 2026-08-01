"""Experimental verifiable hidden-role ceremony primitives.

This module is intentionally self-contained and exposes only deterministic,
offline-verifiable building blocks.  It is a local research RC, not an
externally audited cryptographic product.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


PROFILE_ID = "hidden-role-local-rc-v1"
TEAM_MESSAGE_PADDED_BYTES = 4096
ARTIFACT_SCHEMA = "aigenora-hidden-role-terminal/1"
PEER_STATE_SCHEMA = "aigenora-hidden-role-peer-private/1"
DESCRIPTOR_SCHEMA = "aigenora-hidden-role-peer-public/1"
DECK_SCHEMA = "aigenora-hidden-role-deck/1"
ONION_SCHEMA = "aigenora-hidden-role-onion/1"

# RFC 3526 group 14 (2048-bit MODP).  Generator 2 is in the prime-order
# subgroup for this safe prime.  Fixed-width hexadecimal is used on the wire.
_P_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E08"
    "8A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B"
    "302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9"
    "A637ED6B0BFF5CB6F406B7EDEE386BFB5A899FA5AE9F24117C4B1FE6"
    "49286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8"
    "FD24CF5F83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3BE39E772C"
    "180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AACAA68FFFFFFFFFFFFFFFF"
)
P = int(_P_HEX, 16)
Q = (P - 1) // 2
G = 2
GROUP_BYTES = (P.bit_length() + 7) // 8
SCALAR_BYTES = (Q.bit_length() + 7) // 8

ROLE_CODES = {
    "villager": 1,
    "seer": 2,
    "witch": 3,
    "wolf": 4,
}
CODE_ROLES = {value: key for key, value in ROLE_CODES.items()}
WEREWOLF_ROLES = (
    "villager",
    "villager",
    "villager",
    "seer",
    "witch",
    "wolf",
    "wolf",
)


class HiddenRoleError(RuntimeError):
    """A stable fail-closed hidden-role verification error."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise HiddenRoleError(code, message)


def canonical_json_bytes(value: Any) -> bytes:
    _validate_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_json(value: Any) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        raise HiddenRoleError("NON_CANONICAL", "floating-point values are forbidden")
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise HiddenRoleError("NON_CANONICAL", "object keys must be strings")
            _validate_json(item)
        return
    raise HiddenRoleError("NON_CANONICAL", f"unsupported JSON type: {type(value).__name__}")


def domain_hash(domain: str, value: Any) -> bytes:
    return hashlib.sha256(
        b"aigenora/hidden-role/v1\x00"
        + domain.encode("ascii")
        + b"\x00"
        + canonical_json_bytes(value)
    ).digest()


def object_hash(domain: str, value: Any) -> str:
    return domain_hash(domain, value).hex()


def _scalar(value: bytes | None = None) -> int:
    if value is None:
        return secrets.randbelow(Q - 1) + 1
    result = int.from_bytes(value, "big") % Q
    return result or 1


def _challenge(domain: str, value: Any) -> int:
    return int.from_bytes(domain_hash(domain, value), "big") % Q


def _int_hex(value: int) -> str:
    _require(0 <= value < P, "NON_CANONICAL", "group value is outside the field")
    return value.to_bytes(GROUP_BYTES, "big").hex()


def _scalar_hex(value: int) -> str:
    _require(0 <= value < Q, "NON_CANONICAL", "scalar is outside the group order")
    return value.to_bytes(SCALAR_BYTES, "big").hex()


def _parse_group(value: Any, label: str, *, allow_identity: bool = False) -> int:
    _require(isinstance(value, str), "NON_CANONICAL", f"{label} must be hexadecimal")
    _require(len(value) == GROUP_BYTES * 2, "NON_CANONICAL", f"{label} has wrong length")
    try:
        return _parse_group_cached(value, allow_identity)
    except ValueError as exc:
        raise HiddenRoleError("NON_CANONICAL", f"{label}: {exc}") from exc


@lru_cache(maxsize=65536)
def _parse_group_cached(value: str, allow_identity: bool) -> int:
    try:
        parsed = int(value, 16)
    except ValueError as exc:
        raise ValueError("not hexadecimal") from exc
    if not 0 < parsed < P:
        raise ValueError("outside the field")
    if not allow_identity and parsed == 1:
        raise ValueError("identity element")
    if pow(parsed, Q, P) != 1:
        raise ValueError("outside the subgroup")
    return parsed


def _parse_nonzero_field(value: Any, label: str) -> int:
    _require(isinstance(value, str), "NON_CANONICAL", f"{label} must be hexadecimal")
    _require(len(value) == GROUP_BYTES * 2, "NON_CANONICAL", f"{label} has wrong length")
    try:
        parsed = int(value, 16)
    except ValueError as exc:
        raise HiddenRoleError("NON_CANONICAL", f"{label} is not hexadecimal") from exc
    _require(0 < parsed < P, "NON_CANONICAL", f"{label} is outside the field")
    return parsed


def _parse_scalar(value: Any, label: str, *, allow_zero: bool = False) -> int:
    _require(isinstance(value, str), "NON_CANONICAL", f"{label} must be hexadecimal")
    _require(len(value) == SCALAR_BYTES * 2, "NON_CANONICAL", f"{label} has wrong length")
    try:
        parsed = int(value, 16)
    except ValueError as exc:
        raise HiddenRoleError("NON_CANONICAL", f"{label} is not hexadecimal") from exc
    _require(parsed < Q, "NON_CANONICAL", f"{label} is outside the group order")
    if not allow_zero:
        _require(parsed != 0, "NON_CANONICAL", f"{label} is zero")
    return parsed


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: Any, label: str) -> bytes:
    _require(isinstance(value, str), "NON_CANONICAL", f"{label} must be base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise HiddenRoleError("NON_CANONICAL", f"{label} is invalid base64url") from exc


def _xor_all(values: Iterable[bytes]) -> bytes:
    output = bytearray(32)
    count = 0
    for value in values:
        _require(len(value) == 32, "NON_CANONICAL", "XOR input must be 32 bytes")
        count += 1
        for index, byte in enumerate(value):
            output[index] ^= byte
    _require(count > 0, "NON_CANONICAL", "XOR input cannot be empty")
    return bytes(output)


def _deterministic_bytes(seed: bytes, domain: str, counter: int) -> bytes:
    return hmac.new(
        seed,
        domain.encode("ascii") + b"\x00" + counter.to_bytes(8, "big"),
        hashlib.sha256,
    ).digest()


def _deterministic_scalar(seed: bytes, domain: str, counter: int) -> int:
    return _scalar(_deterministic_bytes(seed, domain, counter))


def _deterministic_permutation(seed: bytes, domain: str, size: int) -> list[int]:
    values = list(range(size))
    counter = 0
    for index in range(size - 1, 0, -1):
        raw = _deterministic_bytes(seed, domain, counter)
        counter += 1
        chosen = int.from_bytes(raw, "big") % (index + 1)
        values[index], values[chosen] = values[chosen], values[index]
    return values


def _apply_permutation(values: list[Any], permutation: list[int]) -> list[Any]:
    _require(
        sorted(permutation) == list(range(len(values))),
        "NON_CANONICAL",
        "invalid permutation",
    )
    return [values[index] for index in permutation]


def _random_coprime(modulus: int) -> tuple[int, int]:
    while True:
        # A 256-bit public exponent keeps the commutative lock fast while the
        # inverse still spans the full 2048-bit group order.
        exponent = secrets.randbits(256) | 3
        if math.gcd(exponent, modulus) == 1:
            return exponent, pow(exponent, -1, modulus)


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_json_bytes(value)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HiddenRoleError("NON_CANONICAL", f"cannot read {path}") from exc
    _require(isinstance(value, dict), "NON_CANONICAL", f"{path} must contain an object")
    _validate_json(value)
    return value


def _private_raw(private_key: x25519.X25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def _public_raw(public_key: x25519.X25519PublicKey) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _derive_box_key(shared: bytes, *, context: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(PROFILE_ID.encode("ascii")).digest(),
        info=("aigenora/hidden-role/box/v1/" + context).encode("utf-8"),
    ).derive(shared)


def seal_direct(public_key_hex: str, plaintext: bytes, *, context: str) -> str:
    try:
        recipient = x25519.X25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
    except (ValueError, TypeError) as exc:
        raise HiddenRoleError("NON_CANONICAL", "invalid X25519 public key") from exc
    ephemeral = x25519.X25519PrivateKey.generate()
    key = _derive_box_key(ephemeral.exchange(recipient), context=context)
    nonce = secrets.token_bytes(12)
    aad = (PROFILE_ID + "\x00" + context).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return _b64(_public_raw(ephemeral.public_key()) + nonce + ciphertext)


def open_direct(private_key_hex: str, envelope: str, *, context: str) -> bytes:
    raw = _unb64(envelope, "sealed envelope")
    _require(len(raw) >= 32 + 12 + 16, "NON_CANONICAL", "sealed envelope is too short")
    try:
        private_key = x25519.X25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
        ephemeral = x25519.X25519PublicKey.from_public_bytes(raw[:32])
        key = _derive_box_key(private_key.exchange(ephemeral), context=context)
        aad = (PROFILE_ID + "\x00" + context).encode("utf-8")
        return AESGCM(key).decrypt(raw[32:44], raw[44:], aad)
    except Exception as exc:
        raise HiddenRoleError("CONTEXT_MISMATCH", "sealed envelope authentication failed") from exc


def _onion_context(ceremony_id: str, batch_id: str, layer: int) -> str:
    return f"onion/{ceremony_id}/{batch_id}/{layer}"


def onion_seal(
    plaintext: dict[str, Any],
    *,
    ceremony_id: str,
    batch_id: str,
    mix_public_keys: list[str],
    padded_bytes: int = 8192,
) -> str:
    raw = canonical_json_bytes(plaintext)
    _require(len(raw) + 4 <= padded_bytes, "NON_CANONICAL", "anonymous payload is too large")
    inner = len(raw).to_bytes(4, "big") + raw + secrets.token_bytes(padded_bytes - len(raw) - 4)
    for layer in reversed(range(len(mix_public_keys))):
        encoded = seal_direct(
            mix_public_keys[layer],
            inner,
            context=_onion_context(ceremony_id, batch_id, layer),
        )
        inner = _unb64(encoded, "onion layer")
    return _b64(inner)


def build_seeded_initial_deck(
    descriptors: list[dict[str, Any]],
    *,
    tie_seed: str,
    roles: tuple[str, ...] = WEREWOLF_ROLES,
) -> tuple[dict[str, Any], list[str]]:
    """Build the public initial deck from all peers' committed entropy.

    ``tie_seed`` is the XOR of the seven pre-committed peer shares. Reusing it
    with a domain-separated deterministic scalar derivation removes the need
    for a privileged constructor to choose hidden initial ElGamal randomness.
    The value is revealed before the seven private shuffles, so it does not
    disclose the final seat-to-role mapping.
    """

    _require(len(descriptors) == 7, "NON_CANONICAL", "seven descriptors are required")
    ceremony_ids = {str(item.get("ceremony_id")) for item in descriptors}
    _require(len(ceremony_ids) == 1, "CONTEXT_MISMATCH", "peer ceremonies differ")
    ceremony_id = ceremony_ids.pop()
    try:
        seed = bytes.fromhex(tie_seed)
    except (TypeError, ValueError) as exc:
        raise HiddenRoleError("NON_CANONICAL", "tie seed is not hexadecimal") from exc
    _require(len(seed) == 32, "NON_CANONICAL", "tie seed must be 32 bytes")
    randomness = [
        _deterministic_scalar(
            seed,
            f"initial-role/{PROFILE_ID}/{ceremony_id}/{tie_seed}",
            card_id,
        )
        for card_id in range(len(roles))
    ]
    return build_initial_deck(descriptors, roles=roles, randomness=randomness)


def onion_open_layer(
    envelope: str,
    *,
    private_key_hex: str,
    ceremony_id: str,
    batch_id: str,
    layer: int,
) -> str:
    # open_direct accepts the same base64url form produced by each layer.
    raw = open_direct(
        private_key_hex,
        envelope,
        context=_onion_context(ceremony_id, batch_id, layer),
    )
    return _b64(raw)


def onion_decode_plaintext(envelope: str) -> dict[str, Any]:
    raw = _unb64(envelope, "onion plaintext")
    _require(len(raw) >= 4, "NON_CANONICAL", "onion plaintext is too short")
    length = int.from_bytes(raw[:4], "big")
    _require(0 < length <= len(raw) - 4, "NON_CANONICAL", "onion plaintext length is invalid")
    try:
        value = json.loads(raw[4 : 4 + length].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HiddenRoleError("NON_CANONICAL", "onion plaintext is invalid JSON") from exc
    _require(isinstance(value, dict), "NON_CANONICAL", "onion plaintext must be an object")
    _validate_json(value)
    return value


def joint_public_key(descriptors: list[dict[str, Any]]) -> str:
    keys = []
    for expected_seat, descriptor in enumerate(descriptors):
        validate_descriptor(descriptor, expected_seat=expected_seat)
        keys.append(_parse_group(descriptor["elgamal_public_share"], "ElGamal public share"))
    result = 1
    for value in keys:
        result = result * value % P
    _require(result != 1, "NON_CANONICAL", "joint public key is the identity")
    return _int_hex(result)


def encrypt_role(role: str, joint_public: str, *, randomness: int | None = None) -> dict[str, str]:
    _require(role in ROLE_CODES, "NON_CANONICAL", "unknown role")
    public = _parse_group(joint_public, "joint public key")
    nonce = randomness or _scalar()
    message = pow(G, ROLE_CODES[role], P)
    return {
        "a": _int_hex(pow(G, nonce, P)),
        "b": _int_hex(pow(public, nonce, P) * message % P),
    }


def rerandomize_role(
    ciphertext: dict[str, Any], joint_public: str, *, randomness: int
) -> dict[str, str]:
    _require(set(ciphertext) == {"a", "b"}, "NON_CANONICAL", "invalid role ciphertext fields")
    a = _parse_group(ciphertext["a"], "role ciphertext a")
    b = _parse_group(ciphertext["b"], "role ciphertext b")
    public = _parse_group(joint_public, "joint public key")
    _require(0 < randomness < Q, "NON_CANONICAL", "invalid rerandomization scalar")
    return {
        "a": _int_hex(a * pow(G, randomness, P) % P),
        "b": _int_hex(b * pow(public, randomness, P) % P),
    }


def _share_context(
    *, ceremony_id: str, card_hash: str, recipient_seat: int, guardian_seat: int
) -> dict[str, Any]:
    return {
        "card_hash": card_hash,
        "ceremony_id": ceremony_id,
        "guardian_seat": guardian_seat,
        "profile_id": PROFILE_ID,
        "recipient_seat": recipient_seat,
    }


def create_decryption_share(
    ciphertext: dict[str, Any],
    *,
    private_share: int,
    public_share: str,
    ceremony_id: str,
    card_hash: str,
    recipient_seat: int,
    guardian_seat: int,
) -> dict[str, Any]:
    a = _parse_group(ciphertext.get("a"), "role ciphertext a")
    y = _parse_group(public_share, "ElGamal public share")
    _require(pow(G, private_share, P) == y, "COMMITMENT_MISMATCH", "private share does not match")
    share = pow(a, private_share, P)
    witness = _scalar()
    t1 = pow(G, witness, P)
    t2 = pow(a, witness, P)
    context = _share_context(
        ceremony_id=ceremony_id,
        card_hash=card_hash,
        recipient_seat=recipient_seat,
        guardian_seat=guardian_seat,
    )
    challenge = _challenge(
        "chaum-pedersen",
        {**context, "a": _int_hex(a), "public_share": _int_hex(y), "share": _int_hex(share), "t1": _int_hex(t1), "t2": _int_hex(t2)},
    )
    response = (witness + challenge * private_share) % Q
    return {
        **context,
        "proof": {"challenge": _scalar_hex(challenge), "response": _scalar_hex(response)},
        "public_share": _int_hex(y),
        "schema": "aigenora-hidden-role-decryption-share/1",
        "share": _int_hex(share),
    }


def verify_decryption_share(
    ciphertext: dict[str, Any], share_value: dict[str, Any], *, descriptor: dict[str, Any]
) -> int:
    expected = {
        "a",
        "b",
    }
    _require(set(ciphertext) == expected, "NON_CANONICAL", "invalid role ciphertext")
    required = {
        "card_hash",
        "ceremony_id",
        "guardian_seat",
        "profile_id",
        "proof",
        "public_share",
        "recipient_seat",
        "schema",
        "share",
    }
    _require(set(share_value) == required, "NON_CANONICAL", "invalid decryption share fields")
    _require(
        share_value["schema"] == "aigenora-hidden-role-decryption-share/1"
        and share_value["profile_id"] == PROFILE_ID,
        "CONTEXT_MISMATCH",
        "wrong decryption share profile",
    )
    seat = int(share_value["guardian_seat"])
    _require(int(descriptor["seat"]) == seat, "CONTEXT_MISMATCH", "guardian seat mismatch")
    _require(
        descriptor["elgamal_public_share"] == share_value["public_share"],
        "CONTEXT_MISMATCH",
        "guardian public share mismatch",
    )
    a = _parse_group(ciphertext["a"], "role ciphertext a")
    y = _parse_group(share_value["public_share"], "ElGamal public share")
    share = _parse_group(share_value["share"], "decryption share", allow_identity=True)
    proof = share_value["proof"]
    _require(isinstance(proof, dict) and set(proof) == {"challenge", "response"}, "NON_CANONICAL", "invalid share proof")
    challenge = _parse_scalar(proof["challenge"], "share challenge", allow_zero=True)
    response = _parse_scalar(proof["response"], "share response", allow_zero=True)
    t1 = pow(G, response, P) * pow(pow(y, challenge, P), -1, P) % P
    t2 = pow(a, response, P) * pow(pow(share, challenge, P), -1, P) % P
    context = {
        key: share_value[key]
        for key in ("card_hash", "ceremony_id", "guardian_seat", "profile_id", "recipient_seat")
    }
    expected_challenge = _challenge(
        "chaum-pedersen",
        {**context, "a": _int_hex(a), "public_share": _int_hex(y), "share": _int_hex(share), "t1": _int_hex(t1), "t2": _int_hex(t2)},
    )
    _require(challenge == expected_challenge, "INVALID_SHARE_PROOF", "Chaum-Pedersen proof failed")
    return share


def combine_role(
    ciphertext: dict[str, Any], shares: list[dict[str, Any]], descriptors: list[dict[str, Any]]
) -> str:
    _require(len(shares) == len(descriptors) == 7, "AUDIT_INCOMPLETE", "exactly seven shares are required")
    by_seat = {int(item.get("guardian_seat", -1)): item for item in shares}
    _require(set(by_seat) == set(range(7)), "AUDIT_INCOMPLETE", "guardian share set is incomplete")
    denominator = 1
    for seat, descriptor in enumerate(descriptors):
        denominator = denominator * verify_decryption_share(
            ciphertext, by_seat[seat], descriptor=descriptor
        ) % P
    b = _parse_group(ciphertext["b"], "role ciphertext b")
    message = b * pow(denominator, -1, P) % P
    matches = [role for role, code in ROLE_CODES.items() if message == pow(G, code, P)]
    _require(len(matches) == 1, "ROLE_MISMATCH", "decrypted role is outside the registry")
    return matches[0]


def schnorr_sign(private_key: int, message: dict[str, Any], *, domain: str) -> dict[str, str]:
    _require(0 < private_key < Q, "NON_CANONICAL", "invalid Schnorr private key")
    public = pow(G, private_key, P)
    nonce = _scalar()
    commitment = pow(G, nonce, P)
    challenge = _challenge(
        "schnorr-signature",
        {"domain": domain, "message": message, "public_key": _int_hex(public), "commitment": _int_hex(commitment)},
    )
    return {
        "challenge": _scalar_hex(challenge),
        "response": _scalar_hex((nonce + challenge * private_key) % Q),
    }


def verify_schnorr(
    public_key: str, message: dict[str, Any], signature: dict[str, Any], *, domain: str
) -> bool:
    try:
        public = _parse_group(public_key, "Schnorr public key")
        _require(set(signature) == {"challenge", "response"}, "NON_CANONICAL", "invalid Schnorr signature")
        challenge = _parse_scalar(signature["challenge"], "Schnorr challenge", allow_zero=True)
        response = _parse_scalar(signature["response"], "Schnorr response", allow_zero=True)
        commitment = pow(G, response, P) * pow(pow(public, challenge, P), -1, P) % P
        expected = _challenge(
            "schnorr-signature",
            {"domain": domain, "message": message, "public_key": _int_hex(public), "commitment": _int_hex(commitment)},
        )
        return challenge == expected
    except (HiddenRoleError, ValueError):
        return False


def derive_two_member_team_key(
    private_key: int,
    ring: list[str],
    *,
    ceremony_id: str,
    role: str,
) -> bytes:
    """Derive a symmetric key shared by exactly two anonymous role credentials."""
    _require(len(ring) == 2, "CONTEXT_MISMATCH", "team key requires a two-member ring")
    public_values = [_parse_group(value, "team ring public key") for value in ring]
    own = pow(G, private_key, P)
    _require(own in public_values, "CONTEXT_MISMATCH", "private key is outside the team ring")
    other = public_values[1 - public_values.index(own)]
    shared = pow(other, private_key, P)
    return domain_hash(
        "two-member-team-key",
        {
            "ceremony_id": ceremony_id,
            "role": role,
            "ring": ring,
            "shared": _int_hex(shared),
        },
    )


def _team_encrypt(
    key: bytes,
    value: dict[str, Any],
    *,
    context: str,
    padded_bytes: int = TEAM_MESSAGE_PADDED_BYTES,
) -> str:
    raw = canonical_json_bytes(value)
    _require(len(raw) + 4 <= padded_bytes, "NON_CANONICAL", "team message is too large")
    plaintext = len(raw).to_bytes(4, "big") + raw + secrets.token_bytes(padded_bytes - len(raw) - 4)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, context.encode("utf-8"))
    return _b64(nonce + ciphertext)


def _team_decrypt(key: bytes, value: str, *, context: str) -> dict[str, Any]:
    raw = _unb64(value, "team message")
    _require(len(raw) >= 28, "NON_CANONICAL", "team message is too short")
    try:
        plaintext = AESGCM(key).decrypt(raw[:12], raw[12:], context.encode("utf-8"))
    except Exception as exc:
        raise HiddenRoleError("CONTEXT_MISMATCH", "team message authentication failed") from exc
    _require(len(plaintext) >= 4, "NON_CANONICAL", "team plaintext is too short")
    length = int.from_bytes(plaintext[:4], "big")
    _require(0 < length <= len(plaintext) - 4, "NON_CANONICAL", "team plaintext length is invalid")
    try:
        result = json.loads(plaintext[4 : 4 + length].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HiddenRoleError("NON_CANONICAL", "team plaintext is invalid JSON") from exc
    _require(isinstance(result, dict), "NON_CANONICAL", "team plaintext must be an object")
    return result


def _hash_to_group(public: int, *, scope: str) -> int:
    exponent = _challenge("lsag-hash-to-group", {"public_key": _int_hex(public), "scope": scope}) or 1
    return pow(G, exponent, P)


def ring_sign(
    private_key: int,
    ring: list[str],
    message: dict[str, Any],
    *,
    scope: str,
) -> dict[str, Any]:
    _require(1 <= len(ring) <= 32, "NON_CANONICAL", "ring size is outside 1..32")
    public_values = [_parse_group(value, "ring public key") for value in ring]
    _require(len(set(public_values)) == len(public_values), "NON_CANONICAL", "ring keys must be unique")
    own_public = pow(G, private_key, P)
    _require(own_public in public_values, "CONTEXT_MISMATCH", "private key is not in the ring")
    signer = public_values.index(own_public)
    key_image = pow(_hash_to_group(own_public, scope=scope), private_key, P)
    responses = [0] * len(public_values)
    challenges = [0] * len(public_values)
    alpha = _scalar()
    next_index = (signer + 1) % len(public_values)
    challenges[next_index] = _challenge(
        "lsag-challenge",
        {
            "L": _int_hex(pow(G, alpha, P)),
            "R": _int_hex(pow(_hash_to_group(own_public, scope=scope), alpha, P)),
            "message": message,
            "ring": ring,
            "scope": scope,
        },
    )
    current = next_index
    while current != signer:
        responses[current] = _scalar()
        public = public_values[current]
        hp = _hash_to_group(public, scope=scope)
        left = pow(G, responses[current], P) * pow(public, challenges[current], P) % P
        right = pow(hp, responses[current], P) * pow(key_image, challenges[current], P) % P
        following = (current + 1) % len(public_values)
        challenges[following] = _challenge(
            "lsag-challenge",
            {"L": _int_hex(left), "R": _int_hex(right), "message": message, "ring": ring, "scope": scope},
        )
        current = following
    responses[signer] = (alpha - challenges[signer] * private_key) % Q
    return {
        "c0": _scalar_hex(challenges[0]),
        "key_image": _int_hex(key_image),
        "responses": [_scalar_hex(value) for value in responses],
        "schema": "aigenora-hidden-role-lsag/1",
    }


def verify_ring_signature(
    ring: list[str], message: dict[str, Any], signature: dict[str, Any], *, scope: str
) -> bool:
    try:
        _require(1 <= len(ring) <= 32, "NON_CANONICAL", "invalid ring size")
        public_values = [_parse_group(value, "ring public key") for value in ring]
        _require(len(set(public_values)) == len(public_values), "NON_CANONICAL", "duplicate ring key")
        _require(
            set(signature) == {"c0", "key_image", "responses", "schema"}
            and signature["schema"] == "aigenora-hidden-role-lsag/1",
            "NON_CANONICAL",
            "invalid LSAG fields",
        )
        responses_raw = signature["responses"]
        _require(isinstance(responses_raw, list) and len(responses_raw) == len(ring), "NON_CANONICAL", "wrong LSAG response count")
        responses = [_parse_scalar(value, "LSAG response", allow_zero=True) for value in responses_raw]
        challenge = _parse_scalar(signature["c0"], "LSAG c0", allow_zero=True)
        original = challenge
        key_image = _parse_group(signature["key_image"], "LSAG key image")
        for index, public in enumerate(public_values):
            hp = _hash_to_group(public, scope=scope)
            left = pow(G, responses[index], P) * pow(public, challenge, P) % P
            right = pow(hp, responses[index], P) * pow(key_image, challenge, P) % P
            challenge = _challenge(
                "lsag-challenge",
                {"L": _int_hex(left), "R": _int_hex(right), "message": message, "ring": ring, "scope": scope},
            )
        return hmac.compare_digest(_scalar_hex(challenge), _scalar_hex(original))
    except (HiddenRoleError, ValueError, TypeError):
        return False


def verify_anonymous_messages(
    messages: list[dict[str, Any]],
    *,
    ring: list[str],
    scope: str,
    expected_count: int,
) -> list[dict[str, Any]]:
    _require(len(messages) == expected_count, "AUDIT_INCOMPLETE", "anonymous message count mismatch")
    images: set[str] = set()
    payloads: list[dict[str, Any]] = []
    for envelope in messages:
        _require(
            isinstance(envelope, dict) and set(envelope) == {"payload", "signature"},
            "NON_CANONICAL",
            "anonymous message has invalid fields",
        )
        payload = envelope["payload"]
        signature = envelope["signature"]
        _require(isinstance(payload, dict) and isinstance(signature, dict), "NON_CANONICAL", "invalid anonymous message")
        _require(
            verify_ring_signature(ring, payload, signature, scope=scope),
            "INVALID_RING_SIGNATURE",
            "anonymous message signature failed",
        )
        image = str(signature["key_image"])
        _require(image not in images, "DUPLICATE_KEY_IMAGE", "duplicate anonymous credential")
        images.add(image)
        payloads.append(payload)
    return payloads


def validate_descriptor(descriptor: dict[str, Any], *, expected_seat: int | None = None) -> None:
    required = {
        "cards",
        "ceremony_id",
        "elgamal_public_share",
        "mix_public_key",
        "mix_seed_commitment",
        "profile_id",
        "schema",
        "seat",
        "secret_commitment",
        "shuffle_seed_commitment",
        "tie_share_commitment",
    }
    _require(set(descriptor) == required, "NON_CANONICAL", "invalid peer descriptor fields")
    _require(
        descriptor["schema"] == DESCRIPTOR_SCHEMA and descriptor["profile_id"] == PROFILE_ID,
        "CONTEXT_MISMATCH",
        "wrong peer descriptor profile",
    )
    seat = descriptor["seat"]
    _require(isinstance(seat, int) and 0 <= seat < 7, "NON_CANONICAL", "invalid peer seat")
    if expected_seat is not None:
        _require(seat == expected_seat, "CONTEXT_MISMATCH", "peer descriptor order mismatch")
    _parse_group(descriptor["elgamal_public_share"], "ElGamal public share")
    mix_key = descriptor["mix_public_key"]
    _require(isinstance(mix_key, str) and len(mix_key) == 64, "NON_CANONICAL", "invalid mix public key")
    try:
        x25519.X25519PublicKey.from_public_bytes(bytes.fromhex(mix_key))
    except ValueError as exc:
        raise HiddenRoleError("NON_CANONICAL", "invalid mix public key") from exc
    for name in (
        "mix_seed_commitment",
        "secret_commitment",
        "shuffle_seed_commitment",
        "tie_share_commitment",
    ):
        _require(isinstance(descriptor[name], str) and len(descriptor[name]) == 64, "NON_CANONICAL", f"invalid {name}")
    cards = descriptor["cards"]
    _require(isinstance(cards, list) and len(cards) == 7, "NON_CANONICAL", "descriptor must contain seven cards")
    for card_id, card in enumerate(cards):
        required_card = {
            "card_id",
            "credential_commitment",
            "credential_locked",
        }
        _require(isinstance(card, dict) and set(card) == required_card, "NON_CANONICAL", "invalid descriptor card")
        _require(card["card_id"] == card_id, "CONTEXT_MISMATCH", "descriptor card order mismatch")
        _parse_group(card["credential_commitment"], "credential commitment")
        _parse_nonzero_field(card["credential_locked"], "locked credential share")


def build_registry(
    descriptors: list[dict[str, Any]], roles: tuple[str, ...] = WEREWOLF_ROLES
) -> list[dict[str, Any]]:
    _require(len(descriptors) == len(roles) == 7, "NON_CANONICAL", "seven descriptors and roles are required")
    for seat, descriptor in enumerate(descriptors):
        validate_descriptor(descriptor, expected_seat=seat)
    registry = []
    for card_id, role in enumerate(roles):
        _require(role in ROLE_CODES, "NON_CANONICAL", "unknown registry role")
        credential = 1
        for descriptor in descriptors:
            card = descriptor["cards"][card_id]
            credential = credential * _parse_group(card["credential_commitment"], "credential commitment") % P
        registry.append(
            {
                "card_id": card_id,
                "credential_public_key": _int_hex(credential),
                "role": role,
            }
        )
    _require(len({item["credential_public_key"] for item in registry}) == 7, "COMMITMENT_MISMATCH", "credential public keys are not unique")
    return registry


def build_initial_deck(
    descriptors: list[dict[str, Any]],
    *,
    roles: tuple[str, ...] = WEREWOLF_ROLES,
    randomness: list[int] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    _require(len(descriptors) == 7 and len(roles) == 7, "NON_CANONICAL", "seven peers and roles are required")
    ceremony_ids = {str(item.get("ceremony_id")) for item in descriptors}
    _require(len(ceremony_ids) == 1, "CONTEXT_MISMATCH", "peer ceremonies differ")
    ceremony_id = ceremony_ids.pop()
    public = joint_public_key(descriptors)
    nonces = randomness or [_scalar() for _ in roles]
    _require(len(nonces) == 7 and all(0 < value < Q for value in nonces), "NON_CANONICAL", "invalid initial role randomness")
    cards: list[dict[str, Any]] = []
    for card_id, role in enumerate(roles):
        components = []
        for descriptor in descriptors:
            source = descriptor["cards"][card_id]
            components.append(
                {
                    "contributor_seat": descriptor["seat"],
                    "credential": source["credential_locked"],
                }
            )
        cards.append(
            {
                "components": components,
                "role_ciphertext": encrypt_role(role, public, randomness=nonces[card_id]),
            }
        )
    deck = {
        "cards": cards,
        "ceremony_id": ceremony_id,
        "joint_public_key": public,
        "profile_id": PROFILE_ID,
        "schema": DECK_SCHEMA,
        "stage": 0,
    }
    return deck, [_scalar_hex(value) for value in nonces]


def validate_deck(deck: dict[str, Any], *, expected_stage: int | None = None) -> None:
    _require(
        set(deck) == {"cards", "ceremony_id", "joint_public_key", "profile_id", "schema", "stage"},
        "NON_CANONICAL",
        "invalid deck fields",
    )
    _require(deck["schema"] == DECK_SCHEMA and deck["profile_id"] == PROFILE_ID, "CONTEXT_MISMATCH", "wrong deck profile")
    _parse_group(deck["joint_public_key"], "joint public key")
    stage = deck["stage"]
    _require(isinstance(stage, int) and 0 <= stage <= 7, "NON_CANONICAL", "invalid deck stage")
    if expected_stage is not None:
        _require(stage == expected_stage, "CONTEXT_MISMATCH", "unexpected deck stage")
    cards = deck["cards"]
    _require(isinstance(cards, list) and len(cards) == 7, "DECK_CONSERVATION_FAILED", "deck must contain seven cards")
    for card in cards:
        _require(isinstance(card, dict) and set(card) == {"components", "role_ciphertext"}, "NON_CANONICAL", "invalid deck card")
        cipher = card["role_ciphertext"]
        _require(isinstance(cipher, dict) and set(cipher) == {"a", "b"}, "NON_CANONICAL", "invalid role ciphertext")
        _parse_group(cipher["a"], "role ciphertext a")
        _parse_group(cipher["b"], "role ciphertext b")
        components = card["components"]
        _require(isinstance(components, list) and len(components) == 7, "DECK_CONSERVATION_FAILED", "card must contain seven components")
        seats = []
        for component in components:
            _require(
                isinstance(component, dict)
                and set(component) == {"contributor_seat", "credential"},
                "NON_CANONICAL",
                "invalid card component",
            )
            seats.append(component["contributor_seat"])
            for name in ("credential",):
                _parse_nonzero_field(component[name], f"component {name}")
        _require(sorted(seats) == list(range(7)), "DECK_CONSERVATION_FAILED", "component contributor set mismatch")


@dataclass(frozen=True)
class TerminalVerification:
    status: str
    artifact_hash: str
    assignments: tuple[dict[str, Any], ...]
    batch_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_hash": self.artifact_hash,
            "assignments": list(self.assignments),
            "batch_count": self.batch_count,
            "profile_id": PROFILE_ID,
            "schema": "aigenora-hidden-role-verification/1",
            "status": self.status,
        }


class HiddenRolePeer:
    """Private peer state used inside one isolated participant process."""

    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)
        self.state = _load_object(self.state_path)
        _require(self.state.get("schema") == PEER_STATE_SCHEMA, "CONTEXT_MISMATCH", "wrong peer state schema")

    @classmethod
    def initialize(
        cls,
        state_path: str | Path,
        *,
        seat: int,
        ceremony_id: str,
        roles: tuple[str, ...] = WEREWOLF_ROLES,
        force: bool = False,
    ) -> "HiddenRolePeer":
        target = Path(state_path)
        _require(0 <= seat < 7, "NON_CANONICAL", "seat must be in 0..6")
        _require(len(roles) == 7 and all(role in ROLE_CODES for role in roles), "NON_CANONICAL", "invalid role registry")
        if target.exists() and not force:
            raise HiddenRoleError("CONTEXT_MISMATCH", "peer state already exists")
        exponent, inverse = _random_coprime(P - 1)
        elgamal_private = _scalar()
        mix_private = x25519.X25519PrivateKey.generate()
        shuffle_seed = secrets.token_bytes(32)
        mix_seed = secrets.token_bytes(32)
        tie_share = secrets.token_bytes(32)
        card_secrets = []
        for card_id, _role in enumerate(roles):
            credential = _scalar()
            card_secrets.append(
                {
                    "card_id": card_id,
                    "credential": _scalar_hex(credential),
                }
            )
        secret_body = {
            "card_secrets": card_secrets,
            "elgamal_private": _scalar_hex(elgamal_private),
            "mix_private_key": _private_raw(mix_private).hex(),
            "mix_seed": mix_seed.hex(),
            "shuffle_seed": shuffle_seed.hex(),
            "sra_exponent": _int_hex(exponent),
            "sra_inverse": _int_hex(inverse),
            "tie_share": tie_share.hex(),
        }
        state = {
            **secret_body,
            "assigned": None,
            "ceremony_id": ceremony_id,
            "mix_records": [],
            "queries": {},
            "role_team": None,
            "seer_results": [],
            "profile_id": PROFILE_ID,
            "schema": PEER_STATE_SCHEMA,
            "seat": seat,
            "shuffle_record": None,
        }
        _atomic_write(target, state)
        return cls(target)

    @property
    def seat(self) -> int:
        return int(self.state["seat"])

    @property
    def ceremony_id(self) -> str:
        return str(self.state["ceremony_id"])

    def _save(self) -> None:
        _atomic_write(self.state_path, self.state)

    def _secret_body(self) -> dict[str, Any]:
        return {
            key: self.state[key]
            for key in (
                "card_secrets",
                "elgamal_private",
                "mix_private_key",
                "mix_seed",
                "shuffle_seed",
                "sra_exponent",
                "sra_inverse",
                "tie_share",
            )
        }

    def descriptor(self) -> dict[str, Any]:
        exponent = _parse_nonzero_field(self.state["sra_exponent"], "SRA exponent")
        cards = []
        for value in self.state["card_secrets"]:
            credential = _parse_scalar(value["credential"], "credential share")
            cards.append(
                {
                    "card_id": value["card_id"],
                    "credential_commitment": _int_hex(pow(G, credential, P)),
                    "credential_locked": _int_hex(pow(credential, exponent, P)),
                }
            )
        mix_private = x25519.X25519PrivateKey.from_private_bytes(bytes.fromhex(self.state["mix_private_key"]))
        descriptor = {
            "cards": cards,
            "ceremony_id": self.ceremony_id,
            "elgamal_public_share": _int_hex(pow(G, _parse_scalar(self.state["elgamal_private"], "ElGamal private share"), P)),
            "mix_public_key": _public_raw(mix_private.public_key()).hex(),
            "mix_seed_commitment": hashlib.sha256(bytes.fromhex(self.state["mix_seed"])).hexdigest(),
            "profile_id": PROFILE_ID,
            "schema": DESCRIPTOR_SCHEMA,
            "seat": self.seat,
            "secret_commitment": object_hash("peer-secret-commitment", self._secret_body()),
            "shuffle_seed_commitment": hashlib.sha256(bytes.fromhex(self.state["shuffle_seed"])).hexdigest(),
            "tie_share_commitment": hashlib.sha256(bytes.fromhex(self.state["tie_share"])).hexdigest(),
        }
        validate_descriptor(descriptor, expected_seat=self.seat)
        return descriptor

    def tie_share(self) -> str:
        return self.state["tie_share"]

    def shuffle(self, deck: dict[str, Any]) -> dict[str, Any]:
        validate_deck(deck, expected_stage=self.seat)
        _require(deck["ceremony_id"] == self.ceremony_id, "CONTEXT_MISMATCH", "deck ceremony mismatch")
        exponent = _parse_nonzero_field(self.state["sra_exponent"], "SRA exponent")
        seed = bytes.fromhex(self.state["shuffle_seed"])
        input_hash = object_hash("deck-stage", deck)
        transformed: list[dict[str, Any]] = []
        randomizers: list[str] = []
        for card_index, card in enumerate(deck["cards"]):
            nonce = _deterministic_scalar(seed, f"shuffle/{self.ceremony_id}/{self.seat}/{input_hash}", card_index)
            randomizers.append(_scalar_hex(nonce))
            components = []
            for component in card["components"]:
                components.append(
                    {
                        "contributor_seat": component["contributor_seat"],
                        "credential": _int_hex(pow(_parse_nonzero_field(component["credential"], "credential component"), exponent, P)),
                    }
                )
            transformed.append(
                {
                    "components": components,
                    "role_ciphertext": rerandomize_role(
                        card["role_ciphertext"], deck["joint_public_key"], randomness=nonce
                    ),
                }
            )
        permutation = _deterministic_permutation(
            seed,
            f"shuffle-permutation/{self.ceremony_id}/{self.seat}/{input_hash}",
            len(transformed),
        )
        output = {
            **{key: deck[key] for key in ("ceremony_id", "joint_public_key", "profile_id", "schema")},
            "cards": _apply_permutation(transformed, permutation),
            "stage": self.seat + 1,
        }
        validate_deck(output, expected_stage=self.seat + 1)
        self.state["shuffle_record"] = {
            "input_hash": input_hash,
            "output_hash": object_hash("deck-stage", output),
            "permutation": permutation,
            "randomizers": randomizers,
        }
        self._save()
        return output

    def unlock_for_recipient(self, card: dict[str, Any], *, recipient_seat: int) -> dict[str, Any]:
        _require(self.seat != recipient_seat, "CONTEXT_MISMATCH", "recipient must unlock locally")
        inverse = _parse_nonzero_field(self.state["sra_inverse"], "SRA inverse")
        output = {"components": [], "role_ciphertext": dict(card["role_ciphertext"])}
        for component in card["components"]:
            values = {}
            contributor = int(component["contributor_seat"])
            for name in ("credential",):
                transformed = pow(_parse_nonzero_field(component[name], name), inverse, P)
                if contributor == self.seat:
                    transformed = pow(transformed, inverse, P)
                values[name] = _int_hex(transformed)
            output["components"].append({"contributor_seat": contributor, **values})
        return output

    def sealed_role_share(
        self,
        card: dict[str, Any],
        *,
        recipient_seat: int,
        recipient_public_key: str,
    ) -> str:
        # Component locks change while peers remove their layers.  The role
        # ciphertext is immutable during delivery and therefore provides the
        # stable assigned-card binding shared by every guardian and recipient.
        card_hash = object_hash("assigned-role-ciphertext", card["role_ciphertext"])
        public_share = _int_hex(
            pow(
                G,
                _parse_scalar(
                    self.state["elgamal_private"], "ElGamal private share"
                ),
                P,
            )
        )
        share = create_decryption_share(
            card["role_ciphertext"],
            private_share=_parse_scalar(self.state["elgamal_private"], "ElGamal private share"),
            public_share=public_share,
            ceremony_id=self.ceremony_id,
            card_hash=card_hash,
            recipient_seat=recipient_seat,
            guardian_seat=self.seat,
        )
        return seal_direct(
            recipient_public_key,
            canonical_json_bytes(share),
            context=f"role-share/{self.ceremony_id}/{card_hash}/{recipient_seat}/{self.seat}",
        )

    def create_role_query(self, *, target_seat: int, scope: str) -> dict[str, Any]:
        assigned = self.private_assignment()
        _require(assigned["role"] == "seer", "CONTEXT_MISMATCH", "only the seer may create a role query")
        _require(0 <= target_seat < 7 and target_seat != self.seat, "NON_CANONICAL", "invalid seer target")
        query_id = object_hash(
            "role-query",
            {
                "ceremony_id": self.ceremony_id,
                "nonce": secrets.token_hex(32),
                "scope": scope,
                "target_seat": target_seat,
            },
        )
        private_key = x25519.X25519PrivateKey.generate()
        reply_public_key = _public_raw(private_key.public_key()).hex()
        self.state["queries"][query_id] = {
            "private_key": _private_raw(private_key).hex(),
            "query_id": query_id,
            "reply_public_key": reply_public_key,
            "scope": scope,
            "status": "pending",
            "target_seat": target_seat,
        }
        self._save()
        return {
            "query_id": query_id,
            "reply_public_key": reply_public_key,
            "target_seat": target_seat,
        }

    def role_query(self, query_id: str) -> dict[str, Any]:
        """Return this peer's public query fields without exposing its private key."""

        record = self.state.get("queries", {}).get(query_id)
        _require(isinstance(record, dict), "CONTEXT_MISMATCH", "unknown role query")
        return {
            "query_id": str(record["query_id"]),
            "reply_public_key": str(record["reply_public_key"]),
            "target_seat": int(record["target_seat"]),
        }

    def has_role_query(self, query_id: str) -> bool:
        """Whether this recipient owns the private state for a public query id."""

        return isinstance(self.state.get("queries", {}).get(query_id), dict)

    def sealed_query_share(
        self,
        card: dict[str, Any],
        *,
        query_id: str,
        reply_public_key: str,
    ) -> str:
        _require(isinstance(query_id, str) and len(query_id) == 64, "NON_CANONICAL", "invalid role query id")
        card_hash = object_hash(
            "queried-role-ciphertext",
            {"query_id": query_id, "role_ciphertext": card["role_ciphertext"]},
        )
        public_share = _int_hex(
            pow(
                G,
                _parse_scalar(self.state["elgamal_private"], "ElGamal private share"),
                P,
            )
        )
        share = create_decryption_share(
            card["role_ciphertext"],
            private_share=_parse_scalar(self.state["elgamal_private"], "ElGamal private share"),
            public_share=public_share,
            ceremony_id=self.ceremony_id,
            card_hash=card_hash,
            recipient_seat=-1,
            guardian_seat=self.seat,
        )
        return seal_direct(
            reply_public_key,
            canonical_json_bytes(share),
            context=f"query-role-share/{self.ceremony_id}/{query_id}/{self.seat}",
        )

    def accept_query_shares(
        self,
        query: dict[str, Any],
        *,
        card: dict[str, Any],
        sealed_shares: list[str],
        descriptors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        query_id = query.get("query_id")
        record = self.state["queries"].get(query_id)
        _require(isinstance(record, dict) and record.get("status") == "pending", "CONTEXT_MISMATCH", "unknown or completed role query")
        _require(
            query.get("target_seat") == record["target_seat"],
            "CONTEXT_MISMATCH",
            "role query target mismatch",
        )
        _require(len(sealed_shares) == 7, "AUDIT_INCOMPLETE", "seven query shares are required")
        card_hash = object_hash(
            "queried-role-ciphertext",
            {"query_id": query_id, "role_ciphertext": card["role_ciphertext"]},
        )
        values = []
        for guardian_seat, envelope in enumerate(sealed_shares):
            plaintext = open_direct(
                record["private_key"],
                envelope,
                context=f"query-role-share/{self.ceremony_id}/{query_id}/{guardian_seat}",
            )
            try:
                value = json.loads(plaintext.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise HiddenRoleError("NON_CANONICAL", "query share plaintext is invalid") from exc
            _require(
                isinstance(value, dict)
                and value.get("card_hash") == card_hash
                and value.get("recipient_seat") == -1,
                "CONTEXT_MISMATCH",
                "query share binding mismatch",
            )
            values.append(value)
        role = combine_role(card["role_ciphertext"], values, descriptors)
        record["status"] = "verified"
        record["result_role"] = role
        self.state["seer_results"].append(
            {
                "query_id": query_id,
                "role": role,
                "scope": record["scope"],
                "target_seat": record["target_seat"],
                "verified": True,
            }
        )
        self._save()
        return {
            "query_id": query_id,
            "role": role,
            "target_seat": record["target_seat"],
            "verified": True,
        }

    def private_seer_results(self) -> list[dict[str, Any]]:
        return list(self.state.get("seer_results") or [])

    def accept_role(
        self,
        card: dict[str, Any],
        *,
        sealed_shares: list[str],
        descriptors: list[dict[str, Any]],
        registry: list[dict[str, Any]],
    ) -> dict[str, Any]:
        _require(self.state.get("assigned") is None, "CONTEXT_MISMATCH", "role was already accepted")
        _require(len(sealed_shares) == 7, "AUDIT_INCOMPLETE", "seven sealed role shares are required")
        inverse = _parse_nonzero_field(self.state["sra_inverse"], "SRA inverse")
        raw_components: list[dict[str, int]] = []
        for component in card["components"]:
            contributor = int(component["contributor_seat"])
            values: dict[str, int] = {}
            for name in ("credential",):
                transformed = pow(_parse_nonzero_field(component[name], name), inverse, P)
                if contributor == self.seat:
                    transformed = pow(transformed, inverse, P)
                _require(0 < transformed < Q, "COMMITMENT_MISMATCH", "unlocked scalar is outside the registry")
                values[name] = transformed
            raw_components.append({"contributor_seat": contributor, **values})
        credential_secret = sum(item["credential"] for item in raw_components) % Q
        credential_public = _int_hex(pow(G, credential_secret, P))
        entries = [item for item in registry if item.get("credential_public_key") == credential_public]
        _require(len(entries) == 1, "COMMITMENT_MISMATCH", "credential does not identify one card")
        registry_entry = entries[0]
        card_id = int(registry_entry["card_id"])
        for component in raw_components:
            contributor = int(component["contributor_seat"])
            source = descriptors[contributor]["cards"][card_id]
            _require(
                pow(G, component["credential"], P) == _parse_group(source["credential_commitment"], "credential commitment"),
                "COMMITMENT_MISMATCH",
                "credential share does not match its commitment",
            )
        mix_private = self.state["mix_private_key"]
        card_hash = object_hash("assigned-role-ciphertext", card["role_ciphertext"])
        shares = []
        for guardian_seat, envelope in enumerate(sealed_shares):
            plaintext = open_direct(
                mix_private,
                envelope,
                context=f"role-share/{self.ceremony_id}/{card_hash}/{self.seat}/{guardian_seat}",
            )
            try:
                value = json.loads(plaintext.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise HiddenRoleError("NON_CANONICAL", "role share plaintext is invalid") from exc
            _require(isinstance(value, dict), "NON_CANONICAL", "role share plaintext must be an object")
            _require(value.get("card_hash") == card_hash, "CONTEXT_MISMATCH", "role share card mismatch")
            shares.append(value)
        role = combine_role(card["role_ciphertext"], shares, descriptors)
        _require(role == registry_entry["role"], "ROLE_MISMATCH", "role ciphertext and credential registry disagree")
        assigned = {
            "card_id": card_id,
            "credential_private": _scalar_hex(credential_secret),
            "credential_public": credential_public,
            "role": role,
        }
        self.state["assigned"] = assigned
        self._save()
        return {
            "accepted": True,
            "credential_commitment": hashlib.sha256(bytes.fromhex(credential_public)).hexdigest(),
            "role_private": True,
            "seat": self.seat,
        }

    def private_assignment(self) -> dict[str, Any]:
        assigned = self.state.get("assigned")
        _require(isinstance(assigned, dict), "AUDIT_INCOMPLETE", "role has not been accepted")
        return dict(assigned)

    def anonymous_sign(self, payload: dict[str, Any], *, ring: list[str], scope: str) -> dict[str, Any]:
        assigned = self.private_assignment()
        signature = ring_sign(
            _parse_scalar(assigned["credential_private"], "credential private key"),
            ring,
            payload,
            scope=scope,
        )
        return {"payload": payload, "signature": signature}

    def credential_sign(self, message: dict[str, Any], *, domain: str) -> dict[str, Any]:
        assigned = self.private_assignment()
        return schnorr_sign(
            _parse_scalar(assigned["credential_private"], "credential private key"),
            message,
            domain=domain,
        )

    def wolf_hello(self, *, wolf_ring: list[str]) -> dict[str, str]:
        assigned = self.private_assignment()
        context = f"wolf-hello/{self.ceremony_id}"
        if assigned["role"] != "wolf":
            # Exact byte length of a genuine encrypted padded plaintext.
            return {
                "blob": _b64(
                    secrets.token_bytes(12 + TEAM_MESSAGE_PADDED_BYTES + 16)
                )
            }
        private_key = _parse_scalar(assigned["credential_private"], "credential private key")
        key = derive_two_member_team_key(
            private_key,
            wolf_ring,
            ceremony_id=self.ceremony_id,
            role="wolf",
        )
        statement = {
            "credential_public_key": assigned["credential_public"],
            "ceremony_id": self.ceremony_id,
            "seat": self.seat,
        }
        signed = {
            **statement,
            "signature": schnorr_sign(private_key, statement, domain=context),
        }
        return {"blob": _team_encrypt(key, signed, context=context)}

    def accept_wolf_hellos(self, messages: list[dict[str, Any]], *, wolf_ring: list[str]) -> dict[str, Any]:
        assigned = self.private_assignment()
        if assigned["role"] != "wolf":
            return {"role": assigned["role"], "team_private": True}
        private_key = _parse_scalar(assigned["credential_private"], "credential private key")
        context = f"wolf-hello/{self.ceremony_id}"
        key = derive_two_member_team_key(
            private_key,
            wolf_ring,
            ceremony_id=self.ceremony_id,
            role="wolf",
        )
        roster: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"blob"}:
                continue
            try:
                value = _team_decrypt(key, message["blob"], context=context)
            except HiddenRoleError:
                continue
            required = {"ceremony_id", "credential_public_key", "seat", "signature"}
            if set(value) != required:
                continue
            statement = {key_name: value[key_name] for key_name in ("credential_public_key", "ceremony_id", "seat")}
            if (
                value["ceremony_id"] != self.ceremony_id
                or value["credential_public_key"] not in wolf_ring
                or not isinstance(value["seat"], int)
                or not verify_schnorr(
                    value["credential_public_key"],
                    statement,
                    value["signature"],
                    domain=context,
                )
            ):
                continue
            roster.append(statement)
        unique = {(item["seat"], item["credential_public_key"]): item for item in roster}
        _require(len(unique) == 2, "AUDIT_INCOMPLETE", "wolf team discovery did not yield two credentials")
        values = sorted(unique.values(), key=lambda item: item["seat"])
        _require(len({item["seat"] for item in values}) == 2, "CONTEXT_MISMATCH", "wolf seats are not unique")
        self.state["role_team"] = values
        self._save()
        return {"role": "wolf", "team_private": True, "teammate_seats": [item["seat"] for item in values]}

    def private_team(self) -> list[dict[str, Any]]:
        value = self.state.get("role_team")
        return list(value) if isinstance(value, list) else []

    def wolf_message(
        self,
        payload: dict[str, Any],
        *,
        wolf_ring: list[str],
        scope: str,
    ) -> dict[str, str]:
        """Seal a signed private team message or an indistinguishable cover blob."""
        assigned = self.private_assignment()
        context = f"wolf-message/{self.ceremony_id}/{scope}"
        if assigned["role"] != "wolf":
            return {
                "blob": _b64(
                    secrets.token_bytes(12 + TEAM_MESSAGE_PADDED_BYTES + 16)
                )
            }
        _require(
            len(wolf_ring) in {1, 2},
            "CONTEXT_MISMATCH",
            "living wolf ring must contain one or two credentials",
        )
        _require(
            assigned["credential_public"] in wolf_ring,
            "CONTEXT_MISMATCH",
            "living wolf credential is outside the team ring",
        )
        if len(wolf_ring) == 1:
            # A lone surviving role member has nobody with whom to derive a
            # team key. Keep traffic shape uniform and authorize the actual
            # game choice separately through the role ring signature.
            return {
                "blob": _b64(
                    secrets.token_bytes(12 + TEAM_MESSAGE_PADDED_BYTES + 16)
                )
            }
        private_key = _parse_scalar(
            assigned["credential_private"], "credential private key"
        )
        key = derive_two_member_team_key(
            private_key,
            wolf_ring,
            ceremony_id=self.ceremony_id,
            role="wolf",
        )
        statement = {
            "credential_public_key": assigned["credential_public"],
            "payload": payload,
            "scope": scope,
        }
        value = {
            **statement,
            "signature": schnorr_sign(private_key, statement, domain=context),
        }
        return {"blob": _team_encrypt(key, value, context=context)}

    def open_wolf_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        wolf_ring: list[str],
        scope: str,
    ) -> list[dict[str, Any]]:
        """Open and verify the two wolves' private messages on a wolf seat only."""
        assigned = self.private_assignment()
        if assigned["role"] != "wolf":
            return []
        _require(
            len(wolf_ring) in {1, 2},
            "CONTEXT_MISMATCH",
            "living wolf ring must contain one or two credentials",
        )
        _require(
            assigned["credential_public"] in wolf_ring,
            "CONTEXT_MISMATCH",
            "living wolf credential is outside the team ring",
        )
        if len(wolf_ring) == 1:
            return []
        private_key = _parse_scalar(
            assigned["credential_private"], "credential private key"
        )
        context = f"wolf-message/{self.ceremony_id}/{scope}"
        key = derive_two_member_team_key(
            private_key,
            wolf_ring,
            ceremony_id=self.ceremony_id,
            role="wolf",
        )
        verified: dict[str, dict[str, Any]] = {}
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"blob"}:
                continue
            try:
                value = _team_decrypt(key, message["blob"], context=context)
            except HiddenRoleError:
                continue
            if set(value) != {
                "credential_public_key",
                "payload",
                "scope",
                "signature",
            }:
                continue
            statement = {
                name: value[name]
                for name in ("credential_public_key", "payload", "scope")
            }
            credential = value["credential_public_key"]
            if (
                credential not in wolf_ring
                or value["scope"] != scope
                or not isinstance(value["payload"], dict)
                or not verify_schnorr(
                    credential,
                    statement,
                    value["signature"],
                    domain=context,
                )
            ):
                continue
            verified[credential] = value["payload"]
        _require(
            len(verified) == len(wolf_ring),
            "AUDIT_INCOMPLETE",
            "wolf team message set is incomplete",
        )
        return [verified[credential] for credential in sorted(verified)]

    def onion_wrap(
        self,
        message: dict[str, Any],
        *,
        batch_id: str,
        mix_public_keys: list[str],
    ) -> str:
        return onion_seal(
            message,
            ceremony_id=self.ceremony_id,
            batch_id=batch_id,
            mix_public_keys=mix_public_keys,
        )

    def mix_batch(self, envelopes: list[str], *, batch_id: str) -> list[str]:
        _require(len(envelopes) > 0, "MIX_CONSERVATION_FAILED", "mix batch is empty")
        input_hash = object_hash("mix-stage-input", envelopes)
        opened = [
            onion_open_layer(
                envelope,
                private_key_hex=self.state["mix_private_key"],
                ceremony_id=self.ceremony_id,
                batch_id=batch_id,
                layer=self.seat,
            )
            for envelope in envelopes
        ]
        seed = bytes.fromhex(self.state["mix_seed"])
        permutation = _deterministic_permutation(
            seed,
            f"mix/{self.ceremony_id}/{batch_id}/{self.seat}/{input_hash}",
            len(opened),
        )
        output = _apply_permutation(opened, permutation)
        record = {
            "batch_id": batch_id,
            "input_hash": input_hash,
            "output_hash": object_hash("mix-stage-output", output),
            "permutation": permutation,
        }
        existing = [
            item
            for item in self.state["mix_records"]
            if item.get("batch_id") == batch_id
        ]
        if existing:
            _require(
                len(existing) == 1 and existing[0] == record,
                "CONTEXT_MISMATCH",
                "mix batch was already processed with different input",
            )
        else:
            self.state["mix_records"].append(record)
            self._save()
        return output

    def reveal(self) -> dict[str, Any]:
        return {
            "assigned": self.state.get("assigned"),
            "ceremony_id": self.ceremony_id,
            "mix_records": list(self.state["mix_records"]),
            "private_records": {
                "queries": self.state.get("queries") or {},
                "role_team": self.state.get("role_team"),
                "seer_results": self.state.get("seer_results") or [],
            },
            "profile_id": PROFILE_ID,
            "schema": "aigenora-hidden-role-peer-reveal/1",
            "seat": self.seat,
            "secrets": self._secret_body(),
            "shuffle_record": self.state.get("shuffle_record"),
        }


def combine_tie_seed(descriptors: list[dict[str, Any]], tie_shares: list[str]) -> str:
    _require(len(descriptors) == len(tie_shares) == 7, "AUDIT_INCOMPLETE", "seven tie shares are required")
    values = []
    for seat, (descriptor, share_hex) in enumerate(zip(descriptors, tie_shares)):
        validate_descriptor(descriptor, expected_seat=seat)
        try:
            share = bytes.fromhex(share_hex)
        except ValueError as exc:
            raise HiddenRoleError("NON_CANONICAL", "tie share is not hexadecimal") from exc
        _require(len(share) == 32, "NON_CANONICAL", "tie share must be 32 bytes")
        _require(
            hashlib.sha256(share).hexdigest() == descriptor["tie_share_commitment"],
            "COMMITMENT_MISMATCH",
            "tie share does not match commitment",
        )
        values.append(share)
    return _xor_all(values).hex()


def deterministic_tie_break(tied_seats: list[int], *, tie_seed: str, day: int) -> int:
    candidates = sorted(set(tied_seats))
    _require(candidates and all(0 <= seat < 7 for seat in candidates), "NON_CANONICAL", "invalid tie candidates")
    try:
        seed = bytes.fromhex(tie_seed)
    except ValueError as exc:
        raise HiddenRoleError("NON_CANONICAL", "tie seed is not hexadecimal") from exc
    _require(len(seed) == 32, "NON_CANONICAL", "tie seed must be 32 bytes")
    digest = hmac.new(seed, f"werewolf-day-tie/{day}/".encode("ascii") + bytes(candidates), hashlib.sha256).digest()
    return candidates[int.from_bytes(digest, "big") % len(candidates)]


def descriptor_rings(registry: list[dict[str, Any]], eliminated_keys: Iterable[str] = ()) -> dict[str, list[str]]:
    eliminated = set(eliminated_keys)
    active = [item for item in registry if item["credential_public_key"] not in eliminated]
    return {
        "all": sorted(item["credential_public_key"] for item in active),
        "seer": sorted(item["credential_public_key"] for item in active if item["role"] == "seer"),
        "witch": sorted(item["credential_public_key"] for item in active if item["role"] == "witch"),
        "wolf": sorted(item["credential_public_key"] for item in active if item["role"] == "wolf"),
    }


def _verify_peer_reveal(descriptor: dict[str, Any], reveal: dict[str, Any]) -> None:
    _require(
        set(reveal) == {"assigned", "ceremony_id", "mix_records", "private_records", "profile_id", "schema", "seat", "secrets", "shuffle_record"},
        "NON_CANONICAL",
        "invalid peer reveal fields",
    )
    _require(
        reveal["schema"] == "aigenora-hidden-role-peer-reveal/1"
        and reveal["profile_id"] == PROFILE_ID
        and reveal["seat"] == descriptor["seat"]
        and reveal["ceremony_id"] == descriptor["ceremony_id"],
        "CONTEXT_MISMATCH",
        "peer reveal context mismatch",
    )
    secrets_value = reveal["secrets"]
    _require(isinstance(secrets_value, dict), "NON_CANONICAL", "peer reveal secrets must be an object")
    _require(
        object_hash("peer-secret-commitment", secrets_value) == descriptor["secret_commitment"],
        "COMMITMENT_MISMATCH",
        "peer reveal does not match its commitment",
    )
    _require(
        hashlib.sha256(bytes.fromhex(secrets_value["shuffle_seed"])).hexdigest()
        == descriptor["shuffle_seed_commitment"],
        "COMMITMENT_MISMATCH",
        "shuffle seed commitment mismatch",
    )
    _require(
        hashlib.sha256(bytes.fromhex(secrets_value["mix_seed"])).hexdigest()
        == descriptor["mix_seed_commitment"],
        "COMMITMENT_MISMATCH",
        "mix seed commitment mismatch",
    )
    _require(
        hashlib.sha256(bytes.fromhex(secrets_value["tie_share"])).hexdigest()
        == descriptor["tie_share_commitment"],
        "COMMITMENT_MISMATCH",
        "tie share commitment mismatch",
    )
    exponent = _parse_nonzero_field(secrets_value["sra_exponent"], "SRA exponent")
    inverse = _parse_nonzero_field(secrets_value["sra_inverse"], "SRA inverse")
    _require(exponent * inverse % (P - 1) == 1, "COMMITMENT_MISMATCH", "SRA inverse is invalid")
    x = _parse_scalar(secrets_value["elgamal_private"], "ElGamal private share")
    _require(pow(G, x, P) == _parse_group(descriptor["elgamal_public_share"], "ElGamal public share"), "COMMITMENT_MISMATCH", "ElGamal reveal mismatch")
    private_key = x25519.X25519PrivateKey.from_private_bytes(bytes.fromhex(secrets_value["mix_private_key"]))
    _require(_public_raw(private_key.public_key()).hex() == descriptor["mix_public_key"], "COMMITMENT_MISMATCH", "mix key reveal mismatch")
    for card_id, secret_card in enumerate(secrets_value["card_secrets"]):
        public_card = descriptor["cards"][card_id]
        credential = _parse_scalar(secret_card["credential"], "credential share")
        _require(pow(G, credential, P) == _parse_group(public_card["credential_commitment"], "credential commitment"), "COMMITMENT_MISMATCH", "credential commitment mismatch")
        _require(pow(credential, exponent, P) == _parse_nonzero_field(public_card["credential_locked"], "locked credential"), "COMMITMENT_MISMATCH", "initial credential lock mismatch")


def validate_peer_reveal(
    descriptor: dict[str, Any],
    reveal: dict[str, Any],
) -> None:
    """Validate one terminal reveal against its actor-bound descriptor."""

    _verify_peer_reveal(descriptor, reveal)


def _replay_shuffle(deck: dict[str, Any], reveal: dict[str, Any]) -> dict[str, Any]:
    seat = int(reveal["seat"])
    secrets_value = reveal["secrets"]
    exponent = _parse_nonzero_field(secrets_value["sra_exponent"], "SRA exponent")
    seed = bytes.fromhex(secrets_value["shuffle_seed"])
    input_hash = object_hash("deck-stage", deck)
    transformed = []
    randomizers = []
    for card_index, card in enumerate(deck["cards"]):
        nonce = _deterministic_scalar(seed, f"shuffle/{deck['ceremony_id']}/{seat}/{input_hash}", card_index)
        randomizers.append(_scalar_hex(nonce))
        components = []
        for component in card["components"]:
            components.append(
                {
                    "contributor_seat": component["contributor_seat"],
                    "credential": _int_hex(pow(_parse_nonzero_field(component["credential"], "credential component"), exponent, P)),
                }
            )
        transformed.append(
            {
                "components": components,
                "role_ciphertext": rerandomize_role(card["role_ciphertext"], deck["joint_public_key"], randomness=nonce),
            }
        )
    permutation = _deterministic_permutation(seed, f"shuffle-permutation/{deck['ceremony_id']}/{seat}/{input_hash}", 7)
    output = {
        **{key: deck[key] for key in ("ceremony_id", "joint_public_key", "profile_id", "schema")},
        "cards": _apply_permutation(transformed, permutation),
        "stage": seat + 1,
    }
    record = reveal["shuffle_record"]
    _require(
        isinstance(record, dict)
        and record == {
            "input_hash": input_hash,
            "output_hash": object_hash("deck-stage", output),
            "permutation": permutation,
            "randomizers": randomizers,
        },
        "DECK_CONSERVATION_FAILED",
        "shuffle transcript does not match the committed seed",
    )
    return output


def _decrypt_final_assignment(
    card: dict[str, Any], *, reveals: list[dict[str, Any]], registry: list[dict[str, Any]]
) -> dict[str, Any]:
    raw_components = []
    for component in card["components"]:
        contributor = int(component["contributor_seat"])
        values = {}
        for name in ("credential",):
            transformed = _parse_nonzero_field(component[name], name)
            for reveal in reveals:
                inverse = _parse_nonzero_field(reveal["secrets"]["sra_inverse"], "SRA inverse")
                transformed = pow(transformed, inverse, P)
            contributor_inverse = _parse_nonzero_field(reveals[contributor]["secrets"]["sra_inverse"], "SRA inverse")
            transformed = pow(transformed, contributor_inverse, P)
            _require(0 < transformed < Q, "DECK_CONSERVATION_FAILED", "decrypted component is outside scalar range")
            values[name] = transformed
        raw_components.append({"contributor_seat": contributor, **values})
    credential_secret = sum(item["credential"] for item in raw_components) % Q
    credential_public = _int_hex(pow(G, credential_secret, P))
    matches = [item for item in registry if item["credential_public_key"] == credential_public]
    _require(len(matches) == 1, "DECK_CONSERVATION_FAILED", "final card does not map to one credential")
    registry_entry = matches[0]
    a = _parse_group(card["role_ciphertext"]["a"], "role ciphertext a")
    b = _parse_group(card["role_ciphertext"]["b"], "role ciphertext b")
    private_sum = sum(_parse_scalar(item["secrets"]["elgamal_private"], "ElGamal private share") for item in reveals) % Q
    message = b * pow(pow(a, private_sum, P), -1, P) % P
    roles = [role for role, code in ROLE_CODES.items() if message == pow(G, code, P)]
    _require(len(roles) == 1 and roles[0] == registry_entry["role"], "ROLE_MISMATCH", "final role ciphertext mismatch")
    return {
        "card_id": registry_entry["card_id"],
        "credential_public_key": credential_public,
        "role": roles[0],
    }


def verify_terminal_artifact(value: dict[str, Any]) -> TerminalVerification:
    required = {
        "anonymous_batches",
        "ceremony_id",
        "descriptors",
        "final_deck",
        "initial_deck",
        "initial_role_randomness",
        "peer_reveals",
        "profile_id",
        "registry",
        "roles",
        "schema",
        "seer_deliveries",
        "shuffle_decks",
        "tie_seed",
        "tie_shares",
    }
    _require(set(value) == required, "NON_CANONICAL", "invalid terminal artifact fields")
    _require(value["schema"] == ARTIFACT_SCHEMA and value["profile_id"] == PROFILE_ID, "CONTEXT_MISMATCH", "wrong terminal artifact profile")
    descriptors = value["descriptors"]
    reveals = value["peer_reveals"]
    roles = tuple(value["roles"])
    _require(isinstance(descriptors, list) and isinstance(reveals, list) and len(descriptors) == len(reveals) == 7, "AUDIT_INCOMPLETE", "terminal artifact requires seven peers")
    for seat, (descriptor, reveal) in enumerate(zip(descriptors, reveals)):
        validate_descriptor(descriptor, expected_seat=seat)
        _verify_peer_reveal(descriptor, reveal)
    expected_registry = build_registry(descriptors, roles)
    _require(value["registry"] == expected_registry, "COMMITMENT_MISMATCH", "credential registry mismatch")
    expected_tie_seed = combine_tie_seed(descriptors, value["tie_shares"])
    _require(value["tie_seed"] == expected_tie_seed, "COMMITMENT_MISMATCH", "tie seed mismatch")
    expected_initial, expected_randomness = build_seeded_initial_deck(
        descriptors,
        tie_seed=expected_tie_seed,
        roles=roles,
    )
    _require(
        value["initial_role_randomness"] == expected_randomness,
        "COMMITMENT_MISMATCH",
        "initial role randomness is not derived from the committed tie seed",
    )
    _require(value["initial_deck"] == expected_initial, "DECK_CONSERVATION_FAILED", "initial deck mismatch")
    shuffle_decks = value["shuffle_decks"]
    _require(isinstance(shuffle_decks, list) and len(shuffle_decks) == 7, "AUDIT_INCOMPLETE", "seven shuffle decks are required")
    deck = expected_initial
    for seat, reveal in enumerate(reveals):
        deck = _replay_shuffle(deck, reveal)
        _require(deck == shuffle_decks[seat], "DECK_CONSERVATION_FAILED", f"shuffle stage {seat} mismatch")
    _require(deck == value["final_deck"], "DECK_CONSERVATION_FAILED", "final deck mismatch")
    assignments = []
    for seat, card in enumerate(deck["cards"]):
        assignment = {
            "seat": seat,
            **_decrypt_final_assignment(card, reveals=reveals, registry=expected_registry),
        }
        claimed = reveals[seat]["assigned"]
        _require(isinstance(claimed, dict), "AUDIT_INCOMPLETE", "peer did not reveal its assignment")
        _require(
            set(claimed)
            == {"card_id", "credential_private", "credential_public", "role"},
            "NON_CANONICAL",
            "invalid claimed assignment fields",
        )
        claimed_private = _parse_scalar(
            claimed["credential_private"], "claimed credential private key"
        )
        _require(
            claimed["card_id"] == assignment["card_id"]
            and claimed["credential_public"]
            == assignment["credential_public_key"]
            and claimed["role"] == assignment["role"]
            and pow(G, claimed_private, P)
            == _parse_group(
                assignment["credential_public_key"],
                "assigned credential public key",
            ),
            "ROLE_MISMATCH",
            "peer assignment does not match the audited deck",
        )
        assignments.append(assignment)
    _require(sorted(item["role"] for item in assignments) == sorted(roles), "DECK_CONSERVATION_FAILED", "role multiset mismatch")
    batches = value["anonymous_batches"]
    _require(isinstance(batches, list), "NON_CANONICAL", "anonymous_batches must be a list")
    for batch in batches:
        _verify_mix_batch(batch, descriptors=descriptors, reveals=reveals)
    deliveries = value["seer_deliveries"]
    _require(isinstance(deliveries, list), "NON_CANONICAL", "seer_deliveries must be a list")
    seen_queries: set[str] = set()
    for delivery in deliveries:
        query_id = _verify_seer_delivery(
            delivery,
            descriptors=descriptors,
            reveals=reveals,
            final_deck=deck,
            assignments=assignments,
        )
        _require(query_id not in seen_queries, "DUPLICATE_KEY_IMAGE", "duplicate seer query delivery")
        seen_queries.add(query_id)
    recorded_queries = {
        item["query_id"]
        for reveal in reveals
        for item in (reveal.get("private_records", {}).get("seer_results") or [])
        if isinstance(item, dict) and item.get("verified") is True
    }
    _require(
        seen_queries == recorded_queries,
        "AUDIT_INCOMPLETE",
        "terminal artifact omits or invents a verified seer delivery",
    )
    return TerminalVerification(
        status="verified",
        artifact_hash=object_hash("terminal-artifact", value),
        assignments=tuple(assignments),
        batch_count=len(batches),
    )


def _verify_seer_delivery(
    delivery: dict[str, Any],
    *,
    descriptors: list[dict[str, Any]],
    reveals: list[dict[str, Any]],
    final_deck: dict[str, Any],
    assignments: list[dict[str, Any]],
) -> str:
    _require(
        isinstance(delivery, dict)
        and set(delivery) == {"card", "delivery_hash", "query", "sealed_shares"},
        "NON_CANONICAL",
        "invalid seer delivery fields",
    )
    query = delivery["query"]
    _require(
        isinstance(query, dict)
        and set(query) == {"query_id", "reply_public_key", "target_seat"},
        "NON_CANONICAL",
        "invalid seer query fields",
    )
    query_id = query["query_id"]
    _require(isinstance(query_id, str) and len(query_id) == 64, "NON_CANONICAL", "invalid seer query id")
    target_seat = query["target_seat"]
    _require(
        isinstance(target_seat, int) and not isinstance(target_seat, bool) and 0 <= target_seat < 7,
        "NON_CANONICAL",
        "invalid seer target seat",
    )
    card = delivery["card"]
    _require(card == final_deck["cards"][target_seat], "CONTEXT_MISMATCH", "seer query card does not match target")
    sealed_shares = delivery["sealed_shares"]
    _require(isinstance(sealed_shares, list) and len(sealed_shares) == 7, "AUDIT_INCOMPLETE", "seer delivery requires seven shares")
    expected_delivery_hash = object_hash(
        "seer-delivery", {"query": query, "sealed_shares": sealed_shares}
    )
    _require(
        delivery["delivery_hash"] == expected_delivery_hash,
        "COMMITMENT_MISMATCH",
        "seer delivery hash mismatch",
    )
    holders = []
    for reveal in reveals:
        records = reveal.get("private_records")
        queries = records.get("queries") if isinstance(records, dict) else None
        if isinstance(queries, dict) and isinstance(queries.get(query_id), dict):
            holders.append((reveal, queries[query_id]))
    _require(len(holders) == 1, "AUDIT_INCOMPLETE", "seer query must have one private recipient")
    seer_reveal, record = holders[0]
    seer_seat = int(seer_reveal["seat"])
    _require(assignments[seer_seat]["role"] == "seer", "ROLE_MISMATCH", "role query recipient is not the seer")
    _require(
        target_seat != seer_seat
        and record.get("target_seat") == target_seat
        and record.get("status") == "verified",
        "CONTEXT_MISMATCH",
        "seer query private record is invalid",
    )
    try:
        reply_private = x25519.X25519PrivateKey.from_private_bytes(
            bytes.fromhex(record["private_key"])
        )
    except (ValueError, TypeError) as exc:
        raise HiddenRoleError("NON_CANONICAL", "seer reply private key is invalid") from exc
    _require(
        _public_raw(reply_private.public_key()).hex() == query["reply_public_key"],
        "COMMITMENT_MISMATCH",
        "seer reply key does not match the query",
    )
    card_hash = object_hash(
        "queried-role-ciphertext",
        {"query_id": query_id, "role_ciphertext": card["role_ciphertext"]},
    )
    shares = []
    for guardian_seat, envelope in enumerate(sealed_shares):
        plaintext = open_direct(
            record["private_key"],
            envelope,
            context=f"query-role-share/{seer_reveal['ceremony_id']}/{query_id}/{guardian_seat}",
        )
        try:
            share = json.loads(plaintext.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise HiddenRoleError("NON_CANONICAL", "seer share plaintext is invalid") from exc
        _require(
            isinstance(share, dict)
            and share.get("card_hash") == card_hash
            and share.get("recipient_seat") == -1,
            "CONTEXT_MISMATCH",
            "seer share binding mismatch",
        )
        shares.append(share)
    role = combine_role(card["role_ciphertext"], shares, descriptors)
    _require(role == assignments[target_seat]["role"], "ROLE_MISMATCH", "seer result role is false")
    results = seer_reveal["private_records"].get("seer_results")
    matching_results = [
        item
        for item in results
        if isinstance(item, dict) and item.get("query_id") == query_id
    ] if isinstance(results, list) else []
    _require(
        len(matching_results) == 1
        and matching_results[0].get("verified") is True
        and matching_results[0].get("role") == role
        and matching_results[0].get("target_seat") == target_seat,
        "ROLE_MISMATCH",
        "seer private result does not match verified shares",
    )
    return query_id


def _verify_mix_batch(
    batch: dict[str, Any], *, descriptors: list[dict[str, Any]], reveals: list[dict[str, Any]]
) -> None:
    _require(
        set(batch) == {"batch_id", "ingress", "plaintexts", "stages"},
        "NON_CANONICAL",
        "invalid mix batch fields",
    )
    batch_id = batch["batch_id"]
    current = batch["ingress"]
    stages = batch["stages"]
    _require(isinstance(batch_id, str) and batch_id, "NON_CANONICAL", "invalid batch id")
    _require(isinstance(current, list) and current, "MIX_CONSERVATION_FAILED", "mix ingress is empty")
    _require(isinstance(stages, list) and len(stages) == 7, "AUDIT_INCOMPLETE", "mix requires seven stages")
    ceremony_id = descriptors[0]["ceremony_id"]
    for seat in range(7):
        input_hash = object_hash("mix-stage-input", current)
        opened = [
            onion_open_layer(
                envelope,
                private_key_hex=reveals[seat]["secrets"]["mix_private_key"],
                ceremony_id=ceremony_id,
                batch_id=batch_id,
                layer=seat,
            )
            for envelope in current
        ]
        seed = bytes.fromhex(reveals[seat]["secrets"]["mix_seed"])
        permutation = _deterministic_permutation(
            seed,
            f"mix/{ceremony_id}/{batch_id}/{seat}/{input_hash}",
            len(opened),
        )
        output = _apply_permutation(opened, permutation)
        expected_stage = {
            "input_hash": input_hash,
            "output": output,
            "output_hash": object_hash("mix-stage-output", output),
            "seat": seat,
        }
        _require(stages[seat] == expected_stage, "MIX_CONSERVATION_FAILED", f"mix stage {seat} mismatch")
        records = [item for item in reveals[seat]["mix_records"] if item.get("batch_id") == batch_id]
        _require(
            len(records) == 1
            and records[0]
            == {
                "batch_id": batch_id,
                "input_hash": input_hash,
                "output_hash": expected_stage["output_hash"],
                "permutation": permutation,
            },
            "MIX_CONSERVATION_FAILED",
            f"peer {seat} mix record mismatch",
        )
        current = output
    plaintexts = [onion_decode_plaintext(item) for item in current]
    _require(plaintexts == batch["plaintexts"], "MIX_CONSERVATION_FAILED", "mix plaintext transcript mismatch")


def verify_terminal_artifact_file(path: str | Path) -> TerminalVerification:
    return verify_terminal_artifact(_load_object(Path(path)))


__all__ = [
    "ARTIFACT_SCHEMA",
    "CODE_ROLES",
    "DESCRIPTOR_SCHEMA",
    "G",
    "HiddenRoleError",
    "HiddenRolePeer",
    "P",
    "PROFILE_ID",
    "Q",
    "ROLE_CODES",
    "TerminalVerification",
    "WEREWOLF_ROLES",
    "build_initial_deck",
    "build_seeded_initial_deck",
    "build_registry",
    "canonical_json_bytes",
    "combine_role",
    "combine_tie_seed",
    "descriptor_rings",
    "deterministic_tie_break",
    "derive_two_member_team_key",
    "joint_public_key",
    "object_hash",
    "onion_decode_plaintext",
    "ring_sign",
    "schnorr_sign",
    "verify_anonymous_messages",
    "validate_peer_reveal",
    "verify_ring_signature",
    "verify_schnorr",
    "verify_terminal_artifact",
    "verify_terminal_artifact_file",
]
