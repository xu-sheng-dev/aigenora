from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from aigenora.engine.config import data_dir as resolve_data_dir
from aigenora.engine.config import get_server

if TYPE_CHECKING:
    from aigenora.engine.keys import KeyPair
    from aigenora.engine.rest import RestClient


@dataclass(frozen=True)
class ServiceContext:
    """Startup-fixed resources for CLI and Runtime service calls.

    Runtime requests never get to replace these values.  This is the boundary
    that prevents a request payload from selecting another data directory,
    server, key file, or HTTP destination.
    """

    data_dir: Path
    server_url: str

    @classmethod
    def create(cls, data_dir: str | Path | None, server_url: str | None) -> "ServiceContext":
        root = (
            Path(data_dir).expanduser().resolve()
            if data_dir is not None
            else resolve_data_dir(None).resolve()
        )
        return cls(data_dir=root, server_url=get_server(server_url).rstrip("/"))

    def keys(self) -> "KeyPair":
        from aigenora.engine.keys import load_keys

        return load_keys(str(self.data_dir))

    def rest(self, timeout: float = 30.0) -> "RestClient":
        from aigenora.engine.rest import RestClient

        return RestClient(self.server_url, self.keys(), timeout=timeout)
