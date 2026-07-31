"""Command-line entry point for the standalone service."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import signal

from .config import load_config
from .runtime import PriceService


async def _main() -> None:
    logging.basicConfig(
        level=os.environ.get("PEP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_path = Path(os.environ.get("PEP_CONFIG", "/config/config.yaml"))
    config = load_config(config_path)
    service = PriceService(config)
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, service.stop)
    await service.run()


if __name__ == "__main__":
    asyncio.run(_main())
