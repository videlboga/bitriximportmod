from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from starlette.datastructures import FormData, UploadFile

from PIL import Image

from .bitrix import BitrixError, bitrix_client
from .config import settings
from .logger import write_log_entry
from .mapping import FormMapping, mapping_store
from .tilda import TildaError, tilda_client

logger = logging.getLogger("bitrix_tilda")
logging.basicConfig(level=logging.INFO)


@dataclass
class SavedUpload:
    field: str
    filename: str
    path: Path
    content_type: Optional[str]
    compressed: bool = False


@dataclass
class SearchValues:
    inn: Optional[str]
    company: Optional[str]
    phones: List[str]
    emails: List[str]


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
DEFAULT_PARTICIPATION_FIELD = "format"
DEFAULT_FILE_FIELD_MAP = {
    "Показ": settings.bitrix_show_file_field,
    "Маркет": settings.bitrix_market_file_field,
}
COMPRESSION_FIELDS = {"illustrations_show", "illustrations_market"}
FILE_TARGET_FIELDS = {
    "illustrations_show": settings.bitrix_show_file_field,
    "illustrations_market": settings.bitrix_market_file_field,
    "linesheet": settings.bitrix_linesheet_file_field,
}
FORM_KEY_ALIASES = {
    "tilda_form_1": "tilda_form_main",
    "tilda_form_2": "tilda_form_secondary",
}


def create_temp_directory() -> Path:
    settings.upload_temp_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="tilda_", dir=settings.upload_temp_dir))


def compress_image_inplace(path: Path) -> bool:
    try:
        image = Image.open(path)
    except Exception:
        return False
    try:
        image = image.convert("RGB")
        temp_path = path.with_suffix(".tmp.jpg")
        image.save(temp_path, format="JPEG", optimize=True, quality=85)
        image.close()
        original_name = path.name
        os.replace(temp_path, path)
        return True
    except Exception:
        return False


async def persist_upload(field: str, upload: UploadFile, destination: Path) -> SavedUpload:
    filename = upload.filename or f"upload_{uuid.uuid4().hex}"
    safe_name = re.sub(r"[^\w.\-]+", "_", Path(filename).name)
    target = destination / safe_name
    with target.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    await upload.close()
    compressed = False
    if field in COMPRESSION_FIELDS:
        compressed = compress_image_inplace(target)
    return SavedUpload(field=field, filename=safe_name, path=target, content_type=upload.content_type, compressed=compressed)


