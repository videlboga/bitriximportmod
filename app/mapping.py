from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from .config import settings


class MappingStore:
    def __init__(self, mapping_path: Path) -> None:
        self._path = mapping_path
        self._cache: Dict[str, Dict[str, str]] | None = None
        self._mtime: float | None = None

    def _load(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(f"Mapping file not found: {self._path}")
        with self._path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("mapping.json must contain an object at the top level")
        self._cache = {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}
        self._mtime = self._path.stat().st_mtime

    def _ensure_loaded(self) -> None:
        needs_reload = False
        if self._cache is None:
            needs_reload = True
        else:
            try:
                current_mtime = self._path.stat().st_mtime
            except FileNotFoundError as exc:  # pragma: no cover - defensive
                raise FileNotFoundError(f"Mapping file not found: {self._path}") from exc
            if self._mtime is None or current_mtime > self._mtime:
                needs_reload = True
        if needs_reload:
            self._load()

    def get_fields_for_form(self, form_key: str) -> Optional[Dict[str, str]]:
        self._ensure_loaded()
        assert self._cache is not None
        return self._cache.get(form_key)


mapping_store = MappingStore(settings.mapping_file)
