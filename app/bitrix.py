from __future__ import annotations

from typing import Any, Dict

import httpx

from .config import settings


class BitrixError(RuntimeError):
    pass


class BitrixClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.bitrix_webhook_base_url,
            timeout=settings.request_timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_deal_fields(self) -> Dict[str, Any]:
        response = await self._client.get("crm.deal.fields")
        response.raise_for_status()
        data = response.json()
        if not data.get("result"):
            raise BitrixError(f"Unexpected response while fetching fields: {data}")
        return data["result"]

    async def create_deal(self, fields: Dict[str, Any]) -> int:
        payload = {
            "fields": fields,
            "params": {"REGISTER_SONET_EVENT": "N"},
        }
        response = await self._client.post("crm.deal.add", json=payload)
        response.raise_for_status()
        data = response.json()
        if "result" not in data:
            raise BitrixError(f"Unexpected response while creating deal: {data}")
        return int(data["result"])


bitrix_client = BitrixClient()
