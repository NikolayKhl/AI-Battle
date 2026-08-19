#!/usr/bin/env python3
"""Диспетчер съёмки выпуска №3. Запускает Клод по слову автора в чат.

Слово автора в чате           →  что запускает Клод
--------------------------------------------------------------
проверка                      →  python3 диспетчер.py проверка
покажи сделку D-25            →  python3 диспетчер.py сделка D-25
покажи сделку D-24 обезличенную → python3 диспетчер.py сделка D-24 --обезличенная
счётчик                       →  python3 диспетчер.py счётчик [--следить 20]
статистика по менеджерам      →  python3 диспетчер.py статистика
закрытые против проигранных   →  python3 диспетчер.py сравнение
приёмка                       →  python3 диспетчер.py приёмка
цена                          →  python3 диспетчер.py цена [--сборка SOL=вход,выход,минут --сборка FABLE=...]
цифра для хука                →  python3 диспетчер.py цифра-для-хука --контролёр SOL

«запускай проверку» диспетчером не обслуживается: это запуск кода
участников, его делает Клод руками в их окнах.

С 16.08 участники пишут вердикт по каждой сделке ПРЯМО В КАРТОЧКУ amoCRM
(примечание «АУДИТ SOL» / «АУДИТ FABLE», формат — в participants/ТЗ.md).
Диспетчер читает эти примечания через stand/аудит.py: «покажи сделку»
показывает их рядом с карточкой, «счётчик» считает заполненные карточки,
«приёмка» и «цифра для хука» берут вердикты оттуда же. Файлы-отчёты
(--отчёт sol.json) остались как запасной путь, если CRM ляжет.

Все экраны печатаются в чат моноширинным блоком — их видно на записи.
Секретов не печатает никогда: токен и поддомен CRM в вывод не попадают.
"""

import argparse
import collections
import json
import os
import pathlib
import re
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent
EPISODE = BASE.parent
DATA = json.loads((BASE / "deals.json").read_text())
CAT = DATA["violations_catalog"]
DEALS = {d["id"]: d for d in DATA["deals"]}
ORDER = [d["id"] for d in DATA["deals"]]

# Н12: метка «Клиент №17» жёстко закреплена за D-24 — под неё написана
# карточка клипа 1, менять нельзя.
LOCKED = {"D-24": 17}

# Пороговые кейсы приёмки: расхождение по ним не считается ошибкой
# контролёра ни в одну сторону.
BORDERLINE = {
    "D-17": ["1.1"],   # первая реакция ровно 40 минут
    "D-28": ["1.1"],   # то же
    "D-27": ["8.2"],   # спорное резюме звонка
    "D-25": ["4.2"],   # «как посчитаю, так сразу» — обещание без срока
}

PHONE = re.compile(r"\+7[\s\d\-()]{9,}")
EMAIL = re.compile(r"[\w.\-]+@[\w\-]+\.\w+")

_AUDIT = None


def audit():
    """Модуль чтения вердиктов из карточек — грузится только когда нужен,
    чтобы экраны без CRM работали и без сети."""
    global _AUDIT
    if _AUDIT is None:
        sys.path.insert(0, str(BASE))
        import importlib.util
        spec = importlib.util.spec_from_file_location("audit", BASE / "аудит.py")
        _AUDIT = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_AUDIT)
    return _AUDIT


def crm_audits(only=None):
    """Вердикты из карточек; при недоступной CRM — None и строка причины."""
    try:
        return audit().fetch(only), None
    except SystemExit as e:
        return None, str(e)
    except Exception as e:  # сеть, таймаут
        return None, f"{type(e).__name__}: {e}"


def verdict_lines(deal_id, audits):
    """Блок «ВЕРДИКТЫ КОНТРОЛЁРОВ» для экрана сделки."""
    out = ["ВЕРДИКТЫ КОНТРОЛЁРОВ · из карточки amoCRM:"]
    a = audits.get(deal_id, {})
    for agent in audit().AGENTS:
        v = a.get(agent)
        if not v:
            out.append(f"  {agent}: карточка ещё не заполнена")
            continue
        head = f"  {agent} ({audit().fmt_time(v['updated'])}): "
        head += ("нарушений нет" if not v["clauses"]
                 else "пункты " + ", ".join(v["clauses"]))
        out.append(head)
        for line in v["text"].splitlines()[1:]:
            if line.strip():
                out.append("      " + line.strip()[:110])
    return out


