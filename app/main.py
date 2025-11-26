from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from starlette.datastructures import FormData, UploadFile

from .bitrix import BitrixError, bitrix_client
from .config import settings
from .logger import write_log_entry
from .mapping import mapping_store
from .tilda import TildaError, tilda_client

logger = logging.getLogger("bitrix_tilda")
logging.basicConfig(level=logging.INFO)


async def cache_bitrix_fields() -> None:
    fields = await bitrix_client.fetch_deal_fields()
    cache_path = settings.bitrix_fields_cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as handle:
        json.dump(fields, handle, ensure_ascii=False, indent=2)
    logger.info("Saved Bitrix24 deal field structure to %s", cache_path)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await cache_bitrix_fields()
    except Exception as exc:  # pragma: no cover - startup diagnostics
        logger.exception("Failed to cache Bitrix fields: %s", exc)
    yield
    await bitrix_client.close()
    await tilda_client.close()


app = FastAPI(title="Tilda ↔ Bitrix24 Bridge", lifespan=lifespan)


FORM_IDENTIFIER_KEYS = (
    "formname",
    "formid",
    "tildaformid",
    "tilda_form_id",
    "form_uid",
    "form_id",
    "lable",
)


def form_data_to_dict(form: FormData) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            result.setdefault(key, []).append(value.filename)
            continue
        if key in result:
            existing = result[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[key] = [existing, value]
        else:
            result[key] = value
    return result


def detect_form_key(payload: Dict[str, Any]) -> str:
    for key in FORM_IDENTIFIER_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot determine Tilda form identifier")


def normalize_value(value: Any) -> Optional[Any]:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    if isinstance(value, list):
        normalized_list = [normalize_value(item) for item in value]
        normalized_list = [item for item in normalized_list if item not in (None, "", [])]
        return normalized_list or None
    return value


def build_deal_fields(form_payload: Dict[str, Any], mapping: Dict[str, str], source_id: str) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "CATEGORY_ID": settings.bitrix_deal_category_id,
        "STAGE_ID": settings.bitrix_deal_stage_id,
        "SOURCE_ID": source_id,
    }
    for form_field, bitrix_field in mapping.items():
        value = normalize_value(form_payload.get(form_field))
        if value is None:
            continue
        fields[bitrix_field] = value
    return fields


async def forward_to_external(payload: Dict[str, Any]) -> None:
    if not settings.b24_outbound_webhook_url:
        return
    if settings.b24_forward_fields:
        payload = {field: payload.get(field) for field in settings.b24_forward_fields if field in payload}
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.post(settings.b24_outbound_webhook_url, json=payload)
            response.raise_for_status()
    except Exception as exc:  # pragma: no cover - background task
        logger.exception("Failed to forward Bitrix webhook: %s", exc)


async def _process_tilda_request(request: Request, forced_form_key: Optional[str] = None) -> JSONResponse:
    form = await request.form()
    payload = form_data_to_dict(form)
    form_key = forced_form_key or detect_form_key(payload)
    mapping = mapping_store.get_fields_for_form(form_key)
    if not mapping:
        write_log_entry(source=form_key, payload_raw=payload)
        return JSONResponse({"status": "ok", "note": f"Mapping for form '{form_key}' is not configured"})

    fields = build_deal_fields(payload, mapping, form_key)
    if len(fields) <= 3:  # only CATEGORY/STAGE/SOURCE present
        write_log_entry(source=form_key, payload_raw=payload, mapped_fields=fields)
        return JSONResponse({"status": "ok", "note": "No mapped fields with values were found"})

    try:
        deal_id = await bitrix_client.create_deal(fields)
    except BitrixError as exc:
        write_log_entry(source=form_key, payload_raw=payload, mapped_fields=fields)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    write_log_entry(source=form_key, payload_raw=payload, mapped_fields=fields, deal_id=deal_id)
    return JSONResponse({"deal_id": deal_id})


@app.post("/webhook/tilda")
async def handle_tilda_webhook(request: Request) -> JSONResponse:
    return await _process_tilda_request(request)


@app.post("/webhook/tilda/{form_key}")
async def handle_named_tilda_webhook(form_key: str, request: Request) -> JSONResponse:
    return await _process_tilda_request(request, forced_form_key=form_key)


async def _read_body_as_dict(request: Request) -> Dict[str, Any]:
    try:
        return await request.json()
    except Exception:
        form = await request.form()
        return form_data_to_dict(form)


@app.post("/webhook/b24")
async def handle_b24_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    payload = await _read_body_as_dict(request)
    write_log_entry(source="bitrix", payload_raw=payload)
    background_tasks.add_task(forward_to_external, payload)
    return JSONResponse({"status": "accepted"})


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


def _load_cached_fields() -> Dict[str, Any]:
    cache_path: Path = settings.bitrix_fields_cache
    if not cache_path.exists():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Bitrix fields cache is empty")
    with cache_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@app.get("/bitrix/fields")
async def get_bitrix_fields(refresh: bool = False) -> Dict[str, Any]:
    if refresh:
        await cache_bitrix_fields()
    return _load_cached_fields()


@app.get("/tilda/forms")
async def list_tilda_forms(project_id: Optional[int] = Query(default=None, ge=1)) -> Dict[str, Any]:
    try:
        forms = await tilda_client.list_forms(project_id=project_id)
    except TildaError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return {"forms": forms}


@app.get("/tilda/forms/{form_id}")
async def get_tilda_form(form_id: int) -> Dict[str, Any]:
    try:
        form = await tilda_client.get_form(form_id)
    except TildaError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return form
