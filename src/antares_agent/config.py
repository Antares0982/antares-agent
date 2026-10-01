from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    workspace: Path = field(default_factory=lambda: Path("~/agent_work").expanduser())
    cli_path: Path | None = None
    codex_home: Path = field(default_factory=lambda: Path("~/.codex-antares").expanduser())
    auth_socket: Path | None = None
    resources_dir: Path | None = None
    host: str = "127.0.0.1"
    port: int = 60001
    socket_path: Path | None = None
    db_path: Path = field(
        default_factory=lambda: Path("~/.local/state/antares-codex/antares.db").expanduser()
    )
    profiles_dir: Path = field(
        default_factory=lambda: Path("~/.local/state/antares-codex/profiles").expanduser()
    )
    max_live_threads: int = 6
    idle_ttl_s: int = 300
    event_buffer: int = 512
    require_sandbox: bool = True
    max_file_bytes: int = 2_000_000_000

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            key = {"socket_path": "SOCKET", "idle_ttl_s": "IDLE_TTL"}.get(name, name.upper())
            raw = os.environ.get("ANTARES_" + key)
            if not raw:
                continue
            default = getattr(defaults, name)
            if isinstance(default, bool):
                values[name] = raw.lower() in {"1", "true", "yes", "on"}
            elif isinstance(default, int):
                values[name] = int(raw)
            elif isinstance(default, Path) or name in {
                "cli_path",
                "socket_path",
                "auth_socket",
                "resources_dir",
            }:
                values[name] = Path(raw).expanduser()
            else:
                values[name] = raw
        result = cls(**values)
        if result.max_live_threads < 1 or result.event_buffer < 1:
            raise ValueError("并发数和事件缓冲必须大于零")
        return result

    @property
    def manifest_path(self) -> Path:
        return self.workspace / "workspace.toml"

    @property
    def artifact_path(self) -> Path:
        return self.db_path.parent / "artifacts"
