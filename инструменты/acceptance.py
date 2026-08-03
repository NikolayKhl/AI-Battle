#!/usr/bin/env python3
"""Приёмка AI-Баттла №2: сверка работы обоих агентов с эталоном.

Принципы (после аудита 2026-07-28):
  - ФИКСИРОВАННАЯ МАТРИЦА: набор проверок каждой заявки определяется только
    эталоном (leads.json), а не тем, что успел сделать агент. Нет сделки —
    все зависимые проверки автоматически FAIL. Знаменатели обоих агентов
    всегда совпадают.
  - Кросс-чек доставки: заявка, не доставленная агенту (по логу диспетчера),
    даёт NOT_TESTED, а не бесплатный OK.
  - Автомат судит только объективное. Стадии и качество текста — колонка
    MANUAL, оценивается в кадре.

Проверки: A сделка со сквозным id · C имя · D телефон · E ответ клиенту
(наличие + не шаблон; суть — MANUAL) · F задача с валидным дедлайном ·
G спам отсечён (токсичные) · H дозапрос телефона (чаты) · I нет дублей.

Окружение: B24_WEBHOOK, SOL_CATEGORY_ID, FABLE_CATEGORY_ID.
Использование: python3 acceptance.py [--dispatch-log logs/dispatch-XXX.csv]
Выход: stand/results/acceptance-<время>.md
"""
import argparse
import csv
import datetime as dt
import glob
import json
import os
import re
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
LEADS = os.environ.get("LEADS") or next(
    (p for p in (os.path.join(BASE, "leads.json"),
                 os.path.join(os.path.dirname(BASE), "заявки", "leads.json"))
     if os.path.exists(p)), os.path.join(BASE, "leads.json"))

def _load_env():
    """Подхватить env.sh рядом со скриптом, если переменные не заданы."""
    path = os.path.join(BASE, "env.sh")
    if os.path.exists(path):
        for line in open(path):
            m = re.match(r"export (\w+)='?([^'\n]+)'?", line.strip())
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2)

_load_env()
WEBHOOK = os.environ.get("B24_WEBHOOK", "").rstrip("/")
# Воронки агентов. Формат CATEGORIES: "имя=id,имя2=id2"
# Пример одного агента:  CATEGORIES="мой=5"
_default_cats = f"sol={os.environ.get('SOL_CATEGORY_ID', '')},fable={os.environ.get('FABLE_CATEGORY_ID', '')}"
CATS = {k: v for k, v in
        (pair.split("=", 1) for pair in os.environ.get("CATEGORIES", _default_cats).split(",") if "=" in pair)
        if v}
APP_RE = re.compile(r"[FC]-\d\d|BOSS-31")


def _fetch(url, tries=4):
    """GET с ретраями: часть CDN-адресов bitrix24.ru может не отвечать."""
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise


def b24(method, params=None):
    results = []
    start = 0
    while True:
        q = dict(params or {})
        q["start"] = start
        data = _fetch(f"{WEBHOOK}/{method}?{urllib.parse.urlencode(q, doseq=True)}")
        chunk = data.get("result", [])
        if isinstance(chunk, dict):
            chunk = chunk.get("tasks", chunk)
        results += chunk if isinstance(chunk, list) else [chunk]
        if "next" not in data:
            return results
        start = data["next"]


def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d[0] in "78":
        d = "7" + d[1:]
    return "+" + d if d else ""


def norm_reply(text, name):
    t = (text or "").lower()
    if name:
        t = t.replace(name.lower(), "")
    return re.sub(r"\W+", " ", t).strip()


def keyword(lead):
    text = (lead["data"]["comment"] if lead["channel"] == "form" else lead["messages"][0]).lower()
    for kw in ["crm", "црм", "битрикс", "бот", "запис", "звонк", "отчёт", "отчет", "магазин",
               "поддержк", "авито", "школ", "кафе", "салон", "автосервис", "цвет", "стоматолог",
               "партнёрств", "партнерств", "прайс", "воронк"]:
        if kw in text:
            return kw
    return None


def parse_deadline(t):
    raw = t.get("deadline") or t.get("DEADLINE") or ""
    try:
        dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return True
    except (ValueError, AttributeError):
        return False


def applicable_checks(lead):
    exp = lead["expected"]
    if exp["type"] == "toxic":
        return ["G_спам", "I_дубли"]
    checks = ["A_сделка", "E_ответ", "F_задача", "I_дубли"]
    if exp.get("name"):
        checks.append("C_имя")
    if exp.get("phone") or (exp.get("phone") is None and exp.get("needs_phone_request")):
        checks.append("D_телефон")
    if lead["channel"] == "chat" and "needs_phone_request" in exp:
        checks.append("H_дозапрос")
    return checks


