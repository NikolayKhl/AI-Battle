#!/usr/bin/env python3
"""Диспетчер AI-Баттла №2: подаёт заявки из leads.json ОБОИМ агентам одинаково.

Заменяет собой Tilda/Telegram-сервер: шлёт те же HTTP-запросы, которые слали бы
реальная форма и чат. Отыгрывает клиента: если агент в ответе на чат-заявку
дозапросил контакт — отправляет заготовленный followup.

Использование:
  python3 dispatcher.py                  # весь пакет (30 заявок) обоим
  python3 dispatcher.py --boss           # босс-заявка №31 обоим
  python3 dispatcher.py --only sol       # только одному агенту
  python3 dispatcher.py --from 12        # продолжить с заявки №12 (после сбоя)
  python3 dispatcher.py --interval 20    # пауза между заявками, сек (деф. 20)

Агенты: SOL http://localhost:8001, FABLE http://localhost:8002
(переопределяется env SOL_URL / FABLE_URL).
Лог: stand/logs/dispatch-<время>.csv — время, статус, задержка, ответ агента.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
LEADS = os.environ.get("LEADS") or next(
    (p for p in (os.path.join(BASE, "leads.json"),
                 os.path.join(os.path.dirname(BASE), "заявки", "leads.json"))
     if os.path.exists(p)), os.path.join(BASE, "leads.json"))
# Кого проверяем. Формат AGENTS: "имя=адрес,имя2=адрес2"
# Пример одного агента:  AGENTS="мой=http://localhost:8000"
_default = "sol=http://localhost:8001,fable=http://localhost:8002"
TARGETS = dict(pair.split("=", 1) for pair in
               os.environ.get("AGENTS", _default).split(",") if "=" in pair)
PHONE_REQUEST_RE = re.compile(r"телефон|номер|контакт|связаться|перезвон|как с вами|куда позвонить", re.I)


def post(url, payload, timeout=120):
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode() or "{}")
            return r.status, round(time.time() - t0, 2), data.get("reply", "")
    except urllib.error.HTTPError as e:
        return e.code, round(time.time() - t0, 2), f"HTTP ERROR: {e.read().decode()[:200]}"
    except Exception as e:
        return 0, round(time.time() - t0, 2), f"NO RESPONSE: {e}"


def send_lead(lead, target_name, target_url, log):
    lid = lead["id"]
    if lead["channel"] == "form":
        payload = {"application_id": lid, **lead["data"]}
        status, latency, reply = post(target_url + "/lead/form", payload)
        log(lid, target_name, "form", status, latency, reply)
        return

    chat_id = f"{lid}-{target_name}"
    payload = {"application_id": lid, "chat_id": chat_id, "text": lead["messages"][0]}
    status, latency, reply = post(target_url + "/lead/chat", payload)
    log(lid, target_name, "chat", status, latency, reply)

    followup = lead.get("followup")
    if followup and reply and PHONE_REQUEST_RE.search(reply):
        time.sleep(3)
        payload = {"application_id": lid, "chat_id": chat_id, "text": followup}
        status, latency, reply2 = post(target_url + "/lead/chat", payload)
        log(lid, target_name, "chat-followup", status, latency, reply2)
    elif followup:
        log(lid, target_name, "followup-skipped", "-", "-",
            "агент НЕ дозапросил контакт — клиент не ответил бы сам")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--boss", action="store_true", help="подать босс-заявку №31")
    p.add_argument("--test", action="store_true",
                   help="пробная заявка вне пакета (модели смотрят формат)")
    p.add_argument("--only", help="проверить только одного агента (его имя из AGENTS)")
    p.add_argument("--from", dest="start", type=int, default=1, help="начать с N-й заявки")
    p.add_argument("--limit", type=int, default=None, help="подать только N заявок (для смоук-теста)")
    p.add_argument("--interval", type=int, default=20)
    a = p.parse_args()

    with open(LEADS) as f:
        pack = json.load(f)
    targets = {k: v for k, v in TARGETS.items() if not a.only or k == a.only}

    os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    logpath = os.path.join(BASE, "logs", f"dispatch-{stamp}.csv")
    logfile = open(logpath, "w", newline="")
    w = csv.writer(logfile)
    w.writerow(["time", "lead", "target", "kind", "http", "latency_s", "reply"])
    log_lock = threading.Lock()

    def log(lid, tgt, kind, status, latency, reply):
        with log_lock:
            w.writerow([dt.datetime.now().strftime("%H:%M:%S"), lid, tgt, kind, status, latency,
                        (reply or "").replace("\n", " ")[:300]])
            logfile.flush()
            print(f"  [{lid} -> {tgt}] {kind} http={status} {latency}s :: {(reply or '')[:80]}", flush=True)

    if a.test:
        leads = [{"id": "TEST-00", "channel": "form",
                  "data": {"name": "Пётр", "phone": "+79990001122",
                           "comment": "Тестовая заявка: проверяю, как вы работаете",
                           "source_page": "/test"},
                  "expected": {"type": "test"}}]
        print("ПРОБНАЯ ЗАЯВКА (вне пакета):", flush=True)
    elif a.boss:
        leads = [dict(pack["boss"], channel="chat")]
        print("БОСС-ЗАЯВКА №31:", flush=True)
    else:
        leads = pack["leads"][a.start - 1:]
        if a.limit:
            leads = leads[:a.limit]
        print(f"Подача {len(leads)} заявок (с №{a.start}), интервал {a.interval}с, "
              f"агенты: {', '.join(targets)}", flush=True)

    for n, lead in enumerate(leads, a.start):
        print(f"— заявка {n}/{a.start - 1 + len(leads)}: {lead['id']} ({lead['channel']})", flush=True)
        # Параллельная подача: оба агента получают заявку в один момент —
        # зависание одного не даёт форы второму (аудит 2026-07-28)
        workers = [threading.Thread(target=send_lead, args=(lead, name, url, log))
                   for name, url in targets.items()]
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        if n < a.start - 1 + len(leads):
            time.sleep(a.interval)

    logfile.close()
    print(f"\nГотово. Лог: {logpath}", flush=True)


if __name__ == "__main__":
    main()
