#!/usr/bin/env python3
"""
Пригородные электрички — ticket.elektropoyezd.uz («Shahar atrofida
yo'lovchi tashish» МЧЖ, дочка УТЙ).

Открытый JSON-API фронтенда, ключ не нужен:
    GET /api/routes                    — все маршруты по регионам
    GET /api/routes/{id}?date=Y-M-D    — маршрут с рейсами и статусом
                                         на дату (отмены, опоздания)
    GET /api/trains/{id}               — остановки рейса, без даты
Язык ответа задаёт заголовок Accept-Language: ru | uz | en.

Пишет только data/suburban.json — данных Яндекса и табло станций
не касается. Там же список проблем: что отменено, что опаздывает,
где данные источника противоречат сами себе.

    python3 elektropoyezd.py                  # снять и записать
    python3 elektropoyezd.py --dry-run        # снять, не записывать
    python3 elektropoyezd.py --fixtures f.json.gz   # офлайн, из снимка
"""

import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://ticket.elektropoyezd.uz/api"
OUT = "data/suburban.json"
UA = "tabloda.uz/1.0 (+https://tabloda.uz)"
LANGS = ("ru", "uz", "en")
TZ = timezone(timedelta(hours=5))  # Ташкент, перевода часов нет

# Сколько id сверх максимального из списка пробуем: у источника есть
# маршруты, которые из списка убраны, но по id отвечают (29 — Коканд–
# Наманган–Андижан без единого рейса). Такие попадают в проблемы.
PROBE_EXTRA = 6

# Защита от усечения, как у Яндекса: если маршрутов стало заметно
# меньше, чем в прошлом файле, это сбой источника, а не отмена.
MIN_SHARE = 0.8

PAUSE = 0.15  # между запросами, чтобы не долбить чужой сервер

NAMES_FILE = "station_names.json"  # ручные названия станций поверх источника


# ---------------------------------------------------------------- сеть

class Source:
    """Живой API. fetch() -> (status, json | None)."""

    def fetch(self, path, lang):
        req = urllib.request.Request(BASE + path, headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": lang,
        })
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    time.sleep(PAUSE)
                    return r.status, json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return 404, None
                if e.code < 500 and e.code != 429:
                    return e.code, None
            except (urllib.error.URLError, TimeoutError, ValueError):
                pass
            time.sleep(2 * (attempt + 1))
        return 0, None


class Fixtures:
    """Офлайн-снимок: {"list_ru": ..., "r1_ru_2026-09-28": ..., "train42_ru": ...}."""

    def __init__(self, path, today):
        op = gzip.open if path.endswith(".gz") else open
        with op(path, "rt", encoding="utf-8") as f:
            self.fx = json.load(f)
        self.today = today

    def fetch(self, path, lang):
        if path == "/routes":
            key = f"list_{lang}"
        elif path.startswith("/trains/"):
            key = f"train{path[len('/trains/'):]}_{lang}"
        else:
            rid, _, q = path[len("/routes/"):].partition("?date=")
            key = f"r{rid}_{lang}_{q}"
        v = self.fx.get(key)
        if v is None or "__status" in v:
            return (v or {}).get("__status", 404), None
        return 200, v


# ---------------------------------------------------------- разбор

def hm(s):
    """'08:47' -> 527 минут от полуночи."""
    h, m = s.split(":")[:2]
    return int(h) * 60 + int(m)


def st_id(st):
    return (st or {}).get("id")


def collect(src, today, tomorrow):
    """Все запросы. Возвращает сырые ответы или бросает RuntimeError."""
    failed = []

    lists = {}
    for lang in LANGS:
        code, j = src.fetch("/routes", lang)
        if code != 200 or not j:
            failed.append(f"/routes [{lang}] → {code}")
        else:
            lists[lang] = j["data"]
    if "ru" not in lists:
        raise RuntimeError("список маршрутов не получен: " + "; ".join(failed))

    listed = sorted({r["id"] for g in lists["ru"] for r in g["routes"]})
    probe = range(1, max(listed) + PROBE_EXTRA + 1)

    details = {}  # (id, lang, date) -> data
    hidden = []
    for rid in probe:
        code, j = src.fetch(f"/routes/{rid}?date={today}", "ru")
        if code == 404:
            continue
        if code != 200 or not j:
            failed.append(f"/routes/{rid} [ru {today}] → {code}")
            continue
        details[(rid, "ru", today)] = j["data"]
        if rid not in listed:
            hidden.append(rid)

    for rid in listed:
        for lang, date in (("ru", tomorrow), ("uz", today), ("en", today)):
            code, j = src.fetch(f"/routes/{rid}?date={date}", lang)
            if code != 200 or not j:
                failed.append(f"/routes/{rid} [{lang} {date}] → {code}")
                continue
            details[(rid, lang, date)] = j["data"]

    return lists, listed, hidden, details, failed


