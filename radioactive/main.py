from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import rich.logging

from radioactive.auto_stop import AutoStopState, auto_stop_loop
from radioactive.azure import AzureVmClient
from radioactive.bot import RadioactiveBot
from radioactive.config import Config

# ── Logging ───────────────────────────────────────────────────────────

_handler = rich.logging.RichHandler(
    show_time=True,
    show_path=False,
    rich_tracebacks=True,
    tracebacks_show_locals=True,
)
_handler.setLevel(logging.INFO)
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[_handler],
)

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
