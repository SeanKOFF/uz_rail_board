#!/usr/bin/env python3
"""
Собирает расписание всей страны через маршруты рейсов.

    export YANDEX_RASP_KEY=...
    python build_yandex.py --anchor 2900001        # Ташкент-Центральный
    python build_yandex.py --anchor 2900001 --apply

Как это работает:

1. По станции-якорю запрашивается расписание на 7 дней вперёд, в обе
   стороны. Отсюда — список рейсов и дни курсирования.
2. Для каждого рейса один раз берётся полный маршрут (/thread) со всеми
   остановками, временем прибытия и отправления.
3. Из остановок строятся табло всех станций сразу. Дни на каждой станции
   сдвигаются на столько суток, сколько поезд идёт до неё от якоря.

В отличие от ручной сборки здесь есть настоящие стоянки: прибытие и
отправление на промежуточной станции — разные времена.
"""

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from yandex import call, schedule, STATIONS, RaspError, PAUSE, DAYS_AHEAD
from from_table import genitive

ROOT = Path(__file__).parent

# Станции, для которых строим табло: express-код -> наш идентификатор.
# Дополняется автоматически по ходу разбора маршрутов.
BOARDS = {
    # Ташкент
    "2900001": "tashkent-central",
    "2900002": "tashkent-south",
    "2900739": "tukimachi",
    # Главный ход
    "2900700": "samarkand",
    "2900800": "bukhara",
    "2900930": "navoi",
    "2900720": "jizzakh",
    "2900850": "gulistan",
    "2900750": "qarshi",
    "2900260": "kattakurgan",
    "2900824": "kyzyltepa",
    "2900775": "dashtobod",
    # Ферганская долина
    "2900680": "andijan",
    "2900940": "namangan",
    "2900880": "kokand",
    "2900920": "margilan",
    "2900693": "pap",
    "2900688": "chartak",
    "2900692": "chust",
    "2900679": "angren",
    "2900701": "kuva",
    "2900709": "altyaryk",
    # Хорезм и Каракалпакстан
    "2900790": "urgench",
    "2900172": "khiva",
    "2900844": "shavat",
    "2900845": "khazarasp",
    "2900970": "nukus",
    "2900885": "kungrad",
    # Юг
    "2900780": "denau",
    "2900864": "kumkurgan",
    "2900868": "sariosiyo",
    "2900796": "kitab",
}

# Города, для которых показываем табло. Список явный, а не по трафику:
# отбор по числу рейсов вымывал бы Нукус и Хиву и притаскивал придорожные
# станции вроде Кызылтепы. Чтобы добавить город — впиши сюда его id;
# коды и названия смотри через `--inventory`.
SHOW = {
    "tashkent-central", "tashkent-south",
    "samarkand", "bukhara", "navoi", "jizzakh", "gulistan", "qarshi",
    "andijan", "namangan", "kokand", "margilan",
    "urgench", "khiva", "nukus",
}


def hhmm(iso):
    return iso[11:16] if iso and len(iso) >= 16 else None


def shift_days(days, by):
    if by == 0 or days in ("even", "odd"):
        return days
    return "".join(sorted(str((int(d) - 1 + by) % 7 + 1) for d in days))


def anchor_threads(code):
    """Рейсы через станцию-якорь с выведенными днями курсирования."""
    days_of = defaultdict(set)
    meta = {}
    today = date.today()

    for offset in range(DAYS_AHEAD):
        day = today + timedelta(days=offset)
        for event in ("departure", "arrival"):
            data = schedule(code, event, day)
            for item in data.get("schedule", []):
                th = item.get("thread", {})
                uid = th.get("uid")
                if not uid:
                    continue
                days_of[uid].add(day.isoweekday())
                meta.setdefault(uid, dict(
                    uid=uid,
                    number=(th.get("number") or "").strip(),
                    title=th.get("title") or "",
                    brand=(th.get("transport_subtype") or {}).get("title"),
                ))
            time.sleep(PAUSE)

    for uid, rec in meta.items():
        rec["days"] = "".join(str(d) for d in sorted(days_of[uid]))
    return meta


def day_of(iso):
    """'2026-08-25T00:02:00' -> date(2026, 8, 25)."""
    return date.fromisoformat(iso[:10]) if iso and len(iso) >= 10 else None


def route_stops(uid):
    """Остановки маршрута: код станции, прибытие, отправление, сдвиг суток.

    Сдвиг берётся из самих дат в ответе, а не выводится из порядка времён —
    так корректно обрабатываются и переход через полночь, и многосуточные
    рейсы вроде ташкентского на Москву.
    """
    info = call("thread", uid=uid, show_systems="all")
    raw = info.get("stops", [])

    first_day = next((day_of(st.get("departure") or st.get("arrival"))
                      for st in raw if st.get("departure") or st.get("arrival")), None)

    stops = []
    for st in raw:
        s = st.get("station", {})
        arr_iso, dep_iso = st.get("arrival"), st.get("departure")
        if not (arr_iso or dep_iso):
            continue

        this_day = day_of(arr_iso or dep_iso)
        offset = (this_day - first_day).days if (this_day and first_day) else 0

        stops.append(dict(
            express=(s.get("codes") or {}).get("express"),
            title=s.get("title") or "",
            arrival=hhmm(arr_iso),
            departure=hhmm(dep_iso),
            offset=offset,
        ))
    return stops