def collect_stops(src, details, today):
    """Остановки каждого рейса: /api/trains/{id}. Без даты — список
    стабилен по id рейса, поэтому берём id один раз, из ru-деталей на
    сегодня, и не привязываем сбор к конкретной дате."""
    failed = []
    train_ids = sorted({t["id"] for (rid, lang, date), d in details.items()
                         if lang == "ru" and date == today
                         for t in (d.get("trains") or [])})
    stops = {}  # (train_id, lang) -> [{station, arrival_time, departure_time}, ...]
    for tid in train_ids:
        for lang in LANGS:
            code, j = src.fetch(f"/trains/{tid}", lang)
            if code != 200 or not j:
                failed.append(f"/trains/{tid} [{lang}] → {code}")
                continue
            stops[(tid, lang)] = (j.get("data") or {}).get("stops") or []
    return stops, failed


def i18n(by_lang, getter):
    out = {}
    for lang in LANGS:
        d = by_lang.get(lang)
        v = getter(d) if d else None
        if v:
            out[lang] = v
    ru = out.get("ru")
    for lang in LANGS:
        out.setdefault(lang, ru)
    return out


def build(lists, listed, hidden, details, today, tomorrow, stops=None):
    stops = stops or {}
    regions = {}   # slug -> {id, slug, name{}}
    stations = {}  # id -> name{}

    def put_station(st, lang):
        if not st or st.get("id") is None:
            return
        stations.setdefault(str(st["id"]), {})[lang] = st.get("name")

    for lang, groups in lists.items():
        for g in groups:
            reg = g["region"]
            r = regions.setdefault(reg["slug"], {"id": reg["id"], "slug": reg["slug"], "name": {}})
            r["name"][lang] = reg["name"]

    routes = []
    for rid in listed + hidden:
        base = details.get((rid, "ru", today))
        if not base:
            continue
        by_lang = {l: details.get((rid, l, today)) for l in LANGS}
        for lang, d in by_lang.items():
            if not d:
                continue
            put_station(d.get("origin_station"), lang)
            put_station(d.get("destination_station"), lang)
            for t in d.get("trains") or []:
                put_station(t.get("origin_station"), lang)
                put_station(t.get("destination_station"), lang)

        # Статус на дату у каждого поезда свой: id поезда в API
        # устойчив, номер — нет (6354 ходит двумя разными ниткой).
        status = {}
        for date in (today, tomorrow):
            d = details.get((rid, "ru", date))
            for t in (d or {}).get("trains") or []:
                s = {}
                if t.get("is_cancelled"):
                    s["cancelled"] = True
                    reasons = {}
                    for lang in LANGS:
                        dl = details.get((rid, lang, date))
                        for tl in (dl or {}).get("trains") or []:
                            if tl.get("id") == t.get("id") and tl.get("cancel_reason"):
                                reasons[lang] = tl["cancel_reason"]
                    if reasons or t.get("cancel_reason"):
                        s["reason"] = {l: reasons.get(l) or reasons.get("ru") or t.get("cancel_reason")
                                       for l in LANGS}
                if t.get("delay_minutes"):
                    s["delay"] = int(t["delay_minutes"])
                    if t.get("delay_reason"):
                        s["delay_reason"] = t["delay_reason"]
                if s:
                    status.setdefault(str(t["id"]), {})[date] = s

        trains = []
        for t in base.get("trains") or []:
            dep, arr = t.get("departs_at") or t.get("departure_time"), t.get("arrives_at") or t.get("arrival_time")
            if not dep or not arr:
                continue
            dur = t.get("duration_minutes")
            if dur is None:
                dur = (hm(arr) - hm(dep)) % 1440
            days = t.get("operating_day_numbers") or [1, 2, 3, 4, 5, 6, 7]
            # Остановки не зависят от даты и языка по составу, только
            # названия внутри меняются — имена регистрируем на всех
            # языках сразу, а порядок/время берём один раз из ru.
            for lang in LANGS:
                for s_ in stops.get((t["id"], lang)) or []:
                    put_station(s_.get("station"), lang)
            ru_stops = stops.get((t["id"], "ru"))
            if ru_stops:
                item_stops = [
                    {"station": st_id(s_.get("station")),
                     "arr": (s_.get("arrival_time") or "")[:5] or None,
                     "dep": (s_.get("departure_time") or "")[:5] or None}
                    for s_ in ru_stops
                ]
            item = {
                "id": t["id"],
                "number": str(t.get("train_number") or ""),
                "dir": "out" if t.get("direction") == "outbound" else "in",
                # Концы рейса — из первой/последней остановки, когда они
                # есть: origin_station/destination_station в /api/routes/{id}
                # бывают попросту неверны (рейс 7049 там кончается в Ташкент
                # Южный, хотя это лишь остановка по пути в Ташкент-
                # Центральный — видно по пустому departure_time только на
                # настоящей конечной). Без stops остаётся как было; для
                # обратных рейсов концы местами перепутаны, направление
                # берём из dir, а расхождение уходит в проблемы.
                "from": (st_id(ru_stops[0]["station"]) if ru_stops else None)
                        or st_id(t.get("origin_station")),
                "to": (st_id(ru_stops[-1]["station"]) if ru_stops else None)
                        or st_id(t.get("destination_station")),
                "dep": dep[:5],
                "arr": arr[:5],
                "dur": int(dur),
                "days": sorted(int(x) for x in days),
                "active": bool(t.get("is_active", True)),
            }
            if hm(arr) < hm(dep):
                item["arr_next_day"] = True
            if str(t["id"]) in status:
                item["status"] = status[str(t["id"])]
            if ru_stops:
                item["stops"] = item_stops
            trains.append(item)
        trains.sort(key=lambda x: (x["dir"] != "out", hm(x["dep"])))

        tariffs = sorted((x.get("price") for x in base.get("tariffs") or [] if x.get("price")), reverse=True)
        reg_slugs = [r["slug"] for r in base.get("regions") or [] if r.get("slug")]
        routes.append({
            "id": rid,
            "region": (base.get("region") or {}).get("slug"),
            "regions": reg_slugs or [(base.get("region") or {}).get("slug")],
            "name": i18n(by_lang, lambda d: d.get("name")),
            "from": st_id(base.get("origin_station")),
            "to": st_id(base.get("destination_station")),
            "price": {"adult": tariffs[0] if tariffs else None,
                      "child": tariffs[1] if len(tariffs) > 1 else None},
            "fleet": base.get("fleet_type"),
            "active": bool(base.get("is_active", True)),
            "listed": rid in listed,
            "trains": trains,
        })

    return regions, stations, routes


