from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import aiomcrcon

if TYPE_CHECKING:
    from radioactive.config import Config

logger = logging.getLogger(__name__)

# ── RCON persistent connection ────────────────────────────────────────
# Reuse a single TCP connection across all RCON calls instead of opening
# a new one per command.  Auto-reconnects on the next call after a drop.

_rcon_client: aiomcrcon.Client | None = None
_rcon_lock = asyncio.Lock()
_rcon_config: tuple[str, int, str] | None = None


async def _get_rcon(config: Config) -> aiomcrcon.Client:
    """Return the shared RCON client, connecting lazily on first use."""
    global _rcon_client, _rcon_config

    cfg = (config.rcon_host, config.rcon_port, config.rcon_password)
    # If config changed (shouldn't happen at runtime), force reconnect
    if _rcon_client is not None and _rcon_config != cfg:
        try:
            await _rcon_client.close()
        except Exception:
            pass
        _rcon_client = None

    if _rcon_client is not None:
        return _rcon_client

    async with _rcon_lock:
        if _rcon_client is not None:
            return _rcon_client
        client = aiomcrcon.Client(config.rcon_host, config.rcon_port, config.rcon_password)
        try:
            await client.connect(timeout=8)
        except aiomcrcon.IncorrectPasswordError:
            logger.critical("RCON authentication failed — check RCON_PASSWORD")
            raise
        except (OSError, aiomcrcon.RCONConnectionError) as exc:
            raise ConnectionError(f"RCON connection failed: {exc}") from exc
        _rcon_client = client
        _rcon_config = cfg
        return _rcon_client


async def _close_rcon() -> None:
    """Tear down the shared RCON connection."""
    global _rcon_client
    if _rcon_client is not None:
        try:
            await _rcon_client.close()
        except Exception:
            pass
        _rcon_client = None


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
    tps_nums = re.findall(r"\b\d{1,2}\.\d+\b", cleaned)
    if len(tps_nums) >= 3:
        return f"{tps_nums[0]}, {tps_nums[1]}, {tps_nums[2]}"

    if tps_nums:
        return tps_nums[0]

    return None


def parse_memory(raw_output: str) -> str | None:
    """Parse `/gc` output into a human-readable memory string.

    Supports two formats:
        EssentialsX: "Memory: 1024.0 MB / 2048.0 MB (free: 500.0 MB)"
        Paper/Vanilla: "Maximum memory: 6,504 MB." + "Free memory: 4,166 MB."
    Returns e.g. "2.3 / 6.4 GB" or None.
    """
    cleaned = _strip_format_codes(raw_output)

    # Strip commas from numbers before any parsing
    cleaned_no_comma = cleaned.replace(",", "")

    # Try EssentialsX format first
    m = re.search(
        r"Memory:\s*([\d.]+)\s*MB\s*/\s*([\d.]+)\s*MB",
        cleaned_no_comma,
        re.IGNORECASE,
    )
    if m:
        used = float(m.group(1))
        maximum = float(m.group(2))
    else:
        # Paper/Vanilla format: parse Maximum memory and Free memory lines
        max_m = re.search(r"Maximum memory:\s*([\d.]+)\s*MB", cleaned_no_comma, re.IGNORECASE)
        free_m = re.search(r"Free memory:\s*([\d.]+)\s*MB", cleaned_no_comma, re.IGNORECASE)
        if not max_m:
            return None
        maximum = float(max_m.group(1))
        free = float(free_m.group(1)) if free_m else 0
        used = maximum - free

    # Convert to GB if >= 1024 MB
    if maximum >= 1024:
        return f"{used / 1024:.1f} / {maximum / 1024:.1f} GB"
    return f"{used:.0f} / {maximum:.0f} MB"


def parse_uptime(raw_output: str) -> str | None:
    """Parse uptime from `/gc` output.

    Paper format: "Uptime: 14 minutes 3 seconds"  (may or may not end with a dot)
    Returns e.g. "14 minutes 3 seconds" or None.
    """
    cleaned = _strip_format_codes(raw_output)
    m = re.search(r"Uptime:\s*(.+?)\.?\s*$", cleaned, re.IGNORECASE | re.MULTILINE)
    if m:
        return m.group(1).strip()
    return None


# ═══════════════════════════════════════════════════════════════════════
# RCON queries — reuse persistent connection
# ═══════════════════════════════════════════════════════════════════════


async def send_command(config: Config, command: str) -> str:
    """Send a single RCON command and return the raw response."""
    client = await _get_rcon(config)
    try:
        response, _ = await client.send_cmd(command)
        return response
    except (aiomcrcon.ClientNotConnectedError, OSError):
        # Connection dropped — reconnect and retry once
        async with _rcon_lock:
            _rcon_client = None
        client = await _get_rcon(config)
        response, _ = await client.send_cmd(command)
        return response


async def query_player_count(config: Config) -> int:
    """Return the number of online players."""
    response = await send_command(config, "list")
    return parse_player_count(response)


async def query_server_status(config: Config) -> str:
    """Query multiple RCON commands and return a formatted status message."""
    client = await _get_rcon(config)
    try:
        list_raw, _ = await client.send_cmd("list")
        tps_raw, _ = await client.send_cmd("tps")
        gc_raw, _ = await client.send_cmd("gc")
    except (aiomcrcon.ClientNotConnectedError, OSError):
        # Reconnect and retry once
        async with _rcon_lock:
            _rcon_client = None
        client = await _get_rcon(config)
        list_raw, _ = await client.send_cmd("list")
        tps_raw, _ = await client.send_cmd("tps")
        gc_raw, _ = await client.send_cmd("gc")

    online, maximum, names = parse_player_list(list_raw)
    tps = parse_tps(tps_raw)
    memory = parse_memory(gc_raw)

    lines: list[str] = []

    # Player line
    player_list_str = ", ".join(names) if names else "\u2014"
    lines.append(f"\U0001f465 **Players:** {online}/{maximum} ({player_list_str})")

    # Memory line
    if memory:
        lines.append(f"\U0001f4be **Memory:** {memory}")

    # TPS line with status indicator
    if tps:
        try:
            tps_1m = float(tps.split(",")[0])
            if tps_1m < 10:
                indicator = "\U0001f534"
            elif tps_1m < 18:
                indicator = "\U0001f7e1"
            else:
                indicator = "\U0001f7e2"
        except (ValueError, IndexError):
            indicator = "\u26aa"
        lines.append(f"{indicator} **TPS:** {tps}")
    else:
        lines.append("\u26aa **TPS:** N/A")

    return "\n".join(lines)
