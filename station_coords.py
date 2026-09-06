"""Координаты станций по express-коду: data/station_coords.json.

Разовый инструмент, не часть автосборки. Запускать вручную, когда в
data/routes.json появляются новые станции (проверить: сравнить вывод
"новые коды" ниже с тем, что уже есть в файле).

    export YANDEX_RASP_KEY=...
    python3 station_coords.py

Источник координат — Yandex.Rasp stations_list (тот же ключ и клиент,
что и у остального проекта). Матчинг по названию станции: в проверке
на 06.09.2026 совпало 59 из 59, дублей координат не было. Если после
обновления графика появится расхождение — оно попадёт в "не найдено"
ниже, разбираться по имени вручную.
"""
import json
import sys
from pathlib import Path

from yandex import call, RaspError

ROUTES = Path("data/routes.json")
OUT = Path("data/station_coords.json")


def our_stations():
    """Все express-коды и названия, что реально встречаются в маршрутах."""
    routes = json.loads(ROUTES.read_text(encoding="utf-8"))
    stations = {}
    for variants in routes.values():
        for r in variants:
            for stop in r["stops"]:
                stations[stop["code"]] = stop["title"]
    return stations


def yandex_titles():
    """Название -> (lat, lon) по узбекским станциям из stations_list."""
    data = call("stations_list")
    uz = next((c for c in data["countries"] if "Узбекистан" in c.get("title", "")), None)
    if uz is None:
        raise RaspError("Узбекистан не найден в ответе stations_list")

    by_title = {}
    for region in uz.get("regions", []):
        for settlement in region.get("settlements", []):
            for st in settlement.get("stations", []):
                lat, lon = st.get("latitude"), st.get("longitude")
                if lat in (None, "") or lon in (None, ""):
                    continue
                by_title.setdefault(st["title"], (lat, lon))
    return by_title


def main():
    ours = our_stations()
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}

    try:
        by_title = yandex_titles()
    except RaspError as e:
        sys.exit(f"Ошибка: {e}")

    result = {}
    not_found = []
    changed = []

    for code, title in ours.items():
        if title in by_title:
            lat, lon = by_title[title]
            result[code] = {"title": title, "lat": lat, "lon": lon}
            prev = old.get(code)
            if prev and (prev.get("lat"), prev.get("lon")) != (lat, lon):
                changed.append((code, title, prev.get("lat"), prev.get("lon"), lat, lon))
        else:
            result[code] = {"title": title, "lat": None, "lon": None}
            not_found.append((code, title))

    new_codes = sorted(set(ours) - set(old))
    removed_codes = sorted(set(old) - set(ours))

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Записано станций: {len(result)}")
    print(f"Без координат: {sum(1 for s in result.values() if s['lat'] is None)}")

    if new_codes:
        print(f"\nНовые коды с прошлого запуска ({len(new_codes)}):")
        for c in new_codes:
            mark = "" if result[c]["lat"] is not None else "  (координат не нашлось)"
            print(f"  {c} {result[c]['title']}{mark}")

    if removed_codes:
        print(f"\nКоды, пропавшие из routes.json ({len(removed_codes)}), можно убрать вручную:")
        for c in removed_codes:
            print(f"  {c} {old[c].get('title', '?')}")

    if changed:
        print(f"\nКоординаты изменились ({len(changed)}) — стоит проверить, не сбой ли это источника:")
        for code, title, olat, olon, nlat, nlon in changed:
            print(f"  {code} {title}: было ({olat}, {olon}) -> стало ({nlat}, {nlon})")

    if not_found:
        print(f"\nНе нашлись в stations_list ({len(not_found)}) — координаты придётся добавить вручную, если понадобятся:")
        for code, title in not_found:
            print(f"  {code} {title}")


if __name__ == "__main__":
    main()
