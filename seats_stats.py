#!/usr/bin/env python3
"""
Сводка по data/seats_log.jsonl (замер seats_watch.py). Только читает.

    python3 seats_stats.py
    python3 seats_stats.py data/seats_log.jsonl --csv    # + returns.csv, sellouts.csv

Разделы:
  1. Покрытие: прогоны, интервалы между ними, ошибки
  2. Возвраты: у поезда было 0 мест → места появились. Сколько, когда,
     как долго держатся (точность — шаг опроса пары: 20 мин, час, 2 часа)
  3. Распродажа: за сколько дней до отправления поезд уходит в 0
  4. Открытие продаж: на сколько дней вперёд и во сколько открывается дата
"""

import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

TZ = timezone(timedelta(hours=5))
args = [a for a in sys.argv[1:] if not a.startswith("--")]
LOG = args[0] if args else "data/seats_log.jsonl"
CSV = "--csv" in sys.argv

try:
    with open("stations.json", encoding="utf-8") as f:
        NAMES = {s["code"]: s["ru"] for s in json.load(f)["stations"]}
except OSError:
    NAMES = {}

BUCKETS = [(0, 6, "<6 ч"), (6, 24, "6–24 ч"), (24, 72, "1–3 дн"),
           (72, 168, "3–7 дн"), (168, 1e9, ">7 дн")]


def bucket(h):
    return next((n for lo, hi, n in BUCKETS if lo <= h < hi), "уже ушёл")


def med(xs):
    return median(xs) if xs else float("nan")


def parse_key(key):
    dep, arv, d, num, tm = key.split("|")
    dep_ts = datetime.fromisoformat(f"{d}T{tm or '00:00'}").replace(tzinfo=TZ).timestamp()
    return dict(route=f"{NAMES.get(dep, dep)}→{NAMES.get(arv, arv)}", date=d, train=num,
                time=tm, dep_ts=dep_ts)


runs = []
with open(LOG, encoding="utf-8") as f:
    for line in f:
        if line.strip():
            runs.append(json.loads(line))
if not runs:
    sys.exit("журнал пуст")

# ---------------- 1. покрытие
ts = [datetime.fromisoformat(r["ts"]).timestamp() for r in runs]
polls = sum(r["polls"] for r in runs)
errs = sum(r["errors"] for r in runs)
days = max((ts[-1] - ts[0]) / 86400, 1e-9)
print(f"Прогонов: {len(runs)}, с {runs[0]['ts'][:16]} по {runs[-1]['ts'][:16]} ({days:.1f} сут)")
if len(ts) > 1:
    gaps = [(b - a) / 60 for a, b in zip(ts, ts[1:])]
    print(f"Интервал между прогонами: медиана {med(gaps):.0f} мин, максимум {max(gaps):.0f} мин")
print(f"Запросов: {polls} ({polls / days:.0f}/сут), ошибок и пустых: {errs} "
      f"({100 * errs / max(polls, 1):.1f}%)")
if len(runs) < 2:
    sys.exit("пока только базовый прогон, сравнивать не с чем")

# ---------------- восстановление итога мест по времени
seats = defaultdict(dict)        # ключ -> {место: мест}
series = defaultdict(list)       # ключ -> [(ts, итог)]
cls_appear = Counter()
for r, t in zip(runs, ts):
    touched = set()
    for key, place, old, new in r["events"]:
        if place:
            seats[key][place] = new
        else:
            seats.setdefault(key, {})
        touched.add(key)
        if place.startswith("cls:") and old == 0 and new > 0 and not r.get("first"):
            cls_appear[place[4:]] += 1
    for key in touched:
        total = sum(v for k, v in seats[key].items() if k.startswith("car:"))
        s = series[key]
        if not s or s[-1][1] != total:
            s.append((t, total))
last_run = ts[-1]
info = {k: parse_key(k) for k in series}

# ---------------- открытие продаж: дата впервые увидена «за горизонтом»
# (0 мест, >=58 дней), первое появление мест на ней — открытие, не возврат
opens, open_ev = [], set()
for key, s in series.items():
    i = info[key]
    if s[0][1] == 0 and (i["dep_ts"] - s[0][0]) / 86400 >= 58:
        fp = next((x for x in s if x[1] > 0), None)
        if fp:
            opens.append((fp[0], (i["dep_ts"] - fp[0]) / 86400))
            open_ev.add((key, fp[0]))

