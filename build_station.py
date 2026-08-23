#!/usr/bin/env python3
"""
Собирает табло промежуточной станции из двух выдач поиска маршрутов.

    python build_station.py Самарканд samarkand \\
        --inbound  to_samarkand.txt \\
        --outbound to_tashkent.txt

inbound  — выдача «Поезда в <город>»  (время в городе = прибытие)
outbound — выдача «Поезда в Ташкент» (время в городе = отправление)

Для проходящих поездов это один и тот же момент: стоянка длится
несколько минут и в источнике не указана. Такой рейс помечается
through=true и показывается на обоих табло с одним временем.
"""

import argparse
import json
import re
from pathlib import Path

from parse_direction import parse, normalize_number
from from_table import genitive

# Дни курсирования берём из уже загруженного расписания по цифрам номера.
def shift_days(days, by):
    """Сдвигает дни курсирования на +N суток."""
    if by == 0 or days in ("even", "odd"):
        return days          # чётные/нечётные корректно не сдвинуть
    return "".join(sorted(str((int(d) - 1 + by) % 7 + 1) for d in days))


def load_transit():
    """Дни курсирования транзитных рейсов (в Ташкенте не начинаются)."""
    f = Path("transit.json")
    if not f.exists():
        return {}
    out = {}
    for r in json.loads(f.read_text(encoding="utf-8")):
        out.setdefault(re.match(r"^\d+", r["number"]).group(0), r["days"])
    return out


def load_reference():
    """Из расписания: дни курсирования и каноничный номер по цифрам."""
    days, canon = {}, {}
    for r in json.loads(Path("seed.json").read_text(encoding="utf-8")):
        # Только исходная таблица по Ташкенту — у других станций дни уже
        # сдвинуты, и повторный сдвиг их сломает.
        if not r.get("station", "").startswith("tashkent"):
            continue
        digits = re.match(r"^\d+", r["number"]).group(0)
        days.setdefault(digits, r["days"])
        canon.setdefault(digits, r["number"])
    return days, canon


# Выдача бывает на узбекском — приводим названия к русским из справочника.
def load_names():
    ref = json.loads(Path("stations.json").read_text(encoding="utf-8"))
    names = {}
    for st in ref["stations"]:
        for k in ("ru", "uz", "en"):
            names[st[k].lower()] = st["ru"]
    # то, чего нет в справочнике направлений
    def key(x):
        return x.lower().replace("`", "'").replace("\u2018", "'").replace("\u2019", "'")
    names = {key(k): v for k, v in names.items()}
    names.update({
        "qo'ng'irot": "Кунград", "qung'irot": "Кунград", "кунград": "Кунград",
        "shovot": "Шават", "шават": "Шават",
        "olot": "Алат", "alat": "Алат", "алат": "Алат",
        "хiva": "Хива", "хива": "Хива", "xiva": "Хива",
        "termiz": "Термез", "qarshi": "Карши", "andijan": "Андижан",
        "samarqand": "Самарканд", "toshkkent": "Ташкент", "olot": "Алат",
        "sariosiyo": "Сарыасия", "sariosiyo": "Сарыасия", "сарыасия": "Сарыасия",
        "shahrisabz": "Шахрисабз", "toshkent": "Ташкент",
        "volgograd": "Волгоград", "moskva": "Москва", "almati": "Алматы",
        "dushanbe": "Душанбе", "qozon": "Казань",
    })
    return names

NAMES = load_names()

# Бренды-категории, которые брендом не являются.
GENERIC = {"пассажирский", "cкорый", "скорый", "yo'lovchi", "tezyurar",
           "yolovchi", "tez yurar"}


def endpoints(route):
    """'Бухара → Ташкент Центральный' -> ('Бухара', 'Ташкент')."""
    if not route:
        return None, None
    parts = re.split(r"\s*[→>]\s*", route)
    if len(parts) < 2:
        return None, None
    def clean(s):
        s = re.sub(r"\s*(Центральный|Северный|южный|Markaziy|Shimoliy|janubiy"
                   r"|Ц|С|Ю|\d)\s*$", "", s.strip(), flags=re.I)
        k = s.lower().replace("`", "'").replace("\u2018", "'").replace("\u2019", "'")
        return NAMES.get(k, s)
    return clean(parts[0]), clean(parts[-1])


def build(city_ru, city_id, inbound_file, outbound_file):
    days_map, canon = load_reference()
    transit_days = load_transit()
    rows = []
    seen = set()

    def add(entry, time_at_city, from_city, to_city, day_shift=0):
        digits = re.match(r"^\d+", entry["number"]).group(0)
        if digits in seen:
            return
        seen.add(digits)

        terminates = to_city == city_ru
        originates = from_city == city_ru
        through = not (terminates or originates)

        brand = entry["brand"]
        if brand and brand.strip().lower() in GENERIC:
            brand = None

        base = dict(
            number=canon.get(digits, digits),
            brand=brand,
            time_local=time_at_city,
            days=shift_days(days_map.get(digits) or transit_days.get(digits, "1234567"),
                            day_shift),
            station=city_id,
            through=through,
            via=None,
        )
        # Отправление — если поезд едет дальше или начинается здесь.
        if not terminates:
            rows.append(dict(base, direction="departure", title=to_city or "—"))
        # Прибытие — если поезд приходит сюда или проходит.
        if not originates:
            rows.append(dict(base, direction="arrival", title=f"из {genitive(from_city)}" if from_city else "—"))

    for path, which in ((inbound_file, "in"), (outbound_file, "out")):
        if not path:
            continue
        for e in parse(Path(path).read_text(encoding="utf-8")):
            frm, to = endpoints(e["route"])
            # В обеих выдачах время в нашем городе — это вторая метка.
            # Через полночь — значит, на нашей станции уже следующие сутки.
            shift = 1 if (which == "in" and e["arr_time"] < e["dep_time"]) else 0
            add(e, e["arr_time"] if which == "in" else e["dep_time"], frm, to, shift)

    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("city_ru")
    ap.add_argument("city_id")
    ap.add_argument("--inbound")
    ap.add_argument("--outbound")
    ap.add_argument("--merge", action="store_true")
    args = ap.parse_args()

    rows = build(args.city_ru, args.city_id, args.inbound, args.outbound)

    out = Path("seed.json")
    if args.merge and out.exists():
        existing = [r for r in json.loads(out.read_text(encoding="utf-8"))
                    if r.get("station") != args.city_id]
        rows = existing + rows

    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    mine = [r for r in rows if r["station"] == args.city_id]
    print(f"{args.city_ru}: {len(mine)} записей "
          f"({sum(1 for r in mine if r['direction']=='departure')} отпр, "
          f"{sum(1 for r in mine if r['direction']=='arrival')} приб)")
    print(f"из них проходящих: {sum(1 for r in mine if r['through'])//2}")
    print(f"всего в seed.json: {len(rows)}")


if __name__ == "__main__":
    main()
