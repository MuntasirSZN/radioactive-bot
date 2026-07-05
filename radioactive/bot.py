from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands

from radioactive.auto_stop import AutoStopState
from radioactive.azure import AzureVmClient, PowerState
from radioactive.config import Config
from radioactive.minecraft import query_player_count, query_server_status

logger = logging.getLogger(__name__)

# ── Helpers shared by all commands ──────────────────────────────────────


async def _defer(interaction: discord.Interaction) -> None:
    """Acknowledge the interaction with an ephemeral deferred response."""
    await interaction.response.defer(ephemeral=True, thinking=True)


async def _edit(interaction: discord.Interaction, content: str) -> None:
    """Replace the content of a previously-deferred ephemeral response."""
    await interaction.edit_original_response(content=content)


# ── Bot client ──────────────────────────────────────────────────────────


class RadioactiveBot(discord.Client):
    def __init__(
        self,
        config: Config,
        azure: AzureVmClient,
        auto_stop_state: AutoStopState,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self._config = config
        self._azure = azure
        self._auto_stop_state = auto_stop_state
        self.tree = app_commands.CommandTree(self)

        # Coordination locks to serialize start/stop races
        self.start_lock = asyncio.Lock()
        self.stop_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        self.tree.add_command(StartCommand(self))
        self.tree.add_command(StopCommand(self))
        self.tree.add_command(PingCommand(self))
        self.tree.add_command(StatusCommand(self))

        if self._config.command_guild_id is not None:
            guild = discord.Object(id=self._config.command_guild_id)
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Registered commands in guild %s", self._config.command_guild_id)
        else:
            await self.tree.sync()
            logger.info("Registered global commands")

    async def on_ready(self) -> None:
        logger.info("Connected as %s", self.user)

    # ── Command accessors (commands get state via the bot) ──────────────

    @property
    def bot_config(self) -> Config:
        return self._config

    @property
    def bot_azure(self) -> AzureVmClient:
        return self._azure

    @property
    def bot_auto_stop_state(self) -> AutoStopState:
        return self._auto_stop_state


# ── Slash command implementations ───────────────────────────────────────


class StartCommand(app_commands.Command):
    def __init__(self, bot: RadioactiveBot) -> None:
        super().__init__(
            name="start",
            description="Start the Azure VM",
            callback=self._callback,
        )
        self._bot = bot

    async def _callback(self, interaction: discord.Interaction) -> None:
        azure = self._bot.bot_azure
        await _defer(interaction)

        logger.info("Received /start command")

        # Quick check: already running?
        power = await azure.power_state()
        if power == PowerState.RUNNING:
            await _edit(interaction, "VM is already running.")
            return

        # Check if another start is in progress
        if self._bot.start_lock.locked():
            await _edit(
                interaction,
                "VM is already being started. You'll be notified when it's ready.",
            )
            return

        async with self._bot.start_lock:
            power = await azure.power_state()
            if power == PowerState.RUNNING:
                await _edit(interaction, "VM is already running.")
                return
            if power == PowerState.STARTING:
                await _edit(interaction, "VM is already starting; waiting for it...")
            else:
                await azure.start_vm()
                logger.info("Azure start request sent")
                await _edit(interaction, "Start request sent. Waiting for VM...")

            # Wait in background and notify when done
            asyncio.create_task(
                self._notify_when_running(interaction, azure),
                name="start-wait",
            )

    @staticmethod
    async def _notify_when_running(
        interaction: discord.Interaction,
        azure: AzureVmClient,
    ) -> None:
        """Wait for VM to reach 'running' state, then notify the user."""
        try:
            started = await _wait_for_power_state(
                azure,
                PowerState.RUNNING,
                timeout=480,
                interval=10,
            )
            if started:
                await _edit(interaction, "VM is started.")
            else:
                await _edit(
                    interaction,
                    "Start request sent, but VM is still starting. Try /ping in a minute.",
                )
        except Exception:
            logger.exception("Background start-wait failed")
            try:
                await _edit(interaction, "An error occurred while waiting for the VM to start.")
            except Exception:
                pass


class StopCommand(app_commands.Command):
    def __init__(self, bot: RadioactiveBot) -> None:
        super().__init__(
            name="stop",
            description="Stop (deallocate) the Azure VM",
            callback=self._callback,
        )
        self._bot = bot

    async def _callback(self, interaction: discord.Interaction) -> None:
        azure = self._bot.bot_azure
        await _defer(interaction)

        logger.info("Received /stop command")

        # Quick check: already stopped?
        power = await azure.power_state()
        if power in (PowerState.DEALLOCATED, PowerState.STOPPED):
            await _edit(interaction, "VM is already stopped.")
            return

        if self._bot.stop_lock.locked():
            await _edit(interaction, "VM is already being stopped.")
            return

        async with self._bot.stop_lock:
            power = await azure.power_state()
            if power in (PowerState.DEALLOCATED, PowerState.STOPPED):
                await _edit(interaction, "VM is already stopped.")
                return

            await azure.deallocate_vm()

            # Reset auto-stop grace timer since we're manually stopping
            async with self._bot.bot_auto_stop_state.lock:
                self._bot.bot_auto_stop_state.empty_since = None

            logger.info("Azure stop (deallocate) request sent")

            # Wait in background and notify when done
            await _edit(interaction, "Stop request sent. Waiting for VM to deallocate...")
            asyncio.create_task(
                self._notify_when_stopped(interaction, azure),
                name="stop-wait",
            )

    @staticmethod
    async def _notify_when_stopped(
        interaction: discord.Interaction,
        azure: AzureVmClient,
    ) -> None:
        try:
            stopped = await _wait_for_power_state(
                azure,
                PowerState.DEALLOCATED,
                timeout=360,
                interval=10,
            )
            if stopped:
                await _edit(interaction, "VM is stopped.")
            else:
                await _edit(
                    interaction,
                    "Stop request sent, but VM is still shutting down.",
                )
        except Exception:
            logger.exception("Background stop-wait failed")
            try:
                await _edit(interaction, "An error occurred while waiting for the VM to stop.")
            except Exception:
                pass


class PingCommand(app_commands.Command):
    def __init__(self, bot: RadioactiveBot) -> None:
        super().__init__(
            name="ping",
            description="Ping the Minecraft server via RCON",
            callback=self._callback,
        )
        self._bot = bot

    async def _callback(self, interaction: discord.Interaction) -> None:
        azure = self._bot.bot_azure
        config = self._bot.bot_config
        await _defer(interaction)

        power = await azure.power_state()
        if power is None:
            await _edit(interaction, "Could not determine VM power state.")
            return
        if power != PowerState.RUNNING:
            await _edit(
                interaction,
                f"VM is {power.value}. Minecraft is not reachable.",
            )
            return

        try:
            players = await query_player_count(config)
        except Exception:
            logger.exception("RCON ping failed")
            await _edit(interaction, "Failed to reach Minecraft server via RCON.")
            return

        await _edit(
            interaction,
            f"Minecraft is reachable. Online players: {players}.",
        )


class StatusCommand(app_commands.Command):
    def __init__(self, bot: RadioactiveBot) -> None:
        super().__init__(
            name="status",
            description="Show server status (players, TPS)",
            callback=self._callback,
        )
        self._bot = bot

    async def _callback(self, interaction: discord.Interaction) -> None:
        azure = self._bot.bot_azure
        config = self._bot.bot_config
        await _defer(interaction)

        power = await azure.power_state()
        if power is None:
            await _edit(interaction, "Could not determine VM power state.")
            return
        if power != PowerState.RUNNING:
            await _edit(
                interaction,
                f"VM is {power.value}. Minecraft is not reachable.",
            )
            return

        try:
            status = await query_server_status(config)
        except Exception:
            logger.exception("Status query failed")
            await _edit(interaction, "Failed to query server status via RCON.")
            return

        await _edit(interaction, status)


# ── Utilities ───────────────────────────────────────────────────────────


async def _wait_for_power_state(
    azure: AzureVmClient,
    target_state: PowerState,
    timeout: int,
    interval: int,
) -> bool:
    """Poll Azure until *target_state* is reached, or *timeout* seconds elapse.

    Returns True if the state was reached.
    """
    deadline = time.monotonic() + timeout
    while True:
        power = await azure.power_state()
        if power == target_state:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)
