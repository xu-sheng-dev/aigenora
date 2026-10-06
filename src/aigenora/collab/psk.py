"""PSK pairing material and trusted-peer registry for cross-device collaboration.

Implements the M1a pairing baseline from
docs/design/cross-device-agent-collaboration.md §4.1:

- A 32-byte high-entropy PSK file (``collab/psk.json``) shared out-of-band by
  the user between two devices. Never placed in prompts, command lines, env
  vars, logs, or protocol options.
- A small local trusted-peer registry (``collab/peers.json``) pinning the
  expected community public key of each peer device plus the PSK key_id/epoch.
- Community signing keys (``key.json``) stay independent per device; the PSK
  is admission material only and is never a substitute for network reach or
  local policy.
"""
from __future__ import annotations

import json
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from aigenora.engine.config import data_dir

PSK_FORMAT = "aigenora-psk-v1"
PEERS_FORMAT = "aigenora-collab-peers-v1"


def collab_root(data_dir_value: str | None = None) -> Path:
    return data_dir(data_dir_value) / "collab"


def psk_path(data_dir_value: str | None = None) -> Path:
    return collab_root(data_dir_value) / "psk.json"


def peers_path(data_dir_value: str | None = None) -> Path:
    return collab_root(data_dir_value) / "peers.json"


@dataclass(frozen=True)
class PreSharedKey:
    key_id: str
    key_epoch: int
    secret: bytes  # 32 bytes, never logged or serialized outside psk.json

    def material(self) -> bytes:
        return self.secret


def generate_psk(out_path: Path | None = None, key_epoch: int = 1) -> PreSharedKey:
    """Generate a fresh 32-byte PSK file from the OS CSPRNG."""
    path = out_path or psk_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"PSK already exists at {path}; move it away first")
    psk = PreSharedKey(
        key_id=secrets.token_hex(8),
        key_epoch=int(key_epoch),
        secret=secrets.token_bytes(32),
    )
    payload = {
        "format": PSK_FORMAT,
        "key_id": psk.key_id,
        "key_epoch": psk.key_epoch,
        "secret": psk.secret.hex(),
        "created_at": int(time.time()),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _harden(path)
    return psk


def load_psk(data_dir_value: str | None = None, path: Path | None = None) -> PreSharedKey:
    target = path or psk_path(data_dir_value)
    raw = json.loads(target.read_text(encoding="utf-8"))
    if raw.get("format") != PSK_FORMAT:
        raise ValueError("unsupported PSK file format")
    secret = bytes.fromhex(raw["secret"])
    if len(secret) != 32:
        raise ValueError("PSK secret must be 32 bytes")
    return PreSharedKey(
        key_id=str(raw["key_id"]),
        key_epoch=int(raw["key_epoch"]),
        secret=secret,
    )


@dataclass
class PeerEntry:
    alias: str
    public_key: str  # community Ed25519 public key (64 hex)
    key_id: str
    key_epoch: int
    disabled: bool = False
    trusted_at: int = field(default_factory=lambda: int(time.time()))
    note: str = ""


class PeerRegistry:
    def __init__(self, data_dir_value: str | None = None) -> None:
        self.path = peers_path(data_dir_value)
        self._peers: dict[str, PeerEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("format") != PEERS_FORMAT:
            raise ValueError("unsupported peers file format")
        for item in raw.get("peers", []):
            entry = PeerEntry(
                alias=str(item["alias"]),
                public_key=str(item["public_key"]).lower(),
                key_id=str(item.get("key_id", "")),
                key_epoch=int(item.get("key_epoch", 1)),
                disabled=bool(item.get("disabled", False)),
                trusted_at=int(item.get("trusted_at", 0)),
                note=str(item.get("note", "")),
            )
            self._peers[entry.public_key] = entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": PEERS_FORMAT,
            "peers": [
                {
                    "alias": p.alias,
                    "public_key": p.public_key,
                    "key_id": p.key_id,
                    "key_epoch": p.key_epoch,
                    "disabled": p.disabled,
                    "trusted_at": p.trusted_at,
                    "note": p.note,
                }
                for p in self._peers.values()
            ],
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def trust(self, entry: PeerEntry) -> None:
        self._peers[entry.public_key] = entry
        self.save()

    def by_alias(self, alias: str) -> PeerEntry | None:
        for peer in self._peers.values():
            if peer.alias == alias:
                return peer
        return None

    def by_public_key(self, public_key: str) -> PeerEntry | None:
        return self._peers.get(public_key.lower())

    def is_allowed(self, public_key: str, key_id: str, key_epoch: int) -> bool:
        peer = self.by_public_key(public_key)
        if peer is None or peer.disabled:
            return False
        return peer.key_id == key_id and peer.key_epoch == key_epoch

    def all(self) -> list[PeerEntry]:
        return list(self._peers.values())


def _harden(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows: rely on NTFS ACLs on the user profile directory.
        pass