# ---------------------------------------------------------- проблемы

SEV_ORDER = {"critical": 0, "warn": 1, "info": 2}


def find_problems(routes, today, tomorrow):
    """Каждая проблема — код + параметры; текст собирает страница на
    своём языке. Для отчёта в Actions есть problem_text_ru()."""
    probs = []

    def add(route, sev, code, **kw):
        probs.append({"route": route["id"], "severity": sev, "code": code, **kw})

    by_number = {}
    for r in routes:
        tr = r["trains"]
        if not r["active"]:
            add(r, "critical", "route_inactive")
        if not tr:
            add(r, "critical", "no_trains", listed=r["listed"])
            continue
        if not r["listed"]:
            add(r, "warn", "hidden_route")

        for t in tr:
            for date in (today, tomorrow):
                s = (t.get("status") or {}).get(date) or {}
                if s.get("cancelled"):
                    add(r, "critical", "cancelled", train=t["number"], train_id=t["id"],
                        date=date, dep=t["dep"], reason=s.get("reason"))
                if s.get("delay"):
                    add(r, "warn", "delayed", train=t["number"], train_id=t["id"],
                        date=date, minutes=s["delay"])
            if not t["active"]:
                add(r, "critical", "train_inactive", train=t["number"], train_id=t["id"])

        # Все рейсы отменены — маршрута сегодня фактически нет.
        live_today = [t for t in tr if not ((t.get("status") or {}).get(today) or {}).get("cancelled")]
        if not live_today:
            add(r, "critical", "all_cancelled", date=today)

        # Станции рейса против концов маршрута и против направления.
        ends = {r["from"], r["to"]}
        swapped, foreign = [], []
        for t in tr:
            exp = (r["from"], r["to"]) if t["dir"] == "out" else (r["to"], r["from"])
            got = (t["from"], t["to"])
            if got == exp:
                continue
            if got == exp[::-1]:
                swapped.append(t["number"])
            elif t["from"] == t["to"]:
                if r["from"] != r["to"]:  # кольцевой маршрут — норма
                    foreign.append(t["number"])
            elif not ({t["from"], t["to"]} <= ends):
                foreign.append(t["number"])
        if swapped:
            # Страница это уже исправляет по dir, пассажира не касается —
            # но при интеграции об этом надо помнить.
            add(r, "info", "stations_swapped", trains=swapped)
        if foreign:
            add(r, "warn", "stations_mismatch", trains=foreign)

        outs = sum(t["dir"] == "out" for t in tr)
        if outs == 0 or outs == len(tr):
            add(r, "info", "one_direction", dir="out" if outs else "in")

        for t in tr:
            if len(t["days"]) < 7:
                add(r, "info", "not_daily", train=t["number"], train_id=t["id"],
                    dep=t["dep"], days=t["days"])

        for t in tr:
            by_number.setdefault(t["number"], []).append((r, t))

    # Один номер в нескольких маршрутах с разным временем — источник
    # сам не знает, какая нитка настоящая.
    for num, items in by_number.items():
        rids = sorted({r["id"] for r, _ in items})
        if len(rids) < 2:
            continue
        times = {(t["dep"], t["arr"]) for _, t in items}
        if len(times) > 1:
            for rid in rids:
                probs.append({"route": rid, "severity": "warn", "code": "number_conflict",
                              "train": num, "routes": rids,
                              "variants": sorted(f"{d}–{a}" for d, a in times)})

    probs.sort(key=lambda p: (SEV_ORDER[p["severity"]], p["route"], p.get("train", "")))
    return probs