def check_agent(agent, leads, dispatch_rows):
    cat = CATS[agent]
    deals_raw = b24("crm.deal.list", {
        "filter[CATEGORY_ID]": cat,
        "select[]": ["ID", "TITLE", "STAGE_ID", "CONTACT_ID", "SOURCE_DESCRIPTION"]})
    deals_by_app = {}
    for d in deals_raw:
        m = APP_RE.search((d.get("SOURCE_DESCRIPTION") or "") + " " + (d.get("TITLE") or ""))
        if m:
            deals_by_app.setdefault(m.group(0), []).append(d)

    contacts = {c["ID"]: c for c in b24("crm.contact.list", {"select[]": ["ID", "NAME", "PHONE"]})}
    tasks = b24("tasks.task.list", {"select[]": ["ID", "TITLE", "DEADLINE", "UF_CRM_TASK"]})
    delivered = {(r["lead"], r["target"]) for r in dispatch_rows
                 if r["kind"] in ("form", "chat") and r["http"] == "200"}
    # Ответ клиенту в HTTP-reply (из лога почтальона) — засчитывается:
    # человеческое ТЗ не требовало дублировать ответ в карточку
    log_replies = {}
    for r in dispatch_rows:
        if r["target"] == agent and r["kind"] in ("form", "chat") and r["http"] == "200":
            log_replies.setdefault(r["lead"], r["reply"] or "")
    asked = {(r["lead"], r["target"]) for r in dispatch_rows if r["kind"] == "chat-followup"}
    not_asked = {(r["lead"], r["target"]) for r in dispatch_rows if r["kind"] == "followup-skipped"}

    # Ответы для детекта шаблонов: заявка -> нормализованный текст комментариев
    replies = {}
    for lid, dlist in deals_by_app.items():
        deal = dlist[0]
        comments = b24("crm.timeline.comment.list",
                       {"filter[ENTITY_ID]": deal["ID"], "filter[ENTITY_TYPE]": "deal",
                        "select[]": ["COMMENT"]})
        text = " ".join((cm.get("COMMENT") or "") for cm in comments)
        exp = next((l["expected"] for l in leads if l["id"] == lid), {})
        replies[lid] = {"raw": text, "norm": norm_reply(text, exp.get("name"))}
    norm_counts = {}
    for v in replies.values():
        if len(v["norm"]) > 40:
            norm_counts[v["norm"]] = norm_counts.get(v["norm"], 0) + 1

    rows = []
    for lead in leads:
        lid = lead["id"]
        exp = lead["expected"]
        r = {"id": lid, "type": exp["type"]}
        checks = applicable_checks(lead)

        if (lid, agent) not in delivered:
            for c in checks:
                r[c] = "NOT_TESTED (не доставлена)"
            rows.append(r)
            continue

        dlist = deals_by_app.get(lid, [])
        deal = dlist[0] if dlist else None
        no_deal = "FAIL (нет сделки)"

        if "G_спам" in checks:
            stage = (deal or {}).get("STAGE_ID", "").upper()
            junk = deal is None or "JUNK" in stage or "LOSE" in stage or "APOLOGY" in stage
            r["G_спам"] = "OK" if junk else "FAIL (рабочая сделка на спам)"
        if "I_дубли" in checks:
            r["I_дубли"] = "OK" if len(dlist) <= 1 else f"FAIL ({len(dlist)} сделки на одну заявку)"
        if "A_сделка" in checks:
            r["A_сделка"] = "OK" if deal else no_deal
        if "C_имя" in checks:
            if not deal:
                r["C_имя"] = no_deal
            else:
                got = contacts.get(str(deal.get("CONTACT_ID") or ""), {}).get("NAME", "")
                match = got and (exp["name"].lower() in got.lower() or got.lower() in exp["name"].lower())
                r["C_имя"] = "OK" if match else f"FAIL (в карточке: '{got}')"
        if "D_телефон" in checks:
            if not deal:
                r["D_телефон"] = no_deal
            else:
                phones = [norm_phone(p.get("VALUE")) for p in
                          contacts.get(str(deal.get("CONTACT_ID") or ""), {}).get("PHONE", [])]
                if exp.get("phone"):
                    r["D_телефон"] = "OK" if exp["phone"] in phones else f"FAIL (в карточке: {phones})"
                else:
                    r["D_телефон"] = "OK (клиент не дал — пусто)" if not any(phones) \
                        else f"FAIL (выдуман телефон: {phones})"
        if "E_ответ" in checks:
            if not deal:
                r["E_ответ"] = no_deal
            else:
                rep = replies.get(lid, {"raw": "", "norm": ""})
                if not rep["raw"].strip() and log_replies.get(lid, "").strip():
                    rep = {"raw": log_replies[lid],
                           "norm": norm_reply(log_replies[lid], exp.get("name"))}
                if not rep["raw"].strip():
                    r["E_ответ"] = "FAIL (ответа нет нигде)"
                elif norm_counts.get(rep["norm"], 0) > 1:
                    r["E_ответ"] = "FAIL (шаблон: тот же текст другим клиентам)"
                elif keyword(lead) and keyword(lead) not in rep["raw"].lower():
                    r["E_ответ"] = "MANUAL (ответ есть, суть проверить в кадре)"
                else:
                    r["E_ответ"] = "OK"
        if "F_задача" in checks:
            if not deal:
                r["F_задача"] = no_deal
            else:
                dtasks = [t for t in tasks if f"D_{deal['ID']}" in
                          json.dumps(t.get("ufCrmTask") or t.get("UF_CRM_TASK") or [])]
                if not dtasks:
                    r["F_задача"] = "FAIL (нет задачи менеджеру)"
                elif not any(parse_deadline(t) for t in dtasks):
                    r["F_задача"] = "FAIL (дедлайн пуст или невалиден)"
                else:
                    r["F_задача"] = "OK"
        if "H_дозапрос" in checks:
            if exp["needs_phone_request"]:
                if (lid, agent) in asked:
                    r["H_дозапрос"] = "OK"
                elif (lid, agent) in not_asked:
                    r["H_дозапрос"] = "FAIL (не дозапросил телефон)"
                else:
                    r["H_дозапрос"] = "MANUAL (лог неполон)"
            else:
                r["H_дозапрос"] = "FAIL (переспросил уже данный телефон)" \
                    if (lid, agent) in asked else "OK"
        rows.append(r)
    return rows


