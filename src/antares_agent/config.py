"""Process-wide settings, read from the environment once at startup.

Deliberately not pydantic-settings: there are a dozen knobs, they are all read
by the systemd unit, and a plain dataclass keeps the import graph small on a Pi.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "ANTARES_"

#: Paths that must never be readable by a Bash tool call. The sandbox only
#: blocks *writes* outside cwd (F19), so reads of these have to be denied by
#: rule. Global, not per-repo -- see 01-workspace-manifest.md.
DEFAULT_SECRET_PATHS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.config/gh",
    "~/.gnupg",
    "~/.netrc",
    "~/.docker/config.json",
    "/run/secrets",
    # agenix decrypts here on the deployment host. Unit-level perms already
    # keep the service uid out, but the deny costs nothing and the path is not
    # obvious from the code.
    "/run/agenix",
)


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


def _env_path(name: str, default: str | None = None) -> Path | None:
    raw = _env(name, default)
    return Path(raw).expanduser() if raw else None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    return raw.strip().lower() in {"1", "true", "yes", "on"} if raw else default


@dataclass(frozen=True)
class Settings:
    #: Constant cwd for every session (D2). Repos live directly under it.
    workspace: Path = field(default_factory=lambda: Path("~/agent_work").expanduser())

    #: Pin the CLI explicitly rather than inherit `_find_cli()`'s resolution
    #: order, which prefers the wheel's bundled binary over PATH (F9).
    cli_path: Path | None = None

    #: Model gateway. Localhost TCP, not a unix socket -- ANTHROPIC_BASE_URL
    #: cannot parse socket URLs (F21). Credential isolation comes from the
    #: gateway running as a separate unix user, not from the transport.
    gateway_base_url: str | None = None
    gateway_auth_token: str | None = None

    #: Alias tier -> concrete model id, for endpoints that never heard of
    #: Anthropic's names. `profile.model` only covers the main model; the CLI
    #: resolves the `opus`/`sonnet`/`haiku` aliases itself for subagents and for
    #: its own background haiku calls, and those requests go out unmapped.
    #: Keys are the tier names as the CLI spells them (`OPUS`/`SONNET`/`HAIKU`).
    model_aliases: dict[str, str] = field(default_factory=dict)

    host: str = "127.0.0.1"
    port: int = 60001

    #: Listen on a unix socket instead of TCP. Set in production: the relay is
    #: the only client, and a socket hands authorisation to the filesystem
    #: rather than to code we would otherwise have to write. Without it the API
    #: is reachable by every uid on the host that can open a loopback socket.
    #: F21 does not apply here -- that is about ANTHROPIC_BASE_URL, not our own
    #: listener.
    socket_path: Path | None = None

    db_path: Path = field(
        default_factory=lambda: Path("~/agent_work/.agent/antares.db").expanduser()
    )
    profiles_dir: Path = field(
        default_factory=lambda: Path("~/agent_work/.agent/profiles").expanduser()
    )

    approval_timeout_s: int = 600

    #: V4 measured ~218MB for the first live client and ~123MB per additional
    #: one (PSS). 6 keeps a 8GB Pi comfortable.
    max_live_threads: int = 6

    #: Seconds a thread may sit idle before its CLI is closed. Not a memory
    #: knob -- a CPU one: after its first turn the CLI spins a ~60Hz loop and
    #: costs a full core for as long as it lives (F31). Eviction is lossless
    #: (`resume`), so the price is the revive on the next message. 0 disables.
    idle_ttl_s: int = 300

    #: Events kept in memory per thread for `?after=` replay; older ones are
    #: served from sqlite.
    event_buffer: int = 512

    secret_paths: tuple[str, ...] = DEFAULT_SECRET_PATHS

    #: Refuse to start if the sandbox self-check fails (F23: the sandbox fails
    #: *open* when bwrap or socat is missing -- a warning line, no exception).
    #: Only turn this off for local development on a machine without bwrap.
    require_sandbox: bool = True

    extra_denied_tools: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> Settings:
        workspace = _env_path("WORKSPACE", "~/agent_work")
        assert workspace is not None
        token = _env("GATEWAY_TOKEN")
        token_file = _env_path("GATEWAY_TOKEN_FILE")
        if not token and token_file:
            token = token_file.read_text().strip()
        extra_deny = _env("EXTRA_DENY") or ""
        aliases = {tier: _env(f"MODEL_{tier}") for tier in ("OPUS", "SONNET", "HAIKU")}
        return cls(
            workspace=workspace,
            cli_path=_env_path("CLI_PATH"),
            gateway_base_url=_env("GATEWAY_URL"),
            gateway_auth_token=token,
            model_aliases={t: m for t, m in aliases.items() if m},
            host=_env("HOST", "127.0.0.1") or "127.0.0.1",
            port=_env_int("PORT", 60001),
            socket_path=_env_path("SOCKET"),
            db_path=_env_path("DB_PATH") or workspace / ".agent" / "antares.db",
            profiles_dir=_env_path("PROFILES_DIR") or workspace / ".agent" / "profiles",
            approval_timeout_s=_env_int("APPROVAL_TIMEOUT", 600),
            max_live_threads=_env_int("MAX_LIVE_THREADS", 6),
            idle_ttl_s=_env_int("IDLE_TTL", 300),
            event_buffer=_env_int("EVENT_BUFFER", 512),
            require_sandbox=_env_bool("REQUIRE_SANDBOX", True),
            extra_denied_tools=tuple(x for x in extra_deny.split(",") if x.strip()),
        )

    @property
    def manifest_path(self) -> Path:
        return self.workspace / "workspace.toml"
