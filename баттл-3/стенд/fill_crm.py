#!/usr/bin/env python3
"""Наполнение amoCRM сделками стенда — как в живой CRM, а не свалкой заметок.

Что создаётся по каждой сделке:
  · контакт с именем и телефоном, привязанный к сделке (телефона нет там,
    где менеджер его не перенёс — это нарушение 8.3 стандарта);
  · поля: Менеджер, Источник заявки, Причина проигрыша (пустая там, где
    менеджер её не заполнил — нарушение 8.4);
  · стадия, соответствующая исходу: выиграна → «Успешно реализовано»,
    проиграна → «Закрыто и не реализовано», в работе → рабочая стадия.
    Исключение — сделки с нарушением 8.1, у них стадия намеренно неверна;
  · переписка — примечаниями в хронологии, звонки — звонковыми
    примечаниями с длительностью;
  · задача с дедлайном там, где менеджер назначил следующий шаг. Где
    нарушен пункт 5.1/5.2 — задачи нет, и это видно в карточке.

Запуск (в чистом окружении, иначе переменные шелла перебьют env.sh):
    env -u AMO_PIPELINE_ID python3 fill_crm.py --check
    env -u AMO_PIPELINE_ID python3 fill_crm.py --new-pipeline
    env -u AMO_PIPELINE_ID python3 fill_crm.py --fill
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import amo  # noqa: E402  (переиспользуем call/ts/env)
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("calls", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "calls.py"))
calls = _ilu.module_from_spec(_spec); _spec.loader.exec_module(calls)

BASE = os.path.dirname(os.path.abspath(__file__))
MARK = amo.MARK

STAGE_ALIAS = {
    "первичный контакт": "первичный контакт",
    "квалификация": "квалификация",
    "переговоры": "переговоры",
    "предложение": "предложение",
    "счёт": "счёт",
}
SOURCE_BY_STAGE = {"Первичный контакт": "Сайт", "Квалификация": "Чат на сайте",
                   "Переговоры": "Сайт", "Предложение": "Рекомендация", "Счёт": "Сайт"}


def fields():
    return json.load(open(os.path.join(BASE, "fields.json")))


def statuses(pipeline):
    res = amo.call("GET", f"/leads/pipelines/{pipeline}")["_embedded"]["statuses"]
    return {s["name"].strip().lower(): s["id"] for s in res}, res


def new_pipeline():
    body = [{"name": "AI-Баттл №3", "is_main": False, "is_unsorted_on": False, "sort": 10,
             "_embedded": {"statuses": [
                 {"name": "Первичный контакт", "sort": 10, "color": "#fffeb2"},
                 {"name": "Квалификация", "sort": 20, "color": "#fffd7f"},
                 {"name": "Переговоры", "sort": 30, "color": "#fff000"},
                 {"name": "Предложение", "sort": 40, "color": "#ffeab2"},
                 {"name": "Счёт", "sort": 50, "color": "#ffdc7f"}]}}]
    p = amo.call("POST", "/leads/pipelines", body)["_embedded"]["pipelines"][0]
    print(f"Создана воронка {p['id']} — {p['name']}")
    path = os.path.join(BASE, "env.sh")
    lines = []
    for line in open(path):
        lines.append(f"export AMO_PIPELINE_ID='{p['id']}'\n"
                     if line.startswith("export AMO_PIPELINE_ID") else line)
    open(path, "w").writelines(lines)
    print("env.sh обновлён")
    return p["id"]


def phone_of(deal):
    """Телефон из переписки, если менеджер его сохранил."""
    if "phone_not_saved" in deal["verdict"]:
        return None
    import re
    for _, _, text in deal["chat"]:
        m = re.search(r"\+?7[\s\-()]*\d[\d\s\-()]{8,}", text)
        if m:
            return "+7" + "".join(c for c in m.group(0)[2:] if c.isdigit())
    # телефон есть у всех клиентов — генерим стабильный по id сделки
    n = int("".join(c for c in deal["id"] if c.isdigit()))
    return f"+79{n:09d}"[:12]


def target_status(deal, by_name):
    """Стадия по исходу; для нарушения 8.1 — намеренно неверная."""
    if "wrong_stage" in deal["verdict"]:
        return by_name.get(STAGE_ALIAS.get(deal["stage"].strip().lower(), ""), None)
    if deal["outcome"] == "won":
        return by_name.get("успешно реализовано")
    if deal["outcome"] == "lost":
        return by_name.get("закрыто и не реализовано")
    return by_name.get(STAGE_ALIAS.get(deal["stage"].strip().lower(), ""))


def last_activity(deal):
    stamps = [amo.ts(at) for _, at, _ in deal["chat"]] + [amo.ts(c["at"]) for c in deal["calls"]]
    return max(stamps) if stamps else amo.ts(deal["created"])


def build_task(deal, lead_id):
    """Задача есть там, где менеджер назначил следующий шаг."""
    if {"no_next_step", "abandoned"} & set(deal["verdict"]):
        return None
    last = last_activity(deal)
    if deal["outcome"] == "open":
        deadline = last + 86400
        completed = False
        text = "Связаться с клиентом по следующему шагу"
    else:
        deadline = last - 3600
        completed = True
        text = "Отработать следующий шаг по договорённости"
    task = {"task_type_id": 1, "text": text, "complete_till": deadline,
            "entity_id": lead_id, "entity_type": "leads",
            "created_at": last - 7200, "is_completed": completed}
    if completed:
        task["result"] = {"text": "Выполнено"}
    return task


def fill(only=None):
    pipeline = os.environ.get("AMO_PIPELINE_ID") or amo.PIPELINE
    by_name, raw = statuses(pipeline)
    F = fields()
    pack = json.load(open(os.path.join(BASE, "deals.json")))
    deals = pack["deals"]
    # идемпотентность: пропускаем то, что уже залито
    import re as _re
    present = set()
    for l in amo.find_stand_leads():
        m = _re.search(r"(D-\d\d)", l.get("name") or "")
        if m:
            present.add(m.group(1))
    if present:
        print(f"Уже в воронке: {len(present)} сделок — пропускаю их")
    deals = [d for d in deals if d["id"] not in present]
    if only:
        deals = [d for d in json.load(open(os.path.join(BASE, "deals.json")))["deals"]
                 if d["id"] == only]
    print(f"Воронка {pipeline}: {', '.join(s['name'] for s in raw)}")
    print(f"К заливке: {len(deals)}\n")
    if not deals:
        print("Всё уже залито")
        return

    for d in deals:
        # 1. контакт
        phone = phone_of(d)
        cf = [{"field_code": "PHONE", "values": [{"value": phone, "enum_code": "WORK"}]}] if phone else []
        contact = amo.call("POST", "/contacts", [{
            "name": d["client"], "created_at": amo.ts(d["created"]),
            "custom_fields_values": cf or None}])["_embedded"]["contacts"][0]["id"]

        # 2. сделка
        cfv = [{"field_id": F["Менеджер"]["id"],
                "values": [{"enum_id": F["Менеджер"]["enums"][d["manager"]]}]},
               {"field_id": F["Источник заявки"]["id"],
                "values": [{"enum_id": F["Источник заявки"]["enums"][
                    SOURCE_BY_STAGE.get(d["stage"], "Сайт")]}]}]
        if d.get("loss_reason"):
            cfv.append({"field_id": F["Причина проигрыша"]["id"],
                        "values": [{"value": d["loss_reason"]}]})
        body = {"name": f"{d['client']} — {MARK} {d['id']}", "price": d["amount"],
                "status_id": target_status(d, by_name), "pipeline_id": int(pipeline),
                "created_at": amo.ts(d["created"]), "custom_fields_values": cfv,
                "_embedded": {"tags": [{"name": MARK}], "contacts": [{"id": contact}]}}
        if d["outcome"] in ("won", "lost"):
            body["closed_at"] = last_activity(d)
        lead = amo.call("POST", "/leads", [body])["_embedded"]["leads"][0]["id"]

        # 3. переписка и звонки в хронологии
        events = [(amo.ts(at), {"note_type": "common", "created_at": amo.ts(at),
                                "params": {"text": ("Клиент: " if who == "client" else "Менеджер: ") + text}})
                  for who, at, text in d["chat"]]
        for ci, c in enumerate(d["calls"]):
            tr = calls.for_deal(d["id"], ci)
            if tr:
                events.append((amo.ts(c["at"]) + 1, {
                    "note_type": "common", "created_at": amo.ts(c["at"]) + 1,
                    "params": {"text": calls.as_text(tr)}}))
            events.append((amo.ts(c["at"]), {
                "note_type": "call_out" if c["dir"] == "out" else "call_in",
                "created_at": amo.ts(c["at"]),
                "params": {"uniq": f"{d['id']}-{c['at']}", "duration": c["sec"],
                           "source": "Телефония", "phone": phone or ""}}))
        events.sort(key=lambda x: x[0])
        amo.call("POST", f"/leads/{lead}/notes", [e[1] for e in events])

        # 4. задача
        task = build_task(d, lead)
        if task:
            amo.call("POST", "/tasks", [task])

        print(f"  {d['id']} · {d['client']:22} · {d['manager']:18} · "
              f"{ {'won':'закрыта','lost':'проиграна','open':'в работе'}[d['outcome']]:9} · "
              f"задача {'есть' if task else 'НЕТ'} · телефон {'есть' if phone else 'НЕТ'} · "
              f"расшифровок {sum(1 for i in range(len(d['calls'])) if calls.for_deal(d['id'], i))}", flush=True)

    print(f"\nГотово: {len(deals)} сделок с контактами, полями, задачами и перепиской")


def check():
    pipeline = os.environ.get("AMO_PIPELINE_ID") or amo.PIPELINE
    by_name, raw = statuses(pipeline)
    leads = amo.find_stand_leads()
    print(f"Воронка {pipeline}: {', '.join(s['name'] for s in raw)}")
    print(f"Сделок стенда: {len(leads)}")
    if leads:
        d = amo.call("GET", f"/leads/{leads[0]['id']}?with=contacts")
        print(f"Пример: {d['name']} · полей {len(d.get('custom_fields_values') or [])} · "
              f"контактов {len(d.get('_embedded', {}).get('contacts', []))}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--new-pipeline", action="store_true")
    p.add_argument("--fill", action="store_true")
    p.add_argument("--only", help="залить только указанную сделку, напр. D-39")
    p.add_argument("--check", action="store_true")
    a = p.parse_args()
    if a.new_pipeline:
        new_pipeline()
    elif a.fill:
        fill(a.only)
    else:
        check()
