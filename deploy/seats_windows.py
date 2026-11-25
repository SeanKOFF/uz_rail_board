#!/usr/bin/env python3
"""
Точечный замер мест на Hetzner: частые опросы только в «окнах», где места
появляются пачками (см. claude/seats-findings.md). Дополняет seats_watch.py
(Actions, раз в 20 минут) и даёт время событий с точностью до минуты.

Окна (время ташкентское):
    mid   23:55–00:40  дата +4 каждую минуту, даты +0…+3 и +5 каждые 6 мин;
                       6 направлений (WIN)
    pre   за 150–50 мин до отправления, каждые 2 мин, только пара «направление+дата»
          с таким поездом (направления WIN, сегодня и завтра). Отсчёт идёт и от
          отправления с запрошенной станции ("pre"), и от отправления со станции
          начала маршрута поезда ("preo", когда окно только по ней): пачка за ~2 ч
          привязана к станции начала, а не к опрашиваемой
    eve   20:30–23:30  каждые 2 мин    Ургенч⇄Ташкент, завтрашняя дата (751М
          Ургенч→Ташкент 07:40: вечером 17.10 +74, 18.10 +19 мест, живут <1 ч)
    hunt  круглосуточно, каждые 2 мин  пары+даты из подписок subs.json (см. hunter.py),
          только в пределах горизонта продаж; уведомления шлёт тот же цикл
    open  06:30–11:30  каждые 5 мин    все 12 направлений, дата +59
    sched раз в час                    направления WIN, сегодня и завтра —
          чтобы знать время отправлений для окна pre

Журнал — по файлу на сутки (ташкентская дата), строка на каждый запрос:
    {"ts": "...", "w": "mid", "t": "откуда|куда|дата", "st": 200, "n": 7,
     "gap": 3600, "ev": [[ключ, место, было, стало], ...]}
gap — сколько секунд прошло с прошлого опроса этой же пары и даты (нет, если
опроса не было): событие случилось где-то в этом промежутке, а не в момент "ts".
Первый опрос даты «+4 дня» в окне mid идёт с gap ~ 23 ч — это не пачка в 23:55,
а накопившееся за сутки; как точное время такие события не считать.
Ключи и места — как в seats_watch.py. Пустой ответ там, где поезда были, —
"skip": true, в состояние не применяется.

    python3 seats_windows.py                  # основной цикл (systemd)
    python3 seats_windows.py --plan           # окна и нагрузка, без сети
    python3 seats_windows.py --upload [ДАТА]  # выгрузить журнал за сутки в репозиторий
                                              # (по умолчанию — вчера)
    python3 seats_windows.py --cert           # срок действия сертификата источника
    python3 seats_windows.py --fresh          # давно ли в репозитории коммиты Actions
    python3 seats_windows.py --alert-test     # тестовое сообщение в Telegram
    python3 seats_windows.py --who            # кто писал боту: id чатов для subs.json
    python3 seats_windows.py --hunt-status [--send]   # что сейчас по подпискам

Оповещения (TG_TOKEN и TG_CHAT из /opt/tabloda-seats/alert.env; без них — только
строка ⚠ в журнале службы): сертификат источника истекает через <7 дней (раз в
сутки, проверка каждые 6 ч); источник отдаёт неверный/просроченный сертификат
(сразу, после первой серии из 5 ошибок); любая другая недоступность — после 3 серий
подряд (~30 мин; ночное обслуживание в 02:31–02:34 укладывается в одну серию);
«снова отвечает» — когда ответы вернулись. 18.10 сертификат источника был просрочен
с 05:00 до 09:16 по Ташкенту, и мы узнали об этом только из разбора журнала.
Ещё одно оповещение — «Actions молчат»: раз в 10 минут по публичному API GitHub смотрится
возраст последних коммитов data/seats_state.json (Места) и data/flights_live.json
(Аэропорты); если оба старше порогов (3 и 4 ч) — сообщение, потом «снова пошли». Один
файл старше порога — не тревога (так выглядит сбой источника, о нём говорят другие
оповещения). 24.11 с 16:07 коммитов не было больше суток: у GitHub не доходили письма на
привязанный адрес, об остановке узнали сами.

Выгрузка: GitHub Contents API, путь data/seats_windows/ДАТА.jsonl, нужен
GH_TOKEN с правом Contents: Read and write. Без него — ✗ 403, журнал остаётся
на сервере. На HTTP 403/429 от источника — ждём 30 минут и пробуем снова; три такие
остановки подряд (ни одного нормального ответа между ними) — выход с кодом 3 без
перезапуска. 24.11 в 23:59 один 429 останавливал службу сразу, и сутки данных ушли.
"""

