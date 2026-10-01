from __future__ import annotations

import json
import socket
from pathlib import Path


class TokenClient:
    def __init__(self, path: Path):
        self.path = path
        self.generation = ""
        self.account: str | None = None

    def fetch(self, refresh=False, account=None):
        request = {
            "refresh": refresh,
            "generation": self.generation,
            "account": account or self.account,
        }
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(8)
            connection.connect(str(self.path))
            connection.sendall(json.dumps(request).encode() + b"\n")
            with connection.makefile("rb") as stream:
                line = stream.readline(65537)
        if len(line) > 65536 or not line.endswith(b"\n"):
            raise RuntimeError("认证服务返回无效响应")
        value = json.loads(line)
        if value.get("error"):
            raise RuntimeError("认证服务不可用，请检查统一登录服务")
        for key in ("accessToken", "chatgptAccountId", "generation"):
            if not isinstance(value.get(key), str) or not value[key]:
                raise RuntimeError("认证服务返回不完整令牌")
        expected = account or self.account
        if expected and expected != value["chatgptAccountId"]:
            raise RuntimeError("认证账户已变更，请重启 Agent")
        self.account = value["chatgptAccountId"]
        self.generation = value.pop("generation")
        return value
