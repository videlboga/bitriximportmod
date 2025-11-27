from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bitrix_webhook_base_url: str
    b24_outbound_webhook_url: Optional[str] = None
    b24_forward_fields: tuple[str, ...] = ()
    tilda_api_base_url: str = "https://api.tilda.cc/"
    tilda_public_key: Optional[str] = None
    tilda_secret_key: Optional[str] = None
    tilda_project_id: Optional[int] = None
    mapping_file: Path = Path("mapping.json")
    log_file: Path = Path("data/events.log")
    bitrix_fields_cache: Path = Path("data/bitrix_fields.json")
    request_timeout_seconds: float = 15.0
    upload_temp_dir: Path = Path("data/tmp_uploads")
    bitrix_category_base_id: int = 6
    bitrix_category_applications_id: int = 8
    bitrix_category_secondary_id: int = 12
    bitrix_stage_base_won: str = "C6:WON"
    bitrix_stage_applications_new: str = "C8:NEW"
    bitrix_stage_secondary_new: str = "C12:NEW"
    bitrix_show_file_field: str = "UF_CRM_1764235976815"
    bitrix_market_file_field: str = "UF_CRM_1764236005770"
    bitrix_inn_field: str = "UF_INN"
    bitrix_title_field: str = "TITLE"
    bitrix_disk_user_id: int = 1
    bitrix_disk_root_folder_name: str = "TildaUploads"
    participation_keywords: tuple[str, ...] = ("Показ", "Маркет", "Шоурум")

    model_config = SettingsConfigDict(env_file=".env", env_prefix="BITRIX_TILDA_", env_file_encoding="utf-8")

    @field_validator("b24_forward_fields", mode="before")
    @classmethod
    def _split_forward_fields(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
            return tuple(items)
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value)
        raise ValueError("Unsupported value for b24_forward_fields")


settings = Settings()
