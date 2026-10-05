import argparse
import asyncio
import json
import sys
from pathlib import Path

from .errors import FormsError
from .session import BrowserSession


def main():
    parser = argparse.ArgumentParser(description="Browser-authenticated Microsoft Forms MCP")
    parser.add_argument(
        "command", nargs="?", default="serve", choices=["serve", "login", "probe-create"]
    )
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--browser", choices=["msedge", "chromium", "chrome"])
    parser.add_argument("--output", type=Path, default=Path(".state/probe-create.json"))
    args = parser.parse_args()
    if args.command == "serve":
        from .server import run

        run(args.profile, args.browser)
        return

    async def execute():
        from .probe import login, probe_create

        session = BrowserSession(args.profile, args.browser)
        try:
            if args.command == "login":
                await login(session)
            else:
                print(json.dumps(await probe_create(session, args.output), indent=2))
        finally:
            await session.close()

    try:
        asyncio.run(execute())
    except FormsError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
