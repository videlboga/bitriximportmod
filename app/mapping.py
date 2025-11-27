from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from .config import settings


@dataclass
class SearchFields:
    inn_keys: Tuple[str, ...] = ()
    company_keys: Tuple[str, ...] = ()
    phone_keys: Tuple[str, ...] = ()
    email_keys: Tuple[str, ...] = ()


@dataclass
class FormMapping:
    name: str
    deal_fields: Dict[str, str]
    contact_fields: Dict[str, str] = field(default_factory=dict)
    kind: str = "primary"
    participation_field: Optional[str] = None
    file_field_map: Dict[str, str] = field(default_factory=dict)
    search: SearchFields = field(default_factory=SearchFields)

    def deal_field_for_bitrix(self, bitrix_field: str) -> Tuple[str, ...]:
        return tuple(key for key, value in self.deal_fields.items() if value == bitrix_field)

    def contact_field_for_bitrix(self, bitrix_field: str) -> Tuple[str, ...]:
        return tuple(key for key, value in self.contact_fields.items() if value == bitrix_field)


class MappingStore:
    def __init__(self, mapping_path: Path) -> None:
        self._path = mapping_path
        self._cache: Dict[str, FormMapping] | None = None
        self._mtime: float | None = None

    def _normalize_sequence(self, data: object) -> Tuple[str, ...]:
        if data is None:
            return ()
        if isinstance(data, str):
            return (data,)
        if isinstance(data, Iterable):
            values = []
            for item in data:
                if isinstance(item, str):
                    values.append(item)
            return tuple(values)
        raise ValueError("Search field configuration must be string or iterable of strings")

    def _build_search_fields(self, mapping: FormMapping, config: Dict[str, object]) -> SearchFields:
        inn_keys = self._normalize_sequence(config.get("inn"))
        if not inn_keys:
            inn_keys = mapping.deal_field_for_bitrix(settings.bitrix_inn_field)
        company_keys = self._normalize_sequence(config.get("company"))
        if not company_keys:
            company_keys = mapping.deal_field_for_bitrix(settings.bitrix_title_field)
        phone_keys = self._normalize_sequence(config.get("phone"))
        if not phone_keys:
            phone_keys = mapping.contact_field_for_bitrix("PHONE")
        email_keys = self._normalize_sequence(config.get("email"))
        if not email_keys:
            email_keys = mapping.contact_field_for_bitrix("EMAIL")
        return SearchFields(
            inn_keys=inn_keys,
            company_keys=company_keys,
            phone_keys=phone_keys,
            email_keys=email_keys,
        )

    def _parse_form(self, name: str, raw: object) -> FormMapping:
        if not isinstance(raw, dict):
            raise ValueError("Each form entry must be an object")
        if raw and all(isinstance(value, str) for value in raw.values()):
            deal_fields = {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
            mapping = FormMapping(name=name, deal_fields=deal_fields)
            mapping.search = self._build_search_fields(mapping, {})
            return mapping

        deal_fields = raw.get("deal_fields") or raw.get("fields") or {}
        if not isinstance(deal_fields, dict):
            raise ValueError(f"Form '{name}' deal_fields must be an object")
        contact_fields = raw.get("contact_fields") or raw.get("contact") or {}
        if contact_fields and not isinstance(contact_fields, dict):
            raise ValueError(f"Form '{name}' contact_fields must be an object")
        kind = str(raw.get("kind", "primary"))
        participation_field = raw.get("participation_field")
        if participation_field is not None:
            participation_field = str(participation_field)
        file_field_map = raw.get("file_fields") or raw.get("attachments") or {}
        if file_field_map and not isinstance(file_field_map, dict):
            raise ValueError(f"Form '{name}' file_fields must be an object")
        mapping = FormMapping(
            name=name,
            deal_fields={str(k): str(v) for k, v in deal_fields.items() if isinstance(v, str)},
            contact_fields={str(k): str(v) for k, v in (contact_fields or {}).items() if isinstance(v, str)},
            kind=kind,
            participation_field=participation_field,
            file_field_map={str(k): str(v) for k, v in (file_field_map or {}).items() if isinstance(v, str)},
        )
        mapping.search = self._build_search_fields(mapping, raw.get("search") or {})
        return mapping

    def _load(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(f"Mapping file not found: {self._path}")
        with self._path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("mapping.json must contain an object at the top level")
        cache: Dict[str, FormMapping] = {}
        for form_name, raw in data.items():
            cache[str(form_name)] = self._parse_form(str(form_name), raw)
        self._cache = cache
        self._mtime = self._path.stat().st_mtime

    def _ensure_loaded(self) -> None:
        needs_reload = False
        if self._cache is None:
            needs_reload = True
        else:
            current_mtime = self._path.stat().st_mtime
            if self._mtime is None or current_mtime > self._mtime:
                needs_reload = True
        if needs_reload:
            self._load()

    def get_form(self, form_key: str) -> Optional[FormMapping]:
        self._ensure_loaded()
        assert self._cache is not None
        return self._cache.get(form_key)


mapping_store = MappingStore(settings.mapping_file)
