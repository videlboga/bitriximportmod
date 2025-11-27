from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import settings


def _ensure_parent(path: Path) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


def write_log_entry(
    *,
    source: str,
    payload_raw: Dict[str, Any],
    mapped_fields: Optional[Dict[str, Any]] = None,
    deal_id: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "payload_raw": payload_raw,
    }
    if mapped_fields is not None:
        entry["mapped_fields"] = mapped_fields
    if deal_id is not None:
        entry["deal_id"] = deal_id
    if extra:
        entry.update(extra)

    log_path = settings.log_file
    _ensure_parent(log_path)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
