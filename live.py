#!/usr/bin/env python3
"""
Живые табло станций с tm.md.uz.

    python live.py            собрать data/live.json
    python live.py --show     показать, что отдают станции

Что это даёт сверх планового расписания:
  * конкретные даты вместо шаблона дней курсирования;
  * номер платформы (пока только по вокзалам Ташкента);
  * наличие мест — не сохраняем, устаревает за минуты.

Только отправления. Прибытий эти табло не показывают.

Имена файлов на сервере произвольные, схемы нет: Северный вокзал —
proxyS.php, Южный — proxyYu.php, Самарканд — proxy.php. Остальные
станции добавляются вручную: открыть tm.md.uz/<станция>/, посмотреть
в DevTools адрес запроса и вписать сюда.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
BASE = "https://tm.md.uz"

# Расхождение больше этого считаем не задержкой, а несовпадением рейсов.
# Обратная сторона: опоздания дольше двух часов останутся незамеченными.
MAX_DELAY = 120

ENDPOINTS = {
    "tashkent-central": "proxyS.php",
    "tashkent-south":   "proxyYu.php",
    "samarkand":        "proxy.php",
    "urgench":          "proxyU.php",
    "bukhara":          "proxyB.php",
    "qarshi":           "proxyKarshi.php",
    "andijan":          "proxyAndijon.php",
    # Дальше — по мере нахождения. Имена непоследовательные: ранние
    # станции получили короткие коды (S, Yu, U, B), поздние названы
    # полностью (Karshi, Andijon). Вычислить нельзя, только подсмотреть:
    # открыть tm.md.uz/<станция>/, DevTools -> Network -> адрес запроса.
    # Остались: Наманган, Коканд, Маргилан, Джизак, Навои, Гулистан,
    # Хива, Нукус.
}

TAG = re.compile(r"<[^>]+>")
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)


def clean(html):
    text = TAG.sub(" ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


# Запасной разбор: часть станций отдаёт другой шаблон (у Карши,
# например, русские заголовки и пять колонок вместо шести). Если строк
# таблицы не нашлось, идём по тексту.
LINE = re.compile(
    r"(\d{3}[А-ЯЁA-Z]?)\s+"          # номер
    r"(.+?)\s+"                      # маршрут
    r"(\d{2}\.\d{2}\.\d{4})\s+"     # дата
    r"(\d{1,2}:\d{2})"               # время
)


def parse_text(html):
    out = []
    for line in clean(html).split("|"):
        m = LINE.search(line)
        if not m:
            continue
        number, route, date, hhmm = m.groups()
        tail = line[m.end():].strip()
        out.append(dict(
            number=number.upper(),
            route=route.strip(),
            date=date,
            time_local=hhmm.zfill(5),
            platform=tail if re.fullmatch(r"\d{1,2}", tail) else None,
            sold_out=bool(re.search(r"МЕСТ НЕТ|JOY YO'Q", line, re.I)),
        ))
    return out


def parse(html):
    """Таблица -> список рейсов. Колонки: рейс, маршрут, дата, время, места, платформа."""
    out = []
    for row in ROW.findall(html):
        cells = [clean(c) for c in CELL.findall(row)]
        if len(cells) < 4:
            continue

        number = cells[0]
        if not re.match(r"^\d{3}", number):        # шапка и мусор
            continue

        date = next((c for c in cells if re.match(r"^\d{2}\.\d{2}\.\d{4}$", c)), None)
        hhmm = next((c for c in cells if re.match(r"^\d{1,2}:\d{2}$", c)), None)
        if not (date and hhmm):
            continue

        # Платформа есть не везде: у Карши колонки вообще нет, и в
        # последней ячейке оказались бы места. Берём только число.
        platform = cells[-1] if re.fullmatch(r"\d{1,2}", cells[-1] or "") else None
        # Места не сохраняем: цифры протухают за минуты. Держим только флаг.
        seats = " ".join(cells)
        sold_out = bool(re.search(r"МЕСТ НЕТ|JOY YO'Q", seats, re.I))

        out.append(dict(
            number=number.upper(),
            route=cells[1],
            date=date,
            time_local=hhmm.zfill(5),
            platform=platform,
            sold_out=sold_out,
        ))

    return out or parse_text(html)


def fetch(path):
    r = httpx.get(f"{BASE}/{path}", timeout=20,
                  headers={"User-Agent": "uz-rail-board/1.0 (station board reader)"})
    r.raise_for_status()
    return r.text


def deviations(result):
    """Сравнивает время на табло с плановым из schedule.json.

    Отдельной графы «фактическое время» на табло нет — есть одна колонка.
    Если она когда-нибудь разойдётся с расписанием, разница и будет
    отклонением: плюс — опоздание, минус — идёт раньше.

    ВАЖНО: постоянное расхождение у одного и того же рейса изо дня в день
    означает не опоздание, а расхождение в расписании. Настоящая задержка
    плавает. Отличить можно только наблюдением за несколько дней.
    """
    f = ROOT / "data" / "schedule.json"
    if not f.exists():
        return 0

    # Один и тот же номер бывает у встречных рейсов: 058З идёт на Шават,
    # 058Ь обратно в Ташкент. Суффиксы у Яндекса латинские (058Z), на
    # табло кириллические (058З), однозначного соответствия нет. Поэтому
    # держим ВСЕ варианты и ниже выбираем ближайший по времени.
    planned = {}
    for t in json.loads(f.read_text(encoding="utf-8"))["trains"]:
        if t["direction"] != "departure":
            continue
        digits = (re.match(r"^\d+", t["number"]) or [""])[0]
        planned.setdefault((t["station"], digits), []).append(t["time_local"])

    def minutes(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    def offset(live_time, plan_time):
        d = minutes(live_time) - minutes(plan_time)
        if d > 720:
            d -= 1440
        elif d < -720:
            d += 1440
        return d

    found = 0
    for station, rows in result.items():
        for r in rows:
            digits = (re.match(r"^\d+", r["number"]) or [""])[0]
            candidates = planned.get((station, digits), [])
            r["planned"] = None
            r["deviation"] = None
            if not candidates:
                continue

            # Из встречных рейсов берём ближайший по времени: они
            # расходятся на часы, а задержка — на минуты.
            best = min(candidates, key=lambda p: abs(offset(r["time_local"], p)))
            diff = offset(r["time_local"], best)

            # Расхождение больше окна — это не задержка, а другой рейс
            # или другая редакция расписания. Молчим, а не выдумываем.
            if abs(diff) > MAX_DELAY:
                continue

            r["planned"] = best
            if diff:
                r["deviation"] = diff
                found += 1
    return found


def weekday_check(result):
    """Сверяет день недели с табло с нашими днями курсирования.

    Ночные рейсы легко уезжают на сутки: перевозчик считает поезд
    «вторничным», хотя отправляется он в 00:10 уже в среду. Источники
    трактуют это по-разному, и ошибка тихая — время совпадает, а день нет.
    """
    f = ROOT / "data" / "schedule.json"
    if not f.exists():
        return []

    days_of = {}
    for t in json.loads(f.read_text(encoding="utf-8"))["trains"]:
        if t["direction"] != "departure":
            continue
        digits = (re.match(r"^\d+", t["number"]) or [""])[0]
        days_of.setdefault((t["station"], digits), set()).add(t["days"])

    bad = []
    for station, rows in result.items():
        for r in rows:
            digits = (re.match(r"^\d+", r["number"]) or [""])[0]
            patterns = days_of.get((station, digits))
            if not patterns:
                continue
            try:
                d = datetime.strptime(r["date"], "%d.%m.%Y")
            except ValueError:
                continue
            iso = str(d.isoweekday())

            # Достаточно, чтобы день подходил хотя бы под один вариант:
            # у встречных рейсов с теми же цифрами шаблоны разные.
            ok = any(iso in pat if pat not in ("even", "odd")
                     else (d.day % 2 == 0) == (pat == "even")
                     for pat in patterns)
            if not ok:
                bad.append((station, r["number"], r["date"], r["time_local"],
                            sorted(patterns)))
    return bad


HISTORY = ROOT / "data" / "history.json"


def remember(result):
    """Дописывает отклонения в историю, по одной записи на прогон.

    Нужна, чтобы отличить опоздание от расхождения в расписании:
    постоянная величина изо дня в день — расхождение, плавающая —
    настоящая задержка. На глаз это не различить, поэтому копим.
    """
    log = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    stamp = time.strftime("%Y-%m-%d %H:%M")

    for station, rows in result.items():
        for r in rows:
            if r.get("deviation") is None:
                continue
            log.append(dict(at=stamp, station=station, number=r["number"],
                            date=r["date"], planned=r["planned"],
                            actual=r["time_local"], deviation=r["deviation"]))

    # Держим последние 2000 записей — этого хватает на месяцы.
    log = log[-2000:]
    HISTORY.parent.mkdir(exist_ok=True)
    HISTORY.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(log)


def show_history():
    """Разброс отклонений по каждому рейсу за всё время наблюдения."""
    if not HISTORY.exists():
        print("Истории пока нет — запусти live.py хотя бы раз.")
        return

    log = json.loads(HISTORY.read_text(encoding="utf-8"))
    if not log:
        print("История пуста: отклонений ещё не встречалось.")
        return

    by_train = {}
    for rec in log:
        key = (rec["station"], rec["number"])
        by_train.setdefault(key, []).append(rec["deviation"])

    print(f"Наблюдений: {len(log)}, рейсов с отклонениями: {len(by_train)}\n")
    print(f"  {'станция':<18} {'рейс':<7} {'раз':<5} {'разброс':<16} вывод")

    for (station, number), devs in sorted(by_train.items(), key=lambda x: -len(x[1])):
        lo, hi = min(devs), max(devs)
        if len(devs) < 3:
            verdict = "мало данных"
        elif lo == hi:
            verdict = "расписание расходится, не задержка"
        else:
            verdict = "плавает — похоже на реальную задержку"
        span = f"{lo:+d}..{hi:+d}" if lo != hi else f"{lo:+d} всегда"
        print(f"  {station:<18} {number:<7} {len(devs):<5} {span:<16} {verdict}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--history", action="store_true",
                    help="разброс отклонений за всё время наблюдения")
    args = ap.parse_args()

    if args.history:
        show_history()
        return

    result, failed = {}, []
    for station, path in ENDPOINTS.items():
        try:
            rows = parse(fetch(path))
        except Exception as e:
            failed.append(f"{station}: {e}")
            continue
        result[station] = rows
        print(f"  {station:<18} {len(rows)} отправлений"
              f"{', платформы есть' if any(r['platform'] for r in rows) else ''}")
        if args.show:
            for r in rows[:8]:
                pf = f"путь {r['platform']}" if r["platform"] else ""
                print(f"      {r['number']:<6} {r['date']} {r['time_local']}  "
                      f"{r['route'][:34]:<34} {pf}")
        time.sleep(0.3)

    for f in failed:
        print(f"  ОШИБКА {f}", file=sys.stderr)

    if not result:
        print("Ничего не собрано.", file=sys.stderr)
        sys.exit(1)

    n = deviations(result)
    if n:
        print(f"\nРасхождений с расписанием: {n}")
        for station, rows in result.items():
            for r in rows:
                if r.get("deviation"):
                    sign = "+" if r["deviation"] > 0 else ""
                    print(f"  {station:<18} {r['number']:<6} план {r['planned']} "
                          f"→ табло {r['time_local']}  ({sign}{r['deviation']} мин)")
        print("Постоянное расхождение = разное расписание, а не опоздание.")
    else:
        print("\nВремя на табло совпадает с расписанием везде.")

    wrong = weekday_check(result)
    if wrong:
        print(f"\nДень недели не совпадает с расписанием: {len(wrong)}")
        for station, number, date, hhmm, pats in wrong:
            print(f"  {station:<18} {number:<6} табло {date} {hhmm}, "
                  f"у нас дни {','.join(pats)}")
        print("Частая причина — рейсы сразу после полуночи: источники")
        print("расходятся, считать ли их вчерашними или сегодняшними.")

    total = remember(result)
    if n:
        print(f"В истории записей: {total}. Разброс: python3 live.py --history")

    out = ROOT / "data" / "live.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