async def parse_form_data(form: FormData, temp_dir: Path) -> tuple[Dict[str, Any], List[SavedUpload]]:
    payload: Dict[str, Any] = {}
    uploads: List[SavedUpload] = []
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            saved = await persist_upload(key, value, temp_dir)
            uploads.append(saved)
            continue
        if key in payload:
            existing = payload[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                payload[key] = [existing, value]
        else:
            payload[key] = value
    return payload, uploads


def normalize_form_key(name: str) -> str:
    return FORM_KEY_ALIASES.get(name, name)


def detect_form_key(payload: Dict[str, Any], forced: Optional[str] = None) -> str:
    if forced:
        return normalize_form_key(forced)
    for key in FORM_IDENTIFIER_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_form_key(value.strip())
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


def extract_first(payload: Dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = normalize_value(payload.get(key))
        if isinstance(value, list):
            if value:
                entry = normalize_value(value[0])
                if entry:
                    return str(entry)
        elif value is not None:
            return str(value)
    return None


def extract_list(payload: Dict[str, Any], keys: tuple[str, ...]) -> List[str]:
    collected: List[str] = []
    for key in keys:
        value = normalize_value(payload.get(key))
        if isinstance(value, list):
            for item in value:
                if item:
                    collected.append(str(item))
        elif value is not None:
            collected.append(str(value))
    return collected


def normalize_phone(value: str) -> str:
    digits = re.sub(r"[^0-9+]", "", value)
    digits = digits.replace("+7", "7", 1) if digits.startswith("+7") else digits
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return digits


def build_search_values(mapping: FormMapping, payload: Dict[str, Any]) -> SearchValues:
    inn = extract_first(payload, mapping.search.inn_keys)
    company = extract_first(payload, mapping.search.company_keys)
    phones = [normalize_phone(item) for item in extract_list(payload, mapping.search.phone_keys)]
    emails = extract_list(payload, mapping.search.email_keys)
    return SearchValues(inn=inn, company=company, phones=phones, emails=emails)


def get_uploads_for_field(uploads: List[SavedUpload], field_name: str) -> List[SavedUpload]:
    return [upload for upload in uploads if upload.field == field_name]


async def find_existing_contact(search: SearchValues) -> Optional[Dict[str, Any]]:
    for phone in search.phones:
        contacts = await bitrix_client.list_contacts({"PHONE": phone}, select=["ID", "COMPANY_ID"])
        if contacts:
            contact = contacts[0]
            contact_detail = await bitrix_client.get_contact(int(contact["ID"]))
            return contact_detail
    for email in search.emails:
        contacts = await bitrix_client.list_contacts({"EMAIL": email}, select=["ID", "COMPANY_ID"])
        if contacts:
            contact = contacts[0]
            contact_detail = await bitrix_client.get_contact(int(contact["ID"]))
            return contact_detail
    if search.company:
        contact = await find_contact_by_company(search.company)
        if contact:
            return contact
    return None


async def find_contact_by_company(company: str) -> Optional[Dict[str, Any]]:
    normalized = company.strip()
    if not normalized:
        return None
    for field in settings.contact_company_fields or ():
        contacts = await bitrix_client.list_contacts({field: normalized}, select=["ID", "COMPANY_ID"])
        if contacts:
            contact = contacts[0]
            contact_detail = await bitrix_client.get_contact(int(contact["ID"]))
            return contact_detail
    return None


def assign_contact_field(container: Dict[str, Any], field_name: str, value: Any) -> None:
    if field_name.upper() == "PHONE":
        numbers = value if isinstance(value, list) else [value]
        container.setdefault("PHONE", [])
        for number in numbers:
            normalized = normalize_phone(str(number))
            if normalized:
                container["PHONE"].append({"VALUE": normalized, "VALUE_TYPE": "WORK"})
        return
    if field_name.upper() == "EMAIL":
        emails = value if isinstance(value, list) else [value]
        container.setdefault("EMAIL", [])
        for email in emails:
            email_str = str(email).strip()
            if email_str:
                container["EMAIL"].append({"VALUE": email_str, "VALUE_TYPE": "WORK"})
        return
    container[field_name] = value


def build_contact_payload(mapping: FormMapping, payload: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for form_field, bitrix_field in mapping.contact_fields.items():
        value = normalize_value(payload.get(form_field))
        if value is None:
            continue
        assign_contact_field(fields, bitrix_field, value)
    return fields


async def ensure_contact(mapping: FormMapping, payload: Dict[str, Any], search: SearchValues) -> tuple[Optional[int], Optional[int]]:
    contact = await find_existing_contact(search)
    if contact:
        return int(contact["ID"]), int(contact.get("COMPANY_ID") or 0) or None

    contact_fields = build_contact_payload(mapping, payload)
    if search.phones and "PHONE" not in contact_fields:
        assign_contact_field(contact_fields, "PHONE", search.phones[:1])
    if search.emails and "EMAIL" not in contact_fields:
        assign_contact_field(contact_fields, "EMAIL", search.emails[:1])
    if not contact_fields:
        return None, None
    contact_fields.setdefault("NAME", contact_fields.get("UF_NAME", "Заявитель из Тильды"))
    contact_id = await bitrix_client.create_contact(contact_fields)
    return contact_id, contact_fields.get("COMPANY_ID")


async def find_base_deal(search: SearchValues) -> Optional[Dict[str, Any]]:
    base_filter = {"CATEGORY_ID": settings.bitrix_category_base_id}
    if search.inn:
        inn_filter = base_filter | {settings.bitrix_inn_field: search.inn}
        deals = await bitrix_client.list_deals(inn_filter, select=["ID", "COMPANY_ID", "CONTACT_ID"])
        if deals:
            return deals[0]
    if search.company:
        for field in settings.bitrix_company_fields or (settings.bitrix_title_field,):
            company_filter = dict(base_filter)
            company_filter[field] = search.company
            deals = await bitrix_client.list_deals(company_filter, select=["ID", "COMPANY_ID", "CONTACT_ID"])
            if deals:
                return deals[0]
    if search.phones or search.emails or search.company:
        contact = await find_existing_contact(search)
        if contact:
            contact_deals = await bitrix_client.list_deals(
                {"CATEGORY_ID": settings.bitrix_category_base_id, "CONTACT_ID": contact["ID"]},
                select=["ID", "COMPANY_ID", "CONTACT_ID"],
            )
            if contact_deals:
                return contact_deals[0]
            company_id = contact.get("COMPANY_ID")
            if company_id:
                company_deals = await bitrix_client.list_deals(
                    {"CATEGORY_ID": settings.bitrix_category_base_id, "COMPANY_ID": company_id},
                    select=["ID", "COMPANY_ID", "CONTACT_ID"],
                )
                if company_deals:
                    return company_deals[0]
    return None


def build_deal_fields(
    form_payload: Dict[str, Any],
    mapping: Dict[str, str],
    *,
    base_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    fields: Dict[str, Any] = dict(base_fields or {})
    for form_field, bitrix_field in mapping.items():
        value = normalize_value(form_payload.get(form_field))
        if value is None:
            continue
        existing = fields.get(bitrix_field)
        if existing is None:
            fields[bitrix_field] = value
            continue
        values: List[Any] = []
        if isinstance(existing, list):
            values.extend(existing)
        else:
            values.append(existing)
        if isinstance(value, list):
            values.extend(value)
        else:
            values.append(value)
        fields[bitrix_field] = values
    return fields


def extract_participation_types(form_mapping: FormMapping, payload: Dict[str, Any]) -> List[str]:
    field_name = form_mapping.participation_field or DEFAULT_PARTICIPATION_FIELD
    raw_value = payload.get(field_name)
    values: List[str] = []
    tokens: List[str] = []
    if isinstance(raw_value, list):
        tokens = [str(item) for item in raw_value if item]
    elif isinstance(raw_value, str):
        tokens = [raw_value]
    for token in tokens:
        parts = re.split(r"[;,/]+", token)
        for part in parts:
            cleaned = part.strip()
            if not cleaned:
                continue
            for keyword in settings.participation_keywords:
                if keyword.lower() in cleaned.lower():
                    if keyword not in values:
                        values.append(keyword)
    return values


async def upload_files_for_deal(deal_id: int, uploads: List[SavedUpload], target_field: Optional[str]) -> List[str]:
    if not uploads or not target_field:
        return []
    parent = await bitrix_client.ensure_uploads_parent()
    folder = await bitrix_client.ensure_folder(parent, f"deal_{deal_id}")
    file_ids: List[str] = []
    for upload in uploads:
        file_id = await bitrix_client.upload_file(folder, upload.path)
        file_ids.append(file_id)
    if file_ids:
        await bitrix_client.update_deal(deal_id, {target_field: file_ids})
    return file_ids


async def handle_primary_form(
    form_key: str,
    mapping: FormMapping,
    payload: Dict[str, Any],
    uploads: List[SavedUpload],
) -> Dict[str, Any]:
    search_values = build_search_values(mapping, payload)
    base_deal = await find_base_deal(search_values)
    if base_deal:
        await bitrix_client.update_deal(int(base_deal["ID"]), {"STAGE_ID": settings.bitrix_stage_base_won})
        write_log_entry(
            source=form_key,
            payload_raw=payload,
            mapped_fields={"base_deal": int(base_deal["ID"])},
            extra={"action": "base_deal_updated"},
        )
    participation = extract_participation_types(mapping, payload)
    if not participation:
        write_log_entry(source=form_key, payload_raw=payload, extra={"action": "no_participation"})
        raise HTTPException(status_code=400, detail="No participation formats were provided")

    contact_id, company_id = await ensure_contact(mapping, payload, search_values)
    created_deals: List[int] = []
    file_fields = {**DEFAULT_FILE_FIELD_MAP, **mapping.file_field_map}
    company_label = search_values.company or payload.get("brands_name") or "Без названия"

    for entry in participation:
        base_fields = {
            "CATEGORY_ID": settings.bitrix_category_applications_id,
            "STAGE_ID": settings.bitrix_stage_applications_new,
            "SOURCE_ID": form_key,
        }
        deal_fields = build_deal_fields(payload, mapping.deal_fields, base_fields=base_fields)
        deal_fields[settings.bitrix_title_field] = f"Заявка: {company_label} — {entry}"
        if company_id:
            deal_fields.setdefault("COMPANY_ID", company_id)
        if contact_id:
            deal_fields["CONTACT_ID"] = contact_id
        deal_id = await bitrix_client.create_deal(deal_fields)
        created_deals.append(deal_id)
        file_summary: Dict[str, List[str]] = {}
        target_field = file_fields.get(entry)
        if entry == "Показ" and target_field:
            show_uploads = get_uploads_for_field(uploads, "illustrations_show")
            ids = await upload_files_for_deal(deal_id, show_uploads, target_field)
            if ids:
                file_summary["illustrations_show"] = ids
        if entry in ("Маркет", "Шоурум"):
            market_field = file_fields.get("Маркет")
            market_uploads = get_uploads_for_field(uploads, "illustrations_market")
            ids = await upload_files_for_deal(deal_id, market_uploads, market_field)
            if ids:
                file_summary["illustrations_market"] = ids
        linesheet_field = mapping.file_field_map.get("linesheet") if mapping.file_field_map else None
        if not linesheet_field:
            linesheet_field = settings.bitrix_linesheet_file_field
        linesheet_uploads = get_uploads_for_field(uploads, "linesheet")
        linesheet_ids = await upload_files_for_deal(deal_id, linesheet_uploads, linesheet_field)
        if linesheet_ids:
            file_summary["linesheet"] = linesheet_ids
        write_log_entry(
            source=form_key,
            payload_raw=payload,
            mapped_fields=deal_fields,
            deal_id=deal_id,
            extra={
                "action": "deal_created",
                "participation": entry,
                "files": file_summary,
            },
        )

    return {"status": "created", "deal_ids": created_deals}


async def handle_secondary_form(
    form_key: str,
    mapping: FormMapping,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    search_values = build_search_values(mapping, payload)
    contact_id, company_id = await ensure_contact(mapping, payload, search_values)
    base_fields = {
        "CATEGORY_ID": settings.bitrix_category_secondary_id,
        "STAGE_ID": settings.bitrix_stage_secondary_new,
        "SOURCE_ID": form_key,
    }
    deal_fields = build_deal_fields(payload, mapping.deal_fields, base_fields=base_fields)
    company_label = search_values.company or payload.get("brands_name") or "Без названия"
    deal_fields[settings.bitrix_title_field] = f"Заявка: {company_label}"
    if company_id:
        deal_fields.setdefault("COMPANY_ID", company_id)
    if contact_id:
        deal_fields["CONTACT_ID"] = contact_id
    deal_id = await bitrix_client.create_deal(deal_fields)
    write_log_entry(
        source=form_key,
        payload_raw=payload,
        mapped_fields=deal_fields,
        deal_id=deal_id,
        extra={"action": "secondary_deal_created"},
    )
    return {"status": "created", "deal_id": deal_id}


async def process_tilda_request(request: Request, forced_form_key: Optional[str] = None) -> JSONResponse:
    temp_dir = create_temp_directory()
    try:
        form = await request.form()
        payload, uploads = await parse_form_data(form, temp_dir)
        form_key = detect_form_key(payload, forced_form_key)
        mapping = mapping_store.get_form(form_key)
        if not mapping:
            write_log_entry(source=form_key, payload_raw=payload, extra={"action": "mapping_not_found"})
            return JSONResponse({"status": "ok", "note": f"Mapping for form '{form_key}' is not configured"})

        if mapping.kind == "secondary":
            result = await handle_secondary_form(form_key, mapping, payload)
        else:
            result = await handle_primary_form(form_key, mapping, payload, uploads)
        return JSONResponse(result)
    except BitrixError as exc:
        write_log_entry(source=forced_form_key or "unknown", payload_raw={}, extra={"error": str(exc)})
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.post("/webhook/tilda")
async def handle_tilda_webhook(request: Request) -> JSONResponse:
    return await process_tilda_request(request)


@app.post("/webhook/tilda/{form_key}")
async def handle_named_tilda_webhook(form_key: str, request: Request) -> JSONResponse:
    return await process_tilda_request(request, forced_form_key=form_key)


async def _read_body_as_dict(request: Request) -> Dict[str, Any]:
    try:
        return await request.json()
    except Exception:
        temp_dir = create_temp_directory()
        try:
            form = await request.form()
            payload, _ = await parse_form_data(form, temp_dir)
            return payload
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


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
