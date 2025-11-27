from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
        self._folder_cache: Dict[str, str] = {}
        self._root_folder_id: Optional[str] = None
        self._uploads_parent_id: Optional[str] = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        *,
        http_method: str = "POST",
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if http_method.upper() == "GET":
            response = await self._client.get(method, params=params)
        else:
            response = await self._client.post(method, params=params, json=json, data=data, files=files)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise BitrixError(payload.get("error_description", payload["error"]))
        return payload

    async def fetch_deal_fields(self) -> Dict[str, Any]:
        data = await self._request("crm.deal.fields", http_method="GET")
        return data["result"]

    async def create_deal(self, fields: Dict[str, Any]) -> int:
        payload = {"fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}}
        data = await self._request("crm.deal.add", json=payload)
        return int(data["result"])

    async def update_deal(self, deal_id: int, fields: Dict[str, Any]) -> None:
        payload = {"id": deal_id, "fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}}
        await self._request("crm.deal.update", json=payload)

    async def get_deal(self, deal_id: int) -> Dict[str, Any]:
        data = await self._request("crm.deal.get", http_method="GET", params={"id": deal_id})
        return data["result"]

    async def list_deals(self, filter_: Dict[str, Any], select: Optional[Iterable[str]] = None, start: int | None = 0) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"filter": filter_, "order": {"ID": "DESC"}}
        if select:
            payload["select"] = list(select)
        if start is not None:
            payload["start"] = start
        data = await self._request("crm.deal.list", json=payload)
        return data.get("result", [])

    async def list_contacts(self, filter_: Dict[str, Any], select: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"filter": filter_, "order": {"ID": "DESC"}}
        if select:
            payload["select"] = list(select)
        data = await self._request("crm.contact.list", json=payload)
        return data.get("result", [])

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        data = await self._request("crm.contact.get", http_method="GET", params={"id": contact_id})
        return data["result"]

    async def create_contact(self, fields: Dict[str, Any]) -> int:
        payload = {"fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}}
        data = await self._request("crm.contact.add", json=payload)
        return int(data["result"])

    async def ensure_storage_root(self) -> str:
        if self._root_folder_id:
            return self._root_folder_id
        data = await self._request(
            "disk.storage.getforuser",
            json={"id": settings.bitrix_disk_user_id},
        )
        storage = data.get("result")
        if not storage:
            raise BitrixError("Unable to resolve Bitrix Disk storage for current user")
        self._root_folder_id = str(storage["rootObjectId"])
        return self._root_folder_id

    async def ensure_uploads_parent(self) -> str:
        if self._uploads_parent_id:
            return self._uploads_parent_id
        root_id = await self.ensure_storage_root()
        folder_id = await self.ensure_folder(root_id, settings.bitrix_disk_root_folder_name)
        self._uploads_parent_id = folder_id
        return folder_id

    async def ensure_folder(self, parent_id: str, name: str) -> str:
        cache_key = f"{parent_id}:{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]
        data = await self._request("disk.folder.getchildren", json={"id": parent_id})
        for entry in data.get("result", []):
            if entry.get("TYPE") == "folder" and entry.get("NAME") == name:
                folder_id = str(entry["ID"])
                self._folder_cache[cache_key] = folder_id
                return folder_id
        created = await self._request(
            "disk.folder.add",
            json={"data": {"NAME": name, "PARENT_ID": parent_id}},
        )
        folder_id = str(created["result"]["ID"])
        self._folder_cache[cache_key] = folder_id
        return folder_id

    async def upload_file(self, folder_id: str, file_path: Path) -> str:
        with file_path.open("rb") as handle:
            files = {"file": (file_path.name, handle, "application/octet-stream")}
            data = await self._request(
                "disk.folder.uploadfile",
                data={"id": folder_id, "generateUniqueName": "true"},
                files=files,
            )
        return str(data["result"]["ID"])


bitrix_client = BitrixClient()