import base64
import json
import os
import signal
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from urllib.parse import urlparse

import hunter as H            # подписки и уведомления в Telegram
import seats_watch as W   # fetch, seats_of, diff, had_trains, station_codes — один источник правды

DATA = os.environ.get("STATE_DIRECTORY", os.path.expanduser("~/tabloda-seats"))
STATE = os.path.join(DATA, "state.json")
REPO = os.environ.get("GH_REPO", "SeanKOFF/uz_rail_board")
GH_API = os.environ.get("GH_API", "https://api.github.com")
PAUSE = 0.5
STOP = False
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
CERT_WARN_DAYS = 7
# Свежесть коммитов Actions: (файл, порог в секундах, как назвать). Проверка раз в 10 минут,
# тревога, когда старше порога ОБА файла. Самые длинные паузы за октябрь–ноябрь: Места 1,4 ч
# (4 ч 20.11 — сбой источника), Аэропорты 3 ч.
FRESH = (("data/seats_state.json", 3 * 3600, "Места"),
         ("data/flights_live.json", 4 * 3600, "Аэропорты"))
FRESH_EVERY = 600
# Лимит источника с 24.11 (гипотеза): ~150 запросов с одного адреса за 5–10 минут, 151-й — 429.
# 24.11 в 23:59:02 службу остановили ровно на 151-м (150 за последние 5 и за 10 минут), два прогона
# Actions 25.11 — тоже на 151-м. До 24.11 ~13:30 пики в 190 за 5 минут проходили. Сами держимся
# ниже: не больше LIMIT_N запросов за любые LIMIT_WIN секунд (запас 20% от 150 при окне 10 минут).
LIMIT_N = 120
LIMIT_WIN = 600
BLOCK_WAIT = 1800    # секунд ждать после 403/429 от источника
BLOCK_TRIES = 3      # столько 403/429 подряд (без нормального ответа между) — остановиться, код 3
EVE = [("Ургенч", "Ташкент"), ("Ташкент", "Ургенч")]   # вечернее окно (751М)
# Направления частых окон: главные из seats_watch плюс Ташкент⇄Ургенч (Мангуберди).
# 6 направлений × 6 дат = 36 запросов в минуту в полночь при ~1 с на запрос.
WIN = W.MAIN + [("Ташкент", "Ургенч"), ("Ургенч", "Ташкент")]


def now():
    return datetime.now(W.TZ)


def mod(t):
    return t.hour * 60 + t.minute


# ---------------------------------------------------------------- окна

def due_tasks(t, codes, sched, last, orig=None, hunt=()):
    """-> список (окно, задача, интервал_сек) для того, что пора опросить.
    sched: {задача: [[номер, "ГГГГ-ММ-ДДTЧЧ:ММ" отправления с запрошенной станции], ...]}
    orig:  {"номер|дата": самое раннее известное отправление этого поезда}"""
    orig = orig or {}
    main = [(codes[a], codes[b]) for a, b in WIN]
    allp = [(codes[a], codes[b]) for a, b in W.ALL]
    m = mod(t)
    want = {}

    def add(w, task, every):
        if task not in want or every < want[task][1]:
            want[task] = (w, every)

    if m >= 23 * 60 + 55 or m <= 40:                       # mid
        base = (t + timedelta(minutes=10)).date()
        for dep, arv in main:
            for i in range(6):          # пачка — на дату +4: её каждую минуту, остальные реже
                add("mid", f"{dep}|{arv}|{base + timedelta(days=i)}", 60 if i == 4 else 360)

    def left(iso):
        return (datetime.fromisoformat(iso).replace(tzinfo=W.TZ) - t).total_seconds() / 60

    for task, trs in sched.items():                       # pre
        label = None
        for num, iso in trs:
            if 50 <= left(iso) <= 150:
                label = "pre"
                break
            if 50 <= left(orig.get(f"{num}|{iso[:10]}", iso)) <= 150:
                label = "preo"                            # окно только по станции начала
        if label:
            add(label, task, 120)

    if 20 * 60 + 30 <= m <= 23 * 60 + 30:                 # eve
        d = t.date() + timedelta(days=1)
        for a, b in EVE:
            add("eve", f"{codes[a]}|{codes[b]}|{d}", 120)

    if 6 * 60 + 30 <= m <= 11 * 60 + 30:                  # open
        d = t.date() + timedelta(days=59)
        for dep, arv in allp:
            add("open", f"{dep}|{arv}|{d}", 300)

    for task in hunt:                                     # hunt: подписки
        add("hunt", task, H.HUNT_EVERY)

    for dep, arv in main:                                 # sched
        for i in (0, 1):
            add("sched", f"{dep}|{arv}|{t.date() + timedelta(days=i)}", 3600)

    ts = t.timestamp()
    return sorted(((w, task, every) for task, (w, every) in want.items()
                   if ts - last.get(task, 0) >= every * 0.95),
                  key=lambda x: (x[2], last.get(x[1], 0)))   # частые окна первыми, среди равных — давно не опрошенные


