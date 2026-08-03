#!/usr/bin/env python3
"""ИИ-менеджер первой линии (FABLE).

Принимает заявки с сайта и из чата, отвечает клиенту человеческим текстом,
заводит карточку в Битрикс24, ставит менеджеру задачу с дедлайном, двигает
сделку по воронке, отсекает спам и ведёт учёт.

Только стандартная библиотека. LLM — через headless `claude -p`.
"""
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE.parent / "настройки-FABLE.json").read_text(encoding="utf-8"))
HOOK = CFG["b24_webhook"].rstrip("/")
CATEGORY = int(CFG["b24_category_id"])
PORT = int(os.environ.get("FABLE_PORT") or CFG["port"])
MANAGER_ID = int(CFG.get("manager_user_id", 1))

MSK = ZoneInfo("Europe/Moscow")
DB_PATH = BASE / os.environ.get("FABLE_DB", "state.db")
RAW_LOG = BASE / "incoming.jsonl"
LLM_JAIL = BASE / "llm_jail"
PROMPT_FILE = BASE / "prompt.md"

STAGE_NEW = f"C{CATEGORY}:NEW"
STAGE_WORK = f"C{CATEGORY}:EXECUTING"
STAGE_CONTACT = f"C{CATEGORY}:CONTACT"

LLM_MODEL = CFG.get("llm_model", "claude-sonnet-5")
LLM_TIMEOUT = 75
LLM_PARALLEL = 2
MAX_BODY = 64 * 1024
MAX_LLM_INPUT = 6000
SESSION_TTL = 48 * 3600
MAX_PHONE_ASKS = 2

