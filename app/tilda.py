from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from .config import settings


class TildaError(RuntimeError):
    pass


class TildaClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.tilda_api_base_url,
            timeout=settings.request_timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _auth_params(self) -> Dict[str, str]:
        if not settings.tilda_public_key or not settings.tilda_secret_key:
            raise TildaError("Tilda API keys are not configured")
        return {
            "publickey": settings.tilda_public_key,
            "secretkey": settings.tilda_secret_key,
        }

    async def list_forms(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        params = self._auth_params()
        project = project_id or settings.tilda_project_id
        if project is not None:
            params["projectid"] = project
        response = await self._client.get("project/getformslist/", params=params)
        response.raise_for_status()
        data = response.json()
        result = data.get("result")
        if result is None:
            raise TildaError(f"Unexpected response from Tilda: {data}")
        if isinstance(result, dict) and "forms" in result:
            forms = result["forms"]
        else:
            forms = result
        if not isinstance(forms, list):
            raise TildaError(f"Tilda did not return a list of forms: {data}")
        return forms

    async def get_form(self, form_id: int) -> Dict[str, Any]:
        params = self._auth_params()
        params["formid"] = form_id
        response = await self._client.get("form/getform/", params=params)
        response.raise_for_status()
        data = response.json()
        result = data.get("result")
        if not isinstance(result, dict):
            raise TildaError(f"Unexpected response from Tilda: {data}")
        return result


tilda_client = TildaClient()