def inventory(anchor):
    """Выписывает все станции, встреченные в маршрутах, с их кодами.

    Отсюда берутся коды для BOARDS — в том числе тех станций, до которых
    мы иначе не доберёмся (Ташкент-Южный, Нукус, Термез).
    """
    meta = anchor_threads(anchor)
    print(f"Рейсов через якорь: {len(meta)}\n")

    found = {}
    for i, uid in enumerate(meta, 1):
        try:
            for s in route_stops(uid):
                if not s["express"]:
                    continue
                rec = found.setdefault(s["express"], dict(title=s["title"], n=0))
                rec["n"] += 1
        except Exception:
            pass
        time.sleep(PAUSE)
        if i % 10 == 0:
            print(f"  {i}/{len(meta)}")

    print(f"\nНайдено станций: {len(found)}\n")
    print(f"  {'код':<10} {'рейсов':<8} название")
    for code, rec in sorted(found.items(), key=lambda x: -x[1]["n"]):
        mark = "  ←  уже в BOARDS" if code in BOARDS else ""
        print(f"  {code:<10} {rec['n']:<8} {rec['title']}{mark}")


def build(anchors):
    meta = {}
    for a in anchors:
        found = anchor_threads(a)
        print(f"Якорь {a}: рейсов {len(found)}")
        for uid, rec in found.items():
            # Дни курсирования берём от первого якоря, где рейс встретился.
            meta.setdefault(uid, rec)
    print(f"Уникальных рейсов: {len(meta)}")

    rows = []
    seen_stations = defaultdict(int)

    for i, (uid, rec) in enumerate(meta.items(), 1):
        try:
            stops = route_stops(uid)
        except Exception as e:
            print(f"  {rec['number']}: маршрут не получен ({e})", file=sys.stderr)
            continue
        time.sleep(PAUSE)

        if not stops:
            continue

        # Сдвиг якоря: дни курсирования привязаны именно к нему.
        anchor_offset = next((s["offset"] for s in stops
                              if s["express"] in anchors), 0)
        origin = stops[0]["title"]
        destination = stops[-1]["title"]

        for s in stops:
            board = BOARDS.get(s["express"])
            if not board:
                continue
            seen_stations[board] += 1
            days = shift_days(rec["days"], s["offset"] - anchor_offset)

            base = dict(number=rec["number"], brand=rec["brand"], days=days,
                        station=board, fast=False, via=None, cities=[], uid=uid)

            if s["departure"] and s is not stops[-1]:
                rows.append(dict(base, direction="departure",
                                 title=destination,
                                 time_local=s["departure"],
                                 through=bool(s["arrival"])))
            if s["arrival"] and s is not stops[0]:
                rows.append(dict(base, direction="arrival",
                                 title="из " + genitive(origin),
                                 time_local=s["arrival"],
                                 through=bool(s["departure"])))

        if i % 10 == 0:
            print(f"  обработано маршрутов: {i}/{len(meta)}")

    weak = {st for st in seen_stations if st not in SHOW}
    rows = [r for r in rows if r["station"] in SHOW]

    print("\nСтанций в выдаче:")
    for st, n in sorted(seen_stations.items(), key=lambda x: -x[1]):
        if st in weak:
            continue
        print(f"  {st:<18} {n}")
    if weak:
        print(f"\nНе показываем (нет в SHOW): {', '.join(sorted(weak))}")
    missing = sorted(st for st in SHOW if st not in seen_stations)
    if missing:
        print(f"В SHOW, но рейсов не нашлось: {', '.join(missing)}")

    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor", default="2900001,2900002",
                    help="коды станций-якорей через запятую")
    ap.add_argument("--apply", action="store_true", help="записать в seed.json")
    ap.add_argument("--inventory", action="store_true",
                    help="показать все станции маршрутов с кодами")
    args = ap.parse_args()

    try:
        if args.inventory:
            inventory(args.anchor.split(",")[0].strip())
            return
        rows = build([a.strip() for a in args.anchor.split(",") if a.strip()])
    except RaspError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    if not rows:
        print("Ничего не собрано.", file=sys.stderr)
        sys.exit(1)

    out = ROOT / ("seed.json" if args.apply else "seed_yandex.json")
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nЗаписей: {len(rows)} → {out.name}")
    if args.apply:
        print("Дальше: python i18n.py && python sync.py --seed")
    else:
        print("Сравни с текущим seed.json, потом запусти с --apply")


if __name__ == "__main__":
    main()