logging.basicConfig(
    filename=str(BASE / "app.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("fable")

STARTED_AT = time.time()
_raw_lock = threading.Lock()
_llm_sem = threading.Semaphore(LLM_PARALLEL)
_req_locks: dict[str, threading.Lock] = {}
_req_locks_guard = threading.Lock()
_recent_errors: list[dict] = []
_errors_lock = threading.Lock()


def now_msk() -> datetime:
    return datetime.now(MSK)


def req_lock(key: str) -> threading.Lock:
    with _req_locks_guard:
        return _req_locks.setdefault(key, threading.Lock())


def note_error(where: str, err: str) -> None:
    with _errors_lock:
        _recent_errors.append({"когда": now_msk().strftime("%Y-%m-%d %H:%M:%S"), "где": where, "ошибка": err[:300]})
        del _recent_errors[:-20]
    log.error("%s: %s", where, err)


# ---------------------------------------------------------------- хранилище

_db_local = threading.local()


def db() -> sqlite3.Connection:
    conn = getattr(_db_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=8000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _db_local.conn = conn
    return conn


def init_db() -> None:
    c = db()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS requests (
            req_num      TEXT PRIMARY KEY,
            body_hash    TEXT,
            raw          TEXT,
            channel      TEXT,
            session_key  TEXT,
            status       TEXT NOT NULL,           -- received|replied|spam|error
            is_spam      INTEGER DEFAULT 0,
            name         TEXT,
            phone        TEXT,
            summary      TEXT,
            reply_text   TEXT,
            deadline_iso TEXT,
            deadline_wish TEXT,
            needs_phone  INTEGER DEFAULT 0,
            contact_id   INTEGER,
            deal_id      INTEGER,
            task_id      TEXT,
            comment_done INTEGER DEFAULT 0,
            stage        TEXT,
            crm_done     INTEGER DEFAULT 0,
            created_at   REAL,
            updated_at   REAL
        );
        CREATE INDEX IF NOT EXISTS ix_requests_hash ON requests(body_hash, created_at);

        CREATE TABLE IF NOT EXISTS sessions (
            session_key   TEXT PRIMARY KEY,
            req_num       TEXT,
            deal_id       INTEGER,
            contact_id    INTEGER,
            awaiting_phone INTEGER DEFAULT 0,
            ask_count     INTEGER DEFAULT 0,
            updated_at    REAL
        );

        CREATE TABLE IF NOT EXISTS crm_jobs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            req_num    TEXT NOT NULL,
            kind       TEXT NOT NULL,             -- create|add_phone
            payload    TEXT,
            attempts   INTEGER DEFAULT 0,
            next_at    REAL,
            status     TEXT DEFAULT 'queued',     -- queued|done|failed
            last_error TEXT,
            created_at REAL
        );
        CREATE INDEX IF NOT EXISTS ix_jobs_due ON crm_jobs(status, next_at);

        CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS seq (name TEXT PRIMARY KEY, value INTEGER DEFAULT 0);
        """
    )


def bump(name: str, delta: int = 1) -> None:
    db().execute(
        "INSERT INTO counters(name, value) VALUES(?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
        (name, delta),
    )


def counter(name: str) -> int:
    row = db().execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
    return row["value"] if row else 0


def next_seq(name: str) -> int:
    c = db()
    c.execute("INSERT OR IGNORE INTO seq(name, value) VALUES(?, 0)", (name,))
    c.execute("UPDATE seq SET value = value + 1 WHERE name=?", (name,))
    return c.execute("SELECT value FROM seq WHERE name=?", (name,)).fetchone()["value"]


def update_request(req_num: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    db().execute(f"UPDATE requests SET {sets} WHERE req_num=?", (*fields.values(), req_num))


def get_request(req_num: str) -> sqlite3.Row | None:
    return db().execute("SELECT * FROM requests WHERE req_num=?", (req_num,)).fetchone()


def enqueue(req_num: str, kind: str, payload: dict | None = None) -> None:
    db().execute(
        "INSERT INTO crm_jobs(req_num, kind, payload, next_at, created_at) VALUES(?,?,?,?,?)",
        (req_num, kind, json.dumps(payload or {}, ensure_ascii=False), time.time(), time.time()),
    )


# ------------------------------------------------------------ Битрикс24 API


class B24Error(RuntimeError):
    pass


def b24_flat(d: dict, prefix: str = "") -> dict:
    """Разворачивает вложенную структуру в плоские ключи fields[PHONE][0][VALUE]."""
    out: dict = {}
    for k, v in d.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out.update(b24_flat(v, key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out.update(b24_flat(item, f"{key}[{i}]"))
                else:
                    out[f"{key}[{i}]"] = item
        elif v is not None:
            out[key] = v
    return out


def b24(method: str, params: dict | None = None, retries: int = 3):
    data = urllib.parse.urlencode(b24_flat(params or {}), doseq=True).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(f"{HOOK}/{method}.json", data=data)
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode())
            if "error" in resp:
                err = f"{resp['error']}: {resp.get('error_description', '')}"
                if resp["error"] in ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT"):
                    last = B24Error(err)
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise B24Error(err)
            return resp["result"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise B24Error(f"{method} недоступен после {retries} попыток: {last}")


# ------------------------------------------------------------------ парсинг

FIELD_SYNONYMS = {
    "name": ["name", "имя", "fio", "фио", "clientname", "contactname", "username", "fullname",
             "sendername", "fromname", "author", "клиент", "заказчик", "customer"],
    "phone": ["phone", "телефон", "тел", "tel", "mobile", "мобильный", "phonenumber",
              "contactphone", "номертелефона", "whatsapp", "телефонклиента"],
    "email": ["email", "mail", "почта", "eмail", "emailaddress"],
    "text": ["message", "сообщение", "text", "текст", "comment", "комментарий", "question",
             "вопрос", "body", "description", "описание", "content", "msg", "задача",
             "запрос", "обращение", "task", "textarea", "messagetext"],
    "req_num": ["requestid", "requestnumber", "requestnum", "заявка", "номерзаявки", "номер",
                "ticketid", "ticket", "leadid", "externalid", "orderid", "resultid",
                "formresultid", "submissionid", "tranid", "transactionid", "messageid",
                "applicationid", "number", "num", "uid", "id"],
    "session": ["sessionid", "session", "chatid", "chat", "dialogid", "dialog", "userid",
                "clientid", "conversationid", "threadid", "peerid", "visitorid", "fromid",
                "contactid"],
    "channel": ["source", "channel", "канал", "form", "formname", "источник", "from", "тип"],
    "callback": ["callbackurl", "callback", "replyurl", "responseurl", "webhookurl", "answerurl"],
}


def norm_key(k: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", str(k).lower())


def walk(obj, prefix=""):
    """Плоский список (путь, значение) из произвольного JSON.

    Пары вида {"name": "PHONE", "values": ["+7..."]} (так шлют формы Битрикс24
    и подобные) разворачиваются в осмысленную пару ключ→значение."""
    if isinstance(obj, dict):
        label = obj.get("name") or obj.get("NAME") or obj.get("field") or obj.get("code")
        payload = obj.get("values", obj.get("value", obj.get("VALUE", obj.get("values"))))
        if isinstance(label, str) and payload is not None and not isinstance(payload, (dict,)):
            vals = payload if isinstance(payload, list) else [payload]
            for v in vals:
                if v is not None and str(v).strip():
                    yield label, v
            return
        for k, v in obj.items():
            yield from walk(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{prefix}[{i}]")
    else:
        if obj is not None and str(obj).strip() != "":
            yield prefix, obj


# Идентификаторы внутри этих объектов — это идентификаторы человека или чата,
# а не номер заявки: chat.id из Telegram номером заявки быть не может.
PERSON_CONTAINERS = {"chat", "from", "user", "visitor", "client", "contact", "author",
                     "sender", "profile", "customer", "lead", "peer"}


def parse_body(raw: str, path: str, headers: dict) -> dict:
    """Best-effort разбор заявки любого формата: JSON, form-urlencoded, query string."""
    pairs: list[tuple[str, object]] = []
    parsed_obj = None
    raw = raw.strip()
    if raw:
        try:
            parsed_obj = json.loads(raw)
            pairs = list(walk(parsed_obj))
        except (json.JSONDecodeError, ValueError):
            if "=" in raw:
                for k, vals in urllib.parse.parse_qs(raw, keep_blank_values=False).items():
                    for v in vals:
                        pairs.append((k, v))
            else:
                pairs.append(("text", raw))
    qs = urllib.parse.urlsplit(path).query
    if qs:
        for k, vals in urllib.parse.parse_qs(qs, keep_blank_values=False).items():
            for v in vals:
                pairs.append((k, v))

    found: dict[str, str] = {}
    for target, syns in FIELD_SYNONYMS.items():
        best_rank = 10**6
        for key, value in pairs:
            segments = [norm_key(s) for s in re.split(r"[.\[\]]+", str(key)) if norm_key(s)]
            if not segments:
                continue
            leaf = segments[-1]
            parent = segments[-2] if len(segments) > 1 else ""
            # id внутри объекта человека/чата — это не номер заявки
            if target == "req_num" and leaf in ("id", "uid", "num", "number") and parent in PERSON_CONTAINERS:
                continue
            # точное совпадение всегда сильнее подстрочного; составной ключ
            # (родитель+лист) сильнее одиночного листа
            candidates = [(parent + leaf, 0, 2000)] if parent else []
            candidates.append((leaf, 100, 3000))
            best_here = None
            for cand, exact_base, part_base in candidates:
                for rank, syn in enumerate(syns):
                    if cand == syn:
                        score = exact_base + rank
                    elif len(syn) >= 5 and syn in cand:
                        score = part_base + rank
                    else:
                        continue
                    if best_here is None or score < best_here:
                        best_here = score
            if best_here is not None and best_here < best_rank:
                best_rank, found[target] = best_here, str(value).strip()
    return {
        "fields": found,
        "pairs": pairs,
        "obj": parsed_obj,
        "headers": {k: v for k, v in headers.items() if k.lower() in ("user-agent", "content-type", "referer", "x-forwarded-for")},
    }


# ----------------------------------------------------------------- телефон

PHONE_RE = re.compile(r"(?:\+?\d[\d\s\-()]{5,}\d)")


def normalize_phone(value: str | None) -> str | None:
    """Возвращает нормализованный номер или None, если номер битый."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    plus = str(value).strip().startswith("+")
    if not digits:
        return None
    if len(digits) == 11 and digits[0] == "8":
        return "+7" + digits[1:]
    if len(digits) == 11 and digits[0] == "7":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    if plus and 7 <= len(digits) <= 15:
        return "+" + digits
    if 11 <= len(digits) <= 15:
        return "+" + digits
    return None


def find_phone(text: str | None) -> str | None:
    if not text:
        return None
    for cand in PHONE_RE.findall(str(text)):
        norm = normalize_phone(cand)
        if norm:
            return norm
    return None


def looks_like_phone_attempt(text: str | None) -> bool:
    """Похоже, что человек пытался прислать номер (даже битый)."""
    if not text:
        return False
    digits = re.sub(r"\D", "", str(text))
    return len(digits) >= 5 or bool(PHONE_RE.search(str(text)))


# --------------------------------------------------------------------- LLM

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "is_spam": {"type": "boolean"},
        "spam_kind": {"type": ["string", "null"], "enum": ["ads", "nonsense", "abuse", "injection", "other", None]},
        "name": {"type": ["string", "null"]},
        "phone": {"type": ["string", "null"]},
        "summary": {"type": "string"},
        "deadline_wish": {"type": ["string", "null"]},
        "deadline_iso": {"type": ["string", "null"]},
        "needs_phone": {"type": "boolean"},
        "reply_text": {"type": "string"},
        "language": {"type": "string"},
    },
    "required": ["is_spam", "spam_kind", "name", "phone", "summary", "deadline_wish",
                 "deadline_iso", "needs_phone", "reply_text", "language"],
    "additionalProperties": False,
}