DAY_RU = {1: "пн", 2: "вт", 3: "ср", 4: "чт", 5: "пт", 6: "сб", 7: "вс"}


def problem_text_ru(p, names):
    c = p["code"]
    tr = p.get("train")
    if c == "cancelled":
        why = (p.get("reason") or {}).get("ru")
        return f"№{tr} ({p['dep']}) отменён {p['date']}" + (f": {why}" if why else "")
    if c == "all_cancelled":
        return f"все рейсы на {p['date']} отменены"
    if c == "delayed":
        return f"№{tr} опаздывает на {p['minutes']} мин ({p['date']})"
    if c == "no_trains":
        return "в маршруте нет ни одного рейса" + ("" if p.get("listed") else " (и он скрыт из списка)")
    if c == "hidden_route":
        return "маршрут отвечает по id, но скрыт из общего списка"
    if c == "route_inactive":
        return "маршрут помечен неактивным"
    if c == "train_inactive":
        return f"№{tr} помечен неактивным"
    if c == "stations_swapped":
        return "станции отправления/прибытия перепутаны у " + ", ".join(p["trains"])
    if c == "stations_mismatch":
        return "рейс идёт не между концами маршрута: " + ", ".join(p["trains"])
    if c == "number_conflict":
        others = ", ".join(names.get(x, str(x)) for x in p["routes"])
        return f"№{tr} есть в маршрутах {others} с разным временем: " + "; ".join(p["variants"])
    if c == "one_direction":
        return "рейсы только в одну сторону"
    if c == "not_daily":
        return f"№{tr} ({p['dep']}) ходит не ежедневно: " + ", ".join(DAY_RU[d] for d in p["days"])
    return c


# ---------------------------------------------------------- названия

def apply_name_overrides(stations, path=NAMES_FILE):
    """Ручные названия из station_names.json поверх источника.
    Ключ — id станции, значение — {язык: название}; подменяются только
    указанные языки. Возвращает (подставлено, нет в данных)."""
    try:
        with open(path, encoding="utf-8") as f:
            ov = json.load(f)
    except FileNotFoundError:
        return [], []
    applied, unknown = [], []
    for sid, names in ov.items():
        if sid.startswith("_"):  # служебные ключи, например _comment
            continue
        if sid not in stations:
            unknown.append(sid)
            continue
        stations[sid].update({l: v for l, v in names.items() if l in LANGS and v})
        applied.append(sid)
    return applied, unknown


