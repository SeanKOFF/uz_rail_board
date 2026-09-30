#!/usr/bin/env python3
"""
Наблюдение за междугородними автобусами — wapi.avtoticket.uz.

Это не сборщик для страницы, а измерительный прибор на неделю-две:
он отвечает на вопрос «как часто и за сколько часов до отправления
меняется расписание», от которого зависит частота настоящего сборщика.

Каждый прогон опрашивает рейсы из Ташкента (регион 1726) во все регионы
«куда» на три дня вперёд, склеивает их по (api_id, id) и сравнивает
с прошлым прогоном. Пишет только два файла:

    data/buses_state.json   состояние после последнего прогона
    data/buses_log.jsonl    по строке на прогон; внутри — события:
                            add   — рейс появился в уже видимом окне
                            gone  — пропал (why: departed — время отправления
                                    уже прошло; vanished — ещё нет)
                            chg   — изменилось поле (f: старое o → новое n)
                            lead  — часов до отправления в момент прогона

    python3 buses_watch.py                  # снять и записать
    python3 buses_watch.py --dry-run        # снять, ничего не записывать
    python3 buses_watch.py --fixtures DIR   # офлайн: DIR/locations.json,
                                            #   DIR/region_<код>.json

Данные Яндекса и других разделов не трогает. Если хоть один запрос не
прошёл или число рейсов не сходится со счётчиком источника, ничего не
пишется и скрипт завершается с ошибкой: неполный снимок породил бы
фальшивые события «пропал».
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

BASE = "https://wapi.avtoticket.uz/api"
STATE = "data/buses_state.json"
LOG = "data/buses_log.jsonl"
UA = "tabloda.uz/1.0 (+https://tabloda.uz)"
TZ = timezone(timedelta(hours=5))  # Ташкент, перевода часов нет

FROM = 1726                        # город Ташкент
# Все регионы «куда» из справочника. Пустые тоже опрашиваем: неделя
# наблюдений покажет, бывает ли в них что-то в другие часы.
REGIONS = [1718, 1712, 1708, 1706, 1710, 1722, 1733, 1735, 1724, 1727, 9900]
DAYS = 3                           # один запрос отдаёт рейсы на три дня
PAUSE = 0.3                        # между запросами: чужой сервер
TIMEOUT = 30
MIN_SHARE = 0.8                    # меньше 80% рейсов будущего дня — сбой источника
MIN_FOR_GUARD = 10                 # проверка включается, если в прошлый раз было не меньше

# Поля, изменение которых считается событием. sold_seats сюда не входит:
# места меняются каждую минуту, считаем их отдельно числом.
WATCH = ("dep", "route", "carrier", "platform", "status", "bus", "route_id")


# ---------------------------------------------------------------- сеть

class Source:
    """Живой API. Возвращает (HTTP-код, json | None)."""

    def get(self, path):
        return self._call(path, None)

    def trips(self, to, date):
        return self._call("/api-trips",
                          {"date": date, "from": FROM, "to": to, "days": DAYS})

    def _call(self, path, body):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Как в разведочном скрипте: с этими заголовками API отвечал.
            # Обходятся ли они без них, не проверялось.
            "Origin": "https://avtoticket.uz",
            "Referer": "https://avtoticket.uz/",
        }, method="POST" if data else "GET")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    raw = r.read().decode("utf-8")
                    status = r.status
                time.sleep(PAUSE)
                return status, json.loads(raw)
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code != 429:
                    return e.code, None
            except (urllib.error.URLError, TimeoutError, ValueError, OSError):
                pass
            time.sleep(2 * (attempt + 1))
        return 0, None


class Fixtures:
    """Офлайн-снимок для проверки без сети."""

    def __init__(self, folder):
        self.folder = folder

    def get(self, path):
        return self._load("locations.json")

    def trips(self, to, date):
        return self._load(f"region_{to}.json")

    def _load(self, name):
        p = os.path.join(self.folder, name)
        if not os.path.exists(p):
            return 404, None
        with open(p, encoding="utf-8") as f:
            return 200, json.load(f)


# ---------------------------------------------------------------- разбор

def parse(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def hm(s):
    """'2026-10-01 07:30:00' | '2026-10-01 07:30' -> '2026-10-01 07:30'."""
    if not s:
        return None
    s = str(s).replace("T", " ")[:16]
    try:
        parse(s)
    except ValueError:
        return None
    return s


def norm(t):
    dep = hm(t.get("departure_at"))
    if not dep:
        return None
    plat = t.get("platform")
    return {
        "d": str(t.get("trip_date") or dep[:10]),
        "dep": dep,
        "route": t.get("route_name_ru"),
        "carrier": t.get("transporter_name"),
        "platform": None if plat in (None, "") else str(plat),
        "status": t.get("status"),
        "bus": t.get("bus_model_name"),
        "route_id": t.get("route_id"),
        "seats": t.get("seats"),
        "sold": t.get("sold_seats"),
        "api": t.get("api_id"),
    }


def collect(src, dates):
    """-> (рейсы по ключу, число записей по регионам, конфликтов, проблемы,
    сколько рейсов впервые пришло из каждого региона)."""
    trips, region_n, problems, conflicts = {}, {}, [], 0
    first_from = {}
    hinted = False
    for to in REGIONS:
        status, js = src.trips(to, dates[0])
        if status != 200 or not isinstance(js, dict) \
                or not isinstance(js.get("data"), list):
            hint = ""
            if status == 403 and not hinted:
                hint = " (источник, возможно, закрыт для адресов GitHub Actions)"
                hinted = True
            problems.append(f"регион {to}: ответ не получен, HTTP {status}{hint}")
            continue
        by_name = {d.get("name"): d for d in js["data"]}
        n = 0
        for dt in dates:
            day = by_name.get(dt)
            if day is None:
                problems.append(f"регион {to}: в ответе нет дня {dt}")
                continue
            rows = day.get("trips") or []
            if len(rows) != (day.get("count") or 0):
                problems.append(f"регион {to}, {dt}: count={day.get('count')}, "
                                f"а trips={len(rows)}")
                continue
            for t in rows:
                rec = norm(t)
                if rec is None or t.get("id") is None:
                    problems.append(f"регион {to}, {dt}: рейс без id или времени")
                    continue
                key = f"{t.get('api_id')}:{t['id']}"
                n += 1
                old = trips.get(key)
                if old is None:
                    trips[key] = rec
                    first_from[key] = to
                elif any(old[f] != rec[f] for f in WATCH):
                    conflicts += 1      # один рейс, разные данные в разных регионах
        region_n[str(to)] = n
    uniq = Counter(str(first_from[k]) for k in trips)
    return (trips, region_n, conflicts, problems,
            {str(r): uniq.get(str(r), 0) for r in REGIONS})


def check_against_prev(cur, prev, today, force):
    """Рейсов будущих суток не может вдруг стать заметно меньше."""
    problems = []
    if not prev or force:
        return problems
    was, now = Counter(), Counter()
    for t in prev["trips"].values():
        was[t["d"]] += 1
    for t in cur["trips"].values():
        now[t["d"]] += 1
    for d, n in was.items():
        # При малых числах процент ничего не значит: 1 из 2 — это не сбой.
        if n >= MIN_FOR_GUARD and d > today and d in cur["window"] \
                and now[d] < MIN_SHARE * n:
            problems.append(f"{d}: рейсов {now[d]} вместо {n} в прошлом прогоне "
                            f"(--force, если это не сбой)")
    return problems


# ---------------------------------------------------------------- сравнение

def ev(kind, key, rec, now):
    lead = (parse(rec["dep"]) - now).total_seconds() / 3600
    return {"t": kind, "k": key, "d": rec["d"], "dep": rec["dep"][11:],
            "lead": round(lead, 1)}


def diff(prev, cur, now):
    events, entered, sold_changed = [], Counter(), 0
    old_window, new_window = set(prev["window"]), set(cur["window"])

    for k, c in cur["trips"].items():
        p = prev["trips"].get(k)
        if p is None:
            if c["d"] in old_window:
                events.append(ev("add", k, c, now))
            else:
                entered[c["d"]] += 1    # сутки только вошли в окно, это не новизна
            continue
        for f in WATCH:
            if p[f] != c[f]:
                events.append({**ev("chg", k, c, now),
                               "f": f, "o": p[f], "n": c[f]})
        if p["sold"] != c["sold"]:
            sold_changed += 1

    for k, p in prev["trips"].items():
        if k in cur["trips"] or p["d"] not in new_window:
            continue                    # сутки вышли из окна, пропажей это не считаем
        why = "departed" if parse(p["dep"]) <= now else "vanished"
        events.append({**ev("gone", k, p, now), "why": why})

    return events, dict(entered), sold_changed


# ---------------------------------------------------------------- файлы

def load_state(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_state(path, state):
    """Один рейс на строку: git-diff читаемый, а не одна простыня."""
    items = sorted(state["trips"].items())
    lines = ["{",
             f'"ts": {json.dumps(state["ts"])},',
             f'"window": {json.dumps(state["window"])},',
             '"trips": {']
    for i, (k, v) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f'{json.dumps(k)}: '
                     f'{json.dumps(v, ensure_ascii=False, separators=(",", ":"))}{comma}')
    lines += ["}", "}"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def append_log(path, run):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(run, ensure_ascii=False, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixtures", metavar="DIR")
    ap.add_argument("--now", help="ГГГГ-ММ-ДД ЧЧ:ММ по Ташкенту, для проверки без сети")
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--force", action="store_true",
                    help="писать, даже если рейсов будущих суток заметно меньше")
    a = ap.parse_args()

    t0 = time.time()
    now = parse(a.now) if a.now else \
        datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)
    today = now.date()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(DAYS)]
    src = Fixtures(a.fixtures) if a.fixtures else Source()
    print(f"Прогон {now:%Y-%m-%d %H:%M} (Ташкент), окно {dates[0]} … {dates[-1]}")

    # Справочник нужен только как контроль: не появились ли регионы.
    dirinfo = None
    status, loc = src.get("/api-locations")
    if status == 200 and isinstance(loc, dict):
        root = loc.get("data", loc)
        to = root.get("to") if isinstance(root, dict) else None
        if isinstance(to, dict):
            dirinfo = {"regions": len(to.get("locations") or []),
                       "stations": len(to.get("stations") or [])}
    else:
        print(f"  справочник не получен (HTTP {status}), на снимок это не влияет")

    trips, region_n, conflicts, problems, uniq = collect(src, dates)
    cur = {"ts": now.strftime("%Y-%m-%dT%H:%M+05:00"),
           "window": dates, "trips": trips}
    prev = load_state(a.state)
    problems += check_against_prev(cur, prev, today.isoformat(), a.force)

    if problems:
        for p in problems:
            print(f"✗ {p}")
        print("Снимок неполный, ничего не записано.")
        sys.exit(1)

    per_date = Counter(t["d"] for t in trips.values())
    run = {"ts": cur["ts"], "dates": dates,
           "n": {d: per_date.get(d, 0) for d in dates},
           "reg": region_n, "uniq": uniq, "conflicts": conflicts, "dir": dirinfo}
    if prev is None:
        run["first"] = True
        run["events"] = []
        print("Первый прогон: состояния для сравнения ещё нет.")
    else:
        events, entered, sold_changed = diff(prev, cur, now)
        run.update({"events": events, "entered": entered,
                    "sold_changed": sold_changed})
    run["sec"] = round(time.time() - t0, 1)

    ev_count = Counter(e["t"] if e["t"] != "gone" else f"gone/{e['why']}"
                       for e in run["events"])
    chg_count = Counter(e["f"] for e in run["events"] if e["t"] == "chg")
    print(f"Рейсов: {len(trips)} ({', '.join(f'{d[5:]}: {n}' for d, n in run['n'].items())}), "
          f"регионов опрошено: {len(region_n)}, конфликтов между регионами: {conflicts}")
    print("По регионам: " + ", ".join(f"{k}: {v}" for k, v in region_n.items()))
    print("Из них рейсов, впервые пришедших из региона: "
          + ", ".join(f"{k}: {v}" for k, v in uniq.items()))
    print(f"События: {dict(ev_count) or 'нет'}; изменились поля: {dict(chg_count) or 'нет'}; "
          f"мест изменилось у {run.get('sold_changed', 0)} рейсов")

    if a.dry_run:
        print("--dry-run: файлы не тронуты.")
        return
    os.makedirs(os.path.dirname(a.state) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(a.log) or ".", exist_ok=True)
    append_log(a.log, run)          # сначала журнал: лучше дубль события, чем потеря
    dump_state(a.state, cur)
    print(f"Записано: {a.state}, {a.log}")


if __name__ == "__main__":
    main()
