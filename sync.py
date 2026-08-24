#!/usr/bin/env python3
"""
Еженедельная сверка расписания.

    python sync.py --seed        первичное наполнение из seed.json
    python sync.py               сверка с источником + экспорт
    python sync.py --dry-run     показать различия, ничего не записывать
    python sync.py --export      только пересобрать data/schedule.json

Логика намеренно консервативная: источник НЕ перезаписывает базу вслепую.
Расхождения сначала попадают в changelog, и только потом применяются.
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DB = ROOT / "schedule.db"
OUT = ROOT / "data" / "schedule.json"
LOG = ROOT / "data" / "changelog.json"

SOURCE_URL = "https://eticket.railway.uz/ru/pages/schedule"
STATION = "tashkent-pass"
TZ = timezone(timedelta(hours=5))          # Ташкент, DST нет

# Если источник вернул меньше этой доли от известных рейсов — считаем,
# что сломалась разметка или сайт отдал заглушку, и НЕ трогаем базу.
SANITY_RATIO = 0.5


# ----------------------------------------------------------------------
# ПАРСЕР. Единственная часть, которую надо подогнать под реальную разметку.
# ----------------------------------------------------------------------

SELECTORS = {
    # Подставить после того, как посмотришь исходник страницы.
    "row": "table tbody tr",
    "cells": "td",
    # Порядок колонок на странице: индексы ячеек.
    "col_number": 0,
    "col_title": 1,
    "col_depart": 2,
    "col_arrive": 3,
    "col_days": 4,
}

BRANDS = [
    (re.compile(r"афросиёб|afrosiyob", re.I), "Афросиёб", True),
    (re.compile(r"шарк|sharq", re.I), "Шарк", False),
]


def classify(text):
    for pattern, name, fast in BRANDS:
        if pattern.search(text):
            return name, fast
    return None, False


def normalize_time(raw):
    """'07:30', '7.30', '07 30' -> '07:30'. Иначе None."""
    m = re.search(r"(\d{1,2})[:.\s](\d{2})", raw or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h < 24 and 0 <= mi < 60):
        return None
    return f"{h:02d}:{mi:02d}"


def scrape():
    """Читает страницу расписания, возвращает список записей."""
    import httpx
    from selectolax.parser import HTMLParser

    headers = {
        # Представляемся честно — так владельцу сайта видно, кто ходит.
        "User-Agent": "tashkent-board/1.0 (weekly schedule sync; contact: you@example.uz)",
        "Accept-Language": "ru",
    }
    resp = httpx.get(SOURCE_URL, headers=headers, timeout=30, follow_redirects=True)
    resp.raise_for_status()

    tree = HTMLParser(resp.text)
    rows = tree.css(SELECTORS["row"])
    out = []

    for row in rows:
        cells = [c.text(strip=True) for c in row.css(SELECTORS["cells"])]
        if len(cells) <= SELECTORS["col_days"]:
            continue

        number = cells[SELECTORS["col_number"]].strip()
        title = cells[SELECTORS["col_title"]].strip()
        if not number or not title:
            continue

        brand, fast = classify(number + " " + title)
        days = re.sub(r"[^1-7]", "", cells[SELECTORS["col_days"]]) or "1234567"

        # Отправление из Ташкента и прибытие в Ташкент — две отдельные записи.
        dep = normalize_time(cells[SELECTORS["col_depart"]])
        arr = normalize_time(cells[SELECTORS["col_arrive"]])

        if dep:
            out.append(dict(number=number, direction="departure", title=title,
                            brand=brand, fast=fast, time_local=dep,
                            days=days, via=None, station=STATION))
        if arr:
            out.append(dict(number=number, direction="arrival", title=f"из {title}",
                            brand=brand, fast=fast, time_local=arr,
                            days=days, via=None, station=STATION))

    return out


# ----------------------------------------------------------------------
# База и сверка
# ----------------------------------------------------------------------

def connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    return conn


def key(r):
    return (r["number"], r["direction"], r.get("station", STATION))


COMPARED = ("title", "brand", "fast", "time_local", "days", "via")


def diff(conn, scraped):
    """Сравнивает выборку с базой. Возвращает (added, changed, removed)."""
    stored = {key(dict(r)): dict(r)
              for r in conn.execute("SELECT * FROM trains WHERE active = 1")}
    fresh = {key(r): r for r in scraped}

    added = [r for k, r in fresh.items() if k not in stored]
    removed = [r for k, r in stored.items() if k not in fresh]
    changed = []

    for k, r in fresh.items():
        if k not in stored:
            continue
        old = stored[k]
        deltas = {f: (old[f], r[f]) for f in COMPARED
                  if (old[f] or None) != (r[f] or None)}
        if deltas:
            changed.append((r, deltas))

    return added, changed, removed


def apply_changes(conn, added, changed, removed, now):
    for r in added:
        conn.execute("""INSERT INTO trains
            (number, direction, title, brand, fast, time_local, days, via,
             station, through, cities, quals, active, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (r["number"], r["direction"], r["title"], r["brand"], int(r.get("fast", 0)),
             r["time_local"], r["days"], r["via"], r.get("station", STATION),
             int(r.get("through", 0)), json.dumps(r.get("cities", []), ensure_ascii=False),
             json.dumps(r.get("quals", []), ensure_ascii=False), now, now))
        log(conn, now, "added", r, f'{r["time_local"]} {r["title"]}')

    for r, deltas in changed:
        conn.execute("""UPDATE trains
            SET title=?, brand=?, fast=?, time_local=?, days=?, via=?, last_seen=?
            WHERE number=? AND direction=? AND station=?""",
            (r["title"], r["brand"], int(r["fast"]), r["time_local"], r["days"],
             r["via"], now, r["number"], r["direction"], r.get("station", STATION)))
        detail = "; ".join(f"{f}: {a} → {b}" for f, (a, b) in deltas.items())
        log(conn, now, "changed", r, detail)

    # Пропавшие рейсы не удаляем — помечаем неактивными.
    # Поезд может исчезнуть из-за сбоя парсинга, а не отмены.
    for r in removed:
        conn.execute("""UPDATE trains SET active = 0, last_seen = ?
            WHERE number=? AND direction=? AND station=?""",
            (now, r["number"], r["direction"], r.get("station", STATION)))
        log(conn, now, "removed", r, f'был {r["time_local"]}')