# ---------------- 2. возвраты
returns = []
for key, s in series.items():
    i = info[key]
    for j in range(1, len(s)):
        (_, ptot), (t, tot) = s[j - 1], s[j]
        if ptot == 0 and tot > 0 and (key, t) not in open_ev and t < i["dep_ts"]:
            end = next((x[0] for x in s[j + 1:] if x[1] == 0), None)
            dur = ((end or min(last_run, i["dep_ts"])) - t) / 60
            returns.append(dict(route=i["route"], date=i["date"], train=i["train"], time=i["time"],
                                ts=t, seats=tot, dur_min=round(dur), closed=end is not None,
                                hours_before=(i["dep_ts"] - t) / 3600))

print(f"\nВозвраты (было 0 мест → появились): {len(returns)}, {len(returns) / days:.1f} в сутки")
if returns:
    by = defaultdict(list)
    for r in returns:
        by[r["route"]].append(r)
    print(f"  {'направление':22} {'всего':>6} {'в сут':>6} {'мест, мед.':>10} {'держатся, мед.':>15}")
    for rt, rs in sorted(by.items(), key=lambda x: -len(x[1])):
        closed = [r["dur_min"] for r in rs if r["closed"]]
        print(f"  {rt:22} {len(rs):6} {len(rs) / days:6.1f} {med([r['seats'] for r in rs]):10.0f} "
              f"{med(closed):12.0f} мин")
    c = Counter(bucket(r["hours_before"]) for r in returns)
    print("  До отправления: " + ", ".join(f"{n} — {c[n]}" for _, _, n in BUCKETS))
    hc = Counter(datetime.fromtimestamp(r["ts"], TZ).hour for r in returns)
    print("  По часам (Ташкент): " + " ".join(f"{h:02d}:{hc[h]}" for h in range(24) if hc[h]))
    sc = Counter(min(r["seats"], 5) for r in returns)
    print("  Сколько мест всплывает: " + ", ".join(
        f"{k if k < 5 else '5+'} — {sc[k]}" for k in sorted(sc)))
    print(f"  Ещё не раскуплены: {sum(1 for r in returns if not r['closed'])}")
if cls_appear:
    print("  По классам (0→>0): " + ", ".join(f"{k} — {v}" for k, v in cls_appear.most_common()))

# ---------------- 3. распродажа
sellouts = []
for key, s in series.items():
    i = info[key]
    fp = next((x for x in s if x[1] > 0), None)
    # после отправления источник ещё какое-то время отдаёт поезд — это не распродажа
    fz = fp and next((x for x in s if fp[0] < x[0] < i["dep_ts"] and x[1] == 0), None)
    if fz:
        sellouts.append(dict(route=i["route"], date=i["date"], train=i["train"], time=i["time"],
                             days_before=round((i["dep_ts"] - fz[0]) / 86400, 1)))
print(f"\nРаспродажа: {len(sellouts)} поездо-дат ушли в 0 за время замера")
if sellouts:
    by = defaultdict(list)
    for x in sellouts:
        by[x["route"]].append(x["days_before"])
    for rt, xs in sorted(by.items(), key=lambda x: -len(x[1])):
        print(f"  {rt:22} {len(xs):4} шт., за {med(xs):.1f} дн до отправления (медиана), "
              f"от {min(xs):.1f} до {max(xs):.1f}")
    bt = defaultdict(list)
    for x in sellouts:
        bt[(x["train"], x["time"])].append(x["days_before"])
    print("  По поездам:")
    for (tr, tm), xs in sorted(bt.items(), key=lambda x: -med(x[1])):
        print(f"    {tr:6} {tm}  {len(xs):3} шт., медиана {med(xs):.1f} дн")

# ---------------- 4. открытие продаж
print(f"\nОткрытие продаж: {len(opens)} наблюдений")
if opens:
    dc = Counter(int(x[1]) for x in opens)
    hc = Counter(datetime.fromtimestamp(x[0], TZ).strftime("%H") + ":00" for x in opens)
    print("  Дней вперёд: " + ", ".join(f"{k} — {v}" for k, v in sorted(dc.items())))
    print("  Час появления (Ташкент, точность — 2 часа): " +
          ", ".join(f"{k} — {v}" for k, v in sorted(hc.items())))

if CSV:
    for fn, rows in (("returns.csv", returns), ("sellouts.csv", sellouts)):
        if rows:
            with open(fn, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                for r in rows:
                    r = dict(r)
                    if "ts" in r:
                        r["ts"] = datetime.fromtimestamp(r["ts"], TZ).strftime("%Y-%m-%d %H:%M")
                    w.writerow(r)
            print(f"записан {fn}")