def score(rows):
    ok = fail = manual = nt = 0
    for r in rows:
        for k, v in r.items():
            if k in ("id", "type"):
                continue
            v = str(v)
            if v.startswith("OK"):
                ok += 1
            elif v.startswith("FAIL"):
                fail += 1
            elif v.startswith("MANUAL"):
                manual += 1
            else:
                nt += 1
    return ok, fail, manual, nt


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dispatch-log", default=None)
    a = p.parse_args()
    if not WEBHOOK or not all(CATS.values()):
        raise SystemExit("Задай окружение: B24_WEBHOOK, SOL_CATEGORY_ID, FABLE_CATEGORY_ID")

    logpath = a.dispatch_log or max(glob.glob(os.path.join(BASE, "logs", "dispatch-*.csv")), default=None)
    dispatch_rows = list(csv.DictReader(open(logpath))) if logpath else []

    with open(LEADS) as f:
        leads = json.load(f)["leads"]

    os.makedirs(os.path.join(BASE, "results"), exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(BASE, "results", f"acceptance-{stamp}.md")

    cols = ["id", "type", "A_сделка", "C_имя", "D_телефон", "E_ответ", "F_задача",
            "G_спам", "H_дозапрос", "I_дубли"]
    lines = [f"# Приёмка AI-Баттла №2 — {stamp}", "", f"Лог диспетчера: {logpath}", ""]
    summary = {}
    for agent in CATS:
        rows = check_agent(agent, leads, dispatch_rows)
        ok, fail, manual, nt = score(rows)
        summary[agent] = (ok, fail, manual, nt)
        lines += [f"## {agent.upper()}: OK {ok} · FAIL {fail} · MANUAL {manual}"
                  + (f" · не доставлено {nt}" if nt else ""), "",
                  "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        lines += ["| " + " | ".join(str(r.get(c, "—")) for c in cols) + " |" for r in rows]
        lines.append("")
    lines += ["## Счёт (матрица фиксированная — знаменатели совпадают)", ""]
    lines += [f"{a.upper()}: OK {v[0]}, FAIL {v[1]}, MANUAL {v[2]}" for a, v in summary.items()]
    lines += ["",
              "MANUAL-пункты и стадии сделок автомат не судит — оценка в кадре.",
              "Время и стоимость — из лога диспетчера и журналов агентов (/stats)."]
    with open(out, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(" | ".join(f"{a.upper()}: OK {v[0]} FAIL {v[1]} MANUAL {v[2]}" for a, v in summary.items()))
    print("Отчёт:", out)


if __name__ == "__main__":
    main()
