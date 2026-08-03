#!/usr/bin/env python3
"""Проверка, что приёмник понимает заявки в разных реальных форматах интеграций
и умеет отвечать на callback-адрес. Тестовые сущности из Битрикс24 удаляются."""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
PORT, CB_PORT = 8103, 8104
URL = f"http://127.0.0.1:{PORT}"
DB = BASE / "test_fmt.db"
CFG = json.loads((BASE.parent / "настройки-FABLE.json").read_text(encoding="utf-8"))
HOOK = CFG["b24_webhook"].rstrip("/")

received_callbacks = []
passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (f" — {detail}" if detail else ""))


class CB(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        received_callbacks.append(json.loads(self.rfile.read(n).decode()))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out.update(flat(v, key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                out.update(flat(item, f"{key}[{i}]")) if isinstance(item, dict) else out.update({f"{key}[{i}]": item})
        elif v is not None:
            out[key] = v
    return out


def b24(method, params=None):
    data = urllib.parse.urlencode(flat(params or {}), doseq=True).encode()
    with urllib.request.urlopen(urllib.request.Request(f"{HOOK}/{method}.json", data=data), timeout=30) as r:
        resp = json.loads(r.read().decode())
    if "error" in resp:
        raise RuntimeError(f"{method}: {resp['error']}")
    return resp["result"]


def post(body, ctype="application/json", path="/"):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(URL + path, data=data, headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def state(num):
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests WHERE req_num=?", (num,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def wait_crm(num, timeout=90):
    for _ in range(timeout * 2):
        if state(num).get("crm_done"):
            break
        time.sleep(0.5)
    return state(num)


for stale in (DB, Path(str(DB) + "-wal"), Path(str(DB) + "-shm")):
    stale.unlink(missing_ok=True)
cb_server = HTTPServer(("127.0.0.1", CB_PORT), CB)
threading.Thread(target=cb_server.serve_forever, daemon=True).start()
env = {**os.environ, "FABLE_PORT": str(PORT), "FABLE_DB": DB.name}
srv = subprocess.Popen([sys.executable, str(BASE / "app.py")], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(40):
    try:
        urllib.request.urlopen(URL + "/health", timeout=5)
        break
    except Exception:
        time.sleep(0.25)
print(f"=== Проверка форматов заявок (порт {PORT}) ===\n")

CASES = [
    ("Битрикс24 CRM-форма (вложенный JSON)", "application/json", json.dumps({
        "event": "ONFORMSUBMIT",
        "data": {"fields": [
            {"name": "CONTACT_NAME", "values": ["Алексей Петров"]},
            {"name": "CONTACT_PHONE", "values": ["+7 903 777-11-22"]},
            {"name": "MESSAGE", "values": ["Хотим подключить ИИ к обработке входящих писем от клиентов"]},
        ], "form_id": 12, "result_id": "FORM-9001"},
    }, ensure_ascii=False), "FORM-9001", "9037771122"),

    ("Tilda-форма (form-urlencoded)", "application/x-www-form-urlencoded", urllib.parse.urlencode({
        "Name": "Ольга", "Phone": "8 (912) 345-67-89", "Email": "olga@shop.ru",
        "Textarea": "Нужен бот в вотсап, который отвечает на вопросы про доставку",
        "formid": "form284932", "tranid": "TILDA-4455",
    }), "TILDA-4455", "9123456789"),

    ("Чат-виджет (Jivo-подобный JSON)", "application/json", json.dumps({
        "event": "CHAT_MESSAGE", "chat_id": "jv-88213", "message_id": "JV-7788",
        "visitor": {"name": "Дмитрий", "phone": "", "email": "d@corp.ru"},
        "text": "Добрый день, у нас 3 менеджера обрабатывают заявки руками, интересно посмотреть автоматизацию. Наберите завтра.",
    }, ensure_ascii=False), "JV-7788", None),

    ("Telegram-подобный апдейт", "application/json", json.dumps({
        "update_id": 55123, "message": {
            "message_id": "TG-3311", "chat": {"id": 44556677, "type": "private"},
            "from": {"first_name": "Сергей"},
            "text": "Здравствуйте, нужна интеграция amoCRM с телефонией. Телефон +79055554433",
        },
    }, ensure_ascii=False), "TG-3311", "9055554433"),
]

try:
    for title, ctype, body, expect_num, expect_phone in CASES:
        print(f"[{title}]")
        r = post(body, ctype)
        print(f"      ответ: {r['reply'][:130]}")
        num = r.get("request_id", "")
        check(f"{title}: номер заявки взят из данных" if expect_num else f"{title}: номер присвоен",
              num == expect_num, f"получено {num!r}, ожидалось {expect_num!r}")
        s = wait_crm(num)
        check(f"{title}: сделка создана", bool(s.get("deal_id")), f"deal={s.get('deal_id')}")
        check(f"{title}: клиент опознан", bool(s.get("name")), f"имя={s.get('name')!r}")
        if expect_phone:
            got = (s.get("phone") or "")
            check(f"{title}: телефон распознан", expect_phone in got, f"{got!r}")
        else:
            check(f"{title}: телефона нет — спросили", s.get("needs_phone") == 1 and
                  any(w in r["reply"].lower() for w in ("телефон", "номер")), r["reply"][:70])
        print()

    print("[Ответ на callback-адрес]")
    r = post(json.dumps({
        "request_id": "CB-1000", "callback_url": f"http://127.0.0.1:{CB_PORT}/reply",
        "name": "Анна", "phone": "+79161112233",
        "message": "Нужна автоматизация выставления счетов",
    }, ensure_ascii=False))
    for _ in range(40):
        if received_callbacks:
            break
        time.sleep(0.25)
    check("ответ продублирован на callback_url", len(received_callbacks) == 1,
          json.dumps(received_callbacks, ensure_ascii=False)[:160])
    if received_callbacks:
        check("в callback пришли номер заявки и текст ответа",
              received_callbacks[0].get("request_id") == "CB-1000" and len(received_callbacks[0].get("reply", "")) > 20)
    wait_crm("CB-1000")

finally:
    print("\n=== Очистка тестовых сущностей ===")
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT deal_id, contact_id, task_id FROM requests").fetchall()
    conn.close()
    removed = {"task": 0, "deal": 0, "contact": 0}
    for row in rows:
        for kind, method, key in (("task", "tasks.task.delete", "taskId"), ("deal", "crm.deal.delete", "id"),
                                  ("contact", "crm.contact.delete", "id")):
            if row[f"{kind}_id"]:
                try:
                    b24(method, {key: row[f"{kind}_id"]})
                    removed[kind] += 1
                except Exception as e:
                    print(f"  не удалилось {kind}: {e}")
    print(f"  удалено: {removed}")
    srv.send_signal(signal.SIGTERM)
    try:
        srv.wait(timeout=5)
    except subprocess.TimeoutExpired:
        srv.kill()
    cb_server.shutdown()

print(f"\n=== Итог: {len(passed)} OK, {len(failed)} FAIL ===")
if failed:
    print("Провалено:", *failed, sep="\n  ")
sys.exit(1 if failed else 0)
