#!/usr/bin/env python3
"""Чтение вердиктов контролёров из карточек amoCRM.

С 16.08 участники (SOL и FABLE) пишут результат аудита прямо в карточку
сделки — примечанием фиксированного формата (см. participants/ТЗ.md):

    АУДИТ SOL
    Нарушений: 2
    3.2 — согласился с возражением и пропал. Цитата: «…» (чат, 01.08 12:40)
    2.1 — цена без выяснения задачи. Цитата: «…» (звонок 01.08 10:12)

или, для чистой сделки:

    АУДИТ FABLE
    Нарушений: нет

Этот модуль обходит сорок сделок стенда, находит такие примечания и
собирает {deal_id: {агент: {...}}}. По ним диспетчер показывает вердикты
рядом с карточкой, ведёт счётчик прогона и считает приёмку. Если агент
переписал своё примечание (PATCH) или создал новое — берётся самое свежее.

Секретов не печатает. Сеть — только чтение (GET).

Запуск руками:
    python3 аудит.py                 сводка: кто сколько карточек заполнил
    python3 аудит.py --dump          выгрузить вердикты в results/audit-crm.json
    python3 аудит.py --deal D-09     показать вердикты по одной сделке
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import amo  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
AGENTS = ("SOL", "FABLE")

HEAD = re.compile(r"^\s*АУДИТ\s*[·:\-—]?\s*(SOL|FABLE)\b", re.I)
# строка нарушения начинается с номера пункта: «3.2 — …», «· 3.2 …», «- 3.2: …»
CLAUSE_LINE = re.compile(r"^\s*(?:[-•·*]\s*)?(?:пункт\s+)?(\d{1,2}\.\d)(?!\d)", re.I)


def known_clauses():
    cat = json.load(open(os.path.join(BASE, "deals.json")))["violations_catalog"]
    return {v["clause"] for v in cat.values()}


KNOWN = known_clauses()


def parse_note(text):
    """Текст примечания → (агент, [пункты]) или None, если это не аудит."""
    lines = text.strip().splitlines()
    if not lines:
        return None
    m = HEAD.match(lines[0])
    if not m:
        return None
    agent = m.group(1).upper()
    clauses = []
    for line in lines[1:]:
        c = CLAUSE_LINE.match(line)
        if c and c.group(1) in KNOWN and c.group(1) not in clauses:
            clauses.append(c.group(1))
    return agent, clauses


def deal_id_of(lead):
    m = re.search(r"AI-BATTLE-3 (D-\d\d)", lead.get("name") or "")
    return m.group(1) if m else None


def fetch(only=None):
    """{deal_id: {agent: {"clauses": [...], "text": str, "updated": int,
                          "note_id": int, "lead_id": int}}}"""
    out = {}
    for lead in amo.find_stand_leads():
        did = deal_id_of(lead)
        if not did or (only and did != only):
            continue
        out.setdefault(did, {})
        res = amo.call("GET", f"/leads/{lead['id']}/notes?limit=250") or {}
        for n in res.get("_embedded", {}).get("notes", []):
            if n.get("note_type") != "common":
                continue
            parsed = parse_note(n.get("params", {}).get("text", "") or "")
            if not parsed:
                continue
            agent, clauses = parsed
            stamp = n.get("updated_at") or n.get("created_at") or 0
            cur = out[did].get(agent)
            if cur is None or stamp >= cur["updated"]:
                out[did][agent] = {"clauses": clauses,
                                   "text": n["params"]["text"],
                                   "updated": stamp, "note_id": n["id"],
                                   "lead_id": lead["id"]}
    return out


def as_reports(audits):
    """→ [(agent, {deal_id: [clauses]})] в формате, который ест приёмка."""
    reps = []
    for agent in AGENTS:
        rep = {d: a[agent]["clauses"] for d, a in audits.items() if agent in a}
        if rep:
            reps.append((agent, rep))
    return reps


def summary(audits, total=40):
    parts = []
    for agent in AGENTS:
        n = sum(1 for a in audits.values() if agent in a)
        parts.append(f"{agent} {n}/{total}")
    return " · ".join(parts)


def dump(audits):
    os.makedirs(os.path.join(BASE, "results"), exist_ok=True)
    path = os.path.join(BASE, "results", "audit-crm.json")
    json.dump(audits, open(path, "w"), ensure_ascii=False, indent=1)
    return path


def fmt_time(stamp):
    return dt.datetime.fromtimestamp(stamp, dt.timezone(dt.timedelta(hours=3))
                                     ).strftime("%H:%M")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dump", action="store_true")
    p.add_argument("--deal")
    a = p.parse_args()
    audits = fetch(a.deal)
    if a.deal:
        for agent in AGENTS:
            v = audits.get(a.deal, {}).get(agent)
            print(f"— {agent}: " + ("карточка не заполнена" if not v else
                                    f"пункты {', '.join(v['clauses']) or 'нет'} "
                                    f"({fmt_time(v['updated'])})"))
            if v:
                print("   " + v["text"].replace("\n", "\n   "))
    else:
        print(summary(audits))
    if a.dump:
        print("→", dump(audits))
