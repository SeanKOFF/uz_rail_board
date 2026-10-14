#!/usr/bin/env python3
"""
Точечный замер мест на Hetzner: частые опросы только в «окнах», где места
появляются пачками (см. claude/seats-findings.md). Дополняет seats_watch.py
(Actions, раз в 20 минут) и даёт время событий с точностью до минуты.

Окна (время ташкентское):
    mid   23:55–00:40  каждую минуту   4 главных направления, даты +0…+5
    pre   за 150–50 мин до отправления, каждые 2 мин, только пара «направление+дата»
          с таким поездом (главные направления, сегодня и завтра)
    open  06:30–11:30  каждые 5 мин    все 12 направлений, дата +59
    sched раз в час                    главные направления, сегодня и завтра —
          чтобы знать время отправлений для окна pre

Журнал — по файлу на сутки (ташкентская дата), строка на каждый запрос:
    {"ts": "...", "w": "mid", "t": "откуда|куда|дата", "st": 200, "n": 7,
     "ev": [[ключ, место, было, стало], ...]}
Ключи и места — как в seats_watch.py. Пустой ответ там, где поезда были, —
"skip": true, в состояние не применяется.

    python3 seats_windows.py                  # основной цикл (systemd)
    python3 seats_windows.py --plan           # окна и нагрузка, без сети
    python3 seats_windows.py --upload [ДАТА]  # выгрузить журнал за сутки в репозиторий
                                              # (по умолчанию — вчера)

Выгрузка: GitHub Contents API, путь data/seats_windows/ДАТА.jsonl, нужен
GH_TOKEN с правом Contents: Read and write. Без него — ✗ 403, журнал остаётся
на сервере. На HTTP 403/429 от источника — выход с кодом 3 без перезапуска.
"""

import base64
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import seats_watch as W   # fetch, seats_of, diff, had_trains, station_codes — один источник правды

DATA = os.environ.get("STATE_DIRECTORY", os.path.expanduser("~/tabloda-seats"))
STATE = os.path.join(DATA, "state.json")
REPO = os.environ.get("GH_REPO", "SeanKOFF/uz_rail_board")
GH_API = os.environ.get("GH_API", "https://api.github.com")
PAUSE = 0.5
STOP = False


def now():
    return datetime.now(W.TZ)


def mod(t):
    return t.hour * 60 + t.minute


# ---------------------------------------------------------------- окна

def due_tasks(t, codes, sched, last):
    """-> список (окно, задача, интервал_сек) для того, что пора опросить."""
    main = [(codes[a], codes[b]) for a, b in W.MAIN]
    allp = [(codes[a], codes[b]) for a, b in W.ALL]
    m = mod(t)
    want = {}

    def add(w, task, every):
        if task not in want or every < want[task][1]:
            want[task] = (w, every)

    if m >= 23 * 60 + 55 or m <= 40:                       # mid
        base = (t + timedelta(minutes=10)).date()
        for dep, arv in main:
            for i in range(6):
                add("mid", f"{dep}|{arv}|{base + timedelta(days=i)}", 60)

    for task, deps in sched.items():                      # pre
        for hm in deps:
            d = task.split("|")[2]
            dep_t = datetime.fromisoformat(f"{d}T{hm}").replace(tzinfo=W.TZ)
            left = (dep_t - t).total_seconds() / 60
            if 50 <= left <= 150:
                add("pre", task, 120)
                break

    if 6 * 60 + 30 <= m <= 11 * 60 + 30:                  # open
        d = t.date() + timedelta(days=59)
        for dep, arv in allp:
            add("open", f"{dep}|{arv}|{d}", 300)

    for dep, arv in main:                                 # sched
        for i in (0, 1):
            add("sched", f"{dep}|{arv}|{t.date() + timedelta(days=i)}", 3600)

    ts = t.timestamp()
    return sorted(((w, task, every) for task, (w, every) in want.items()
                   if ts - last.get(task, 0) >= every * 0.95),
                  key=lambda x: x[2])                    # частые окна первыми


# ---------------------------------------------------------------- состояние и журнал

def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            s = json.load(f)
        return s.get("seats", {}), s.get("sched", {})
    except FileNotFoundError:
        return {}, {}


def save_state(seats, sched):
    today = now().date().isoformat()
    seats = {k: v for k, v in seats.items() if k.split("|")[2] >= today}
    sched = {k: v for k, v in sched.items() if k.split("|")[2] >= today}
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seats": seats, "sched": sched}, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE)
    return seats, sched


def log(line):
    path = os.path.join(DATA, now().date().isoformat() + ".jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")


