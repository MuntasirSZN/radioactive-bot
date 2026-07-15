from __future__ import annotations

import logging
import time
from enum import StrEnum

import httpx

from radioactive.config import Config

logger = logging.getLogger(__name__)

# Lazy import for azure-identity — heavy module, only loaded when used
_ClientSecretCredential = None


def _get_credential(tenant_id: str, client_id: str, client_secret: str):
    """Lazy import of azure-identity to reduce startup memory."""
    global _ClientSecretCredential
    if _ClientSecretCredential is None:
        from azure.identity.aio import ClientSecretCredential

        _ClientSecretCredential = ClientSecretCredential
    return _ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )


class PowerState(StrEnum):
    RUNNING = "running"
    STARTING = "starting"
    DEALLOCATED = "deallocated"
    DEALLOCATING = "deallocating"
    STOPPED = "stopped"
    STOPPING = "stopping"


class AzureVmError(Exception):
    """Azure API call failed."""


class AzureVmClient:
    """Thin Azure VM client with cached power state and minimal connection overhead."""

    __slots__ = ("_config", "_http", "_credential", "_token_cache", "_power_cache")

    _POWER_CACHE_TTL = 5  # seconds — avoid hammering Azure on rapid command spam

    def __init__(self, config: Config) -> None:
        self._config = config
        self._http = httpx.AsyncClient(
            limits=httpx.Limits(
                max_keepalive_connections=1,
                max_connections=2,
            ),
            http2=False,  # Azure mgmt API doesn't need HTTP/2
        )
        self._credential = _get_credential(
            config.azure_tenant_id,
            config.azure_client_id,
            config.azure_client_secret,
        )
        # (expires_on_unix_ts, token_str) — None means unset
        self._token_cache: tuple[float, str] | None = None
        # (monotonic_ts, PowerState | None) — None means unset
        self._power_cache: tuple[float, PowerState | None] | None = None

    async def _bearer_token(self) -> str:
        """Return a cached bearer token, refreshing when close to expiry."""
        if self._token_cache is not None:
            expires_on, token = self._token_cache
            # Use the actual token expiry from Azure — refresh 5 min early
            if time.time() < expires_on - 300:
                return token
        token = await self._credential.get_token("https://management.azure.com/.default")
        self._token_cache = (token.expires_on, token.token)
        return token.token

    def _vm_url(self, action: str = "") -> str:
        base = (
            f"https://management.azure.com/subscriptions/{self._config.azure_subscription_id}"
            f"/resourceGroups/{self._config.azure_resource_group}"
            f"/providers/Microsoft.Compute/virtualMachines/{self._config.azure_vm_name}"
        )
        if action:
            base = f"{base}/{action}"
        return f"{base}?api-version=2024-03-01"

    async def start_vm(self) -> None:
        await self._post_vm_action("start")
        self._invalidate_power_cache()

    async def deallocate_vm(self) -> None:
        await self._post_vm_action("deallocate")
        self._invalidate_power_cache()

    async def power_state(self) -> PowerState | None:
        """Return the VM power state, cached for ``_POWER_CACHE_TTL`` seconds."""
        now = time.monotonic()
        if self._power_cache is not None:
            ts, state = self._power_cache
            if now - ts < self._POWER_CACHE_TTL:
                return state

        token = await self._bearer_token()
        url = self._vm_url("instanceView")
        resp = await self._http.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.is_error:
            raise AzureVmError(f"Azure instanceView failed with {resp.status_code}: {resp.text}")

        data = resp.json()
        state: PowerState | None = None
        for status in data.get("statuses", []):
            code: str | None = status.get("code")
            if code and code.startswith("PowerState/"):
                state = PowerState(code.removeprefix("PowerState/"))
                break

        self._power_cache = (now, state)
        return state

    def _invalidate_power_cache(self) -> None:
        self._power_cache = None

    async def _post_vm_action(self, action: str) -> None:
        token = await self._bearer_token()
        url = self._vm_url(action)
        resp = await self._http.post(url, headers={"Authorization": f"Bearer {token}"})
        if not (resp.is_success or resp.status_code == 202):
            raise AzureVmError(f"Azure VM {action} failed with {resp.status_code}: {resp.text}")

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._credential.close()
