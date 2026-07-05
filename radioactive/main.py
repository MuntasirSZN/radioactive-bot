from __future__ import annotations

import asyncio
import logging
import os
import ssl
from pathlib import Path

# ── CA certificate bootstrap ──────────────────────────────────────────
# Python 3.14+ on some distros (NixOS, static builds) has cafile=None
# and doesn't find system CA certs automatically.  aiohttp (used by
# discord.py) and httpx both rely on ssl.get_default_verify_paths() to
# locate the bundle, so we must set SSL_CERT_FILE before they create
# their SSL contexts.
_CA_BUNDLE_CANDIDATES = [
    "/etc/ssl/certs/ca-bundle.crt",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
]


def _ensure_ca_bundle() -> None:
    if os.environ.get("SSL_CERT_FILE"):
        return
    paths = ssl.get_default_verify_paths()
    if paths.cafile and os.path.exists(paths.cafile):
        return
    for path in _CA_BUNDLE_CANDIDATES:
        if os.path.exists(path):
            os.environ["SSL_CERT_FILE"] = path
            return


_ensure_ca_bundle()

import dotenv  # noqa: E402

from radioactive.auto_stop import AutoStopState, auto_stop_loop  # noqa: E402
from radioactive.azure import AzureVmClient  # noqa: E402
from radioactive.bot import RadioactiveBot  # noqa: E402
from radioactive.config import Config  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
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
