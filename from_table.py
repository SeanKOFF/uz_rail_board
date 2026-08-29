#!/usr/bin/env python3
"""
Превращает таблицу, скопированную со страницы расписания, в seed.json.

    python from_table.py departures.txt --direction departure
    python from_table.py arrivals.txt   --direction arrival

Ожидает текст, вставленный как есть: колонки разделены табами,
шапка и служебные строки отбрасываются автоматически.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Станции Ташкента, по которым строим табло. Всё остальное — транзит.
TASHKENT = {
    "ташкент пасс": "tashkent-pass",
    "ташкент южн":  "tashkent-yuzhny",
}

DAYS = {
    "пн":1, "понедельник":1,
    "вт":2, "вторник":2,
    "ср":3, "среда":3,
    "чт":4, "четверг":4,
    "пт":5, "пятница":5,
    "сб":6, "суббота":6,
    "вс":7, "воскресенье":7,
}

BRANDS = [
    (re.compile(r"^\d+F$"), "Афросиёб", True),     # 7xxF — скоростные
]


def parse_days(raw):
    """'Ежедневно' -> '1234567'; 'Ср, Чт' -> '34'; 'Четные числа' -> 'even'."""
    s = (raw or "").strip().lower()
    if not s:
        return "1234567"
    if "ежедневн" in s:
        return "1234567"
    if "нечетн" in s or "нечётн" in s:
        return "odd"
    if "четн" in s or "чётн" in s:
        return "even"

    nums = sorted({DAYS[p.strip()] for p in re.split(r"[,;/]", s)
                   if p.strip() in DAYS})
    return "".join(map(str, nums)) or "1234567"


def classify(number, route):
    """Бренд по номеру не определяется — 710Ф это Sharq, 716Ф Nasaf,
    730Ф O'zbekiston. Берётся только из выдачи поиска (build_station.py)."""
    return None, False


# Родительный падеж для прибытий: «из Бухары», «из Андижана».
INDECLINABLE = {"карши", "душанбе", "алматы", "сарыассия"}

# Вторая часть дефисных названий — прилагательное, склоняется отдельно.
ADJ = {"Южный": "Южного", "Северный": "Северного", "Центральный": "Центрального",
       "Пасс.": "Пасс.", "Пассажирский": "Пассажирского"}

def genitive(name):
    n = name.strip()
    low = n.lower()
    # Составные и дефисные оставляем как есть: «из Ташкент-Южный»
    # звучит хуже, чем «из Ташкента-Южного», но лучше, чем «Ташкент-Южныйа».
    if "-" in n or " " in n:
        # 'Ташкент-Южный' -> 'Ташкента-Южного', 'Бухара 1' -> 'Бухары 1'
        head, sep, tail = n.partition("-")
        if sep:
            return genitive(head) + sep + ADJ.get(tail, tail)
        head, _, tail = n.partition(" ")
        return genitive(head) + " " + tail
    if low in INDECLINABLE:
        return "Сарыассии" if low == "сарыассия" else n
    if n.endswith("ия"):
        return n[:-1] + "и"
    if n.endswith("а"):
        return n[:-1] + ("и" if n[-2] in "кгхжчшщ" else "ы")
    if n.endswith("ь"):
        return n[:-1] + "и"
    if n[-1] in "иеыоу":
        return n
    return n + "а"


def parts_origin(route):
    return route.split("-")[0].strip()


def split_route(route, direction):
    """Возвращает (станция Ташкента, заголовок, через)."""
    parts = [p.strip() for p in route.split("-")]
    if direction == "departure":
        # 'Ташкент пасс - Наманган - Андижан'
        return parts[0], " — ".join(parts[1:]), None
    # 'Андижан - Наманган - Ташкент пасс'
    via = " — ".join(parts[1:-1]) or None
    return parts[-1], "из " + genitive(parts[0]), via


def tashkent_station(origin):
    key = origin.strip().lower()
    return TASHKENT.get(key)


def parse(text, direction):
    rows, skipped = [], []

    for line in text.splitlines():
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) < 6:
            continue
        number, route, dep_time, dep_days, arr_time, arr_days = cells[:6]

        if not re.match(r"^\d+[A-ZА-Я]?$", number):     # шапка и мусор
            continue

        endpoint, title, via = split_route(route, direction)
        station = tashkent_station(endpoint)

        if station is None:
            skipped.append(dict(number=number, route=route,
                                origin=endpoint if direction == "departure" else parts_origin(route),
                                origin_time=dep_time,
                                days=parse_days(dep_days)))
            continue

        brand, fast = classify(number, route)

        rows.append(dict(
            number=number,
            direction=direction,
            title=title,
            brand=brand,
            fast=fast,
            time_local=dep_time if direction == "departure" else arr_time,
            days=parse_days(dep_days if direction == "departure" else arr_days),
            via=via,
            station=station,
        ))

    return rows, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--direction", choices=["departure", "arrival"], default="departure")
    ap.add_argument("--merge", action="store_true", help="дописать в существующий seed.json")
    args = ap.parse_args()

    text = Path(args.file).read_text(encoding="utf-8")
    rows, skipped = parse(text, args.direction)

    out = Path(__file__).parent / "seed.json"
    if args.merge and out.exists():
        existing = json.loads(out.read_text(encoding="utf-8"))
        seen = {(r["number"], r["direction"], r["station"]) for r in rows}
        rows = [r for r in existing
                if (r["number"], r["direction"], r["station"]) not in seen] + rows

    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Записано рейсов: {len(rows)}")
    by_station = {}
    for r in rows:
        by_station[r["station"]] = by_station.get(r["station"], 0) + 1
    for st, n in sorted(by_station.items()):
        print(f"  {st}: {n}")

    if skipped:
        ref = Path(__file__).parent / "transit.json"
        old = json.loads(ref.read_text(encoding="utf-8")) if (args.merge and ref.exists()) else []
        seen = {(r["number"], r["route"]) for r in skipped}
        merged = [r for r in old if (r["number"], r["route"]) not in seen] + skipped
        ref.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nТранзитных (в transit.json): {len(skipped)}")
        for r in skipped:
            print(f"  {r['number']:<6} {r['days']:<10} {r['route']}")


if __name__ == "__main__":
    main()
