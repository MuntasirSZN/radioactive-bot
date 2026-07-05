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

# ── Embed colour palette ────────────────────────────────────────────────

_COLOUR_GREEN = 0x57F287  # success / running
_COLOUR_YELLOW = 0xFEE75C  # in-progress / warning
_COLOUR_RED = 0xED4245  # error / stopped
_COLOUR_GREY = 0x808080  # inactive


def _embed(
    colour: int,
    title: str,
    description: str = "",
) -> discord.Embed:
    """Build a consistently-styled embed."""
    e = discord.Embed(colour=colour, title=title, description=description)
    e.set_footer(text="Radioactive Bot")
    e.timestamp = discord.utils.utcnow()
    return e


# ── Response helpers ────────────────────────────────────────────────────


async def _defer(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=False, thinking=True)


async def _edit_embed(interaction: discord.Interaction, embed: discord.Embed) -> None:
    await interaction.edit_original_response(embed=embed)


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
        # Kick off presence updater
        asyncio.create_task(self._presence_loop(), name="presence")

    async def _presence_loop(self) -> None:
        """Periodically update bot activity with live player count."""
        while True:
            try:
                await self._update_presence()
            except Exception:
                logger.exception("Presence update failed")
            await asyncio.sleep(300)  # 5 min

    async def _update_presence(self) -> None:
        """Set bot activity based on current server state."""
        azure = self._azure
        try:
            power = await azure.power_state()
        except Exception:
            power = None

        if power is None:
            activity = discord.Game(name="Unknown server status")
        elif power != PowerState.RUNNING:
            activity = discord.Game(name=f"VM is {power.value}")
        else:
            try:
                players = await query_player_count(self._config)
                activity = discord.Game(name=f"{players} player{'s' if players != 1 else ''} online")
            except Exception:
                activity = discord.Game(name="Server status unknown")

        await self.change_presence(activity=activity)

    # ── Command accessors ─────────────────────────────────────────────

    @property
    def bot_config(self) -> Config:
        return self._config

    @property
    def bot_azure(self) -> AzureVmClient:
        return self._azure

    @property
    def bot_auto_stop_state(self) -> AutoStopState:
        return self._auto_stop_state


