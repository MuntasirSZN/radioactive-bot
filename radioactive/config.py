from __future__ import annotations

import os
from dataclasses import dataclass


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ValueError(f"Missing required env var {name}")
    return val


def _optional_env(name: str) -> str | None:
    val = os.environ.get(name)
    return val if val else None


def _optional_env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None:
        return default
    return int(val)


@dataclass(frozen=True)
class Config:
    discord_token: str
    discord_application_id: int
    command_guild_id: int | None
    azure_tenant_id: str
    azure_client_id: str
    azure_client_secret: str
    azure_subscription_id: str
    azure_resource_group: str
    azure_vm_name: str
    rcon_host: str
    rcon_port: int
    rcon_password: str
    command_channel_id: int | None
    auto_stop_check_interval_secs: int
    auto_stop_empty_grace_secs: int

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            discord_token=_require_env("DISCORD_TOKEN"),
            discord_application_id=int(_require_env("DISCORD_APPLICATION_ID")),
            command_guild_id=(int(v) if (v := _optional_env("COMMAND_GUILD_ID")) else None),
            azure_tenant_id=_require_env("AZURE_TENANT_ID"),
            azure_client_id=_require_env("AZURE_CLIENT_ID"),
            azure_client_secret=_require_env("AZURE_CLIENT_SECRET"),
            azure_subscription_id=_require_env("AZURE_SUBSCRIPTION_ID"),
            azure_resource_group=_require_env("AZURE_RESOURCE_GROUP"),
            azure_vm_name=_require_env("AZURE_VM_NAME"),
            rcon_host=_optional_env("RCON_HOST") or _optional_env("MINECRAFT_HOST") or "127.0.0.1",
            rcon_port=_optional_env_int("RCON_PORT", 25575),
            rcon_password=_require_env("RCON_PASSWORD"),
            command_channel_id=(int(v) if (v := _optional_env("COMMAND_CHANNEL_ID")) else None),
            auto_stop_check_interval_secs=_optional_env_int("AUTO_STOP_CHECK_INTERVAL_SECS", 900),
            auto_stop_empty_grace_secs=_optional_env_int("AUTO_STOP_EMPTY_GRACE_SECS", 300),
        )
