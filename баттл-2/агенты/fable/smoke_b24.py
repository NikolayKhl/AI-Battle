#!/usr/bin/env python3
"""Смоук-тест записи в Битрикс24: создаёт боевые UF-поле и стадию «Есть контакт»,
проверяет на тестовых сущностях всё, что нельзя проверить read-only
(фильтруемость ORIGIN_ID, contact.add с одним NAME, task.add, timeline),
затем удаляет тестовые сущности. UF-поле и стадия остаются — они боевые."""
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CFG = json.loads((Path(__file__).parent.parent / "настройки-FABLE.json").read_text())
HOOK = CFG["b24_webhook"].rstrip("/")
CAT = CFG["b24_category_id"]
MSK = ZoneInfo("Europe/Moscow")


def call(method, params=None):
    data = urllib.parse.urlencode(params or {}, doseq=True).encode()
    req = urllib.request.Request(f"{HOOK}/{method}.json", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode())
    if "error" in resp:
        raise RuntimeError(f"{method}: {resp['error']} — {resp.get('error_description')}")
    return resp["result"]


def flat(d, prefix=""):
    """Разворачивает вложенный dict в плоские ключи вида fields[PHONE][0][VALUE]."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out.update(flat(v, key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out.update(flat(item, f"{key}[{i}]"))
                else:
                    out[f"{key}[{i}]"] = item
        else:
            out[key] = v
    return out


results = []


def step(name, fn):
    try:
        r = fn()
        results.append((name, "OK", r))
        print(f"  OK   {name}: {r}")
        return r
    except Exception as e:
        results.append((name, "FAIL", str(e)))
        print(f"  FAIL {name}: {e}")
        return None


print("=== Смоук-тест записи B24 ===")

# 1. UF-поле «Номер заявки» (боевое, остаётся)
def uf_field():
    existing = call("crm.deal.userfield.list", flat({"filter": {"FIELD_NAME": "UF_CRM_REQUEST_NUM"}}))
    if existing:
        return f"уже есть, id={existing[0]['ID']}"
    fid = call("crm.deal.userfield.add", flat({"fields": {
        "FIELD_NAME": "REQUEST_NUM",
        "USER_TYPE_ID": "string",
        "XML_ID": "FABLE_REQUEST_NUM",
        "SHOW_IN_LIST": "Y",
        "SHOW_FILTER": "I",
        "EDIT_FORM_LABEL": {"ru": "Номер заявки", "en": "Request number"},
        "LIST_COLUMN_LABEL": {"ru": "Номер заявки", "en": "Request number"},
        "LIST_FILTER_LABEL": {"ru": "Номер заявки", "en": "Request number"},
    }}))
    return f"создано, id={fid}"

step("UF-поле UF_CRM_REQUEST_NUM", uf_field)

# 2. Стадия «Есть контакт» (боевая, остаётся)
def stage_contact():
    stages = call("crm.status.list", flat({"filter": {"ENTITY_ID": f"DEAL_STAGE_{CAT}"}}))
    if any(s["STATUS_ID"] == f"C{CAT}:CONTACT" for s in stages):
        return "уже есть"
    sid = call("crm.status.add", flat({"fields": {
        "ENTITY_ID": f"DEAL_STAGE_{CAT}",
        "STATUS_ID": f"C{CAT}:CONTACT",
        "NAME": "Есть контакт",
        "SORT": 45,
    }}))
    return f"создана, id={sid}"

step("Стадия C4:CONTACT «Есть контакт»", stage_contact)

# 3. Контакт с одним NAME (проверка required LAST_NAME)
def contact_name_only():
    cid = call("crm.contact.add", flat({"fields": {"NAME": "Смоук-Тест"}}))
    return {"contact_id": cid, "note": "прошёл с одним NAME"}

contact = step("crm.contact.add (только NAME)", contact_name_only)
contact_id = contact["contact_id"] if contact else None

# 4. Сделка с ORIGIN_ID + UF-полем
SMOKE_NUM = "SMOKE-FABLE-TEST"

def deal_add():
    did = call("crm.deal.add", flat({"fields": {
        "TITLE": "Смоук-тест FABLE (удалить)",
        "CATEGORY_ID": CAT,
        "STAGE_ID": f"C{CAT}:NEW",
        "CONTACT_ID": contact_id or "",
        "ORIGIN_ID": SMOKE_NUM,
        "ORIGINATOR_ID": "fable",
        "UF_CRM_REQUEST_NUM": SMOKE_NUM,
        "COMMENTS": "смоук",
    }}))
    return {"deal_id": did}

deal = step("crm.deal.add (кат.4, ORIGIN_ID, UF)", deal_add)
deal_id = deal["deal_id"] if deal else None

# 5. Фильтруемость ORIGIN_ID
def origin_filter():
    rows = call("crm.deal.list", flat({
        "filter": {"ORIGIN_ID": SMOKE_NUM, "CATEGORY_ID": CAT},
        "select": ["ID", "ORIGIN_ID", "UF_CRM_REQUEST_NUM"],
    }))
    match = [r for r in rows if r.get("ORIGIN_ID") == SMOKE_NUM]
    if len(rows) != len(match):
        return f"ФИЛЬТР НЕ РАБОТАЕТ: вернулось {len(rows)}, совпало {len(match)} — дедуп только клиентской сверкой"
    if not match:
        raise RuntimeError("сделка не нашлась по ORIGIN_ID")
    uf = match[0].get("UF_CRM_REQUEST_NUM")
    return f"фильтр работает, UF в select: {uf!r}"

step("Фильтр deal.list по ORIGIN_ID+CATEGORY_ID", origin_filter)

# 6. Стадия сделки → CONTACT (проверка update на новую стадию)
def deal_stage_update():
    call("crm.deal.update", flat({"id": deal_id, "fields": {"STAGE_ID": f"C{CAT}:CONTACT"}}))
    got = call("crm.deal.get", {"id": deal_id})
    return f"стадия теперь {got['STAGE_ID']}"

if deal_id:
    step("crm.deal.update → C4:CONTACT", deal_stage_update)

# 7. Timeline-коммент
def timeline():
    tid = call("crm.timeline.comment.add", flat({"fields": {
        "ENTITY_ID": deal_id, "ENTITY_TYPE": "deal", "COMMENT": "Смоук-коммент FABLE",
    }}))
    return {"comment_id": tid}

tl = step("crm.timeline.comment.add", timeline) if deal_id else None

# 8. Задача с дедлайном и привязкой к сделке
def task_add():
    dl = (datetime.now(MSK) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S+03:00")
    r = call("tasks.task.add", flat({"fields": {
        "TITLE": "Смоук-задача FABLE (удалить)",
        "RESPONSIBLE_ID": 1,
        "DEADLINE": dl,
        "DESCRIPTION": "смоук",
        "UF_CRM_TASK": [f"D_{deal_id}"],
    }}))
    tid = r["task"]["id"] if isinstance(r, dict) and "task" in r else r
    got = call("tasks.task.get", flat({"taskId": tid, "select": ["ID", "DEADLINE", "UF_CRM_TASK"]}))
    t = got["task"] if isinstance(got, dict) and "task" in got else got
    return {"task_id": tid, "deadline": t.get("deadline"), "crm": t.get("ufCrmTask")}

task = step("tasks.task.add (DEADLINE, UF_CRM_TASK)", task_add) if deal_id else None

# 9. Очистка тестовых сущностей (UF-поле и стадию оставляем)
def cleanup():
    done = []
    if task:
        call("tasks.task.delete", flat({"taskId": task["task_id"]}))
        done.append("task")
    if deal_id:
        call("crm.deal.delete", {"id": deal_id})
        done.append("deal")
    if contact_id:
        call("crm.contact.delete", {"id": contact_id})
        done.append("contact")
    return done

step("Очистка (task, deal, contact)", cleanup)

fails = [r for r in results if r[1] == "FAIL"]
print(f"\nИтог: {len(results) - len(fails)}/{len(results)} OK" + (f", ПРОВАЛЫ: {[r[0] for r in fails]}" if fails else " — всё зелёное"))
sys.exit(1 if fails else 0)
