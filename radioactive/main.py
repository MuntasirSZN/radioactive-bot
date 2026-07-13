from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from radioactive.auto_stop import AutoStopState, auto_stop_loop
from radioactive.azure import AzureVmClient
from radioactive.bot import RadioactiveBot
from radioactive.config import Config

# ── Logging ───────────────────────────────────────────────────────────

_DEFAULT_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"


class _ColorFormatter(logging.Formatter):
    """Minimal ANSI-colored formatter — no dependency overhead."""

    _COLORS = {
        logging.DEBUG: "\033[90m",  # grey
        logging.INFO: "\033[32m",  # green
        logging.WARNING: "\033[33m",  # yellow
        logging.ERROR: "\033[31m",  # red
        logging.CRITICAL: "\033[1;31m",  # bold red
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        original = record.levelname
        color = self._COLORS.get(record.levelno, self._RESET)
        record.levelname = f"{color}{original}{self._RESET}"
        result = super().format(record)
        record.levelname = original
        return result


_handler = logging.StreamHandler()
_handler.setLevel(logging.INFO)
_handler.setFormatter(_ColorFormatter(fmt=_DEFAULT_FORMAT, datefmt="%H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])

# Suppress verbose SDK request logging
for name in (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "httpx",
    "httpcore",
):
    logging.getLogger(name).setLevel(logging.WARNING)

logger = logging.getLogger("radioactive")


# ── Env file loading ──────────────────────────────────────────────────


def _load_env_files() -> None:
    """Load .env.local, then .env from the project root and cwd."""
    import dotenv

    manifest_dir = Path(__file__).resolve().parent.parent
    dotenv.load_dotenv(manifest_dir / ".env.local", override=True)
    dotenv.load_dotenv(manifest_dir / ".env", override=True)
    dotenv.load_dotenv(".env.local", override=True)
    dotenv.load_dotenv(".env", override=True)


# ── Main ──────────────────────────────────────────────────────────────


async def main() -> None:
    _load_env_files()

    config = Config.from_env()
    logger.info(
        "Booting bot for VM '%s' in resource group '%s'",
        config.azure_vm_name,
        config.azure_resource_group,
    )

    azure = AzureVmClient(config)
    auto_stop_state = AutoStopState()

    bot = RadioactiveBot(
        config=config,
        azure=azure,
        auto_stop_state=auto_stop_state,
    )

    # Start the auto-stop background loop
    loop = asyncio.get_running_loop()
    loop.create_task(
        auto_stop_loop(config, azure, auto_stop_state),
        name="auto-stop",
    )

    logger.info("Connecting to Discord gateway...")
    try:
        await bot.start(config.discord_token, reconnect=True)
    finally:
        await azure.aclose()


def entry() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    entry()