def money(n):
    return f"{n:,}".replace(",", " ") + " ₽"


def clauses(d):
    return [CAT[k]["clause"] for k in d.get("verdict", [])]


def clause_text(c):
    for v in CAT.values():
        if v["clause"] == c:
            return v["what"]
    return "?"


def client_no(deal_id):
    """Номер клиента в обезличенном виде. D-24 всегда №17."""
    if deal_id in LOCKED:
        return LOCKED[deal_id]
    free = [n for n in range(1, len(ORDER) + 1) if n not in LOCKED.values()]
    return free[[i for i in ORDER if i not in LOCKED].index(deal_id)]


def manager_no(name):
    names = sorted({d["manager"] for d in DEALS.values()})
    return names.index(name) + 1


def mask(text):
    text = PHONE.sub("+7 ••• •••-••-••", text)
    return EMAIL.sub("•••@•••", text)


# --------------------------------------------------------------- экраны


def screen_deal(deal_id, anon=False, with_crm=True):
    d = DEALS[deal_id]
    out = []
    if anon:
        out.append("ЭКРАН КОНТРОЛЁРА — то, что уходит в модель")
        out.append("")
        out.append(f"Сделка {deal_id} · Клиент №{client_no(deal_id)} · "
                   f"{money(d['amount'])}")
        out.append(f"Менеджер №{manager_no(d['manager'])} · стадия «{d['stage']}»")
    else:
        out.append(f"КАРТОЧКА amoCRM · {deal_id}")
        out.append("")
        out.append(f"{d['client']} · {money(d['amount'])} · менеджер {d['manager']}")
        out.append(f"Стадия «{d['stage']}» · создана {d['created']}")
        if d.get("loss_reason"):
            out.append(f"Причина проигрыша: {d['loss_reason']}")
        else:
            out.append("Причина проигрыша: не указана"
                       if d["outcome"] == "lost" else
                       {"won": "Исход: закрыта успешно",
                        "open": "Исход: в работе"}[d["outcome"]])
    out.append("")
    out.append("Переписка:")
    for who, at, text in d["chat"]:
        label = "Клиент " if who == "client" else "Менеджер"
        line = mask(text) if anon else text
        out.append(f"  {at}  {label}: {line}")
    if d.get("calls"):
        out.append("")
        out.append("Звонки:")
        for c in d["calls"]:
            out.append(f"  {c['at']}  {c['dir']}, {c['sec']} сек")
    out.append("")
    if anon:
        out.append("Ни имени, ни телефона, ни почты. Таблица соответствия")
        out.append("остаётся на сервере автора и в модель не уходит.")
        return "\n".join(out)

    out.append("МОЯ РАЗМЕТКА (эталон):")
    if not d.get("verdict"):
        out.append("  нарушений нет")
    for c in clauses(d):
        out.append(f"  пункт {c} — {clause_text(c)}")
    if deal_id in BORDERLINE:
        out.append(f"  пороговый пункт: {', '.join(BORDERLINE[deal_id])} — "
                   f"расхождение по нему ошибкой не считается")
    out.append("")
    out.append("ПИСЬМО РУКОВОДИТЕЛЮ ОТДЕЛА ПРОДАЖ (эталон):")
    out.append(f"  {d['manager']} · сделка {deal_id} «{d['client']}» · "
               f"{money(d['amount'])}")
    for c in clauses(d):
        out.append(f"  нарушен пункт {c}: {clause_text(c)}")
    if with_crm:
        out.append("")
        audits, err = crm_audits(deal_id)
        if audits is None:
            out.append(f"ВЕРДИКТЫ КОНТРОЛЁРОВ: CRM не ответила ({err[:80]})")
        else:
            out.extend(verdict_lines(deal_id, audits))
    return "\n".join(out)


