"""Async client for the Oxygen Updater / OS Updater API v2.10."""

from __future__ import annotations

from typing import Any

import httpx

from .models import Device, UpdateData, UpdateMethod

API_BASE_URL = "https://oxygenupdater.com/api/v2.10/"
APP_VERSION = "7.1.0"
API_USER_AGENT = f"Oxygen_updater_{APP_VERSION}"
DOWNLOAD_USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 15)"


class OxygenUpdaterApi:
    """Async client communicating with https://oxygenupdater.com/api/v2.10/."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers={
                "User-Agent": API_USER_AGENT,
                "Accept": "application/json",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OxygenUpdaterApi:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def fetch_devices(self, filter_name: str = "enabled") -> list[Device]:
        resp = await self._client.get(f"devices/{filter_name}")
        resp.raise_for_status()
        return [Device.from_api(item) for item in resp.json()]

    async def fetch_update_methods(
        self, device_id: int, language: str = "en"
    ) -> list[UpdateMethod]:
        resp = await self._client.get(
            f"updateMethods/{device_id}", params={"language": language}
        )
        resp.raise_for_status()
        return [UpdateMethod.from_api(item) for item in resp.json()]

    async def fetch_most_recent_update_data(
        self, device_id: int, method_id: int
    ) -> UpdateData:
        resp = await self._client.get(f"mostRecentUpdateData/{device_id}/{method_id}")
        resp.raise_for_status()
        return UpdateData.from_api(resp.json())

    async def get_fresh_download_url(
        self, device_id: int, method_id: int, update_data: UpdateData
    ) -> str | None:
        payload = {
            "id": update_data.id,
            "otaVersionNumber": update_data.ota_version_number,
            "downloadUrl": update_data.download_url,
            "md5sum": update_data.md5sum,
        }
        resp = await self._client.post(
            f"updateDataDownloadUrl/{device_id}/{method_id}",
            params={"appVersion": APP_VERSION},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("success") and data.get("result"):
            return str(data["result"])
        return None


def pick_default_full_method(methods: list[UpdateMethod]) -> UpdateMethod | None:
    """Prefer 'Stable (full)', then any 'full' method, else first available."""
    for m in methods:
        name = m.name.lower()
        if "stable" in name and "full" in name:
            return m
    for m in methods:
        if m.is_full:
            return m
    return methods[0] if methods else None