# ═══════════════════════════════════════════════════════════════════════
# Slash commands
# ═══════════════════════════════════════════════════════════════════════


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

        power = await azure.power_state()
        if power == PowerState.RUNNING:
            await _edit_embed(
                interaction,
                _embed(_COLOUR_YELLOW, "VM is already running."),
            )
            return

        if self._bot.start_lock.locked():
            await _edit_embed(
                interaction,
                _embed(
                    _COLOUR_YELLOW,
                    "Already starting",
                    "VM is already being started. You'll be notified when it's ready.",
                ),
            )
            return

        async with self._bot.start_lock:
            power = await azure.power_state()
            if power == PowerState.RUNNING:
                await _edit_embed(interaction, _embed(_COLOUR_YELLOW, "VM is already running."))
                return

            if power == PowerState.STARTING:
                await _edit_embed(
                    interaction,
                    _embed(_COLOUR_YELLOW, "Starting VM", "VM is already starting; waiting for it..."),
                )
            else:
                await azure.start_vm()
                logger.info("Azure start request sent")
                await _edit_embed(
                    interaction,
                    _embed(_COLOUR_YELLOW, "Starting VM", "Start request sent. Waiting for VM to become ready..."),
                )

            asyncio.create_task(
                self._notify_when_running(interaction, azure),
                name="start-wait",
            )

    @staticmethod
    async def _notify_when_running(
        interaction: discord.Interaction,
        azure: AzureVmClient,
    ) -> None:
        try:
            started = await _wait_for_power_state(azure, PowerState.RUNNING, timeout=480, interval=10)
            if started:
                embed = _embed(_COLOUR_GREEN, "VM is started", "The virtual machine is now running.")
            else:
                embed = _embed(
                    _COLOUR_YELLOW,
                    "VM still starting",
                    "Start request sent, but VM is still starting. Try `/ping` in a minute.",
                )
            await _edit_embed(interaction, embed)
        except Exception:
            logger.exception("Background start-wait failed")
            try:
                await _edit_embed(
                    interaction,
                    _embed(_COLOUR_RED, "Error", "An error occurred while waiting for the VM to start."),
                )
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

        power = await azure.power_state()
        if power in (PowerState.DEALLOCATED, PowerState.STOPPED):
            await _edit_embed(
                interaction,
                _embed(_COLOUR_YELLOW, "VM is already stopped."),
            )
            return

        if self._bot.stop_lock.locked():
            await _edit_embed(
                interaction,
                _embed(_COLOUR_YELLOW, "Already stopping", "VM is already being stopped."),
            )
            return

        async with self._bot.stop_lock:
            power = await azure.power_state()
            if power in (PowerState.DEALLOCATED, PowerState.STOPPED):
                await _edit_embed(interaction, _embed(_COLOUR_YELLOW, "VM is already stopped."))
                return

            await azure.deallocate_vm()

            async with self._bot.bot_auto_stop_state.lock:
                self._bot.bot_auto_stop_state.empty_since = None

            logger.info("Azure stop (deallocate) request sent")
            await _edit_embed(
                interaction,
                _embed(_COLOUR_YELLOW, "Stopping VM", "Stop request sent. Waiting for VM to deallocate..."),
            )
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
            stopped = await _wait_for_power_state(azure, PowerState.DEALLOCATED, timeout=360, interval=10)
            if stopped:
                embed = _embed(_COLOUR_RED, "VM is stopped", "The virtual machine has been deallocated.")
            else:
                embed = _embed(
                    _COLOUR_YELLOW,
                    "VM still shutting down",
                    "Stop request sent, but VM is still shutting down.",
                )
            await _edit_embed(interaction, embed)
        except Exception:
            logger.exception("Background stop-wait failed")
            try:
                await _edit_embed(
                    interaction,
                    _embed(_COLOUR_RED, "Error", "An error occurred while waiting for the VM to stop."),
                )
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
            await _edit_embed(
                interaction,
                _embed(_COLOUR_RED, "Error", "Could not determine VM power state."),
            )
            return
        if power != PowerState.RUNNING:
            await _edit_embed(
                interaction,
                _embed(_COLOUR_GREY, "Minecraft Offline", f"VM is **{power.value}**. Minecraft is not reachable."),
            )
            return

        try:
            players = await query_player_count(config)
        except Exception:
            logger.exception("RCON ping failed")
            await _edit_embed(
                interaction,
                _embed(_COLOUR_RED, "RCON Error", "Failed to reach Minecraft server via RCON."),
            )
            return

        embed = _embed(
            _COLOUR_GREEN,
            "Minecraft Online",
            f"Server is reachable. **{players}** player{'s' if players != 1 else ''} online.",
        )
        await _edit_embed(interaction, embed)


class StatusCommand(app_commands.Command):
    def __init__(self, bot: RadioactiveBot) -> None:
        super().__init__(
            name="status",
            description="Show server status (players, TPS, memory)",
            callback=self._callback,
        )
        self._bot = bot

    async def _callback(self, interaction: discord.Interaction) -> None:
        azure = self._bot.bot_azure
        config = self._bot.bot_config
        await _defer(interaction)

        power = await azure.power_state()
        if power is None:
            await _edit_embed(
                interaction,
                _embed(_COLOUR_RED, "Error", "Could not determine VM power state."),
            )
            return

        if power != PowerState.RUNNING:
            await _edit_embed(
                interaction,
                _embed(_COLOUR_GREY, "VM Offline", f"VM is **{power.value}**. Minecraft is not reachable."),
            )
            return

        try:
            status_text = await query_server_status(config)
        except Exception:
            logger.exception("Status query failed")
            await _edit_embed(
                interaction,
                _embed(_COLOUR_RED, "RCON Error", "Failed to query server status via RCON."),
            )
            return

        embed = _embed(_COLOUR_GREEN, "Server Status", status_text)
        await _edit_embed(interaction, embed)


# ═══════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════


async def _wait_for_power_state(
    azure: AzureVmClient,
    target_state: PowerState,
    timeout: int,
    interval: int,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        power = await azure.power_state()
        if power == target_state:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)