def load_usage():
    """расход.json из папок участников: {агент: dict|None}."""
    out = {}
    # участники могут работать в любой из двух папок — берём самый свежий файл
    roots = [BASE.parent / "агенты", EPISODE / "participants"]
    for agent in ("SOL", "FABLE"):
        cands = [r / agent.lower() / "расход.json" for r in roots]
        cands = [c for c in cands if c.exists()]
        cands.sort(key=lambda c: c.stat().st_mtime, reverse=True)
        try:
            out[agent] = json.loads(cands[0].read_text()) if cands else None
        except Exception:
            out[agent] = {"ошибка": "расход.json не читается"}
    return out


def parse_build(items):
    """--сборка SOL=вход,выход,минут → {агент: (вход, выход, минут)}."""
    out = {}
    for it in items or []:
        name, _, rest = it.partition("=")
        nums = []
        for x in rest.split(","):
            x = x.strip().replace(" ", "").replace("_", "")
            mult = 1.0
            if x[-1:] in ("k", "K", "к", "К"):
                mult, x = 1e3, x[:-1]
            elif x[-1:] in ("M", "m", "М", "м"):
                mult, x = 1e6, x[:-1]
            try:
                nums.append(float(x) * mult)
            except ValueError:
                nums.append(0.0)
        while len(nums) < 3:
            nums.append(0.0)
        out[name.strip().upper()] = tuple(nums[:3])
    return out


def rub(usd, rate):
    return f"{usd * rate:,.0f} ₽".replace(",", " ")


def screen_price(build):
    """Сколько стоила сборка и проверка — по прайсу API и по подписке."""
    prices = json.loads((BASE / "цены.json").read_text())
    rate = prices["usd_rub"]
    usage = load_usage()
    out = [f"ЦЕНА · прайс API за 1M токенов, курс ЦБ {rate:.2f} ₽/$ ({prices['дата']})", ""]
    out.append(f"{'':<26}{'SOL':>16}{'FABLE':>16}")
    P = {a: prices["модели"][a] for a in ("SOL", "FABLE")}
    out.append(f"{'модель':<26}{P['SOL']['модель']:>16}{P['FABLE']['модель']:>16}")
    out.append(f"{'прайс вход / выход, $':<26}"
               f"{str(P['SOL']['вход']) + ' / ' + str(P['SOL']['выход']):>16}"
               f"{str(P['FABLE']['вход']) + ' / ' + str(P['FABLE']['выход']):>16}")
    out.append("")

    def cost(agent, tin, tout):
        return tin / 1e6 * P[agent]["вход"] + tout / 1e6 * P[agent]["выход"]

    # сборка — из окон участников (/cost, /status), вводит автор
    out.append("СБОРКА КОНТРОЛЁРА (токены из окон участников):")
    if not build:
        out.append("  цифры сборки не переданы — напиши: цена: SOL вход,выход,минут FABLE вход,выход,минут")
    else:
        row = lambda label, f: out.append(f"  {label:<24}" + "".join(f"{f(a):>16}" for a in ("SOL", "FABLE")))
        row("минут", lambda a: f"{build.get(a, (0, 0, 0))[2]:.0f}" if a in build else "—")
        row("токенов вход / выход", lambda a: f"{build[a][0]/1000:.0f}k / {build[a][1]/1000:.0f}k" if a in build else "—")
        row("по прайсу API, $", lambda a: f"{cost(a, build[a][0], build[a][1]):.2f}" if a in build else "—")
        row("по прайсу API, ₽", lambda a: rub(cost(a, build[a][0], build[a][1]), rate) if a in build else "—")
        row("по подписке", lambda a: "0 ₽ сверх" if a in build else "—")
    out.append("")

    # прогон — из расход.json контролёров
    out.append("ПРОВЕРКА СОРОКА СДЕЛОК (расход.json контролёра):")
    row = lambda label, f: out.append(f"  {label:<24}" + "".join(f"{f(a):>16}" for a in ("SOL", "FABLE")))
    def g(a, k, default=None):
        u = usage.get(a)
        return u.get(k, default) if isinstance(u, dict) else default
    row("прогон", lambda a: str(g(a, "прогон", "нет файла"))[:14])
    row("модель контролёра", lambda a: str(g(a, "модель", "—"))[:16])
    row("вызовов / секунд", lambda a: f"{g(a,'вызовов','—')} / {g(a,'секунд','—')}")
    row("токенов вход / выход", lambda a: (f"{g(a,'вход_токенов',0)/1000:.0f}k / {g(a,'выход_токенов',0)/1000:.0f}k"
                                          if g(a, "вход_токенов") is not None else "—"))
    def run_cost(a):
        if g(a, "вход_токенов") is None:
            return None
        return cost(a, float(g(a, "вход_токенов", 0)), float(g(a, "выход_токенов", 0)))
    row("по прайсу API, $", lambda a: f"{run_cost(a):.2f}" if run_cost(a) is not None else "—")
    row("по прайсу API, ₽", lambda a: rub(run_cost(a), rate) if run_cost(a) is not None else "—")
    row("₽ на одну сделку", lambda a: rub(run_cost(a) / max(1, len(ORDER)), rate) if run_cost(a) is not None else "—")
    n = prices.get("прогонов_в_месяц", 4)
    row(f"₽ в месяц ({n} прогона)", lambda a: rub(run_cost(a) * n, rate) if run_cost(a) is not None else "—")
    row("как ходил в модель", lambda a: str(g(a, "как", "—")))
    row("по подписке", lambda a: ("0 ₽ сверх абонентской" if g(a, "как") == "подписка" else
                                  ("платно, по прайсу" if g(a, "как") == "api" else "—")))
    out.append("")
    out.append("Источники прайса: " + "; ".join(f"{a}: {P[a]['источник']}" for a in ("SOL", "FABLE")))
    out.append(f"Курс: {prices['usd_rub_источник']}")
    out.append("")
    out.append("Для плашки монтажёру: строки «по прайсу API, ₽» и «₽ на одну сделку»; вслух — минуты сборки,")
    out.append("секунды прогона, рубли за сорок сделок, «по подписке — ноль сверх абонентской».")
    return "\n".join(out)