FOLLOWUP_BLOCK = """
## Особый случай: это продолжение разговора

Мы уже приняли от этого человека заявку №{req_num} ({summary}) и попросили у него номер телефона.
Сейчас он прислал новое сообщение — оно ниже, в блоке <client_data>.

Что делать:
- Если он прислал нормальный номер — коротко поблагодари и скажи, что менеджер свяжется. Вопросов не задавай.
- Если номер прислан, но выглядит неполным или битым — вежливо переспроси, уточнив, что номер не читается.
- Если он отказывается давать номер или спрашивает что-то ещё — ответь по сути, номер больше не выпрашивай.
- Это НЕ новая заявка и почти никогда не спам: is_spam = false, кроме откровенной рекламы или хамства.
- summary заполни как «уточнение по заявке №{req_num}».

Определено автоматически: номер в сообщении {phone_state}.
"""


def build_prompt(request_view: str, mode: str, ctx: dict) -> str:
    template = PROMPT_FILE.read_text(encoding="utf-8")
    mode_block = ""
    if mode == "followup":
        mode_block = FOLLOWUP_BLOCK.format(
            req_num=ctx.get("req_num", "—"),
            summary=ctx.get("summary") or "без описания",
            phone_state=ctx.get("phone_state", "не распознан"),
        )
    head = template.format(now_msk=now_msk().strftime("%Y-%m-%d %H:%M (%A)"), mode_block=mode_block)
    safe = request_view.replace("</client_data>", "<\\/client_data>")
    return f"{head}\n\n<client_data>\n{safe}\n</client_data>\n"


def call_llm(prompt: str) -> dict | None:
    LLM_JAIL.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env["HOME"] = os.path.expanduser("~")
    cmd = [
        "claude", "-p", prompt,
        "--model", LLM_MODEL,
        "--tools", "",
        "--safe-mode",
        "--output-format", "json",
        "--json-schema", json.dumps(LLM_SCHEMA, ensure_ascii=False),
    ]
    with _llm_sem:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=LLM_TIMEOUT,
                                  cwd=str(LLM_JAIL), env=env)
        except subprocess.TimeoutExpired:
            note_error("llm", f"таймаут {LLM_TIMEOUT}s")
            return None
    if proc.returncode != 0:
        note_error("llm", f"код {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}")
        return None
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        note_error("llm", f"невалидный конверт: {proc.stdout[:200]}")
        return None
    out = envelope.get("structured_output")
    if not isinstance(out, dict):
        try:
            out = json.loads(envelope.get("result", ""))
        except (json.JSONDecodeError, TypeError):
            note_error("llm", f"нет structured_output: {str(envelope)[:200]}")
            return None
    return out if isinstance(out, dict) else None


# -------------------------------------------------- проверка ответа клиенту

