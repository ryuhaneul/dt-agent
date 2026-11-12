"""Run the standalone web container."""

from __future__ import annotations

import os
from pathlib import Path

from aiohttp import web

from ductor_bot.webchat.web.app import create_app


def main() -> None:
    token = Path(os.environ.get("DTW_TOKEN_FILE", "/run/dtw/bridge.token")).read_text().strip()
    if not token:
        raise ValueError("Bridge token file is empty")
    app = create_app(
        bridge_url=os.environ.get("DTW_BRIDGE_URL", "http://ductor-bot:8743"),
        public_origin=os.environ["DTW_PUBLIC_ORIGIN"],
        bridge_token=token,
    )
    web.run_app(app, host="0.0.0.0", port=8744, access_log=None)  # noqa: S104


if __name__ == "__main__":
    main()
