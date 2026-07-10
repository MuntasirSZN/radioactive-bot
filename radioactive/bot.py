from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands

from radioactive.auto_stop import AutoStopState
from radioactive.azure import AzureVmClient, PowerState
from radioactive.config import Config
from radioactive.minecraft import (
    _strip_format_codes,
    parse_uptime,
    query_player_count,
    query_server_status,
    send_command,
)

logger = logging.getLogger(__name__)

# ── Embed colour palette ────────────────────────────────────────────────

COLOUR_GREEN = 0x57F287  # success / running
COLOUR_YELLOW = 0xFEE75C  # in-progress / warning
COLOUR_RED = 0xED4245  # error / stopped
COLOUR_BLUE = 0x5865F2  # info
COLOUR_GREY = 0x808080  # inactive


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


class _CommandTree(app_commands.CommandTree[discord.Client]):
    """Tree that restricts commands to a configured channel."""

    def __init__(
        self,
        client: discord.Client,
        channel_id: int | None,
    ) -> None:
        super().__init__(client)
        self._channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        cid = self._channel_id
        if cid is None:
            return True
        if interaction.guild is None:
            return True
        if interaction.channel_id == cid:
            return True
        embed = _embed(
            COLOUR_YELLOW,
            "Wrong channel",
            f"Commands only work in <#{cid}>.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False


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
        self.tree = _CommandTree(self, config.command_channel_id)

        self.start_lock = asyncio.Lock()
        self.stop_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        self.tree.add_command(StartCommand(self))
        self.tree.add_command(StopCommand(self))
        self.tree.add_command(PingCommand(self))
        self.tree.add_command(StatusCommand(self))
        self.tree.add_command(RconCommand(self))
        self.tree.add_command(AutoStopCommand(self))
        self.tree.add_command(SayCommand(self))
        self.tree.add_command(UptimeCommand(self))

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
            activity = discord.Game(name="☁️ Azure unreachable")
        elif power != PowerState.RUNNING:
            tag = str(power.value).replace("deallocated", "💤 off").replace("stopped", "💤 off")
            activity = discord.Game(name=tag)
        else:
            try:
                players = await query_player_count(self._config)
                label = f"🎮 {players}" if players else "🎮 empty"
                activity = discord.Game(name=label)
            except Exception:
                activity = discord.Game(name="🔌 RCON unreachable")

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
            description="Start the VM and Minecraft server",
            callback=self._callback,
        )
        self._bot = bot

    async def _callback(self, interaction: discord.Interaction) -> None:
        azure = self._bot.bot_azure
        config = self._bot.bot_config
        await _defer(interaction)
        logger.info("Received /start command")

        power = await azure.power_state()
        if power == PowerState.RUNNING:
            embed = _embed(COLOUR_YELLOW, "VM is already running", "Minecraft might already be up. Try `/status`.")
            await _edit_embed(interaction, embed)
            return

        if self._bot.start_lock.locked():
            await _edit_embed(
                interaction,
                _embed(
                    COLOUR_YELLOW,
                    "Already starting",
                    "VM is already being started. You'll be notified when it's ready.",
                ),
            )
            return

        async with self._bot.start_lock:
            power = await azure.power_state()
            if power == PowerState.RUNNING:
                embed = _embed(COLOUR_YELLOW, "VM is already running", "Minecraft might already be up. Try `/status`.")
                await _edit_embed(interaction, embed)
                return

            if power == PowerState.STARTING:
                await _edit_embed(
                    interaction,
                    _embed(COLOUR_YELLOW, "\u23f3 Starting VM", "VM is already starting; waiting for it..."),
                )
            else:
                await azure.start_vm()
                logger.info("Azure start request sent")
                await _edit_embed(
                    interaction,
                    _embed(COLOUR_YELLOW, "\u23f3 Starting VM", "Start request sent. Waiting for VM to power on..."),
                )

            asyncio.create_task(
                self._notify_when_ready(interaction, azure, config),
                name="start-wait",
            )

    @staticmethod
    async def _notify_when_ready(
        interaction: discord.Interaction,
        azure: AzureVmClient,
        config: Config,
    ) -> None:
        try:
            # Stage 1 — wait for Azure VM to reach RUNNING
            started = await _wait_for_power_state(azure, PowerState.RUNNING, timeout=480, interval=10)
            if not started:
                await _edit_embed(
                    interaction,
                    _embed(
                        COLOUR_YELLOW,
                        "\u23f3 VM still starting",
                        "Start sent, but VM is taking a while. Try `/ping` in a minute.",
                    ),
                )
                return

            await _edit_embed(
                interaction,
                _embed(COLOUR_GREEN, "\u2705 VM started", "Waiting for Minecraft to come online..."),
            )

            # Stage 2 — wait for RCON to respond
            deadline = time.monotonic() + 120  # 2 min for Minecraft to boot
            mc_ready = False
            while time.monotonic() < deadline:
                try:
                    resp = await send_command(config, "ping")
                    if "Pong!" in resp:
                        mc_ready = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(15)

            if mc_ready:
                embed = _embed(COLOUR_GREEN, "\u2705 Server ready", "Minecraft is online and accepting connections.")
            else:
                embed = _embed(
                    COLOUR_YELLOW,
                    "\u23f3 Minecraft still starting",
                    "VM is running but Minecraft is taking longer than expected. Try `/status` in a moment.",
                )
            await _edit_embed(interaction, embed)
        except Exception:
            logger.exception("Background start-wait failed")
            try:
                await _edit_embed(
                    interaction,
                    _embed(COLOUR_RED, "Error", "An error occurred while waiting for the server to start."),
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
                _embed(COLOUR_YELLOW, "VM is already stopped."),
            )
            return

        if self._bot.stop_lock.locked():
            await _edit_embed(
                interaction,
                _embed(COLOUR_YELLOW, "Already stopping", "VM is already being stopped."),
            )
            return

        async with self._bot.stop_lock:
            power = await azure.power_state()
            if power in (PowerState.DEALLOCATED, PowerState.STOPPED):
                await _edit_embed(interaction, _embed(COLOUR_YELLOW, "VM is already stopped."))
                return

            await azure.deallocate_vm()

            async with self._bot.bot_auto_stop_state.lock:
                self._bot.bot_auto_stop_state.empty_since = None

            logger.info("Azure stop (deallocate) request sent")
            await _edit_embed(
                interaction,
                _embed(COLOUR_YELLOW, "Stopping VM", "Stop request sent. Waiting for VM to deallocate..."),
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
                embed = _embed(COLOUR_RED, "VM is stopped", "The virtual machine has been deallocated.")
            else:
                embed = _embed(
                    COLOUR_YELLOW,
                    "VM still shutting down",
                    "Stop request sent, but VM is still shutting down.",
                )
            await _edit_embed(interaction, embed)
        except Exception:
            logger.exception("Background stop-wait failed")
            try:
                await _edit_embed(
                    interaction,
                    _embed(COLOUR_RED, "Error", "An error occurred while waiting for the VM to stop."),
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
                _embed(COLOUR_RED, "Error", "Could not determine VM power state."),
            )
            return
        if power != PowerState.RUNNING:
            await _edit_embed(
                interaction,
                _embed(COLOUR_GREY, "Minecraft Offline", f"VM is **{power.value}**. Minecraft is not reachable."),
            )
            return

        try:
            players = await query_player_count(config)
        except Exception:
            logger.exception("RCON ping failed")
            await _edit_embed(
                interaction,
                _embed(COLOUR_RED, "RCON Error", "Failed to reach Minecraft server via RCON."),
            )
            return

        embed = _embed(
            COLOUR_GREEN,
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
                _embed(COLOUR_RED, "Error", "Could not determine VM power state."),
            )
            return

        if power != PowerState.RUNNING:
            await _edit_embed(
                interaction,
                _embed(COLOUR_GREY, "VM Offline", f"VM is **{power.value}**. Minecraft is not reachable."),
            )
            return

        try:
            status_text = await query_server_status(config)
        except Exception:
            logger.exception("Status query failed")
            await _edit_embed(
                interaction,
                _embed(COLOUR_RED, "RCON Error", "Failed to query server status via RCON."),
            )
            return

        embed = _embed(COLOUR_GREEN, "Server Status", status_text)
        await _edit_embed(interaction, embed)


class RconCommand(app_commands.Command):
    """Run an RCON command (admin only)."""

    def __init__(self, bot: RadioactiveBot) -> None:
        super().__init__(
            name="rcon",
            description="Run an RCON command on the Minecraft server",
            callback=self._callback,
        )
        self.default_permissions = discord.Permissions(administrator=True)
        self._bot = bot

    async def _callback(
        self,
        interaction: discord.Interaction,
        command: str,
    ) -> None:
        config = self._bot.bot_config
        azure = self._bot.bot_azure
        await _defer(interaction)

        power = await azure.power_state()
        if power is None:
            await _edit_embed(
                interaction,
                _embed(COLOUR_RED, "Error", "Could not determine VM power state."),
            )
            return
        if power != PowerState.RUNNING:
            await _edit_embed(
                interaction,
                _embed(COLOUR_GREY, "VM Offline", f"VM is **{power.value}**. Cannot execute RCON commands."),
            )
            return

        try:
            raw = await send_command(config, command)
        except Exception as e:
            logger.exception("RCON command failed: %s", command)
            await _edit_embed(
                interaction,
                _embed(COLOUR_RED, "RCON Error", f"Command failed: {e}"),
            )
            return

        MAX_LENGTH = 1024
        output = raw.strip()
        if len(output) > MAX_LENGTH:
            output = output[:MAX_LENGTH] + "\n\n*(truncated)*"
        output = _strip_format_codes(output)

        embed = _embed(
            COLOUR_GREEN,
            f"💻 `{command}`",
            f"```{output}```" if output else "*(no output)*",
        )
        await _edit_embed(interaction, embed)


class AutoStopCommand(app_commands.Command):
    def __init__(self, bot: RadioactiveBot) -> None:
        super().__init__(
            name="autostop",
            description="Show auto-stop timer status",
            callback=self._callback,
        )
        self._bot = bot

    async def _callback(self, interaction: discord.Interaction) -> None:
        config = self._bot.bot_config
        azure = self._bot.bot_azure
        state = self._bot.bot_auto_stop_state
        await _defer(interaction)

        power = await azure.power_state()

        interval = config.auto_stop_check_interval_secs
        grace = config.auto_stop_empty_grace_secs

        lines: list[str] = []

        # VM state
        lines.append(f"⏹️ **VM:** `{power.value}`" if power else "Not reachable")
        lines.append(f"⏱️ **Check interval:** `{interval // 60}m {interval % 60}s`")
        lines.append(f"⏳ **Grace period:** `{grace // 60}m {grace % 60}s`")

        async with state.lock:
            if state.empty_since is not None:
                elapsed = time.monotonic() - state.empty_since
                remaining = max(0.0, grace - elapsed)
                lines.append(f"🟢 **Auto-stop timer:** active (`{remaining / 60:.1f}` min remaining)")
                lines.append("⚠️ Server is empty; VM will deallocate when the timer expires.")
            elif power == PowerState.RUNNING:
                lines.append("⚪ **Auto-stop timer:** inactive (players online)")
            else:
                lines.append("⚪ **Auto-stop timer:** inactive (VM not running)")

        embed = _embed(COLOUR_BLUE, "Auto-Stop Status", "\n".join(lines))
        await _edit_embed(interaction, embed)


class SayCommand(app_commands.Command):
    def __init__(self, bot: RadioactiveBot) -> None:
        super().__init__(
            name="say",
            description="Send a message to Minecraft chat",
            callback=self._callback,
        )
        self._bot = bot

    async def _callback(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        config = self._bot.bot_config
        azure = self._bot.bot_azure
        await _defer(interaction)

        power = await azure.power_state()
        if power != PowerState.RUNNING:
            await _edit_embed(
                interaction,
                _embed(COLOUR_GREY, "VM Offline", "Cannot send message; server is not running."),
            )
            return

        try:
            mc_message = f"[Discord] {interaction.user.display_name}: {message}"
            await send_command(config, f"say {mc_message}")
            logger.info("Say '%s' sent to Minecraft by %s", message, interaction.user)
            await _edit_embed(
                interaction,
                _embed(
                    COLOUR_GREEN,
                    "Message sent",
                    f"```\n[Discord] {interaction.user.display_name}: {message}\n```",
                ),
            )
        except Exception as e:
            logger.exception("Say command failed")
            await _edit_embed(
                interaction,
                _embed(COLOUR_RED, "Error", f"Failed to send message: {e}"),
            )


class UptimeCommand(app_commands.Command):
    def __init__(self, bot: RadioactiveBot) -> None:
        super().__init__(
            name="uptime",
            description="Show Minecraft server uptime",
            callback=self._callback,
        )
        self._bot = bot

    async def _callback(self, interaction: discord.Interaction) -> None:
        config = self._bot.bot_config
        azure = self._bot.bot_azure
        await _defer(interaction)

        power = await azure.power_state()
        if power is None:
            await _edit_embed(
                interaction,
                _embed(COLOUR_RED, "Error", "Could not determine VM power state."),
            )
            return
        if power != PowerState.RUNNING:
            await _edit_embed(
                interaction,
                _embed(COLOUR_GREY, "VM Offline", f"VM is **{power.value}**. Cannot query uptime."),
            )
            return

        try:
            gc_raw = await send_command(config, "gc")
            uptime = parse_uptime(gc_raw)
        except Exception:
            logger.exception("Uptime query failed")
            await _edit_embed(
                interaction,
                _embed(COLOUR_RED, "RCON Error", "Failed to query uptime via RCON."),
            )
            return

        embed = _embed(
            COLOUR_GREEN,
            ":clock1: Server Uptime",
            f"The Minecraft server has been running for **{uptime}**." if uptime else "Could not parse uptime.",
        )
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