SECRETS = [HOOK, HOOK.rsplit("/", 1)[-1], str(BASE), str(BASE.parent)]
PRICE_PATTERNS = [
    re.compile(r"(?:скидк|дисконт|discount)\w*[^.!?]{0,40}?\d", re.I),
    re.compile(r"\d+\s*%\s*(?:скидк|off)", re.I),
    re.compile(r"\d[\d\s]{2,}\s*(?:руб|₽|rub|долл|\$|eur|€)", re.I),
    re.compile(r"(?:цена|стоимость|стоит|тариф|обойдётся|обойдется)[^.!?]{0,30}?\d[\d\s]{2,}", re.I),
]
SAFE_REPLY = ("Спасибо за обращение — заявку принял и передал менеджеру. "
              "Он свяжется с вами, чтобы уточнить детали.")
SAFE_REPLY_PHONE = ("Спасибо за обращение — заявку принял и передал менеджеру. "
                    "Подскажите, пожалуйста, номер телефона для связи?")
SAFE_REPLY_SPAM = "Спасибо за сообщение, но оно не по профилю нашей работы — заявку не оформляем."


def sanitize_reply(text: str, needs_phone: bool) -> tuple[str, str | None]:
    """Возвращает (безопасный текст, причина подмены или None)."""
    if not text or not text.strip():
        return (SAFE_REPLY_PHONE if needs_phone else SAFE_REPLY), "пустой ответ"
    for secret in SECRETS:
        if secret and len(secret) > 8 and secret in text:
            return (SAFE_REPLY_PHONE if needs_phone else SAFE_REPLY), "утечка служебных данных"
    for pat in PRICE_PATTERNS:
        if pat.search(text):
            return (SAFE_REPLY_PHONE if needs_phone else SAFE_REPLY), f"выдуманные условия ({pat.pattern[:24]})"
    return text.strip()[:1200], None


# ------------------------------------------------------------------ дедлайн


def resolve_deadline(iso: str | None) -> tuple[datetime, str]:
    """Валидирует дедлайн от LLM; возвращает (дата, как получено)."""
    base = now_msk()
    dt, how = None, "по умолчанию"
    if iso:
        try:
            cand = datetime.fromisoformat(str(iso).strip())
            cand = cand.replace(tzinfo=MSK) if cand.tzinfo is None else cand.astimezone(MSK)
            if base + timedelta(minutes=5) < cand < base + timedelta(days=14):
                dt, how = cand, "по пожеланию клиента"
        except (ValueError, TypeError):
            pass
    if dt is None:
        dt = base + timedelta(hours=3)
    if dt.hour >= 22:
        dt = dt.replace(hour=21, minute=30, second=0, microsecond=0)
    elif dt.hour < 8:
        dt = dt.replace(hour=10, minute=0, second=0, microsecond=0)
    if dt <= base:
        dt = base + timedelta(hours=2)
    return dt, how


# --------------------------------------------------------- CRM: шаги сделки


def ensure_contact(req: sqlite3.Row) -> int:
    if req["contact_id"]:
        return req["contact_id"]
    phone = req["phone"]
    if phone:
        res = b24("crm.duplicate.findbycomm", {"type": "PHONE", "values": [phone], "entity_type": "CONTACT"})
        ids = res.get("CONTACT") if isinstance(res, dict) else None
        if ids:
            cid = int(ids[0])
            update_request(req["req_num"], contact_id=cid)
            return cid
    name = (req["name"] or "").strip() or "Клиент с сайта"
    fields: dict = {"NAME": name, "OPENED": "Y", "TYPE_ID": "CLIENT", "SOURCE_ID": "WEB"}
    if phone:
        fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
    try:
        cid = int(b24("crm.contact.add", {"fields": fields}))
    except B24Error as e:
        if "required" not in str(e).lower():
            raise
        parts = name.split()
        fields["NAME"], fields["LAST_NAME"] = parts[0], (parts[1] if len(parts) > 1 else "(клиент)")
        cid = int(b24("crm.contact.add", {"fields": fields}))
    update_request(req["req_num"], contact_id=cid)
    bump("контактов_создано")
    return cid


def find_existing_deal(req_num: str) -> int | None:
    rows = b24("crm.deal.list", {
        "filter": {"ORIGIN_ID": req_num, "CATEGORY_ID": CATEGORY},
        "select": ["ID", "ORIGIN_ID"],
    })
    for row in rows or []:
        if row.get("ORIGIN_ID") == req_num:
            return int(row["ID"])
    return None


def ensure_deal(req: sqlite3.Row, contact_id: int) -> int:
    if req["deal_id"]:
        return req["deal_id"]
    existing = find_existing_deal(req["req_num"])
    if existing:
        update_request(req["req_num"], deal_id=existing, stage="существующая")
        return existing
    name = (req["name"] or "клиент").strip()
    summary = (req["summary"] or "заявка с сайта").strip()
    title = f"Заявка {req['req_num']} — {name}: {summary}"[:250]
    comments = [f"Заявка {req['req_num']}", f"Канал: {req['channel'] or 'не указан'}"]
    if req["phone"]:
        comments.append(f"Телефон: {req['phone']}")
    if req["deadline_wish"]:
        comments.append(f"Пожелание по времени: {req['deadline_wish']}")
    comments.append("")
    comments.append("Текст обращения:")
    comments.append((req["raw"] or "")[:3000])
    did = int(b24("crm.deal.add", {"fields": {
        "TITLE": title,
        "CATEGORY_ID": CATEGORY,
        "STAGE_ID": STAGE_NEW,
        "CONTACT_ID": contact_id,
        "OPENED": "Y",
        "ASSIGNED_BY_ID": MANAGER_ID,
        "SOURCE_ID": "WEB",
        "SOURCE_DESCRIPTION": f"ИИ-менеджер FABLE, канал: {req['channel'] or 'web'}",
        "ORIGIN_ID": req["req_num"],
        "ORIGINATOR_ID": "fable",
        "UF_CRM_REQUEST_NUM": req["req_num"],
        "COMMENTS": "\n".join(comments),
    }, "params": {"REGISTER_SONET_EVENT": "Y"}}))
    update_request(req["req_num"], deal_id=did, stage=STAGE_NEW)
    bump("сделок_создано")
    log.info("заявка %s → сделка %s", req["req_num"], did)
    return did


