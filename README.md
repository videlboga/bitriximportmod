# Tilda → Bitrix24 Bridge

Небольшой сервис на FastAPI, который принимает вебхуки из Тильды, создаёт/обновляет сделки в разных воронках Bitrix24, загружает файлы в Bitrix.Disk и пересылает события дальше при необходимости.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Перед запуском заполните `.env` (минимум URL вебхука Bitrix24) и при необходимости переопределите остальные параметры:

```
BITRIX_TILDA_BITRIX_WEBHOOK_BASE_URL=https://example.bitrix24.ru/rest/22/secret/
BITRIX_TILDA_BITRIX_CATEGORY_BASE_ID=6
BITRIX_TILDA_BITRIX_CATEGORY_APPLICATIONS_ID=8
BITRIX_TILDA_BITRIX_CATEGORY_SECONDARY_ID=12
BITRIX_TILDA_BITRIX_STAGE_BASE_WON=C6:WON
BITRIX_TILDA_BITRIX_STAGE_APPLICATIONS_NEW=C8:NEW
BITRIX_TILDA_BITRIX_STAGE_SECONDARY_NEW=C12:NEW
BITRIX_TILDA_BITRIX_SHOW_FILE_FIELD=UF_CRM_1764235976815
BITRIX_TILDA_BITRIX_MARKET_FILE_FIELD=UF_CRM_1764236005770
BITRIX_TILDA_BITRIX_DISK_USER_ID=22
BITRIX_TILDA_BITRIX_DISK_ROOT_FOLDER_NAME=TildaUploads
BITRIX_TILDA_BITRIX_DISK_USE_COMMON=true
BITRIX_TILDA_B24_OUTBOUND_WEBHOOK_URL=https://external.example.com/webhook     # опционально
BITRIX_TILDA_B24_FORWARD_FIELDS=id,UF_CUSTOM_123                              # опционально
BITRIX_TILDA_TILDA_PUBLIC_KEY=public_key_value                                 # для /tilda/forms
BITRIX_TILDA_TILDA_SECRET_KEY=secret_key_value                                 # для /tilda/forms
BITRIX_TILDA_TILDA_PROJECT_ID=12345                                            # опционально
```

## mapping.json

Файл `mapping.json` — единственный источник информации о полях формы. Для каждой формы задаётся объект следующего вида:

```json
{
  "tilda_form_main": {
    "kind": "primary",
    "participation_field": "format",
    "deal_fields": {
      "brands_name": "TITLE",
      "INN": "UF_INN"
    },
    "contact_fields": {
      "Contact_person": "NAME",
      "Contact_cell": "PHONE",
      "email": "EMAIL"
    },
    "file_fields": {
      "Показ": "UF_CRM_1764235976815",
      "Маркет": "UF_CRM_1764236005770"
    },
    "search": {
      "inn": ["INN"],
      "company": ["brands_name", "jur_lico"],
      "phone": ["Contact_cell"],
      "email": ["email"]
    }
  },
  "tilda_form_secondary": {
    "kind": "secondary",
    "deal_fields": {
      "brands_name": "TITLE"
    },
    "contact_fields": {
      "email": "EMAIL"
    },
    "search": {
      "company": ["brands_name"],
      "email": ["email"]
    }
  }
}
```

* `kind` — `primary` (основная форма с файлами, воронками 6/8) или `secondary` (упрощённая форма → CATEGORY_ID=12).
* `deal_fields` — соответствия «поле формы → поле сделки в Bitrix».
* `contact_fields` — какие поля пойдут в контакт (`PHONE`/`EMAIL` автоматически формируются в нужный формат).
* `participation_field` — поле формы, где перечислены типы участия (Показ / Маркет / Шоурум). Если не указано, используется `format`.
* `file_fields` — override для UF-полей, куда сохраняются ID файлов Disk по конкретному типу участия.
* `search` — список полей формы, которые нужны для поиска существующих сделок/контактов (ИНН, название, телефон, e-mail).

## Поведение сервиса

- `POST /webhook/tilda` (или `POST /webhook/tilda/<имя_формы>`) получает multipart/form-data, парсит поля и файлы, определяет тип формы.
  - Для `primary` форм выполняет весь пайплайн: поиск и перевод сделки в воронке «База» (CATEGORY_ID=6), создание 1–3 сделок в CATEGORY_ID=8 (по выбранным форматам «Показ/Маркет/Шоурум»), загрузка файлов в Bitrix.Disk и привязка к UF-полям, создание/привязка контакта.
  - Для `secondary` форм просто создаёт сделку в CATEGORY_ID=12 со стадией «Получена новая заявка».
  - Все шаги подробно логируются в `data/events.log`.
- `POST /webhook/b24` — входящие вебхуки из Bitrix24 (например, переход сделки на нужную стадию). Payload логируется и при наличии `BITRIX_TILDA_B24_OUTBOUND_WEBHOOK_URL` пересылается во внешний ЛК.
- `GET /bitrix/fields` — отдаёт кеш `crm.deal.fields` (есть `?refresh=true`).
- `GET /tilda/forms` и `GET /tilda/forms/{form_id}` — вспомогательные ручки для инспекции форм Тильды (API ключи берём из `.env`).
- `GET /health` — простой healthcheck.

При старте сервис один раз вызывает `crm.deal.fields` и кеширует результат в `data/bitrix_fields.json`. Сервис не пытается угадывать поля: всё, что не описано в `mapping.json`, игнорируется. Файлы складываются во временную директорию `data/tmp_uploads`, после успешной загрузки в Bitrix.Disk временные файлы удаляются.
