# SOL AI-менеджер

Сервис принимает заявки на порту из `../настройки-SOL.json`, отвечает клиенту,
сохраняет состояние в SQLite и синхронизирует принятую заявку с воронкой SOL в
Bitrix24.

## HTTP

- `POST /` или `POST /lead` — JSON либо `application/x-www-form-urlencoded`.
  Другие POST-пути также принимаются, чтобы не потерять заявку от источника с
  заранее заданным webhook path.
- `GET /health` — состояние сервиса без персональных данных.
- `GET /stats` — агрегированные счётчики; доступен только с localhost.

Минимальный пример:

```bash
curl -sS http://127.0.0.1:8001/lead \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"example-1","name":"Анна","phone":"+79991234567","message":"Нужно автоматизировать обработку заявок в CRM"}'
```

Повтор с тем же `request_id` идемпотентен. Если первая заявка была без телефона,
повторное сообщение с тем же `request_id` и валидным `phone` дополнит контакт и
переведёт сделку на стадию «Есть контакт».

## Эксплуатация

```bash
python3 sol_manager.py --setup-crm
curl -sS http://127.0.0.1:8001/health
curl -sS http://127.0.0.1:8001/stats
journalctl --user -u sol-ai-manager.service -n 100 --no-pager
```

Webhook Bitrix24 читается напрямую из файла настроек и не копируется в unit,
SQLite или журнал. База и каталог `var/` создаются с правами только для владельца.
