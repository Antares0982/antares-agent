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
        help="run the startup self-check and exit (see F23: the sandbox fails open)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    settings = Settings.from_env()

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

    uvicorn.run(
        create_app(settings),
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
