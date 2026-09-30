#!/usr/bin/env python3
"""
Сводка по data/buses_log.jsonl: как часто и за сколько часов до
отправления меняются автобусные рейсы. Только читает.

    python3 buses_stats.py
    python3 buses_stats.py data/buses_log.jsonl
"""

import json
import sys
from collections import Counter, defaultdict
from statistics import median

LOG = sys.argv[1] if len(sys.argv) > 1 else "data/buses_log.jsonl"

BUCKETS = [(-1e9, 0, "уже ушёл"), (0, 2, "0–2 ч"), (2, 6, "2–6 ч"),
           (6, 24, "6–24 ч"), (24, 48, "24–48 ч"), (48, 1e9, "больше 48 ч")]


def bucket(h):
    for lo, hi, name in BUCKETS:
        if lo <= h < hi:
            return name
    return "?"


def table(title, rows_by_label):
    """rows_by_label: {подпись: Counter(корзина -> число)}."""
    print(f"\n{title}")
    names = [b[2] for b in BUCKETS]
    print("  " + " " * 22 + "".join(f"{n:>13}" for n in names))
    for label, c in rows_by_label.items():
        print(f"  {label:22}" + "".join(f"{c.get(n, 0):>13}" for n in names))


runs = []
with open(LOG, encoding="utf-8") as f:
    for line in f:
        if line.strip():
            runs.append(json.loads(line))
if not runs:
    sys.exit("журнал пуст")

work = [r for r in runs if not r.get("first")]
print(f"Прогонов: {len(runs)} (сравнений: {len(work)}), "
      f"с {runs[0]['ts'][:16]} по {runs[-1]['ts'][:16]}")
if len(runs) > 1:
    from datetime import datetime
    ts = [datetime.fromisoformat(r["ts"]) for r in runs]
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(ts, ts[1:])]
    print(f"Интервал между прогонами: медиана {median(gaps):.1f} ч, "
          f"максимум {max(gaps):.1f} ч (пропуски — упавшие или задержанные запуски)")
if not work:
    sys.exit("пока только базовый прогон, сравнивать не с чем")

events = []            # (прогон, событие)
for r in work:
    for e in r["events"]:
        events.append((r, e))

# ---- 1. что вообще происходит
kinds = Counter()
for _, e in events:
    kinds[e["t"] if e["t"] != "gone" else f"gone/{e['why']}"] += 1
print("\nСобытия всего:", dict(kinds) or "нет")
fields = Counter(e["f"] for _, e in events if e["t"] == "chg")
print("Изменения по полям:", dict(fields) or "нет")
print(f"Мест (sold_seats) менялось в среднем у "
      f"{sum(r.get('sold_changed', 0) for r in work) / len(work):.0f} рейсов за прогон")

# ---- 2. за сколько часов до отправления
rows = defaultdict(Counter)
for _, e in events:
    if e["t"] == "chg":
        if e["f"] == "platform":
            rows["платформа: назначена" if e["o"] is None else "платформа: сменена"][bucket(e["lead"])] += 1
        else:
            rows[f"изменено: {e['f']}"][bucket(e["lead"])] += 1
    elif e["t"] == "add":
        rows["новый рейс"][bucket(e["lead"])] += 1
    elif e["t"] == "gone" and e["why"] == "vanished":
        rows["рейс пропал (не ушёл)"][bucket(e["lead"])] += 1
if rows:
    table("За сколько часов до отправления это видно (запас на момент прогона):", rows)

# ---- 3. убирает ли источник ушедшие рейсы
dep = [e for _, e in events if e["t"] == "gone" and e["why"] == "departed"]
van = [e for _, e in events if e["t"] == "gone" and e["why"] == "vanished"]
print(f"\nПропало рейсов после времени отправления: {len(dep)}"
      + (f", медианно через {-median(e['lead'] for e in dep):.1f} ч после отправления"
         if dep else "")
      + f"; пропало до отправления: {len(van)}")

# ---- 4. в какие часы бывают изменения
by_hour = defaultdict(lambda: [0, 0])
for r in work:
    h = int(r["ts"][11:13])
    real = [e for e in r["events"]
            if e["t"] in ("add", "chg") or (e["t"] == "gone" and e["why"] == "vanished")]
    by_hour[h][0] += 1
    by_hour[h][1] += bool(real)
print("\nЧас по Ташкенту: прогонов / из них с изменениями")
for h in sorted(by_hour):
    n, k = by_hour[h]
    print(f"  {h:02d}:xx  {n:3} / {k:3}")

# ---- 5. что пропустил бы запуск раз в сутки
first_of_day = {}
for r in work:
    d = r["ts"][:10]
    if d not in first_of_day or r["ts"] < first_of_day[d]:
        first_of_day[d] = r["ts"]
live = [(r, e) for r, e in events
        if e["lead"] > 0 and (e["t"] in ("add", "chg")
                              or (e["t"] == "gone" and e["why"] == "vanished"))]
late = [(r, e) for r, e in live if r["ts"] != first_of_day[r["ts"][:10]]]
print(f"\nАктуальных изменений (рейс ещё не ушёл): {len(live)}; "
      f"из них замечены не в первом прогоне суток: {len(late)}")
if late:
    c = Counter(e["f"] if e["t"] == "chg" else e["t"] for _, e in late)
    print("  раз в сутки после полуночи их не увидели бы до следующей ночи:", dict(c))
    soon = sum(1 for _, e in late if e["lead"] < 6)
    print(f"  из них с отправлением менее чем через 6 ч: {soon}")

# ---- 6. регионы и качество
reg = defaultdict(list)
for r in work:
    for k, v in r["reg"].items():
        reg[k].append(v)
print("\nРейсов по регионам за прогон (мин / макс / прогонов с нулём):")
for k, v in sorted(reg.items(), key=lambda x: -max(x[1])):
    print(f"  {k:6} {min(v):4} / {max(v):4} / {sum(1 for x in v if x == 0):3}")
uq = defaultdict(list)
for r in work:
    for k, v in (r.get("uniq") or {}).items():
        uq[k].append(v)
if uq:
    print("\nСколько рейсов регион дал впервые, после опрошенных раньше (мин / макс за прогон);")
    print("если у всех, кроме первого, нули — хватит одного запроса:")
    for k, v in uq.items():
        print(f"  {k:6} {min(v):4} / {max(v):4}")
print(f"\nКонфликтов между регионами (один рейс, разные данные): "
      f"{sum(r.get('conflicts', 0) for r in work)}")
dirs = {json.dumps(r.get('dir'), sort_keys=True) for r in runs}
print(f"Справочник (регионов/станций) принимал значения: {sorted(dirs)}")
print(f"Время прогона: медиана {median(r.get('sec', 0) for r in runs):.0f} с, "
      f"максимум {max(r.get('sec', 0) for r in runs):.0f} с")
