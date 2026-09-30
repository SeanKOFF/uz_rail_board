#!/usr/bin/env python3
"""
Наблюдение за свободными местами в скоростных поездах — eticket.uzrailpass.uz.
Афросиёб (Ташкент, Самарканд, Бухара) и Джалолиддин Мангуберди (Ургенч, Хива).

Это не сборщик для страницы, а измерительный прибор на неделю-две. Он
отвечает на вопросы, от которых зависят «охотник за билетами» и календарь
заполненности:

    - как часто в распроданном поезде всплывают места (возвраты), сколько
      их и как долго они держатся;
    - за сколько дней до отправления поезд уходит в ноль;
    - на сколько дней вперёд и во сколько открывается продажа новой даты.

Запускается в Actions раз в 20 минут (seats-watch.yml). За прогон
опрашивает только «созревшие» пары «направление + дата» (ярусы TIERS),
сравнивает с прошлым состоянием и пишет два файла:

    data/seats_state.json   состояние: когда опрошена каждая пара и
                            сколько мест у каждого поезда ({} — мест нет)
    data/seats_log.jsonl    по строке на прогон; events — изменения мест:
                            [ключ, место, было, стало], было = null —
                            поезд увиден впервые, место "" — впервые
                            увиден без мест

Ключ поезда: "откуда|куда|дата|номер|ЧЧ:ММ". Место: car:<тип вагона>
(число мест вагона — итог считается по нему) или cls:<класс> (разбивка
по классам: у Афросиёба 1С бизнес, 2Е эконом).

    python3 seats_watch.py              # снять и записать
    python3 seats_watch.py --dry-run    # снять, ничего не записывать
    python3 seats_watch.py --probe      # один запрос Ташкент→Самарканд на +10 дней
    python3 seats_watch.py --plan       # ярусы и нагрузка, без сети

Неполный снимок не пишется: если не прошло больше половины запросов,
скрипт завершается с ошибкой и ничего не меняет. На HTTP 403 или 429 —
тоже выход без записи (код 3): это похоже на блокировку. Отдельный
запрос, вернувший пустой список там, где раньше были поезда, считается
сбоем и не применяется.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://eticket.uzrailpass.uz/api/v3/handbook/trains/list"
STATE = "data/seats_state.json"
LOG = "data/seats_log.jsonl"
UA = "tabloda.uz/1.0 (+https://tabloda.uz)"
TZ = timezone(timedelta(hours=5))  # Ташкент, перевода часов нет
# Скоростные бренды, подстрока без учёта регистра. В ответе 30.09:
# "Afrosiyob" и "Jaloliddin Manguberdi" (752Ж). До 01.10 писался только
# Афросиёб, поэтому направления с Ургенчем в журнале стартуют позже.
BRANDS = ("afrosiyob", "manguberdi")
PAUSE = 0.5                        # между запросами: чужой сервер
TIMEOUT = 20
MAX_REQUESTS = 260                 # потолок за прогон, ~4 минуты
EXIT_BLOCKED = 3

CITIES = ["Ташкент", "Самарканд", "Бухара", "Ургенч"]
MAIN = [("Ташкент", "Самарканд"), ("Самарканд", "Ташкент"),
        ("Ташкент", "Бухара"), ("Бухара", "Ташкент")]
ALL = [(a, b) for a in CITIES for b in CITIES if a != b]
OTHER = [p for p in ALL if p not in MAIN]

# (направления, дни от сегодня [от, до), минимальный интервал, мин).
# Интервалы чуть меньше номинала (20 мин, час, 2 часа): запуски Actions
# по расписанию плавают на минуты, и пара не должна пропускать прогон.
TIERS = [
    (MAIN, 0, 14, 15),     # каждый прогон — возвраты
    (OTHER, 0, 14, 55),    # раз в час — с Ургенчем 1–2 поезда в день
    (ALL, 14, 62, 115),    # раз в 2 часа — распродажа и открытие (~60 дней)
]


def station_codes():
    with open("stations.json", encoding="utf-8") as f:
        return {s["ru"]: s["code"] for s in json.load(f)["stations"]}


# ---------------------------------------------------------------- сеть

class Blocked(Exception):
    pass


NOTES = []   # необычные ответы — печатаются в лог прогона, чтобы разобрать на данных


def fetch(dep, arv, date):
    """-> (HTTP-код, список поездов | None, ошибка | None)"""
    body = json.dumps({
        "directions": {"forward": {"date": date, "depStationCode": dep, "arvStationCode": arv}},
        "routeType": "INTERCITY",
    }).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": "ru",      # без него 400 «Required header 'Accept-Language'»
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Blocked(f"HTTP {e.code}")
        return e.code, None, e.read()[:200].decode("utf-8", "replace")
    except Exception as e:
        return 0, None, repr(e)[:200]
    try:
        j = json.loads(raw)
    except ValueError:
        return code, None, "не JSON: " + raw[:200].decode("utf-8", "replace")
    if not j.get("data"):
        return code, None, json.dumps(j.get("error"), ensure_ascii=False)[:200]
    try:
        fwd = (j["data"].get("directions") or {}).get("forward")
        if fwd is None:
            # 30.09 в Actions пришёл ответ без directions.forward. Предположение:
            # так источник отвечает, когда поездов на дату нет. Не проверено —
            # сырой ответ уходит в лог. Считаем пустым списком; если раньше
            # поезда на эту пару были, run() отбракует ответ как сбой.
            NOTES.append(f"{dep}→{arv} {date}: нет forward, ответ: "
                         + raw[:300].decode("utf-8", "replace"))
            return code, [], None
        return code, fwd.get("trains") or [], None
    except (AttributeError, TypeError) as e:
        return code, None, f"неожиданная структура ({e!r}): " + raw[:200].decode("utf-8", "replace")


def seats_of(train):
    """{'car:Сидячий': 119, 'cls:1С': 14, 'cls:2Е': 105}; нули не хранятся.
    Распроданный поезд источник не убирает, а отдаёт с cars: [] → {}."""
    s = {}
    for c in train.get("cars") or []:
        k = "car:" + str(c.get("type", "?"))
        s[k] = s.get(k, 0) + int(c.get("freeSeats") or 0)
        for tf in c.get("tariffs") or []:
            k2 = "cls:" + str(tf.get("classServiceType", "?"))
            s[k2] = s.get(k2, 0) + int(tf.get("freeSeats") or 0)
    return {k: v for k, v in s.items() if v}


def train_key(task, t):
    return f"{task}|{t.get('number', '')}|{(t.get('departureDate') or '')[11:16]}"


# ---------------------------------------------------------------- расписание

def plan(codes, today):
    """-> {"откуда|куда|дата": интервал в секундах}; при пересечении — меньший."""
    tasks = {}
    for pairs, lo, hi, minutes in TIERS:
        for a, b in pairs:
            for i in range(lo, hi):
                k = f"{codes[a]}|{codes[b]}|{(today + timedelta(days=i)).isoformat()}"
                tasks[k] = min(tasks.get(k, minutes * 60), minutes * 60)
    return tasks


def pick(tasks, polled, now):
    """Созревшие задачи, самые просроченные относительно интервала — первыми."""
    due = [((now - polled.get(k, 0)) / every, k) for k, every in tasks.items()]
    due = sorted((x for x in due if x[0] >= 1), reverse=True)
    return [k for _, k in due[:MAX_REQUESTS]], len(due)


# ---------------------------------------------------------------- сравнение

def diff(state_seats, task, trains):
    """Применить ответ по одной задаче. -> список событий."""
    events = []
    for t in trains:
        brand = str(t.get("brand", "")).lower()
        if not any(b in brand for b in BRANDS):
            continue
        key = train_key(task, t)
        new = seats_of(t)
        old = state_seats.get(key)
        if old is None:
            events += [[key, k, None, v] for k, v in sorted(new.items())] or [[key, "", None, 0]]
        else:
            for k in sorted(set(old) | set(new)):
                if old.get(k, 0) != new.get(k, 0):
                    events.append([key, k, old.get(k, 0), new.get(k, 0)])
        state_seats[key] = new
    return events


def had_trains(state_seats, task):
    prefix = task + "|"
    return any(k.startswith(prefix) for k in state_seats)


# ---------------------------------------------------------------- прогон

def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save(state, line):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, STATE)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")


def run(dry_run):
    codes = station_codes()
    names = {v: k for k, v in codes.items()}
    started = datetime.now(TZ)
    now = int(started.timestamp())
    today = started.date()

    state = load_state()
    first = state is None
    state = state or {"polled": {}, "seats": {}}
    tasks = plan(codes, today)
    todo, n_due = pick(tasks, state["polled"], now)
    print(f"Прогон {started:%d.%m %H:%M}: созрело {n_due} из {len(tasks)}, "
          f"опрашиваю {len(todo)}" + (" (базовый)" if first else ""))
    if not todo:
        print("  ✓ нечего опрашивать, ничего не пишу")
        return

    results, errors, suspicious = {}, [], []
    for i, task in enumerate(todo):
        dep, arv, d = task.split("|")
        try:
            code, trains, err = fetch(dep, arv, d)
        except Blocked as e:
            print(f"  ✗ {e} на {names.get(dep)}→{names.get(arv)} {d} после {i} запросов — "
                  f"похоже на блокировку, ничего не пишу")
            sys.exit(EXIT_BLOCKED)
        if trains is None:
            errors.append((task, code, err))
        elif not trains and had_trains(state["seats"], task):
            suspicious.append(task)
        else:
            results[task] = trains
        time.sleep(PAUSE)

    for note in NOTES[:5]:
        print(f"  · {note}")
    if len(NOTES) > 5:
        print(f"  · ещё {len(NOTES) - 5} ответов без forward")
    for task, code, err in errors[:5]:
        dep, arv, d = task.split("|")
        print(f"  ✗ {names.get(dep)}→{names.get(arv)} {d}: HTTP {code} {err}")
    for task in suspicious[:5]:
        dep, arv, d = task.split("|")
        print(f"  ✗ {names.get(dep)}→{names.get(arv)} {d}: пустой список, а раньше поезда были — пропускаю")
    if todo and len(errors) + len(suspicious) > len(todo) / 2:
        print(f"  ✗ не прошло {len(errors) + len(suspicious)} из {len(todo)} — снимок неполный, ничего не пишу")
        sys.exit(1)

    events = []
    for task, trains in results.items():
        events += diff(state["seats"], task, trains)
        state["polled"][task] = now

    # прошедшие даты больше не нужны
    cut = today.isoformat()
    state["polled"] = {k: v for k, v in state["polled"].items() if k.split("|")[2] >= cut}
    state["seats"] = {k: v for k, v in state["seats"].items() if k.split("|")[2] >= cut}

    appeared = [e for e in events if e[1].startswith("car:") and e[2] == 0 and e[3] > 0]
    line = {"ts": started.isoformat(timespec="seconds"), "polls": len(todo),
            "errors": len(errors) + len(suspicious), "first": first, "events": events}
    print(f"  ✓ ответов {len(results)}, ошибок {len(errors)}, пустых {len(suspicious)}, "
          f"событий {len(events)}, поездов в состоянии {len(state['seats'])}")
    if not first:
        for key, _, _, n in appeared[:20]:
            dep, arv, d, num, tm = key.split("|")
            print(f"  + места: {names.get(dep)}→{names.get(arv)} {d} {num} {tm} — {n}")

    if dry_run:
        print("  --dry-run: ничего не записано")
        return
    save(state, line)


# ---------------------------------------------------------------- утилиты

def cmd_probe():
    codes = station_codes()
    d = (datetime.now(TZ).date() + timedelta(days=10)).isoformat()
    try:
        code, trains, err = fetch(codes["Ташкент"], codes["Самарканд"], d)
    except Blocked as e:
        sys.exit(f"✗ {e}")
    print(f"Ташкент→Самарканд {d}: HTTP {code}" + (f", ошибка: {err}" if err else ""))
    for t in trains or []:
        print(f"  {t.get('number', ''):6} {str(t.get('brand')):22} "
              f"{(t.get('departureDate') or '')[11:]}  {seats_of(t) or 'мест нет'}")


def cmd_plan():
    runs = 72  # раз в 20 минут
    total = 0
    for pairs, lo, hi, minutes in TIERS:
        n = len(pairs) * (hi - lo)
        per_run = n * min(1, 20 / (minutes + 5))
        total += per_run
        print(f"  {len(pairs):2} напр. × дни {lo:2}–{hi - 1:2} = {n:3} задач, "
              f"раз в ~{minutes + 5} мин → ~{per_run:.0f} за прогон")
    print(f"Итого ~{total:.0f} запросов за прогон (потолок {MAX_REQUESTS}), "
          f"~{total * runs:.0f} в сутки при запуске раз в 20 минут")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args()
    if a.probe:
        cmd_probe()
    elif a.plan:
        cmd_plan()
    else:
        run(a.dry_run)


if __name__ == "__main__":
    main()
