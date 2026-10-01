"""Entry point. `python -m antares_agent` or the `antares-agent` script."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .config import Settings
from .preflight import SandboxUnavailable
from .preflight import run as run_preflight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="antares-agent")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the Codex sandbox self-check and exit",
    )
    parser.add_argument(
        "--migrate-workspace", action="store_true", help="preview native skill migration"
    )
    parser.add_argument("--apply", action="store_true", help="apply migration with backups")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    settings = Settings.from_env()

    if args.apply and not args.migrate_workspace:
        parser.error("--apply requires --migrate-workspace")
    if args.migrate_workspace:
        from .migrate import migrate

        try:
            moves, backup = migrate(settings.workspace, args.apply)
        except (OSError, ValueError) as exc:
            print(exc, file=sys.stderr)
            return 1
        for source, target in moves:
            print(f"{source} → {target}")
        print(f"备份：{backup}" if backup else "预览完成；使用 --apply 执行迁移")
        return 0

    if args.check:
        try:
            report = run_preflight(settings)
        except SandboxUnavailable as exc:
            print(exc, file=sys.stderr)
            return 1
        for note in report.notes:
            print(f"ok   {note}")
        for problem in report.problems:
            print(f"WARN {problem}", file=sys.stderr)
        return 0

    from .api import create_app

    app = create_app(settings)
    if settings.socket_path is not None and not (args.host or args.port):
        uvicorn.run(app, uds=str(settings.socket_path), log_level=args.log_level)
    else:
        uvicorn.run(
            app,
            host=args.host or settings.host,
            port=args.port or settings.port,
            log_level=args.log_level,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
