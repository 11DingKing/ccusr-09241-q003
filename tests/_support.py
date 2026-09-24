"""测试公共辅助：UTC 时间构造与本地 HTTP API 客户端。"""

from __future__ import annotations

import http.client
import json
from datetime import datetime, timezone


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())


class ApiClient:
    def __init__(self, server) -> None:
        self.host, self.port = server.server_address[:2]

    def request(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        payload = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        )
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        data = json.loads(raw) if raw else None
        return resp.status, data
