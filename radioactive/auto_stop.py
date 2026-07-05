from __future__ import annotations

import asyncio
import logging
import time

from radioactive.azure import AzureVmClient, PowerState
from radioactive.config import Config
from radioactive.minecraft import query_player_count

logger = logging.getLogger(__name__)


class AutoStopState:
    """Shared mutable state for the auto-stop grace timer."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.empty_since: float | None = None  # time.monotonic() timestamp


async def auto_stop_loop(
    config: Config,
    azure: AzureVmClient,
    state: AutoStopState,
) -> None:
    """Background task: periodically check Minecraft players and deallocate when empty."""
    interval = max(config.auto_stop_check_interval_secs, 10)
    while True:
        try:
            await auto_stop_tick(config, azure, state)
        except Exception:
            logger.exception("Auto-stop tick failed")

        await asyncio.sleep(interval)


async def auto_stop_tick(
    config: Config,
    azure: AzureVmClient,
    state: AutoStopState,
) -> None:
    power = await azure.power_state()
    if power is None:
        logger.warning("Unable to determine VM power state")
        return

    if power != PowerState.RUNNING:
        async with state.lock:
            state.empty_since = None
        return

    # Try to query RCON.  If it fails we still check the grace timer below
    # so a prior "empty" tick isn't wasted by a transient RCON blip.
    known_empty = False
    try:
        players = await query_player_count(config)
        logger.info("Auto-stop check: online players = %d", players)
        if players > 0:
            async with state.lock:
                state.empty_since = None
            return
        known_empty = True
    except Exception as exc:
        logger.warning("Failed to query Minecraft online players: %s", exc)

    # ── Grace timer ──────────────────────────────────────────────────
    now = time.monotonic()
    grace = max(config.auto_stop_empty_grace_secs, 30)
    async with state.lock:
        if state.empty_since is None:
            # Only start the timer when we *know* the server is empty.
            # A transient RCON failure alone doesn't prove emptiness.
            if known_empty:
                state.empty_since = now
                logger.info("Minecraft is empty; starting auto-stop grace timer")
        elif now - state.empty_since >= grace:
            logger.info("Minecraft empty past grace period; deallocating VM")
            try:
                await azure.deallocate_vm()
            except Exception:
                logger.exception("Failed to deallocate VM during auto-stop")
            state.empty_since = None