# ---------------------------------------------------------------- оповещения

def over_limit(sent, now_ts):
    """sent — deque времён наших запросов; True, если за последние LIMIT_WIN секунд их уже LIMIT_N."""
    while sent and sent[0] < now_ts - LIMIT_WIN:
        sent.popleft()
    return len(sent) >= LIMIT_N


def alert(text):
    """Строка ⚠ в журнал службы и, если заданы TG_TOKEN и TG_CHAT, сообщение в Telegram."""
    print(f"⚠ {text}", flush=True)
    if not (TG_TOKEN and TG_CHAT):
        return False
    ok, why = H.tg_send(TG_TOKEN, TG_CHAT, "tabloda.uz, замер мест: " + text)
    if not ok:
        print(f"✗ в Telegram не отправлено: {why}", flush=True)
    return ok


def cert_left(host=None, port=443, ctx=None):
    """-> (дней до конца сертификата, строка notAfter). Бросает ssl.SSLCertVerificationError,
    если сертификат уже просрочен или не проходит проверку (проверку не отключаем)."""
    host = host or urlparse(W.API).hostname
    ctx = ctx or ssl.create_default_context()
    with socket.create_connection((host, port), timeout=15) as s:
        with ctx.wrap_socket(s, server_hostname=host) as t:
            na = t.getpeercert()["notAfter"]
    return (ssl.cert_time_to_seconds(na) - time.time()) / 86400, na


def check_cert(warned_day):
    """Печатает срок сертификата; предупреждает не чаще раза в сутки. -> дата последнего предупреждения."""
    try:
        days, na = cert_left()
    except Exception as e:
        print(f"✗ сертификат источника не проверить: {type(e).__name__} {str(e)[:150]}", flush=True)
        return warned_day
    print(f"сертификат источника действует до {na}: {days:.0f} дн", flush=True)
    today = now().date().isoformat()
    if days < CERT_WARN_DAYS and warned_day != today:
        alert(f"сертификат источника ({urlparse(W.API).hostname}) истекает через {days:.0f} дн "
              f"({na}). Если не обновят — замер встанет, пока не обновят.")
        return today
    return warned_day


def repo_age(path):
    """-> секунд с последнего коммита в main, менявшего path (публичный API, без токена);
    None, если GitHub не ответил или лимит запросов — тогда ничего не решаем."""
    url = f"{GH_API}/repos/{REPO}/commits?path={path}&sha=main&per_page=1"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "tabloda.uz-seats/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            rows = json.loads(r.read() or b"[]")
        iso = rows[0]["commit"]["committer"]["date"]
        return time.time() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fmt_age(sec):
    h = sec / 3600
    return f"{h:.1f} ч" if h < 48 else f"{h / 24:.1f} сут"


def check_fresh(was_down):
    """Раз в FRESH_EVERY: оповещает, когда коммиты Actions пропали / вернулись.
    -> новое значение was_down (True — уже сообщили, что молчат)."""
    ages = [(repo_age(path), limit, name) for path, limit, name in FRESH]
    if any(a is None for a, _, _ in ages):
        return was_down
    down = all(a > limit for a, limit, _ in ages)
    detail = ", ".join(f"{name} — {fmt_age(a)}" for a, _, name in ages)
    if down and not was_down:
        alert(f"в репозитории давно нет коммитов Actions ({detail} назад). Проверьте вкладку Actions "
              f"(не отключены ли workflow), токен диспетчера и почту GitHub.")
    elif was_down and not down:
        alert(f"коммиты Actions снова идут ({detail} назад).")
    return down


