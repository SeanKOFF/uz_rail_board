#!/usr/bin/env python3
"""
Клиент API Яндекс.Расписаний. Заменяет ручной сбор данных.

    export YANDEX_RASP_KEY=...
    python yandex.py --probe            проверить ключ и коды станций
    python yandex.py --station tashkent-pass --event departure
    python yandex.py --build            собрать seed.json по всем станциям

Дни курсирования выводятся эмпирически: расписание запрашивается на семь
дней вперёд, и день недели считается рабочим, если рейс в нём встретился.
Это надёжнее, чем разбирать поле days, которое приходит человекочитаемым
текстом («ежедневно», «кроме 3 сентября»).
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import httpx

from from_table import genitive

ROOT = Path(__file__).parent
BASE = "https://api.rasp.yandex.net/v3.0"
KEY = os.environ.get("YANDEX_RASP_KEY", "")

# Наши станции -> коды в системе express (из бандла сайта УТЙ).
# ВНИМАНИЕ: коды городские. Для вокзалов внутри одного города
# (Ташкент-Пассажирский / Южный) нужны отдельные коды — проверить
# через --probe и при необходимости уточнить.
STATIONS = {
    "tashkent-central": "2900001",
    "tashkent-yuzhny": "2900002",   # код не проверен
    "samarkand":       "2900700",
    "bukhara":         "2900800",
    "qarshi":          "2900750",
    "urgench":         "2900790",
    "andijan":         "2900680",
}

DAYS_AHEAD = 7          # столько дней опрашиваем, чтобы вывести расписание
PAUSE = 0.3             # пауза между запросами, вежливость к API


class RaspError(RuntimeError):
    pass


def call(path, **params):
    if not KEY:
        raise RaspError("Не задан YANDEX_RASP_KEY")
    params.update(apikey=KEY, format="json", lang="ru_RU")
    try:
        r = httpx.get(f"{BASE}/{path}/", params=params, timeout=30)
    except httpx.HTTPError as e:
        raise RaspError(f"Сеть: {type(e).__name__}")
    if r.status_code == 401:
        raise RaspError("Ключ отклонён (401). Проверь YANDEX_RASP_KEY.")
    if r.status_code == 403:
        raise RaspError("Ключ не принят (403). Возможно, он ещё не активирован "
                        "или не разрешён для этого метода.")
    if r.status_code == 429:
        raise RaspError("Превышен лимит запросов (429). Попробуй позже.")
    if r.status_code >= 400:
        # намеренно без URL — в нём ключ
        raise RaspError(f"HTTP {r.status_code} на /{path}/")
    return r.json()


def schedule(code, event, day):
    """Расписание по станции на одну дату."""
    return call("schedule",
                station=code,
                system="express",
                transport_types="train",
                event=event,
                date=day.isoformat())


def thread(uid):
    """Полный маршрут рейса со всеми остановками."""
    return call("thread", uid=uid)


# ----------------------------------------------------------------------


def route(station_id, event="departure", index=0):
    """Печатает полный маршрут одного рейса: остановки, времена, коды станций.

    Отсюда берутся коды станций, которых нет в нашем справочнике —
    например, вокзалов Ташкента.
    """
    code = STATIONS.get(station_id, station_id)
    data = schedule(code, event, date.today())
    items = data.get("schedule", [])
    if not items:
        print("Рейсов не нашлось.")
        return

    item = items[min(index, len(items) - 1)]
    th = item.get("thread", {})
    print(f"Рейс {th.get('number')} — {th.get('title')}")
    print(f"uid: {th.get('uid')}\n")

    info = call("thread", uid=th.get("uid"), show_systems="all")
    for st in info.get("stops", []):
        s_ = st.get("station", {})
        codes = s_.get("codes", {})
        arr = (st.get("arrival") or "")[11:16] or "  —  "
        dep = (st.get("departure") or "")[11:16] or "  —  "
        print(f"  {arr} / {dep}  {s_.get('title', '?'):<28} "
              f"yandex={s_.get('code', '?'):<12} express={codes.get('express', '—')}")


def probe():
    """Проверяет ключ и то, что коды станций принимаются."""
    print(f"Ключ: {'задан' if KEY else 'НЕ ЗАДАН'}")
    today = date.today()
    for name, code in STATIONS.items():
        try:
            data = schedule(code, "departure", today)
            st = data.get("station", {})
            n = len(data.get("schedule", []))
            print(f"  {name:<18} {code}  ok — {st.get('title', '?')}, рейсов: {n}")
        except Exception as e:
            print(f"  {name:<18} {code}  ОШИБКА: {e}")
        time.sleep(PAUSE)


def collect(code, event):
    """Собирает рейсы за DAYS_AHEAD дней, выводя дни недели."""
    seen = {}                      # ключ рейса -> запись
    weekdays = defaultdict(set)    # ключ рейса -> {1..7}

    today = date.today()
    for offset in range(DAYS_AHEAD):
        day = today + timedelta(days=offset)
        data = schedule(code, event, day)

        for item in data.get("schedule", []):
            th = item.get("thread", {})
            number = (th.get("number") or "").strip()
            if not number:
                continue

            when = item.get("departure") or item.get("arrival") or ""
            hhmm = when[11:16] if len(when) >= 16 else ""
            if not hhmm:
                continue

            key = (number, hhmm)
            weekdays[key].add(day.isoweekday())
            seen.setdefault(key, dict(
                number=number,
                uid=th.get("uid"),
                title=th.get("short_title") or th.get("title") or "",
                brand=(th.get("transport_subtype") or {}).get("title"),
                time_local=hhmm,
                terminal=(item.get("terminal") or None),
                stops=item.get("stops") or "",
            ))
        time.sleep(PAUSE)

    for key, rec in seen.items():
        rec["days"] = "".join(str(d) for d in sorted(weekdays[key]))
    return list(seen.values())


def to_seed(records, station, event):
    """Приводит выдачу Яндекса к формату seed.json."""
    out = []
    for r in records:
        title = r["title"]
        # 'Ташкент — Бухара' -> нужная половина
        parts = [p.strip() for p in title.replace("—", "-").split("-")]
        if event == "departure":
            shown = " — ".join(parts[1:]) if len(parts) > 1 else title
        else:
            shown = "из " + genitive(parts[0] if parts else title)

        out.append(dict(
            number=r["number"],
            direction=event,
            title=shown,
            brand=r["brand"],
            fast=False,
            time_local=r["time_local"],
            days=r["days"] or "1234567",
            via=None,
            station=station,
            through=False,
            cities=[],
            uid=r["uid"],
        ))
    return out


def build():
    rows = []
    for station, code in STATIONS.items():
        for event in ("departure", "arrival"):
            try:
                recs = collect(code, event)
                rows += to_seed(recs, station, event)
                print(f"  {station:<18} {event:<10} {len(recs)}")
            except Exception as e:
                print(f"  {station:<18} {event:<10} ОШИБКА: {e}", file=sys.stderr)

    if not rows:
        print("Ничего не собрано — seed.json не тронут.", file=sys.stderr)
        sys.exit(1)

    out = ROOT / "seed_yandex.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nЗаписано {len(rows)} записей в {out.name}")
    print("Сравни с текущим seed.json, прежде чем заменять:")
    print("  python sync.py --dry-run")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--route", metavar="STATION",
                    help="показать маршрут рейса с этой станции и коды остановок")
    ap.add_argument("--index", type=int, default=0, help="какой по счёту рейс взять")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--station")
    ap.add_argument("--event", default="departure", choices=["departure", "arrival"])
    args = ap.parse_args()

    try:
        if args.probe:
            probe()
        elif args.route:
            route(args.route, args.event, args.index)
        elif args.build:
            build()
        elif args.station:
            code = STATIONS.get(args.station, args.station)
            for r in sorted(collect(code, args.event), key=lambda x: x["time_local"]):
                print(f"  {r['number']:<8} {r['time_local']} days={r['days']:<8} "
                      f"{r['brand'] or '—':<14} {r['title']}")
        else:
            ap.print_help()
    except RaspError as e:
        print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