def run():
    os.makedirs(DATA, exist_ok=True)
    codes = W.station_codes()
    seats, sched = load_state()
    last, fails, n, last_save, last_report = {}, 0, 0, time.time(), time.time()
    print(f"старт: состояние {len(seats)} поездо-дат, журнал в {DATA}", flush=True)
    while not STOP:
        t = now()
        todo = due_tasks(t, codes, sched, last)
        if not todo:
            time.sleep(5)
            continue
        w, task, _ = todo[0]
        dep, arv, d = task.split("|")
        W.NOTES.clear()
        try:
            code, trains, err = W.fetch(dep, arv, d)
        except W.Blocked as e:
            print(f"✗ {e} на {task} — похоже на блокировку, останавливаюсь", flush=True)
            save_state(seats, sched)
            sys.exit(3)
        last[task] = time.time()
        n += 1
        line = {"ts": now().isoformat(timespec="seconds"), "w": w, "t": task, "st": code}
        if trains is None:
            line["err"] = (err or "")[:200]
            fails += 1
            if fails >= 5:
                print("✗ 5 ошибок подряд — пауза 10 минут", flush=True)
                time.sleep(600)
                fails = 0
        elif not trains and W.had_trains(seats, task):
            line["skip"] = True            # поезда ушли или сбой — состояние не трогаем
        else:
            fails = 0
            line["n"] = len(trains)
            ev = W.diff(seats, task, trains)
            if ev:
                line["ev"] = ev
            deps = sorted({(x.get("departureDate") or "")[11:16] for x in trains
                           if any(b in str(x.get("brand", "")).lower() for b in W.BRANDS)} - {""})
            if task.split("|")[:2] in [[codes[a], codes[b]] for a, b in W.MAIN] and deps:
                sched[task] = deps
        log(line)
        if time.time() - last_save > 600:
            seats, sched = save_state(seats, sched)
            last_save = time.time()
        if time.time() - last_report > 3600:
            print(f"за час: {n} запросов", flush=True)
            n, last_report = 0, time.time()
        time.sleep(PAUSE)
    save_state(seats, sched)
    print("остановлен", flush=True)


# ---------------------------------------------------------------- выгрузка

def gh(method, path, body=None):
    req = urllib.request.Request(GH_API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {os.environ.get('GH_TOKEN', '')}",
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                          "User-Agent": "tabloda.uz-seats/1.0",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"message": e.read().decode("utf-8", "replace")[:300]}


def upload(day):
    src = os.path.join(DATA, f"{day}.jsonl")
    if not os.path.exists(src):
        print(f"✗ нет журнала {src}")
        return False
    raw = open(src, "rb").read()
    path = f"/repos/{REPO}/contents/data/seats_windows/{day}.jsonl"
    code, cur = gh("GET", path + "?ref=main")
    body = {"message": f"Места, точечный замер: {day}",
            "content": base64.b64encode(raw).decode(),
            "branch": "main",
            "committer": {"name": "schedule-bot", "email": "bot@users.noreply.github.com"}}
    if code == 200 and cur.get("sha"):
        body["sha"] = cur["sha"]
    code, res = gh("PUT", path, body)
    if code in (200, 201):
        lines = raw.count(b"\n")
        print(f"✓ выгружено {day}: {len(raw) // 1024} КБ, {lines} строк")
        return True
    hint = {401: "токен неверный или истёк",
            403: "у токена нет права Contents: Read and write",
            404: "токен не видит репозиторий"}.get(code, "")
    print(f"✗ выгрузка {day}: HTTP {code} {hint} {res.get('message', '')[:200]}")
    return False


def cmd_plan():
    print("Окна и примерная нагрузка в сутки:")
    print("  mid    23:55–00:40, раз в минуту, 4 напр. × 6 дат      ~1100")
    print("  pre    за 150–50 мин до отправления, раз в 2 мин       ~1500–2500")
    print("  open   06:30–11:30, раз в 5 мин, 12 напр., дата +59    ~720")
    print("  sched  раз в час, 4 напр. × сегодня/завтра              ~190")
    print("Итого ~3500–4500 запросов в сутки, строго по одному.")


def main():
    def stop(*_):
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    a = sys.argv[1:]
    if a[:1] == ["--plan"]:
        cmd_plan()
    elif a[:1] == ["--upload"]:
        day = a[1] if len(a) > 1 else (now().date() - timedelta(days=1)).isoformat()
        sys.exit(0 if upload(day) else 1)
    else:
        run()


if __name__ == "__main__":
    main()
