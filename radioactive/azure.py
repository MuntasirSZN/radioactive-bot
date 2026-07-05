from __future__ import annotations

import logging
from enum import StrEnum

import httpx
from azure.identity.aio import ClientSecretCredential

from radioactive.config import Config

logger = logging.getLogger(__name__)


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
    def __init__(self, config: Config) -> None:
        self._config = config
        self._http = httpx.AsyncClient()
        self._credential = ClientSecretCredential(
            tenant_id=config.azure_tenant_id,
            client_id=config.azure_client_id,
            client_secret=config.azure_client_secret,
        )

    async def _bearer_token(self) -> str:
        token = await self._credential.get_token("https://management.azure.com/.default")
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

    async def deallocate_vm(self) -> None:
        await self._post_vm_action("deallocate")

    async def power_state(self) -> PowerState | None:
        token = await self._bearer_token()
        url = self._vm_url("instanceView")
        resp = await self._http.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.is_error:
            raise AzureVmError(f"Azure instanceView failed with {resp.status_code}: {resp.text}")

        data = resp.json()
        for status in data.get("statuses", []):
            code: str | None = status.get("code")
            if code and code.startswith("PowerState/"):
                return PowerState(code.removeprefix("PowerState/"))
        return None

    async def _post_vm_action(self, action: str) -> None:
        token = await self._bearer_token()
        url = self._vm_url(action)
        resp = await self._http.post(url, headers={"Authorization": f"Bearer {token}"})
        # Azure returns 202 Accepted for async operations
        if not (resp.is_success or resp.status_code == 202):
            raise AzureVmError(f"Azure VM {action} failed with {resp.status_code}: {resp.text}")

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._credential.close()
