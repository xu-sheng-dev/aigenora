from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import NON_CANONICAL, VsdpError


MAX_SAFE_INTEGER = (1 << 53) - 1


def _validate_string(value: str, path: str) -> None:
    for character in value:
        code_point = ord(character)
        if code_point < 0x20:
            raise VsdpError(NON_CANONICAL, f"control character is forbidden at {path}")
        if 0xD800 <= code_point <= 0xDFFF:
            raise VsdpError(NON_CANONICAL, f"unpaired surrogate is forbidden at {path}")


def _reject_float(value: str) -> None:
    raise VsdpError(NON_CANONICAL, f"floating-point number is forbidden: {value}")


def _reject_constant(value: str) -> None:
    raise VsdpError(NON_CANONICAL, f"non-finite number is forbidden: {value}")


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VsdpError(NON_CANONICAL, f"duplicate object key: {key}")
        result[key] = value
    return result


def _validate_value(value: Any, path: str = "$") -> None:
    if value is None:
        raise VsdpError(NON_CANONICAL, f"null is forbidden at {path}")
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise VsdpError(NON_CANONICAL, f"integer outside safe range at {path}")
        return
    if isinstance(value, float):
        raise VsdpError(NON_CANONICAL, f"floating-point number is forbidden at {path}")
    if isinstance(value, str):
        _validate_string(value, path)
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise VsdpError(NON_CANONICAL, f"non-string object key at {path}")
            _validate_string(key, f"{path}.<key>")
            _validate_value(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        for index, child in enumerate(value):
            _validate_value(child, f"{path}[{index}]")
        return
    raise VsdpError(NON_CANONICAL, f"unsupported value type at {path}: {type(value).__name__}")


def canonical_json_text(value: Any) -> str:
    """Serialize the restricted VSDP JSON subset into unique UTF-8 text."""

    _validate_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json_text(value).encode("utf-8")


def parse_canonical_json(raw: bytes | str, *, require_canonical: bool = True) -> Any:
    """Parse JSON while rejecting duplicate keys, floats, null, and drift."""

    if isinstance(raw, bytes):
        if raw.startswith(b"\xef\xbb\xbf"):
            raise VsdpError(NON_CANONICAL, "UTF-8 BOM is forbidden")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise VsdpError(NON_CANONICAL, f"invalid UTF-8: {exc}") from exc
    else:
        text = raw
        if text.startswith("\ufeff"):
            raise VsdpError(NON_CANONICAL, "UTF-8 BOM is forbidden")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except VsdpError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise VsdpError(NON_CANONICAL, f"invalid JSON: {exc}") from exc

    _validate_value(value)
    if require_canonical and canonical_json_text(value) != text:
        raise VsdpError(NON_CANONICAL, "JSON bytes are not canonical")
    return value


def domain_hash(tag: str, value: Any) -> bytes:
    try:
        tag_bytes = tag.encode("ascii", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("domain tag must be ASCII") from exc
    return hashlib.sha256(tag_bytes + b"\x00" + canonical_json_bytes(value)).digest()


def domain_hash_hex(tag: str, value: Any) -> str:
    return domain_hash(tag, value).hex()


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(value: str, *, expected_length: int | None = None) -> bytes:
    if not isinstance(value, str) or not value:
        raise VsdpError(NON_CANONICAL, "base64url value must be a non-empty string")
    if "=" in value:
        raise VsdpError(NON_CANONICAL, "base64url padding is forbidden")
    try:
        raw = base64.b64decode(
            value + "=" * ((4 - len(value) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise VsdpError(NON_CANONICAL, "invalid base64url value") from exc
    if b64u_encode(raw) != value:
        raise VsdpError(NON_CANONICAL, "non-canonical base64url value")
    if expected_length is not None and len(raw) != expected_length:
        raise VsdpError(
            NON_CANONICAL,
            f"base64url value must decode to {expected_length} bytes",
        )
    return raw


def require_hex(value: Any, length: int, field: str) -> str:
    if not isinstance(value, str) or len(value) != length:
        raise VsdpError(NON_CANONICAL, f"{field} must be {length} lowercase hex characters")
    if value.lower() != value:
        raise VsdpError(NON_CANONICAL, f"{field} must use lowercase hex")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise VsdpError(NON_CANONICAL, f"{field} is not valid hex") from exc
    return value
