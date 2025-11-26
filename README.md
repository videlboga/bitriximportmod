# Tilda → Bitrix24 Bridge

Небольшой сервис на FastAPI, который принимает две формы из Тильды, создаёт сделки в Bitrix24 с ручным маппингом и отправляет вебхуки Bitrix24 в внешний ЛК.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Перед запуском создайте файл `.env` и заполните обязательные переменные:

```
BITRIX_TILDA_BITRIX_WEBHOOK_BASE_URL=https://example.bitrix24.ru/rest/1/secret/
BITRIX_TILDA_BITRIX_DEAL_CATEGORY_ID=12
BITRIX_TILDA_BITRIX_DEAL_STAGE_ID=NEW_STAGE_CODE
BITRIX_TILDA_B24_OUTBOUND_WEBHOOK_URL=https://external.example.com/webhook    # опционально
BITRIX_TILDA_B24_FORWARD_FIELDS=id,UF_CUSTOM_123                             # опционально
BITRIX_TILDA_TILDA_PUBLIC_KEY=public_key_value                               # для /tilda/forms
BITRIX_TILDA_TILDA_SECRET_KEY=secret_key_value                               # для /tilda/forms
BITRIX_TILDA_TILDA_PROJECT_ID=12345                                          # опционально, project_id по умолчанию
```

Файл `mapping.json` описывает жёсткое соответствие полей форм Тильды и полей сделки Bitrix24. Ключ верхнего уровня — идентификатор формы (`formname`, `formid`, `tildaformid` и т. п.). Значения — объекты вида `"tilda_field": "B24_FIELD"`. Если поле не указано в маппинге или отправлено пустым, оно игнорируется.

## Поведение сервиса

- `POST /webhook/tilda` — принимает multipart/x-www-form-urlencoded запросы, определяет форму, смотрит маппинг, заполняет нужные поля сделки и вызывает `crm.deal.add`. Все запросы и ошибки логируются в `data/events.log`. Если удобнее направить каждую форму на отдельный URL, используйте `POST /webhook/tilda/<имя_формы>` — там имя должно совпадать с ключом из `mapping.json` (например, `tilda_form_1`).
- `POST /webhook/b24` — принимает вебхуки из Bitrix24, логирует и, при наличии `BITRIX_TILDA_B24_OUTBOUND_WEBHOOK_URL`, пересылает указанные поля в внешний сервис.
- `GET /bitrix/fields` — возвращает кешированную структуру полей `crm.deal.fields`. Передайте `?refresh=true`, чтобы принудительно обновить кеш.
- `GET /tilda/forms` — тянет список форм Tilda (по project_id из query или из `BITRIX_TILDA_TILDA_PROJECT_ID`) и отдаёт ответ Tilda API, который включает метаданные и описание полей.
- `GET /tilda/forms/{form_id}` — отдаёт структуру конкретной формы из Tilda.
- `GET /health` — простой healthcheck.

При старте сервис забирает структуру полей `crm.deal.fields`, складывает её в `data/bitrix_fields.json` и переиспользует как справочник (без автоматического сопоставления).