def screen_counter():
    """Счётчик прогона: сколько карточек из сорока заполнил каждый."""
    audits, err = crm_audits()
    if audits is None:
        return f"СЧЁТЧИК: CRM не ответила ({err[:80]})"
    A = audit()
    out = [f"СЧЁТЧИК ПРОГОНА · {A.summary(audits, len(ORDER))}", ""]
    row = []
    for i in ORDER:
        marks = "".join(agent[0] if agent in audits.get(i, {}) else "·"
                        for agent in A.AGENTS)
        row.append(f"{i[2:]}:{marks}")
    for k in range(0, len(row), 10):
        out.append("  " + "  ".join(row[k:k + 10]))
    out.append("")
    out.append("  S = SOL заполнил карточку, F = FABLE, · = пусто")
    return "\n".join(out)


def screen_managers():
    mg = collections.defaultdict(lambda: dict(n=0, v=0, won=0, lost=0, open=0,
                                              cnt=collections.Counter()))
    for d in DEALS.values():
        m = mg[d["manager"]]
        m["n"] += 1
        m["v"] += len(d.get("verdict", []))
        m[d["outcome"]] += 1
        for c in clauses(d):
            m["cnt"][c] += 1
    rows = sorted(mg.items(), key=lambda kv: -kv[1]["v"])
    out = ["ПЯТЕРО МЕНЕДЖЕРОВ · сортировка по числу нарушений, сверху вниз", ""]
    out.append(f"{'Менеджер':<18}{'Сделок':>7}{'Наруш.':>8}{'На сделку':>11}"
               f"{'Чаще всего':>22}")
    for name, m in rows:
        top = ", ".join(f"{c} ({n})" for c, n in m["cnt"].most_common(2)) or "—"
        out.append(f"{name:<18}{m['n']:>7}{m['v']:>8}{m['v']/m['n']:>11.1f}"
                   f"{top:>22}")
    out.append("")
    out.append("Имена и переписки синтетические. Стандарт и проверка настоящие.")
    return "\n".join(out)