def set_stage(req_num: str, deal_id: int, stage: str) -> None:
    row = get_request(req_num)
    if row and row["stage"] == stage:
        return
    b24("crm.deal.update", {"id": deal_id, "fields": {"STAGE_ID": stage}})
    update_request(req_num, stage=stage)


def add_comment(req: sqlite3.Row, deal_id: int) -> None:
    if req["comment_done"]:
        return
    parts = [f"<b>Заявка {req['req_num']}</b> принята ИИ-менеджером.", ""]
    parts.append("<b>Обращение клиента:</b>")
    parts.append((req["raw"] or "")[:3000])
    parts.append("")
    parts.append("<b>Ответ, отправленный клиенту:</b>")
    parts.append(req["reply_text"] or "—")
    if req["deadline_wish"]:
        parts.append("")
        parts.append(f"<b>Клиент просил связаться:</b> {req['deadline_wish']}")
    if req["needs_phone"] and not req["phone"]:
        parts.append("")
        parts.append("<b>Телефон не оставлен</b> — запросили у клиента.")
    b24("crm.timeline.comment.add", {"fields": {
        "ENTITY_ID": deal_id, "ENTITY_TYPE": "deal", "COMMENT": "\n".join(parts),
    }})
    update_request(req["req_num"], comment_done=1)


def ensure_task(req: sqlite3.Row, deal_id: int) -> str:
    if req["task_id"]:
        return req["task_id"]
    dt, how = resolve_deadline(req["deadline_iso"])
    name = (req["name"] or "клиент").strip()
    desc = [
        f"Заявка {req['req_num']} с канала «{req['channel'] or 'сайт'}».",
        f"Клиент: {name}",
        f"Телефон: {req['phone'] or 'НЕ ОСТАВЛЕН — запрошен у клиента'}",
        f"Суть: {req['summary'] or '—'}",
        "",
        f"Пожелание клиента по времени связи: {req['deadline_wish'] or 'не указано'}",
        f"Дедлайн выставлен {how}.",
        "",
        "Текст обращения:",
        (req["raw"] or "")[:2000],
        "",
        "Ответ, отправленный клиенту:",
        (req["reply_text"] or "—"),
    ]
    res = b24("tasks.task.add", {"fields": {
        "TITLE": f"Связаться с клиентом — заявка {req['req_num']}"[:250],
        "RESPONSIBLE_ID": MANAGER_ID,
        "DEADLINE": dt.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "DESCRIPTION": "\n".join(desc),
        "UF_CRM_TASK": [f"D_{deal_id}"],
        "PRIORITY": 2 if req["deadline_wish"] else 1,
    }})
    tid = str(res["task"]["id"]) if isinstance(res, dict) and "task" in res else str(res)
    update_request(req["req_num"], task_id=tid)
    bump("задач_создано")
    return tid


def run_crm_create(req_num: str) -> None:
    req = get_request(req_num)
    if req is None or req["crm_done"]:
        return
    contact_id = ensure_contact(req)
    req = get_request(req_num)
    deal_id = ensure_deal(req, contact_id)
    set_stage(req_num, deal_id, STAGE_WORK)
    req = get_request(req_num)
    add_comment(req, deal_id)
    ensure_task(get_request(req_num), deal_id)
    if get_request(req_num)["phone"]:
        set_stage(req_num, deal_id, STAGE_CONTACT)
    update_request(req_num, crm_done=1)
    db().execute("UPDATE sessions SET deal_id=?, contact_id=? WHERE req_num=?", (deal_id, contact_id, req_num))
    log.info("заявка %s: CRM готова (сделка %s, контакт %s)", req_num, deal_id, contact_id)


def run_crm_add_phone(req_num: str, payload: dict) -> None:
    """Дозапись телефона в существующие контакт и сделку."""
    req = get_request(req_num)
    if req is None:
        return
    phone = payload.get("phone")
    if not phone:
        return
    if not req["crm_done"]:
        run_crm_create(req_num)
        req = get_request(req_num)
    contact_id, deal_id = req["contact_id"], req["deal_id"]
    if not contact_id or not deal_id:
        raise B24Error("нет контакта/сделки для дозаписи телефона")
    current = b24("crm.contact.get", {"id": contact_id}) or {}
    have = {str(p.get("VALUE", "")) for p in (current.get("PHONE") or [])}
    if phone not in have:
        b24("crm.contact.update", {"id": contact_id,
                                   "fields": {"PHONE": [{"VALUE": phone, "VALUE_TYPE": "WORK"}]}})
    update_request(req_num, phone=phone)
    set_stage(req_num, deal_id, STAGE_CONTACT)
    b24("crm.timeline.comment.add", {"fields": {
        "ENTITY_ID": deal_id, "ENTITY_TYPE": "deal",
        "COMMENT": f"<b>Клиент оставил телефон:</b> {phone}\nЗаписан в карточку контакта.",
    }})
    if req["task_id"]:
        try:
            b24("tasks.task.update", {"taskId": req["task_id"], "fields": {
                "DESCRIPTION": f"ТЕЛЕФОН КЛИЕНТА: {phone}\n\n" + (req["summary"] or ""),
            }})
        except B24Error as e:
            note_error("task.update", str(e))
    bump("телефонов_дописано")
    log.info("заявка %s: телефон %s записан в контакт %s", req_num, phone, contact_id)