def log(conn, now, kind, r, detail):
    conn.execute("""INSERT INTO changelog (checked_at, kind, number, direction, detail)
                    VALUES (?,?,?,?,?)""",
                 (now, kind, r["number"], r["direction"], detail))


# ----------------------------------------------------------------------
# Экспорт для статического сайта
# ----------------------------------------------------------------------

def export(conn):
    rows = conn.execute("""SELECT number, direction, title, brand, fast,
                                  time_local, days, via, station, through,
                                  cities, quals
                           FROM trains WHERE active = 1
                           ORDER BY time_local""").fetchall()

    payload = {
        "station": "Ташкент",
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "source": SOURCE_URL,
        "note": "Время по расписанию. Фактические задержки не учитываются.",
        "trains": [dict(r) | {"fast": bool(r["fast"]), "through": bool(r["through"]),
                              "cities": json.loads(r["cities"] or "[]"),
                              "quals": json.loads(r["quals"] or "[]")}
                   for r in rows],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    recent = conn.execute("""SELECT checked_at, kind, number, direction, detail
                             FROM changelog ORDER BY id DESC LIMIT 50""").fetchall()
    LOG.write_text(json.dumps([dict(r) for r in recent], ensure_ascii=False, indent=1),
                   encoding="utf-8")

    return len(rows)


# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="залить seed.json в пустую базу")
    ap.add_argument("--dry-run", action="store_true", help="показать различия, не писать")
    ap.add_argument("--export", action="store_true", help="только пересобрать JSON")
    args = ap.parse_args()

    conn = connect()
    now = datetime.now(TZ).isoformat(timespec="seconds")

    if args.seed:
        seed = json.loads((ROOT / "seed.json").read_text(encoding="utf-8"))
        added, changed, removed = diff(conn, seed)
        apply_changes(conn, added, changed, [], now)
        conn.commit()
        n = export(conn)
        print(f"Сид загружен: {len(added)} добавлено, {len(changed)} обновлено. В выдаче {n}.")
        return

    if args.export:
        print(f"Экспортировано рейсов: {export(conn)}")
        return

    stored_count = conn.execute(
        "SELECT count(*) c FROM trains WHERE active = 1").fetchone()["c"]

    try:
        scraped = scrape()
    except Exception as e:
        conn.execute("INSERT INTO runs (started_at, status, scraped, note) VALUES (?,?,?,?)",
                     (now, "error", 0, str(e)[:300]))
        conn.commit()
        print(f"Источник недоступен: {e}", file=sys.stderr)
        print("База не тронута, сайт продолжает работать на прошлых данных.", file=sys.stderr)
        sys.exit(1)

    # Защита от пустой или сломанной выдачи.
    if stored_count and len(scraped) < stored_count * SANITY_RATIO:
        note = f"получено {len(scraped)} при {stored_count} в базе — похоже на сбой разметки"
        conn.execute("INSERT INTO runs (started_at, status, scraped, note) VALUES (?,?,?,?)",
                     (now, "aborted", len(scraped), note))
        conn.commit()
        print(f"Сверка прервана: {note}", file=sys.stderr)
        print("Проверь SELECTORS в sync.py — вероятно, страница изменилась.", file=sys.stderr)
        sys.exit(2)

    added, changed, removed = diff(conn, scraped)

    print(f"Источник отдал {len(scraped)} рейсов.")
    print(f"Новых: {len(added)}, изменено: {len(changed)}, пропало: {len(removed)}")
    for r in added:
        print(f"  + {r['number']:<6} {r['time_local']} {r['title']}")
    for r, d in changed:
        print(f"  ~ {r['number']:<6} " + "; ".join(f"{f}: {a} → {b}" for f, (a, b) in d.items()))
    for r in removed:
        print(f"  - {r['number']:<6} {r['time_local']} {r['title']}")

    if args.dry_run:
        print("\n--dry-run: изменения не применены.")
        return

    apply_changes(conn, added, changed, removed, now)
    conn.execute("INSERT INTO runs (started_at, status, scraped, note) VALUES (?,?,?,?)",
                 (now, "ok", len(scraped), None))
    conn.commit()
    print(f"\nЭкспортировано рейсов: {export(conn)}")


if __name__ == "__main__":
    main()