# ---------------------------------------------------------------- состояние и журнал

def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            s = json.load(f)
        # расписание старого формата (список "ЧЧ:ММ") отбрасываем: через час соберётся заново
        sched = {k: v for k, v in s.get("sched", {}).items()
                 if v and all(isinstance(x, list) for x in v)}
        return s.get("seats", {}), sched, s.get("orig", {}), s.get("last", {}), s.get("empty", {})
    except FileNotFoundError:
        return {}, {}, {}, {}, {}


def save_state(seats, sched, orig, last, empty):
    today = now().date().isoformat()
    seats = {k: v for k, v in seats.items() if k.split("|")[2] >= today}
    sched = {k: v for k, v in sched.items() if k.split("|")[2] >= today}
    orig = {k: v for k, v in orig.items() if k.split("|")[1] >= today}
    last = {k: v for k, v in last.items() if k.split("|")[2] >= today}
    empty = {k: v for k, v in empty.items() if k.split("|")[2] >= today}
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seats": seats, "sched": sched, "orig": orig, "last": last, "empty": empty},
                  f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE)
    return seats, sched, orig, last, empty


def dep_iso(x):
    """departureDate "ДД.ММ.ГГГГ ЧЧ:ММ" -> "ГГГГ-ММ-ДДTЧЧ:ММ" ('' если не разобрать)."""
    try:
        return datetime.strptime(x.get("departureDate") or "", "%d.%m.%Y %H:%M").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return ""


def log(line):
    path = os.path.join(DATA, now().date().isoformat() + ".jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")


