from __future__ import annotations

import json
from typing import Any

import httpx

from .keys import KeyPair, signed_request_headers


class RestClient:
    def __init__(self, server: str, keys: KeyPair, timeout: float = 30.0):
        self.server = server.rstrip("/")
        self.keys = keys
        self.timeout = timeout

    def _body(self, payload: Any | None) -> bytes:
        if payload is None:
            return b""
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def request(self, method: str, path: str, payload: Any | None = None) -> httpx.Response:
        body = self._body(payload)
        headers = signed_request_headers(self.keys, body, method=method, path=path)
        url = f"{self.server}{path}"
        return httpx.request(method, url, content=body, headers=headers, timeout=self.timeout, trust_env=False)

    def json(self, method: str, path: str, payload: Any | None = None, expected: set[int] | None = None) -> Any:
        resp = self.request(method, path, payload)
        if expected and resp.status_code not in expected:
            raise RuntimeError(f"{method} {path} failed: HTTP {resp.status_code}: {resp.text[:500]}")
        if not resp.content:
            return None
        return resp.json()
