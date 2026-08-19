#!/usr/bin/env python3
"""Коннектор к amoCRM для стенда баттла №3.

Наполняет CRM сделками из deals.json (переписка + звонки + стадии) и умеет
их читать обратно — по этим данным приёмка сверяет вердикты агентов.

Доступ (файл env.sh рядом, в git не попадает):
    export AMO_SUBDOMAIN='мойаккаунт'          # мойаккаунт.amocrm.ru
    export AMO_TOKEN='долгосрочный токен'      # из интеграции
    export AMO_PIPELINE_ID='123456'            # воронка для стенда

Без доступа работает в режиме заглушки: --dry-run печатает, что бы сделал,
и проверяет корректность payload. Это позволяет собрать и отладить стенд
до получения токена.

Команды:
    python3 amo.py --check              проверить доступ и воронку
    python3 amo.py --fill               наполнить CRM 30 сделками
    python3 amo.py --fill --dry-run     то же без сети (проверка payload)
    python3 amo.py --clear              удалить сделки стенда из воронки
    python3 amo.py --dump               выгрузить сделки с перепиской в JSON
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
MARK = "AI-BATTLE-3"          # метка стенда: по ней сделки находятся и чистятся


def load_env():
    path = os.path.join(BASE, "env.sh")
    if os.path.exists(path):
        for line in open(path):
            m = re.match(r"export (\w+)='?([^'\n]+)'?", line.strip())
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2)


load_env()
SUB = os.environ.get("AMO_SUBDOMAIN", "")
TOKEN = os.environ.get("AMO_TOKEN", "")
PIPELINE = os.environ.get("AMO_PIPELINE_ID", "")
API = f"https://{SUB}.amocrm.ru/api/v4" if SUB else ""


def call(method, path, payload=None, tries=3):
    url = API + path
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    })
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode()
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in (429, 502, 503) and i < tries - 1:
                continue
            raise SystemExit(f"amoCRM {method} {path} → HTTP {e.code}: {detail}")
        except Exception:
            if i == tries - 1:
                raise


def ts(s):
    """«2026-08-01 10:12» → unix-время (МСК)."""
    naive = dt.datetime.strptime(s, "%Y-%m-%d %H:%M")
    return int(naive.replace(tzinfo=dt.timezone(dt.timedelta(hours=3))).timestamp())


def stage_map():
    """Соответствие названий стадий из deals.json id-шникам воронки."""
    res = call("GET", f"/leads/pipelines/{PIPELINE}")
    statuses = res["_embedded"]["statuses"]
    by_name = {s["name"].strip().lower(): s["id"] for s in statuses}
    order = [s["id"] for s in sorted(statuses, key=lambda s: s["sort"])]
    return by_name, order, statuses


# Стадии из deals.json → названия стандартных стадий amoCRM
STAGE_ALIAS = {
    "первичный контакт": ["первичный контакт"],
    "квалификация": ["первичный контакт", "неразобранное"],
    "переговоры": ["переговоры"],
    "предложение": ["принимают решение", "переговоры"],
    "счёт": ["согласование договора", "принимают решение"],
}


def build_payload(deal, by_name, order):
    """Сделка → тело запроса amoCRM. Стадия ищется по имени, затем по алиасам."""
    key = deal["stage"].strip().lower()
    sid = by_name.get(key)
    if sid is None:
        for alias in STAGE_ALIAS.get(key, []):
            if alias in by_name:
                sid = by_name[alias]
                break
    if sid is None:
        sid = order[min(1, len(order) - 1)]
    body_name = f"{deal['client']} — {MARK} {deal['id']}"
    return {
        "name": body_name,
        "price": deal["amount"],
        "status_id": sid,
        "pipeline_id": int(PIPELINE) if PIPELINE else 0,
        "created_at": ts(deal["created"]),
        "_embedded": {"tags": [{"name": MARK}]},
    }


def fill(dry):
    pack = json.load(open(os.path.join(BASE, "deals.json")))
    deals = pack["deals"]
    if dry:
        by_name, order = {}, [1, 2, 3, 4, 5]
        print(f"[заглушка] воронка не запрашивалась, стадии подставлены условно")
    else:
        by_name, order, statuses = stage_map()
        print(f"Воронка {PIPELINE}: стадии — {', '.join(s['name'] for s in statuses)}")

    created = 0
    for d in deals:
        body = build_payload(d, by_name, order)
        head = [{"note_type": "common",
                 "params": {"text": f"КАРТОЧКА · менеджер: {d['manager']} · "
                                    f"исход: { {'won':'закрыта успешно','lost':'проиграна','open':'в работе'}[d['outcome']]}"
                                    + (f" · причина: {d['loss_reason']}" if d.get('loss_reason')
                                       else (" · причина не указана" if d['outcome'] == 'lost' else ""))},
                 "created_at": ts(d["created"])}]
        notes = head + [{"note_type": "common",
                  "params": {"text": f"[{at}] {'КЛИЕНТ' if who == 'client' else 'МЕНЕДЖЕР'}: {text}"},
                  "created_at": ts(at)}
                 for who, at, text in d["chat"]]
        for c in d["calls"]:
            notes.append({"note_type": "common",
                          "params": {"text": f"[{c['at']}] ЗВОНОК {'исходящий' if c['dir'] == 'out' else 'входящий'}, "
                                             f"{c['sec']} сек"},
                          "created_at": ts(c["at"])})
        if dry:
            assert body["name"] and body["price"] >= 0 and notes, d["id"]
            print(f"  [dry] {d['id']}: сделка «{body['name']}», записей переписки {len(notes)}")
            created += 1
            continue
        res = call("POST", "/leads", [body])
        lead_id = res["_embedded"]["leads"][0]["id"]
        call("POST", f"/leads/{lead_id}/notes", notes)
        created += 1
        print(f"  {d['id']} → сделка {lead_id}, записей {len(notes)}", flush=True)
    print(f"\nГотово: {created} сделок{' (заглушка, без сети)' if dry else ''}")


def find_stand_leads():
    out, page = [], 1
    while True:
        q = urllib.parse.urlencode({"filter[pipeline_id]": PIPELINE, "limit": 250,
                                    "page": page, "query": MARK})
        res = call("GET", f"/leads?{q}")
        if not res or "_embedded" not in res:
            break
        out += res["_embedded"]["leads"]
        if len(res["_embedded"]["leads"]) < 250:
            break
        page += 1
    return [l for l in out if MARK in (l.get("name") or "")]


def clear():
    """Удаление сделок: amoCRM v4 не поддерживает DELETE /leads,
    поэтому используем метод корзины (v2) — он живой и штатный."""
    leads = find_stand_leads()
    if not leads:
        print("Сделок стенда не найдено — воронка чистая")
        return
    url = f"https://{SUB}.amocrm.ru/api/v2/leads/delete"
    body = urllib.parse.urlencode({"ID[]": [l["id"] for l in leads]}, doseq=True).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
        print(f"Удалено сделок стенда: {len(leads)}")
    except urllib.error.HTTPError as e:
        print(f"Массовое удаление недоступно (HTTP {e.code}), удаляю поштучно...")
        ok = 0
        for l in leads:
            try:
                call("PATCH", f"/leads/{l['id']}", {"status_id": 143})  # закрыто-не реализовано
                ok += 1
            except SystemExit:
                pass
        print(f"Помечено закрытыми: {ok} из {len(leads)}. "
              "Полное удаление — вручную в интерфейсе (выделить и «Удалить»).")


def dump():
    leads = find_stand_leads()
    out = []
    for l in leads:
        notes = call("GET", f"/leads/{l['id']}/notes?limit=250")
        texts = [n["params"].get("text", "") for n in
                 notes.get("_embedded", {}).get("notes", [])] if notes else []
        m = re.search(r"AI-BATTLE-3 (D-\d\d)", l.get("name", ""))
        out.append({"deal_id": m.group(1) if m else None, "lead_id": l["id"],
                    "name": l["name"], "status_id": l["status_id"], "notes": texts})
    path = os.path.join(BASE, "crm-dump.json")
    json.dump(out, open(path, "w"), ensure_ascii=False, indent=1)
    print(f"Выгружено сделок: {len(out)} → {path}")


def check():
    if not (SUB and TOKEN and PIPELINE):
        print("Доступ не задан. Нужен env.sh рядом со скриптом:")
        print("  export AMO_SUBDOMAIN='...'\n  export AMO_TOKEN='...'\n  export AMO_PIPELINE_ID='...'")
        print("\nПока доступа нет, стенд проверяется заглушкой: python3 amo.py --fill --dry-run")
        sys.exit(1)
    acc = call("GET", "/account")
    by_name, order, statuses = stage_map()
    print(f"Аккаунт: {acc.get('name')} ({SUB}.amocrm.ru)")
    print(f"Воронка {PIPELINE}, стадии: {', '.join(s['name'] for s in statuses)}")
    print(f"Сделок стенда сейчас: {len(find_stand_leads())}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    p.add_argument("--fill", action="store_true")
    p.add_argument("--clear", action="store_true")
    p.add_argument("--dump", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if a.check:
        check()
    elif a.fill:
        if not a.dry_run and not (SUB and TOKEN and PIPELINE):
            check()
        fill(a.dry_run)
    elif a.clear:
        clear()
    elif a.dump:
        dump()
    else:
        p.print_help()
