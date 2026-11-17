"""Start the trading bot and its web dashboard.

    .venv/bin/python run.py

Environment variables (optional):
    BOT_HOST      address to listen on (default 0.0.0.0 = reachable from your local network)
    BOT_PORT      port (default 8080)
    BOT_DATA_DIR  where settings, secrets and the database live (default ./data)
"""
import logging
import os
import sys

import uvicorn

from bot import secrets_store
from bot.web import create_app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not secrets_store.password_is_set():
        print("No dashboard password set yet. Run:\n\n    .venv/bin/python -m bot.cli set-password\n", file=sys.stderr)
        return 1
    host = os.environ.get("BOT_HOST", "0.0.0.0")
    port = int(os.environ.get("BOT_PORT", "8080"))
    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