def crm_worker() -> None:
    while True:
        try:
            job = db().execute(
                "SELECT * FROM crm_jobs WHERE status='queued' AND next_at<=? ORDER BY id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if job is None:
                time.sleep(1.0)
                continue
            payload = json.loads(job["payload"] or "{}")
            with req_lock(job["req_num"]):
                if job["kind"] == "create":
                    run_crm_create(job["req_num"])
                elif job["kind"] == "add_phone":
                    run_crm_add_phone(job["req_num"], payload)
            db().execute("UPDATE crm_jobs SET status='done' WHERE id=?", (job["id"],))
        except Exception as e:  # noqa: BLE001 — воркер не должен умирать
            attempts = (job["attempts"] if job else 0) + 1
            note_error(f"crm:{job['kind'] if job else '?'}", f"{e}\n{traceback.format_exc(limit=3)}")
            bump("ошибок")
            if job:
                if attempts >= 6:
                    db().execute("UPDATE crm_jobs SET status='failed', attempts=?, last_error=? WHERE id=?",
                                 (attempts, str(e)[:400], job["id"]))
                    update_request(job["req_num"], status="error")
                else:
                    db().execute("UPDATE crm_jobs SET attempts=?, next_at=?, last_error=? WHERE id=?",
                                 (attempts, time.time() + 20 * attempts, str(e)[:400], job["id"]))
            time.sleep(1.0)


# ------------------------------------------------------------ бизнес-логика


def session_key_for(fields: dict, pairs: list) -> str | None:
    for candidate in (fields.get("session"), fields.get("email"),
                      normalize_phone(fields.get("phone")), (fields.get("name") or "").strip().lower()):
        if candidate and len(str(candidate)) >= 3:
            return f"k:{candidate}"
    return None


def request_view(raw: str, parsed: dict) -> str:
    """Читаемое представление заявки для LLM: разобранные поля + сырое тело."""
    lines = []
    for key, value in parsed["pairs"][:60]:
        val = str(value)
        lines.append(f"{key}: {val[:600]}")
    view = "\n".join(lines) if lines else raw
    if not lines or len(view) < len(raw) * 0.5:
        view = f"{view}\n\n--- сырое тело запроса ---\n{raw}"
    return view[:MAX_LLM_INPUT]


def find_awaiting_session(key: str | None, text: str | None) -> sqlite3.Row | None:
    if key:
        row = db().execute(
            "SELECT * FROM sessions WHERE session_key=? AND awaiting_phone=1 AND updated_at>?",
            (key, time.time() - SESSION_TTL),
        ).fetchone()
        if row:
            return row
    if looks_like_phone_attempt(text) and len(re.sub(r"[^\wа-яА-Я]", "", str(text or ""))) < 40:
        rows = db().execute(
            "SELECT * FROM sessions WHERE awaiting_phone=1 AND updated_at>?",
            (time.time() - 24 * 3600,),
        ).fetchall()
        if len(rows) == 1:
            log.info("follow-up без ключа приклеен к единственной ожидающей сессии %s", rows[0]["session_key"])
            return rows[0]
    return None


def handle_followup(session: sqlite3.Row, raw: str, parsed: dict, text: str) -> dict:
    """Клиент прислал телефон (или что-то ещё) в ответ на наш вопрос."""
    req = get_request(session["req_num"])
    phone = find_phone(text) or normalize_phone(parsed["fields"].get("phone"))
    ctx = {
        "req_num": session["req_num"],
        "summary": req["summary"] if req else None,
        "phone_state": f"распознан: {phone}" if phone else "не распознан (номер битый или его нет)",
    }
    out = call_llm(build_prompt(request_view(raw, parsed), "followup", ctx))
    if out:
        reply, blocked = sanitize_reply(out.get("reply_text", ""), needs_phone=not phone)
    else:
        bump("ошибок")
        reply, blocked = ((f"Спасибо, записал номер {phone}. Менеджер свяжется с вами.") if phone else
                          "Кажется, номер не полный — пришлите, пожалуйста, ещё раз в формате +7 900 000-00-00."), None
    if blocked:
        bump("ответов_подменено")
        log.warning("ответ подменён (%s) для follow-up сессии %s", blocked, session["session_key"])

    if phone:
        db().execute("UPDATE sessions SET awaiting_phone=0, updated_at=? WHERE session_key=?",
                     (time.time(), session["session_key"]))
        enqueue(session["req_num"], "add_phone", {"phone": phone})
        bump("телефонов_получено")
    else:
        asks = session["ask_count"] + 1
        stop = asks >= MAX_PHONE_ASKS
        db().execute("UPDATE sessions SET ask_count=?, awaiting_phone=?, updated_at=? WHERE session_key=?",
                     (asks, 0 if stop else 1, time.time(), session["session_key"]))
        if stop and req and req["deal_id"]:
            try:
                b24("crm.timeline.comment.add", {"fields": {
                    "ENTITY_ID": req["deal_id"], "ENTITY_TYPE": "deal",
                    "COMMENT": "<b>Телефон получить не удалось</b> — клиент не прислал корректный номер "
                               f"после {asks} попыток. Текст последнего сообщения:\n{text[:500]}",
                }})
            except B24Error as e:
                note_error("timeline.nofone", str(e))
    bump("follow_up_обработано")
    return {"reply": reply, "req_num": session["req_num"], "kind": "follow-up"}


def send_callback(url: str, payload: dict) -> None:
    """Если заявка принесла адрес для ответа — дублируем ответ туда."""
    if not re.match(r"^https?://", url or ""):
        return
    body = json.dumps(payload, ensure_ascii=False).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
            with urllib.request.urlopen(req, timeout=20):
                bump("ответов_на_callback")
                log.info("ответ по заявке %s отправлен на %s", payload.get("request_id"), url)
                return
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                note_error("callback", f"{url}: {e}")
                bump("ошибок")
            else:
                time.sleep(2 * (attempt + 1))


def deliver(result: dict, callback_url: str | None) -> dict:
    if callback_url:
        threading.Thread(
            target=send_callback,
            args=(callback_url, {"request_id": result.get("req_num"), "reply": result["reply"], "status": "received"}),
            name="callback", daemon=True,
        ).start()
    return result


def handle_request(raw: str, path: str, headers: dict, remote: str) -> dict:
    parsed = parse_body(raw, path, headers)
    fields = parsed["fields"]
    text = fields.get("text") or raw
    callback_url = fields.get("callback")
    body_hash = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()

    # 1. Дешёвый префильтр — до LLM и до CRM.
    if not raw.strip():
        bump("пустых_отброшено")
        return deliver({"reply": "Пустое обращение — напишите, пожалуйста, что вам нужно.", "kind": "empty"}, callback_url)

    # 2. Follow-up: клиент отвечает на наш вопрос про телефон.
    skey = session_key_for(fields, parsed["pairs"])
    session = find_awaiting_session(skey, text)
    if session:
        with req_lock(session["req_num"]):
            return deliver(handle_followup(session, raw, parsed, text), callback_url)

    # 3. Номер заявки: из данных клиента либо свой.
    req_num = (fields.get("req_num") or "").strip()
    generated = False
    if not req_num or len(req_num) > 64:
        dup = db().execute(
            "SELECT req_num, reply_text FROM requests WHERE body_hash=? AND created_at>? ORDER BY created_at DESC LIMIT 1",
            (body_hash, time.time() - 900),
        ).fetchone()
        if dup:
            bump("повторных_доставок")
            return deliver({"reply": dup["reply_text"] or SAFE_REPLY, "req_num": dup["req_num"], "kind": "duplicate"}, callback_url)
        req_num = f"FAB-{now_msk():%y%m%d}-{next_seq('req'):04d}"
        generated = True

    # 4. Идемпотентность: первая запись выигрывает, остальные получают её ответ.
    with req_lock(req_num):
        cur = db().execute(
            "INSERT OR IGNORE INTO requests(req_num, body_hash, raw, channel, session_key, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,'received',?,?)",
            (req_num, body_hash, raw[:20000], fields.get("channel") or "web", skey, time.time(), time.time()),
        )
        if cur.rowcount == 0:
            existing = get_request(req_num)
            bump("повторных_доставок")
            log.info("повторная доставка заявки %s", req_num)
            return deliver({"reply": existing["reply_text"] or SAFE_REPLY, "req_num": req_num, "kind": "duplicate"}, callback_url)
        bump("принято")
        log.info("новая заявка %s (%s) от %s", req_num, "свой номер" if generated else "номер клиента", remote)

    # 5. Разбор обращения ИИ-менеджером.
    out = call_llm(build_prompt(request_view(raw, parsed), "new", {}))
    fallback = out is None
    if fallback:
        bump("ошибок")
        out = {"is_spam": False, "spam_kind": None, "name": fields.get("name"),
               "phone": fields.get("phone"), "summary": (text or "")[:200],
               "deadline_wish": None, "deadline_iso": None,
               "needs_phone": not bool(find_phone(text) or fields.get("phone")),
               "reply_text": "", "language": "ru"}

    phone = find_phone(out.get("phone")) or find_phone(fields.get("phone")) or find_phone(text)
    needs_phone = bool(out.get("needs_phone")) or not phone
    is_spam = bool(out.get("is_spam"))

    reply, blocked = sanitize_reply(out.get("reply_text", ""), needs_phone and not is_spam)
    if is_spam and (blocked or not out.get("reply_text")):
        reply = SAFE_REPLY_SPAM
    if blocked:
        bump("ответов_подменено")
        log.warning("ответ подменён (%s) для заявки %s", blocked, req_num)

    with req_lock(req_num):
        update_request(
            req_num,
            status="spam" if is_spam else "replied",
            is_spam=1 if is_spam else 0,
            name=(out.get("name") or fields.get("name") or None),
            phone=phone,
            summary=(out.get("summary") or (text or "")[:200]),
            reply_text=reply,
            deadline_iso=out.get("deadline_iso"),
            deadline_wish=out.get("deadline_wish"),
            needs_phone=1 if (needs_phone and not is_spam) else 0,
        )

        if is_spam:
            bump("спам_отсеян")
            log.info("заявка %s — спам (%s), в CRM не заводим", req_num, out.get("spam_kind"))
            return deliver({"reply": reply, "req_num": req_num, "kind": "spam"}, callback_url)

        bump("отвечено")
        if fallback:
            log.warning("заявка %s обработана без LLM (fail-open)", req_num)
        enqueue(req_num, "create")
        if needs_phone:
            bump("телефон_запрошен")
            key = skey or f"auto:{req_num}"
            db().execute(
                "INSERT INTO sessions(session_key, req_num, awaiting_phone, ask_count, updated_at) "
                "VALUES(?,?,1,0,?) ON CONFLICT(session_key) DO UPDATE SET "
                "req_num=excluded.req_num, awaiting_phone=1, ask_count=0, updated_at=excluded.updated_at",
                (key, req_num, time.time()),
            )
    return deliver({"reply": reply, "req_num": req_num, "kind": "new"}, callback_url)


# -------------------------------------------------------------------- HTTP


def build_stats() -> dict:
    c = db()
    by_status = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM requests GROUP BY status")}
    queued = c.execute("SELECT COUNT(*) n FROM crm_jobs WHERE status='queued'").fetchone()["n"]
    failed = c.execute("SELECT COUNT(*) n FROM crm_jobs WHERE status='failed'").fetchone()["n"]
    deals = c.execute("SELECT COUNT(*) n FROM requests WHERE deal_id IS NOT NULL").fetchone()["n"]
    tasks = c.execute("SELECT COUNT(*) n FROM requests WHERE task_id IS NOT NULL").fetchone()["n"]
    with _errors_lock:
        errors = list(_recent_errors[-10:])
    return {
        "сервис": "ИИ-менеджер FABLE",
        "работает_с": datetime.fromtimestamp(STARTED_AT, MSK).strftime("%Y-%m-%d %H:%M:%S"),
        "аптайм_часов": round((time.time() - STARTED_AT) / 3600, 2),
        "принято_заявок": counter("принято"),
        "отвечено_клиентам": counter("отвечено"),
        "спам_отсеян": counter("спам_отсеян"),
        "сделок_в_битрикс24": deals,
        "задач_менеджеру": tasks,
        "телефон_запрошен": counter("телефон_запрошен"),
        "телефон_получен": counter("телефонов_получено"),
        "телефон_дописан_в_карточку": counter("телефонов_дописано"),
        "ошибок": counter("ошибок"),
        "повторных_доставок_отсечено": counter("повторных_доставок"),
        "ответов_заменено_на_безопасный": counter("ответов_подменено"),
        "ответов_отправлено_на_callback": counter("ответов_на_callback"),
        "очередь_в_битрикс24": queued,
        "не_доставлено_в_битрикс24": failed,
        "заявки_по_статусам": by_status,
        "последние_ошибки": errors,
    }


STATS_HTML = """<!doctype html><meta charset="utf-8"><title>ИИ-менеджер FABLE — учёт</title>
<style>body{{font:15px/1.6 system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#1a1a1a}}
h1{{font-size:20px}}table{{border-collapse:collapse;width:100%}}td{{padding:6px 8px;border-bottom:1px solid #eee}}
td:last-child{{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}}
pre{{background:#f6f6f6;padding:12px;border-radius:6px;overflow:auto;font-size:13px}}</style>
<h1>ИИ-менеджер FABLE — учёт</h1><table>{rows}</table>
<p style="color:#666;font-size:13px">Машиночитаемо: <a href="/stats?format=json">/stats?format=json</a></p>
{errors}"""


class Handler(BaseHTTPRequestHandler):
    server_version = "FableAI/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("http %s", fmt % args)

    def _send(self, code: int, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            log.warning("клиент отвалился до получения ответа")

    def _route(self):
        return urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"

    def do_GET(self):
        route = self._route()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if route == "/health":
            return self._send(200, {"status": "ok", "время": now_msk().strftime("%Y-%m-%d %H:%M:%S")})
        if route == "/stats":
            stats = build_stats()
            wants_html = "text/html" in (self.headers.get("Accept") or "") and query.get("format", [""])[0] != "json"
            if wants_html:
                rows = "".join(
                    f"<tr><td>{k.replace('_', ' ')}</td><td>{v}</td></tr>"
                    for k, v in stats.items() if not isinstance(v, (dict, list))
                )
                errs = ""
                if stats["последние_ошибки"]:
                    errs = "<h2 style='font-size:16px'>Последние ошибки</h2><pre>" + \
                           json.dumps(stats["последние_ошибки"], ensure_ascii=False, indent=1) + "</pre>"
                html = STATS_HTML.format(rows=rows, errors=errs).encode()
                return self._send(200, html, "text/html; charset=utf-8")
            return self._send(200, stats)
        return self._handle_incoming()

    def do_POST(self):
        return self._handle_incoming()

    do_PUT = do_PATCH = do_POST

    def _handle_incoming(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self.rfile.read(min(length, MAX_BODY))
            bump("слишком_больших_отброшено")
            return self._send(413, {"status": "error", "reply": "Сообщение слишком длинное."})
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        remote = self.headers.get("X-Forwarded-For") or self.client_address[0]

        record = {"ts": now_msk().isoformat(), "method": self.command, "path": self.path,
                  "remote": remote, "headers": dict(self.headers), "body": raw}
        with _raw_lock:
            with open(RAW_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        try:
            result = handle_request(raw, self.path, dict(self.headers), remote)
        except Exception as e:  # noqa: BLE001 — заявку терять нельзя
            note_error("handle_request", f"{e}\n{traceback.format_exc(limit=4)}")
            bump("ошибок")
            return self._send(200, {"status": "received", "reply": SAFE_REPLY})
        payload = {"status": "received", "reply": result["reply"]}
        if result.get("req_num"):
            payload["request_id"] = result["req_num"]
        return self._send(200, payload)


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32


def main() -> None:
    LLM_JAIL.mkdir(exist_ok=True)
    init_db()
    threading.Thread(target=crm_worker, name="crm", daemon=True).start()
    log.info("ИИ-менеджер FABLE запущен на порту %s, категория %s, менеджер %s", PORT, CATEGORY, MANAGER_ID)
    print(f"ИИ-менеджер FABLE слушает 0.0.0.0:{PORT} (учёт: /stats, здоровье: /health)", flush=True)
    Server(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
