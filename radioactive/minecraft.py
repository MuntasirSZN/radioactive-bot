from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from async_mcrcon import MinecraftClient

if TYPE_CHECKING:
    from radioactive.config import Config

logger = logging.getLogger(__name__)

# Matches Minecraft §-style format codes, optionally prefixed with Â (UTF-8 encoding artifact)
_FORMAT_CODE_RE = re.compile(r"Â?§[0-9a-fk-or]")


def _strip_format_codes(text: str) -> str:
    """Remove Minecraft §-style colour/format codes from a string."""
    return _FORMAT_CODE_RE.sub("", text)


def _first_number(text: str) -> int | None:
    """Return the first contiguous ASCII integer in *text*, or None."""
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


# ═══════════════════════════════════════════════════════════════════════
# Parse helpers
# ═══════════════════════════════════════════════════════════════════════


def parse_player_count(raw_output: str) -> int:
    """Parse online-player count from a Minecraft `list` command response.

    Handles format codes and two output variants:
        "There are X of a max of Y players online: …"
        "There are X out of maximum Y players online: …"
    Falls back to the first number found.
    """
    cleaned = _strip_format_codes(raw_output)

    for marker in ("out of maximum", "of a max of"):
        if marker in cleaned:
            left, _ = cleaned.split(marker, 1)
            count = _first_number(left)
            if count is not None:
                return count

    count = _first_number(cleaned)
    if count is None:
        raise ValueError(f"Could not parse player count from RCON output: {raw_output!r}")
    return count


def parse_player_list(raw_output: str) -> tuple[int, int, list[str]]:
    """Parse `/list` output into (online, max, [names]).

    Handles both formats:
        "There are 3 of a max of 20 players online: a, b, c"
        "There are 0 out of maximum 20 players online:"
    """
    cleaned = _strip_format_codes(raw_output)

    online = 0
    maximum = 0
    names: list[str] = []

    # Extract online/max counts
    for marker in ("out of maximum", "of a max of"):
        if marker in cleaned:
            left, right = cleaned.split(marker, 1)
            online = _first_number(left) or 0
            maximum = _first_number(right) or 0
            break
    else:
        # Fallback — just grab first two numbers
        nums = re.findall(r"\d+", cleaned)
        if len(nums) >= 2:
            online = int(nums[0])
            maximum = int(nums[1])
        elif len(nums) == 1:
            online = int(nums[0])

    # Extract player names after ": "
    if ": " in cleaned:
        _, after = cleaned.split(": ", 1)
        if after.strip():
            names = [n.strip() for n in after.split(", ") if n.strip()]

    return online, maximum, names


def parse_tps(raw_output: str) -> str | None:
    """Parse `/tps` output into a human-readable string.

    Paper format:
        "The server's current TPS is 20.0, 20.0, 20.0 (mean of 1m, 5m, 15m)"
    Spigot format:
        "TPS from last 1m, 5m, 15m: 20.0, 20.0, 20.0"
    Returns None if TPS can't be parsed (e.g. unknown command).
    """
    cleaned = _strip_format_codes(raw_output)

    # Try to find three comma-separated floats
    # Paper: "TPS is 20.0, 20.0, 20.0"
    # Spigot: "TPS from ...: 20.0, 20.0, 20.0"
    tps_nums = re.findall(r"\b\d{1,2}\.\d+\b", cleaned)
    if len(tps_nums) >= 3:
        return f"{tps_nums[0]}, {tps_nums[1]}, {tps_nums[2]}"

    # Single TPS value
    if tps_nums:
        return tps_nums[0]

    return None


def parse_memory(raw_output: str) -> str | None:
    """Parse EssentialsX `/gc` memory line into a human-readable string.

    Output format (after stripping codes):
        "Memory: 1024.0 MB / 2048.0 MB (free: 500.0 MB)"
    Returns e.g. "1.0 / 2.0 GB" or None.
    """
    cleaned = _strip_format_codes(raw_output)

    m = re.search(r"Memory:\s*([\d.]+)\s*MB\s*/\s*([\d.]+)\s*MB", cleaned, re.IGNORECASE)
    if m:
        used = float(m.group(1))
        maximum = float(m.group(2))
        # Convert to GB if >= 1024 MB
        if maximum >= 1024:
            return f"{used / 1024:.1f} / {maximum / 1024:.1f} GB"
        return f"{used:.0f} / {maximum:.0f} MB"

    return None


# ═══════════════════════════════════════════════════════════════════════
# RCON queries
# ═══════════════════════════════════════════════════════════════════════


async def send_command(config: Config, command: str) -> str:
    """Send a single RCON command and return the raw response."""
    async with MinecraftClient(
        host=config.rcon_host,
        port=config.rcon_port,
        password=config.rcon_password,
    ) as client:
        return await client.send(command)


async def query_player_count(config: Config) -> int:
    """Return the number of online players."""
    response = await send_command(config, "list")
    return parse_player_count(response)


async def query_server_status(config: Config) -> str:
    """Query multiple RCON commands and return a formatted status message."""
    async with MinecraftClient(
        host=config.rcon_host,
        port=config.rcon_port,
        password=config.rcon_password,
    ) as client:
        list_raw = await client.send("list")
        tps_raw = await client.send("tps")
        gc_raw = await client.send("gc")

    online, maximum, names = parse_player_list(list_raw)
    tps = parse_tps(tps_raw)
    memory = parse_memory(gc_raw)

    lines: list[str] = []

    # Player line
    player_list_str = ", ".join(names) if names else "\u2014"
    lines.append(f"\U0001f465 **Players:** {online}/{maximum} ({player_list_str})")

    # Memory line (EssentialsX /gc)
    if memory:
        lines.append(f"\U0001f4be **Memory:** {memory}")

    # TPS line with status indicator
    if tps:
        try:
            tps_1m = float(tps.split(",")[0])
            if tps_1m < 10:
                indicator = "\U0001f534"  # red circle
            elif tps_1m < 18:
                indicator = "\U0001f7e1"  # yellow circle
            else:
                indicator = "\U0001f7e2"  # green circle
        except (ValueError, IndexError):
            indicator = "\u26aa"  # white circle
        lines.append(f"{indicator} **TPS:** {tps}")
    else:
        lines.append("\u26aa **TPS:** N/A")

    return "\n".join(lines)
