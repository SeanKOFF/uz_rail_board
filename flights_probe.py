#!/usr/bin/env python3
"""
Разведка авиарасписаний Яндекса по Узбекистану.

Ничего не пишет в данные сайта — только в .cache/flights/.

  python3 flights_probe.py --airports        # найти коды аэропортов
  python3 flights_probe.py --schedule s9600216 --date 2026-08-30
  python3 flights_probe.py --fields s9600216 # какие поля непустые
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date

API = "https://api.rasp.yandex.net/v3.0"
CACHE = ".cache/flights"
STATIONS = os.path.join(CACHE, "stations_list.json")


def key():
    k = os.environ.get("YANDEX_RASP_KEY")
    if not k:
        sys.exit("Не задан YANDEX_RASP_KEY")
    return k


def get(endpoint, **params):
    params["apikey"] = key()
    params["format"] = "json"
    url = f"{API}/{endpoint}/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read()[:400].decode('utf-8', 'replace')}")


def fetch_schedule(station, day, event):
    """Забирает расписание целиком, постранично.

    Яндекс отдаёт максимум 100 сегментов за раз. Неполную выдачу
    применять нельзя — на этом проект уже обжигался.
    """
    segments = []
    offset = 0
    total = None
    while True:
        data = get("schedule", station=station, date=day,
                   transport_types="plane", event=event,
                   limit=100, offset=offset)
        page = data.get("schedule", [])
        segments.extend(page)
        pg = data.get("pagination") or {}
        total = pg.get("total", len(segments))
        offset += len(page)
        if not page or offset >= total:
            break

    if total is not None and len(segments) != total:
        sys.exit(f"забрал {len(segments)} из {total} — выдача неполная, "
                 f"дальше идти нельзя")
    print(f"забрал {len(segments)} сегментов (total={total})")
    return segments


def download_stations():
    os.makedirs(CACHE, exist_ok=True)
    if os.path.exists(STATIONS):
        print(f"справочник уже скачан: {STATIONS}")
        return
    print("качаю справочник станций (десятки мегабайт, небыстро)...")
    data = get("stations_list")
    with open(STATIONS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    size = os.path.getsize(STATIONS) / 1e6
    print(f"сохранил {STATIONS} ({size:.1f} МБ)")


def airports():
    download_stations()
    with open(STATIONS, encoding="utf-8") as f:
        data = json.load(f)

    found = []
    for country in data.get("countries", []):
        if "збекистан" not in (country.get("title") or ""):
            continue
        for region in country.get("regions", []):
            for settlement in region.get("settlements", []):
                city = settlement.get("title") or "?"
                for st in settlement.get("stations", []):
                    if st.get("transport_type") != "plane":
                        continue
                    found.append({
                        "city": city,
                        "title": st.get("title"),
                        "code": (st.get("codes") or {}).get("yandex_code"),
                        "iata": (st.get("codes") or {}).get("iata"),
                        "type": st.get("station_type"),
                    })

    if not found:
        print("аэропортов не нашлось — проверь фильтр по стране")
        return

    print(f"\nаэропортов: {len(found)}\n")
    for a in sorted(found, key=lambda x: x["city"]):
        iata = a["iata"] or "—"
        print(f"  {a['code']:<12} {iata:<5} {a['city']:<16} {a['title']}")

    out = os.path.join(CACHE, "airports.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(found, f, ensure_ascii=False, indent=2)
    print(f"\nсохранил {out}")


def schedule(station, day, event="departure"):
    os.makedirs(CACHE, exist_ok=True)
    segments = fetch_schedule(station, day, event)
    out = os.path.join(CACHE, f"{station}-{event}-{day}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"schedule": segments}, f, ensure_ascii=False, indent=2)

    print(f"сырой ответ: {out}")
    if segments:
        print("\nпервый сегмент целиком:\n")
        print(json.dumps(segments[0], ensure_ascii=False, indent=2))


def fields(station, day, event="departure"):
    """Какие поля реально заполнены — от этого зависит раскрытие по клику."""
    segments = fetch_schedule(station, day, event)
    if not segments:
        print("пусто")
        return

    filled = Counter()
    carriers = Counter()
    for s in segments:
        thread = s.get("thread") or {}
        for name, value in (
            ("номер рейса", thread.get("number")),
            ("перевозчик", (thread.get("carrier") or {}).get("title")),
            ("борт", thread.get("vehicle")),
            ("терминал", s.get("terminal")),
            ("дни курсирования", s.get("days")),
            ("длительность", s.get("duration")),
            ("отправление", s.get("departure")),
            ("прибытие", s.get("arrival")),
            ("направление", s.get("direction") or thread.get("title")),
        ):
            if value not in (None, "", []):
                filled[name] += 1
        c = (thread.get("carrier") or {}).get("title")
        if c:
            carriers[c] += 1

    n = len(segments)
    print(f"\nсегментов: {n}\n")
    for name, count in filled.most_common():
        mark = "полно" if count == n else f"{count}/{n}"
        print(f"  {name:<20} {mark}")
    print(f"\nперевозчики ({len(carriers)}):")
    for c, count in carriers.most_common():
        print(f"  {count:>3}  {c}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--airports", action="store_true",
                   help="найти коды аэропортов Узбекистана")
    p.add_argument("--schedule", metavar="CODE",
                   help="выгрузить сырое расписание по коду станции")
    p.add_argument("--fields", metavar="CODE",
                   help="показать, какие поля заполнены")
    p.add_argument("--date", default=str(date.today()))
    p.add_argument("--event", default="departure",
                   choices=["departure", "arrival"])
    a = p.parse_args()

    if a.airports:
        airports()
    elif a.schedule:
        schedule(a.schedule, a.date, a.event)
    elif a.fields:
        fields(a.fields, a.date, a.event)
    else:
        p.print_help()