def screen_compare():
    by = collections.defaultdict(list)
    for d in DEALS.values():
        by[d["outcome"]].append(d)
    def v(g, skip=None):
        return sum(1 for d in g for c in clauses(d) if c != skip)
    won, lost = by["won"], by["lost"]
    out = ["ЗАКРЫТЫЕ ПРОТИВ ПРОИГРАННЫХ", ""]
    out.append(f"{'':<28}{'закрыты':>10}{'проиграны':>12}")
    out.append(f"{'Сделок':<28}{len(won):>10}{len(lost):>12}")
    out.append(f"{'Нарушений всего':<28}{v(won):>10}{v(lost):>12}")
    out.append(f"{'Нарушений на сделку':<28}{v(won)/len(won):>10.2f}"
               f"{v(lost)/len(lost):>12.2f}")
    out.append(f"{'Сделок без нарушений':<28}"
               f"{sum(1 for d in won if not d.get('verdict')):>10}"
               f"{sum(1 for d in lost if not d.get('verdict')):>12}")
    out.append(f"{'Без пункта 8.4, на сделку':<28}{v(won,'8.4')/len(won):>10.2f}"
               f"{v(lost,'8.4')/len(lost):>12.2f}")
    out.append("")
    fl = collections.Counter(c for d in lost for c in clauses(d))
    out.append("Чаще всего нарушено в проигранных:")
    for c, n in sorted(fl.items(), key=lambda kv: (-kv[1], kv[0]))[:8]:
        out.append(f"  {c} — {clause_text(c)}: {n}")
    return "\n".join(out)


def confirmed_from_crm(agent):
    """Проигранные сделки с нарушениями, где контролёр в карточке назвал
    хотя бы один пункт из эталона."""
    audits, err = crm_audits()
    if audits is None:
        sys.exit(f"CRM не ответила ({err[:80]}) — передай список руками: "
                 "--подтверждено D-24,D-25,...")
    ok = []
    for d in DEALS.values():
        if d["outcome"] != "lost" or not d.get("verdict"):
            continue
        got = set(audits.get(d["id"], {}).get(agent, {}).get("clauses", []))
        if got & set(clauses(d)):
            ok.append(d["id"])
    return ok


def screen_hook_number(confirmed, agent=None):
    lost_with = {d["id"]: d for d in DEALS.values()
                 if d["outcome"] == "lost" and d.get("verdict")}
    total = sum(d["amount"] for d in lost_with.values())
    ok = [i for i in confirmed if i in lost_with]
    got = sum(lost_with[i]["amount"] for i in ok)
    out = ["ЦИФРА ДЛЯ ХУКА" + (f" · по карточкам {agent}" if agent else ""), ""]
    out.append(f"Проигранных сделок с нарушениями в воронке: {len(lost_with)} "
               f"на {money(total)}")
    out.append(f"Контролёр подтвердил номером пункта: {len(ok)} на {money(got)}")
    out.append("")
    if len(ok) == len(lost_with):
        branch = "А"
    elif got >= 1_000_000:
        branch = "Б"
    else:
        branch = "В"
    out.append(f"ВЕТКА: {branch}")
    out.append("")
    out.append("Строка в хук — читать дословно:")
    if branch == "А":
        out.append("  «Всего эта пятёрка потеряла миллион девятьсот шестьдесят")
        out.append("   тысяч. Он прочитал все сорок переписок и нашёл каждую")
        out.append("   из этих потерь — с номером нарушенного правила.»")
    elif branch == "Б":
        out.append("  «Всего эта пятёрка потеряла миллион девятьсот шестьдесят")
        out.append(f"   тысяч. Он прочитал все сорок переписок и нашёл из них")
        out.append(f"   {ru_millions(got)} — с номером нарушенного правила.»")
    else:
        out.append("  «Всего эта пятёрка потеряла миллион девятьсот шестьдесят")
        out.append("   тысяч. Я дал искусственному интеллекту доступ к этой CRM —")
        out.append("   и он не нашёл и половины.»")
    return "\n".join(out)