# ---------------------------------------------------------- запуск

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="не записывать файл")
    ap.add_argument("--fixtures", help="офлайн-снимок ответов API (json / json.gz)")
    ap.add_argument("--date", help="«сегодня» в формате YYYY-MM-DD (для снимков)")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()

    now = datetime.now(TZ)
    today = a.date or now.strftime("%Y-%m-%d")
    tomorrow = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    src = Fixtures(a.fixtures, today) if a.fixtures else Source()
    try:
        lists, listed, hidden, details, failed = collect(src, today, tomorrow)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1

    stops, stops_failed = ({}, []) if failed else collect_stops(src, details, today)
    regions, stations, routes = build(lists, listed, hidden, details, today, tomorrow, stops)
    named, unknown = apply_name_overrides(stations)
    problems = find_problems(routes, today, tomorrow)
    n_trains = sum(len(r["trains"]) for r in routes)

    print(f"Маршрутов: {len(routes)} (в списке {len(listed)}, скрытых {len(hidden)}), "
          f"рейсов: {n_trains}, даты: {today}, {tomorrow}")

    if failed:
        print(f"✗ Не получено {len(failed)} ответов — файл не трогаю:")
        for f in failed:
            print("   ", f)
        return 1

    if stops_failed:
        # Мягкий отказ: без остановок рейс просто не раскрывается на
        # странице, расписанию это не мешает — файл всё равно пишем.
        print(f"\n! Остановки: не получено {len(stops_failed)} ответов "
              f"— эти рейсы без раскрытия маршрута:")
        for f in stops_failed[:10]:
            print("   ", f)
        if len(stops_failed) > 10:
            print(f"    … и ещё {len(stops_failed) - 10}")

    prev = None
    if os.path.exists(a.out):
        try:
            with open(a.out, encoding="utf-8") as f:
                prev = json.load(f)
        except ValueError:
            prev = None
    if prev and prev.get("routes"):
        # Считаем только маршруты из общего списка: скрытые добираются
        # перебором id и не должны маскировать усохший список.
        was = sum(1 for r in prev["routes"] if r.get("listed", True))
        was_tr = sum(len(r["trains"]) for r in prev["routes"] if r.get("listed", True))
        now_tr = sum(len(r["trains"]) for r in routes if r["listed"])
        if len(listed) < was * MIN_SHARE or now_tr < was_tr * MIN_SHARE:
            print(f"✗ Резкое падение: маршрутов {was} → {len(listed)}, рейсов {was_tr} → {now_tr}. "
                  f"Похоже на сбой источника — файл не трогаю.")
            return 1

    names = {r["id"]: r["name"]["ru"] for r in routes}
    sev_mark = {"critical": "✗", "warn": "!", "info": "·"}
    if problems:
        print(f"\nПроблемы ({len(problems)}):")
        for p in problems:
            print(f"  {sev_mark[p['severity']]} [{p['route']:>2}] {names.get(p['route'], '?')}: "
                  f"{problem_text_ru(p, names)}")

    if named or unknown:
        print(f"\nНазвания станций: подставлено {len(named)}"
              + (f", нет в данных: {', '.join(unknown)}" if unknown else ""))

    doc = {
        "updated": now.isoformat(timespec="seconds"),
        "source": "https://ticket.elektropoyezd.uz/schedule",
        "today": today,
        "tomorrow": tomorrow,
        # 1 = понедельник … 7 = воскресенье (ISO). Источник это нигде
        # не пишет; вывод из того, что у Нукус–Ургенч особая нитка по
        # «4», а четверг там и правда особый.
        "day_numbering": "iso",
        "regions": sorted(regions.values(), key=lambda r: r["id"]),
        "stations": stations,
        "routes": routes,
        "problems": problems,
    }

    if a.dry_run:
        print("\n(dry-run, файл не записан)")
        return 0

    body = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    old = None
    if prev is not None:
        # Метка времени меняется каждый час; коммитить есть смысл,
        # только если поменялись сами данные.
        p2 = dict(prev); p2.pop("updated", None)
        d2 = dict(doc); d2.pop("updated", None)
        old = json.dumps(p2, ensure_ascii=False, sort_keys=True) == json.dumps(d2, ensure_ascii=False, sort_keys=True)
    if old:
        print("\nДанные не изменились.")
        return 0
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"\n✓ Записано {a.out} ({len(body) // 1024} КБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
