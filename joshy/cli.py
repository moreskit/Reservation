"""Command line entry point: ``joshy serve``, ``joshy check``, ``joshy test-notify``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Settings
from .db import Database
from .engine import Engine
from .notify import Notifier


def _engine(settings: Settings) -> Engine:
    return Engine(settings, Database(settings.data_dir), Notifier(settings))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="joshy", description="Personal reservation sniper for Resy and SevenRooms")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the dashboard and the background watcher")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("check", help="check every active watch once and exit")
    sub.add_parser("test-notify", help="send a test notification")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    if args.cmd in (None, "serve"):
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 8000)
        if not settings.password and host not in ("127.0.0.1", "localhost"):
            sys.exit("Set JOSHY_PASSWORD before exposing Joshy beyond localhost. It holds your Resy login.")
        import uvicorn

        from .web import create_app

        uvicorn.run(create_app(settings), host=host, port=port)
    elif args.cmd == "check":
        engine = _engine(settings)

        async def run() -> None:
            for w in engine.db.list_watches(status="active"):
                print(f"[{w.id}] {w.venue_name}: {await engine.check_watch(w.id)}")

        asyncio.run(run())
    elif args.cmd == "test-notify":
        notifier = Notifier(settings)
        if not notifier.channels:
            sys.exit("No notification channel configured. See .env.example.")
        failed = asyncio.run(notifier.send("Joshy test", "Notifications are working."))
        print("Failed: " + ", ".join(failed) if failed else f"Sent via {', '.join(notifier.channels)}")


if __name__ == "__main__":
    main()