def ru_millions(n):
    """Округление вниз до десятых миллиона, словами."""
    whole, frac = divmod(int(n // 100_000), 10)
    mil = {0: "", 1: "миллион", 2: "два миллиона", 3: "три миллиона",
           4: "четыре миллиона", 5: "пять миллионов"}[whole]
    hund = {0: "", 1: "сто", 2: "двести", 3: "триста", 4: "четыреста",
            5: "пятьсот", 6: "шестьсот", 7: "семьсот", 8: "восемьсот",
            9: "девятьсот"}[frac]
    parts = [p for p in (mil, f"{hund} тысяч" if hund else "") if p]
    return " ".join(parts)


def hidden_in_calls():
    """{deal_id: [пункты]}, которые видны только в расшифровках звонков.
    Ключи в calls.py — коды каталога, переводим в номера пунктов."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("calls", BASE / "calls.py")
        calls = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(calls)
    except Exception:
        return {}
    out = {}
    for did, keys in calls.hidden_violations().items():
        cs = sorted({CAT[k]["clause"] for k in keys if k in CAT}
                    | {k for k in keys if k not in CAT and re.match(r"\d+\.\d", k)})
        if cs:
            out[did] = cs
    return out


def screen_review(reports, source="из карточек amoCRM"):
    """Сверка вердиктов участников с эталоном."""
    out = [f"ПРИЁМКА · сорок сделок, вердикты {source}, сверка с моей разметкой", ""]
    out.append(f"{'Сделка':<9}{'эталон':>8}" +
               "".join(f"{name:>12}" for name, _ in reports))
    plus = {name: 0 for name, _ in reports}
    extra = {name: [] for name, _ in reports}
    missed = {name: [] for name, _ in reports}
    for i in ORDER:
        d = DEALS[i]
        ref = set(clauses(d))
        cells = []
        for name, rep in reports:
            if i not in rep:
                cells.append("пусто")
                missed[name].append((i, sorted(ref)))
                continue
            got = set(rep.get(i, []))
            soft = set(BORDERLINE.get(i, []))
            miss = ref - got - soft
            over = got - ref - soft
            if not miss and not over:
                plus[name] += 1
                cells.append("+")
            else:
                cells.append(f"−{len(miss)}/+{len(over)}")
                if miss:
                    missed[name].append((i, sorted(miss)))
                if over:
                    extra[name].append((i, sorted(over)))
        out.append(f"{i:<9}{len(ref):>8}" + "".join(f"{c:>12}" for c in cells))
    out.append("")
    clean = [i for i in ORDER if not DEALS[i].get("verdict")]
    for name, rep in reports:
        out.append(f"{name}: совпало {plus[name]} из {len(ORDER)}"
                   f" · карточек заполнено {sum(1 for i in ORDER if i in rep)}")
    out.append("")
    hidden = hidden_in_calls()
    if hidden:
        n_hidden = sum(len(v) for v in hidden.values())
        out.append(f"СПРЯТАНО В ЗВОНКАХ · {n_hidden} нарушений в {len(hidden)} сделках "
                   "видны только в расшифровке — сколько нашли:")
        for name, rep in reports:
            found = sum(1 for i, cs in hidden.items()
                        for c in cs if c in set(rep.get(i, [])))
            out.append(f"  {name}: {found} из {n_hidden}")
        out.append("")
    out.append(f"ЛОВУШКА · {len(clean)} чистых сделок — сколько выдумано:")
    for name, _ in reports:
        n = sum(1 for i, _cs in extra[name] if i in clean)
        out.append(f"  {name}: {n} " + ("— чисто" if n == 0 else
                   "(" + ", ".join(i for i, _cs in extra[name] if i in clean) + ")"))
    out.append("")
    out.append("СВЕРХ ЭТАЛОНА (не ошибка автоматически — смотрим глазами):")
    for name, _ in reports:
        for i, cs in extra[name]:
            out.append(f"  {name} · {i} · пункты {', '.join(cs)}")
        if not extra[name]:
            out.append(f"  {name} · пусто")
    return "\n".join(out)


def screen_check():
    ok, bad = [], []

    def test(cond, good, wrong):
        (ok if cond else bad).append(good if cond else wrong)

    test(len(DEALS) == 40, "сорок сделок в deals.json", "сделок не сорок")
    d39 = DEALS["D-39"]["chat"][0][1]
    test(d39.endswith("20:40"),
         "D-39: первое сообщение клиента 20:40, после рабочего дня",
         f"D-39: метка {d39}, ожидалось 20:40")
    folder = EPISODE / "participants"
    files = sorted(p.name for p in folder.glob("*.md"))
    test(files == ["СТАНДАРТ.md", "ТЗ.md"],
         f"съёмочная папка: {', '.join(files)} и больше ничего",
         f"в съёмочной папке лишние файлы: {', '.join(files)}")
    same = (folder / "СТАНДАРТ.md").read_text() == (BASE / "СТАНДАРТ.md").read_text()
    test(same, "копия стандарта совпадает с эталоном",
         "копия стандарта разошлась с stand/СТАНДАРТ.md")
    test(not list(folder.glob("env*")), "секретов в съёмочной папке нет",
         "в съёмочной папке лежит файл окружения — убрать")
    test("Клиент №17" in screen_deal("D-24", anon=True),
         "экран обезличивания: D-24 = Клиент №17",
         "экран обезличивания не отдаёт Клиента №17")
    test((EPISODE / "prep" / "ЧИСЛА.md").exists(),
         "prep/ЧИСЛА.md на месте", "prep/ЧИСЛА.md не сгенерирован")
    ws = EPISODE / "participants"
    ws.mkdir(exist_ok=True)
    for name in ("ТЗ.md", "СТАНДАРТ.md"):
        src = (folder / name).read_text()
        if not (ws / name).exists() or (ws / name).read_text() != src:
            (ws / name).write_text(src)
    for sub in ("sol", "fable"):
        (ws / sub).mkdir(exist_ok=True)
    access = ws / "доступ.txt"
    envs = {}
    for line in (BASE / "env.sh").read_text().splitlines():
        m = re.match(r"export (AMO_\w+)='?([^'\n]+)'?", line.strip())
        if m:
            envs[m.group(1)] = m.group(2)
    access.write_text("# доступ к amoCRM для контролёра. На экран не печатать.\n"
                      f"AMO_SUBDOMAIN={envs.get('AMO_SUBDOMAIN','')}\n"
                      f"AMO_PIPELINE_ID={envs.get('AMO_PIPELINE_ID','')}\n"
                      f"AMO_TOKEN={envs.get('AMO_TOKEN','')}\n")
    os.chmod(access, 0o600)
    extra = sorted(p.name for p in ws.iterdir()
                   if p.name not in ("ТЗ.md", "СТАНДАРТ.md", "доступ.txt", "sol", "fable"))
    test(not extra, "рабочая папка участников (participants/): ТЗ, стандарт, доступ.txt, sol/, fable/",
         f"в рабочей папке участников лишнее: {', '.join(extra)}")
    leftovers = [str(q.relative_to(ws)) for sub in ("sol", "fable") for q in (ws / sub).iterdir()]
    test(not leftovers, "папки sol/ и fable/ пустые — участники начнут с нуля",
         f"в sol/ или fable/ остатки прошлой попытки: {', '.join(leftovers[:5])} — убрать в prep/")
    test(all(v for v in envs.values()) and len(envs) == 3,
         "доступ.txt для участников записан (поддомен, воронка, ключ)",
         "в stand/env.sh не хватает AMO_* — доступ.txt неполный")
    tz = (EPISODE / "participants" / "ТЗ.md").read_text()
    test("АУДИТ SOL" in tz and "АУДИТ FABLE" in tz,
         "ТЗ участникам требует писать вердикт в карточку (АУДИТ SOL/FABLE)",
         "в ТЗ участникам нет формата примечания АУДИТ SOL/FABLE")
    test("поехали" in tz and "Все сорок" in tz,
         "ТЗ участникам: работа по командам автора (план → поехали → D-09 → пять → все сорок)",
         "в ТЗ участникам нет порядка работы по командам")
    audits, err = crm_audits()
    if audits is None:
        bad.append(f"amoCRM не ответила: {err[:90]}")
    else:
        test(len(audits) == 40,
             f"amoCRM: воронка стенда, сделок {len(audits)}",
             f"amoCRM: в воронке стенда сделок {len(audits)}, ожидалось 40")
        filled = {ag: sum(1 for a in audits.values() if ag in a)
                  for ag in audit().AGENTS}
        test(not any(filled.values()),
             "карточки чистые — вердиктов контролёров в них ещё нет",
             "в карточках уже есть вердикты (SOL "
             f"{filled['SOL']}, FABLE {filled['FABLE']}) — остатки репетиции; "
             "API их не удаляет: либо участники перепишут свои примечания "
             "(ТЗ это требует), либо удалить руками в карточке")
    out = ["ПРОВЕРКА ПЕРЕД ЗАПИСЬЮ", ""]
    for line in ok:
        out.append(f"  готово   {line}")
    for line in bad:
        out.append(f"  ЧИНЮ     {line}")
    out.append("")
    out.append("Отдельно, руками у Клода (в этот список не влезает):")
    out.append("  — карточка D-39 в CRM показывает 20:40")
    out.append("  — запись примечаний через API проверена (16.08: создать/PATCH ок)")
    out.append("  — VS Code открыт на рабочей папке участников, чаты Codex и Claude Code новые")
    out.append("  — плашка настроек моделей и плашка «имена синтетические» у монтажёра")
    out.append("")
    out.append("ГОТОВО — можно включать камеру" if not bad
               else "НЕ ГОТОВО — чиню и напишу, когда можно")
    return "\n".join(out)


# --------------------------------------------------------------- CLI


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("проверка")
    s = sub.add_parser("сделка")
    s.add_argument("id")
    s.add_argument("--обезличенная", action="store_true", dest="anon")
    s.add_argument("--без-crm", action="store_true", dest="no_crm",
                   help="не ходить в amoCRM за вердиктами")
    c = sub.add_parser("счётчик")
    c.add_argument("--следить", dest="watch", type=int, default=0,
                   help="перечитывать каждые N секунд, пока обе не заполнят 40")
    sub.add_parser("статистика")
    sub.add_parser("сравнение")
    h = sub.add_parser("цифра-для-хука")
    h.add_argument("--подтверждено", dest="confirmed", default="")
    h.add_argument("--контролёр", dest="agent", choices=["SOL", "FABLE"],
                   help="посчитать подтверждённые сделки по карточкам этого контролёра")
    pr = sub.add_parser("цена")
    pr.add_argument("--сборка", dest="build", action="append", default=[],
                    help="SOL=вход,выход,минут (токены из /status и /cost участников)")
    r = sub.add_parser("приёмка")
    r.add_argument("--отчёт", dest="reports", action="append", default=[],
                   help="запасной путь: файлы sol.json/fable.json вместо карточек")
    r.add_argument("--сохранить", dest="save", action="store_true",
                   help="выгрузить вердикты из карточек в results/audit-crm.json")
    a = p.parse_args()

    if a.cmd == "проверка":
        print(screen_check())
    elif a.cmd == "сделка":
        if a.id not in DEALS:
            sys.exit(f"сделки {a.id} нет")
        print(screen_deal(a.id, anon=a.anon, with_crm=not a.no_crm))
    elif a.cmd == "счётчик":
        last = None
        while True:
            scr = screen_counter()
            if scr != last:
                print(scr, flush=True)
                last = scr
            if not a.watch or f"SOL {len(ORDER)}/{len(ORDER)} · FABLE {len(ORDER)}/{len(ORDER)}" in scr:
                break
            time.sleep(a.watch)
    elif a.cmd == "статистика":
        print(screen_managers())
    elif a.cmd == "сравнение":
        print(screen_compare())
    elif a.cmd == "цена":
        print(screen_price(parse_build(a.build)))
    elif a.cmd == "цифра-для-хука":
        if a.agent:
            print(screen_hook_number(confirmed_from_crm(a.agent), a.agent))
        else:
            print(screen_hook_number([x.strip() for x in a.confirmed.split(",") if x.strip()]))
    elif a.cmd == "приёмка":
        if a.reports:
            reports = []
            for path in a.reports:
                pp = pathlib.Path(path)
                reports.append((pp.stem.upper(), json.loads(pp.read_text())))
            print(screen_review(reports, source="из файлов-отчётов"))
        else:
            audits, err = crm_audits()
            if audits is None:
                sys.exit(f"CRM не ответила ({err[:80]}) — запасной путь: "
                         "--отчёт sol.json --отчёт fable.json")
            reports = audit().as_reports(audits)
            if not reports:
                sys.exit("в карточках нет ни одного вердикта — участники ещё не писали")
            print(screen_review(reports))
            if a.save:
                print("\nвыгружено →", audit().dump(audits))


if __name__ == "__main__":
    main()