def run():
    os.makedirs(DATA, exist_ok=True)
    codes = W.station_codes()
    seats, sched, orig, last, empty = load_state()
    hunter = H.Hunter(codes, TG_TOKEN, alert=alert, owner=TG_CHAT, state_dir=DATA)
    listener = H.Listener(hunter, TG_TOKEN) if TG_TOKEN else None
    if listener:
        listener.start()
    fails, n, last_save, last_report = 0, 0, time.time(), time.time()
    pauses, alerted, out_since, cert_at, cert_warned = 0, False, None, 0, None
    fresh_at, fresh_down = 0, False
    blocks, block_since = 0, None
    sent = deque()                                 # времена запросов для ограничителя нагрузки
    print(f"старт: состояние {len(seats)} поездо-дат, журнал в {DATA}", flush=True)
    while not STOP:
        t = now()
        if time.time() - cert_at > 6 * 3600:
            cert_at = time.time()
            cert_warned = check_cert(cert_warned)
        if time.time() - fresh_at > FRESH_EVERY:
            fresh_at = time.time()
            fresh_down = check_fresh(fresh_down)
        if over_limit(sent, time.time()):             # место в окне освободится само
            time.sleep(5)
            continue
        hunt = hunter.tasks(t.date())
        todo = due_tasks(t, codes, sched, last, orig, hunt)
        if not todo:
            time.sleep(5)
            continue
        w, task, _ = todo[0]
        dep, arv, d = task.split("|")
        W.NOTES.clear()
        t0 = time.time()
        sent.append(t0)
        try:
            code, trains, err = W.fetch(dep, arv, d)
        except W.Blocked as e:
            blocks += 1
            block_since = block_since or time.time()
            code_ = str(e).split()[-1]
            log({"ts": now().isoformat(timespec="seconds"), "w": w, "t": task,
                 "st": int(code_) if code_.isdigit() else 0, "err": "blocked"})
            print(f"✗ {e} на {task} — похоже на блокировку ({blocks} из {BLOCK_TRIES})", flush=True)
            if blocks >= BLOCK_TRIES:
                alert(f"источник отвечает {e} уже {blocks} раза подряд — замер остановлен (код 3). "
                      f"Когда лимит снимется: sudo systemctl restart tabloda-seats")
                save_state(seats, sched, orig, last, empty)
                sys.exit(3)
            if blocks == 1:
                alert(f"источник ответил {e} (похоже на лимит запросов или блокировку) — жду {BLOCK_WAIT // 60} мин "
                      f"и пробую снова")
            for _ in range(max(BLOCK_WAIT // 5, 1)):      # ждём короткими шагами, чтобы слышать остановку
                if STOP:
                    break
                time.sleep(5)
            continue
        if block_since:                       # после 403/429 источник ответил нормально
            alert(f"источник снова отвечает (после {blocks} отказ(ов), "
                  f"{round((time.time() - block_since) / 60)} мин)")
            blocks, block_since = 0, None
        prev, last[task] = last.get(task), t0
        n += 1
        line = {"ts": now().isoformat(timespec="seconds"), "w": w, "t": task, "st": code}
        if prev:
            line["gap"] = round(t0 - prev)
        if trains is not None:                 # источник ответил — серия ошибок кончилась
            if alerted:
                alert(f"источник снова отвечает (перерыв {round((time.time() - out_since) / 60)} мин)")
            pauses, alerted, out_since = 0, False, None
        if trains is None:
            line["err"] = (err or "")[:200]
            fails += 1
            if fails >= 5:
                print("✗ 5 ошибок подряд — пауза 10 минут", flush=True)
                pauses += 1
                out_since = out_since or time.time()
                bad_cert = "CERTIFICATE_VERIFY_FAILED" in (err or "")
                if not alerted and (bad_cert or pauses >= 3):
                    alerted = True
                    if bad_cert:
                        alert("источник отдаёт сертификат, который не проходит проверку "
                              "(просрочен?). Данные не пишутся, проверку не отключаем. "
                              f"Последняя ошибка: {(err or '')[:160]}")
                    else:
                        alert(f"источник не отвечает уже ~{pauses * 10} мин. "
                              f"Последняя ошибка: HTTP {code} {(err or '')[:160]}")
                time.sleep(600)
                fails = 0
        elif not trains and W.had_trains(seats, task):
            line["skip"] = True            # поезда ушли или сбой — состояние не трогаем
        else:
            fails = 0
            line["n"] = len(trains)
            before = hunter.snapshot(seats, task) if hunter.subs else {}
            was_empty = task in empty
            ev = W.diff(seats, task, trains)
            if ev:
                line["ev"] = ev
            if any(b in str(x.get("brand", "")).lower() for x in trains for b in W.BRANDS):
                empty.pop(task, None)
            else:
                empty[task] = int(time.time())     # «поездов нет»: потом появятся — продажа открылась
            if hunter.subs:
                stale = prev is None or t0 - prev > H.STALE
                hunter.note(task, {W.train_key(task, x): x for x in trains
                                   if any(b in str(x.get("brand", "")).lower() for b in W.BRANDS)})
                hunter.info(task, seats)
                sent = hunter.after(task, before, was_empty, seats, stale)
                if sent:
                    line["tg"] = sent
            mine = [x for x in trains
                    if any(b in str(x.get("brand", "")).lower() for b in W.BRANDS) and dep_iso(x)]
            horizon = (now().date() + timedelta(days=7)).isoformat()
            for x in mine:        # самое раннее отправление поезда среди опрошенных станций = начало маршрута
                k, iso = f"{x.get('number', '')}|{dep_iso(x)[:10]}", dep_iso(x)
                if iso[:10] <= horizon and iso < orig.get(k, "9"):
                    orig[k] = iso
            if mine and task.split("|")[:2] in [[codes[a], codes[b]] for a, b in WIN]:
                sched[task] = sorted([x.get("number", ""), dep_iso(x)] for x in mine)
        log(line)
        if time.time() - last_save > 600:
            seats, sched, orig, last, empty = save_state(seats, sched, orig, last, empty)
            last_save = time.time()
        if time.time() - last_report > 3600:
            print(f"за час: {n} запросов", flush=True)
            n, last_report = 0, time.time()
        time.sleep(PAUSE)
    if listener:
        listener.stopped = True
    save_state(seats, sched, orig, last, empty)
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


def cmd_hunt_status(send):
    """Один раз опросить пары+даты из подписок и показать (и с --send разослать) текущие места."""
    codes = W.station_codes()
    sd = DATA                                   # снятые кнопкой даты лежат в каталоге состояния сервиса
    if "STATE_DIRECTORY" not in os.environ and os.path.isdir("/var/lib/tabloda-seats"):
        sd = "/var/lib/tabloda-seats"
    hunter = H.Hunter(codes, TG_TOKEN, alert=alert, owner=TG_CHAT, state_dir=sd)
    active = hunter.tasks(now().date())
    if not active:
        print("подписок нет (или все даты вне горизонта продаж): см. subs.json, /watch в боте")
        return
    text = {}
    for task in sorted(active, key=lambda k: (k.split("|")[2], k)):
        dep, arv, d = task.split("|")
        try:
            code, trains, err = W.fetch(dep, arv, d)
        except W.Blocked as e:
            sys.exit(f"✗ {e} — похоже на блокировку")
        time.sleep(PAUSE)
        dd = datetime.fromisoformat(d)
        head = f"{hunter.names.get(dep, dep)} → {hunter.names.get(arv, arv)}, {H.WD[dd.weekday()]} {dd:%d.%m}"
        if trains is None:
            text[task] = f"{head}: ошибка запроса ({(err or '')[:80]})"
            continue
        ours = [x for x in trains if any(b in str(x.get("brand", "")).lower() for b in W.BRANDS)]
        rows = [f"  {x.get('number', '')} {dep_iso(x)[11:16]} — "
                + H.seats_word(sum(v for k, v in W.seats_of(x).items() if k.startswith('car:')))
                for x in sorted(ours, key=dep_iso)]
        text[task] = head + (":\n" + "\n".join(rows) if rows else ": поездов нет (продажа не открыта)")
    by_chat = {}
    for task, subs in active.items():
        for s in subs:
            by_chat.setdefault((s["chat"], s["name"]), []).append(task)
    for (chat, name), tasks in by_chat.items():
        msg = "Сейчас по подпискам:\n" + "\n".join(text[t] for t in sorted(tasks, key=lambda k: k.split("|")[2]))
        print(f"--- {name} ({chat})\n{msg}")
        if send:
            ok, why = H.tg_send(TG_TOKEN, chat, msg)
            print("  отправлено" if ok else f"  ✗ не отправлено: {why}")


def cmd_plan():
    print("Окна и примерная нагрузка в сутки:")
    print("  mid    23:55–00:40, +4 дня раз в минуту, остальные раз в 6 мин  ~500")
    print("  pre    за 150–50 мин до отправления (и от станции начала       ~1200–1400")
    print("         маршрута), раз в 2 мин")
    print("  eve    20:30–23:30, раз в 2 мин, Ургенч⇄Ташкент, завтра         ~180")
    print("  open   06:30–11:30, раз в 5 мин, 12 напр., дата +59    ~720")
    print("  hunt   подписки (subs.json), раз в 2 мин, круглосуточно  до 720 на пару+дату, не больше 12 пар+дат")
    print("  sched  раз в час, 6 напр. × сегодня/завтра              ~290")
    print(f"Итого ~3000 запросов в сутки, строго по одному; не больше {LIMIT_N} за {LIMIT_WIN} с.")


def main():
    def stop(*_):
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    a = sys.argv[1:]
    if a[:1] == ["--plan"]:
        cmd_plan()
    elif a[:1] == ["--cert"]:
        try:
            days, na = cert_left()
            print(f"сертификат источника действует до {na}: {days:.0f} дн")
        except Exception as e:
            print(f"✗ {type(e).__name__}: {str(e)[:200]}")
            sys.exit(1)
    elif a[:1] == ["--fresh"]:
        for path, limit, name in FRESH:
            age = repo_age(path)
            print(f"{name} ({path}): " + ("GitHub не ответил" if age is None else
                  f"последний коммит {fmt_age(age)} назад, порог {limit // 3600} ч"
                  + (" — СТАРЕЕ" if age > limit else "")))
    elif a[:1] == ["--alert-test"]:
        if not (TG_TOKEN and TG_CHAT):
            sys.exit("✗ нет TG_TOKEN или TG_CHAT (см. /opt/tabloda-seats/alert.env)")
        sys.exit(0 if alert("проверка связи, ничего делать не нужно") else 1)
    elif a[:1] == ["--who"]:
        if not TG_TOKEN:
            sys.exit("✗ нет TG_TOKEN (см. /opt/tabloda-seats/alert.env)")
        seen = H.who(TG_TOKEN)
        if not seen:
            print("пока никто не писал боту: пусть откроют его в Telegram и нажмут /start")
        for chat, (first, user, text) in seen.items():
            print(f"chat {chat}  {first}  @{user}  «{text}»")
    elif a[:1] == ["--hunt-status"]:
        cmd_hunt_status("--send" in a)
    elif a[:1] == ["--upload"]:
        day = a[1] if len(a) > 1 else (now().date() - timedelta(days=1)).isoformat()
        sys.exit(0 if upload(day) else 1)
    else:
        run()


if __name__ == "__main__":
    main()
