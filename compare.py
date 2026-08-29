#!/usr/bin/env python3
"""
Сравнивает seed_yandex.json с текущим seed.json.

    python compare.py

Показывает, какие рейсы есть только в одном источнике и где расходится
время. Сшивка по цифрам номера: суффиксы у одного поезда плавают.
"""

import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
digits = lambda n: re.match(r"^\d+", n).group(0)


def load(name):
    p = ROOT / name
    if not p.exists():
        raise SystemExit(f"Нет файла {name}")
    return json.loads(p.read_text(encoding="utf-8"))


def index(rows):
    """(цифры номера, направление) -> {станция: время}"""
    out = defaultdict(dict)
    for r in rows:
        out[(digits(r["number"]), r["direction"])][r["station"]] = r["time_local"]
    return out


def main():
    old, new = load("seed.json"), load("seed_yandex.json")
    a, b = index(old), index(new)

    trains_old = {k[0] for k in a}
    trains_new = {k[0] for k in b}

    print(f"Ручная сборка: {len(old)} записей, {len(trains_old)} рейсов")
    print(f"Яндекс:        {len(new)} записей, {len(trains_new)} рейсов\n")

    only_old = sorted(trains_old - trains_new)
    only_new = sorted(trains_new - trains_old)

    print(f"Есть у нас, нет в Яндексе ({len(only_old)}):")
    for n in only_old:
        where = {r["station"] for r in old if digits(r["number"]) == n}
        titles = {r["title"] for r in old if digits(r["number"]) == n}
        print(f"  {n:<6} {', '.join(sorted(where)):<34} {list(titles)[0][:30]}")

    print(f"\nЕсть в Яндексе, нет у нас ({len(only_new)}):")
    for n in only_new:
        titles = {r["title"] for r in new if digits(r["number"]) == n}
        print(f"  {n:<6} {list(titles)[0][:40]}")

    # Расхождения во времени по общим рейсам и станциям
    print("\nРасхождения во времени:")
    clashes = 0
    for key in sorted(set(a) & set(b)):
        for station, t_old in a[key].items():
            t_new = b[key].get(station)
            if t_new and t_new != t_old:
                print(f"  {key[0]:<6} {key[1][:4]} {station:<18} {t_old} → {t_new}")
                clashes += 1
    if not clashes:
        print("  нет — время совпадает везде, где сравнимо")

    # Станции
    st_old = {r["station"] for r in old}
    st_new = {r["station"] for r in new}
    print(f"\nСтанции только у нас:     {', '.join(sorted(st_old - st_new)) or '—'}")
    print(f"Станции только в Яндексе: {', '.join(sorted(st_new - st_old)) or '—'}")


if __name__ == "__main__":
    main()
