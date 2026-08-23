#!/usr/bin/env python3
"""
Разбор выдачи поиска маршрутов (страница trains-page).

    python parse_direction.py samarkand_24.txt

Из блока вида

    08:37
    ТАШКЕНТ
    03:09
    11:46
    САМАРКАНД
    Sharq
    710Ф (СК)
    Ташкент Центральный → Бухара

достаёт: номер, бренд, вокзал отправления, время в обеих точках.
Наличие мест и цены игнорируются — они меняются ежеминутно.
"""

import argparse
import json
import re
from pathlib import Path

TIME = re.compile(r"^\d{1,2}:\d{2}$")
NUMBER = re.compile(r"^(\d+[А-ЯA-Z]?)\s*\(([^)]+)\)$")
ROUTE = re.compile(r"^(.+?)\s*[→>-]{1,2}\s*(.+)$")

# Кириллические суффиксы номеров -> латиница, как в таблице расписания.
TRANSLIT = str.maketrans("ФЗЧЖХСКЕЩЬГ", "FZCJXCKEQQG")

STATIONS = {
    "ташкент центральный": "tashkent-central",
    "ташкент северный":    "tashkent-north",
    "ташкент южный":       "tashkent-south",
    "ташкент":             "tashkent-unspecified",
}


def normalize_number(raw):
    """'764Ф' -> '764F', чтобы сшивалось с расписанием."""
    return raw.strip().translate(TRANSLIT).upper()


def parse(text):
    lines = [l.strip(" *\t") for l in text.splitlines()]
    lines = [l for l in lines if l]

    entries = []
    i = 0
    while i < len(lines):
        # Якорь — строка с номером поезда вида '710Ф (СК)'.
        m = NUMBER.match(lines[i])
        if not m:
            i += 1
            continue

        number = normalize_number(m.group(1))
        category = m.group(2).strip()

        # Назад от номера: бренд, город прибытия, время прибытия,
        # длительность, город отправления, время отправления.
        try:
            brand = lines[i-1]
            arr_city = lines[i-2]
            arr_time = lines[i-3]
            duration = lines[i-4]
            dep_city = lines[i-5]
            dep_time = lines[i-6]
        except IndexError:
            i += 1
            continue

        if not (TIME.match(arr_time) and TIME.match(dep_time)):
            i += 1
            continue

        # Вперёд от номера: строка маршрута с конкретными вокзалами.
        dep_station = arr_station = None
        if i + 1 < len(lines):
            rm = ROUTE.match(lines[i+1])
            if rm:
                dep_station = STATIONS.get(rm.group(1).strip().lower())
                full_route = lines[i+1]
            else:
                full_route = None
        else:
            full_route = None

        entries.append(dict(
            number=number,
            brand=None if brand.lower() in ("пассажирский", "cкорый", "скорый") else brand,
            category=category,
            dep_city=dep_city.capitalize(),
            dep_time=dep_time,
            dep_station=dep_station,
            arr_city=arr_city.capitalize(),
            arr_time=arr_time,
            duration=duration,
            route=full_route,
        ))
        i += 1

    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    all_sets = {}
    for f in args.files:
        entries = parse(Path(f).read_text(encoding="utf-8"))
        all_sets[f] = entries
        print(f"\n{f}: {len(entries)} рейсов")
        for e in sorted(entries, key=lambda x: x["dep_time"]):
            st = (e["dep_station"] or "?").replace("tashkent-", "")
            print(f"  {e['number']:<6} {e['dep_time']} → {e['arr_time']}  "
                  f"{st:<12} {e['brand'] or '—'}")

    # Если файлов несколько — сверяем, стабильны ли времена между датами.
    if len(all_sets) > 1:
        names = list(all_sets)
        a = {e["number"]: e for e in all_sets[names[0]]}
        b = {e["number"]: e for e in all_sets[names[1]]}

        print("\n" + "=" * 58)
        both = sorted(set(a) & set(b))
        drift = [n for n in both
                 if (a[n]["dep_time"], a[n]["arr_time"]) != (b[n]["dep_time"], b[n]["arr_time"])]
        print(f"В обеих датах: {len(both)} рейсов, из них с разным временем: {len(drift)}")
        for n in drift:
            print(f"  {n}: {a[n]['dep_time']}→{a[n]['arr_time']} vs {b[n]['dep_time']}→{b[n]['arr_time']}")

        only_a = sorted(set(a) - set(b))
        only_b = sorted(set(b) - set(a))
        print(f"\nТолько в {names[0]}: {', '.join(only_a) or '—'}")
        print(f"Только в {names[1]}: {', '.join(only_b) or '—'}")

    if args.json:
        out = Path("direction.json")
        out.write_text(json.dumps(list(all_sets.values())[0], ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"\nЗаписано в {out}")


if __name__ == "__main__":
    main()
