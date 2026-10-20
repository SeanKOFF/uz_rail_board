#!/usr/bin/env python3
"""
Охотник за билетами: уведомления в Telegram о появлении мест. Работает внутри
seats_windows.py (тот же опрос, те же события — отдельного опроса «для бота» нет),
только на стандартной библиотеке.

Подписки — файл subs.json рядом со скриптом (/opt/tabloda-seats/subs.json), правится
вручную, перечитывается раз в минуту без перезапуска сервиса:

    {"subs": [
      {"chat": 123456789, "name": "Sean",
       "from": "Ташкент", "to": "Самарканд",
       "dates": ["2026-10-25", "2026-10-27..2026-10-29"],
       "time": "06:00-12:00",     # необязательно: отправление со станции «from»
       "min": 1,                  # необязательно: сколько мест минимум (по умолчанию 1)
       "cooldown": 600,           # необязательно: не повторять про тот же поезд раньше, сек
       "new": false}              # необязательно: сообщать об открытии продаж на любые даты
    ]}

События:
  seats   у поезда было 0 мест, стало >= min — на даты и время из подписки
  opened  пара+дата, которая отвечала «поездов нет», получила поезда с местами:
          продажа открылась (подписчикам на эту дату и тем, у кого new: true)
  new     у пары+даты, где поезда уже были, появился ещё один поезд (возможно,
          замена номера: 778Ф → 778Ж)

Опрос подписок — «hunt», раз в HUNT_EVERY секунд, только даты в пределах
горизонта продаж (0…59 дней). Потолки: MAX_PER_CHAT пар «направление+дата» на
человека и MAX_TOTAL всего, лишнее не опрашивается (владельцу — одно ⚠).

    python3 seats_windows.py --who            # кто писал боту: id чатов для subs.json
    python3 seats_windows.py --hunt-status    # что сейчас по подпискам (--send — разослать)
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.environ.get("HUNT_CONFIG", os.path.join(HERE, "subs.json"))
TG_API = os.environ.get("TG_API", "https://api.telegram.org")
SITE = "https://eticket.uzrailpass.uz"
TZ = timezone(timedelta(hours=5))
HUNT_EVERY = 240          # сек между опросами одной пары+даты
HORIZON = 59              # дней вперёд, где продажа открыта
MAX_PER_CHAT = 8
MAX_TOTAL = 12
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ---------------------------------------------------------------- Telegram

def tg(token, method, body, timeout=15):
    req = urllib.request.Request(f"{TG_API}/bot{token}/{method}", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def tg_send(token, chat, text):
    """-> (ok, пояснение). Токена в пояснении нет."""
    for attempt in (1, 2):
        try:
            tg(token, "sendMessage", {"chat_id": chat, "text": text,
                                      "disable_web_page_preview": True})
            return True, ""
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt == 1:
                try:
                    wait = int(json.loads(body).get("parameters", {}).get("retry_after", 3))
                except Exception:
                    wait = 3
                time.sleep(min(wait, 10))
                continue
            return False, f"HTTP {e.code} {body[:120]}"
        except Exception as e:
            if attempt == 1:
                time.sleep(2)
                continue
            return False, type(e).__name__
    return False, ""


def who(token):
    """Кто писал боту (getUpdates): id чата для subs.json."""
    seen = {}
    for u in tg(token, "getUpdates", {"timeout": 0}).get("result", []):
        m = u.get("message") or {}
        c = m.get("chat") or {}
        if c.get("id") is not None:
            seen[c["id"]] = (c.get("first_name") or "", c.get("username") or "",
                             (m.get("text") or "")[:40])
    return seen


# ---------------------------------------------------------------- конфиг

def parse_dates(v):
    out = set()
    for item in ([v] if isinstance(v, str) else list(v)):
        a, _, b = str(item).partition("..")
        d0 = date.fromisoformat(a.strip())
        d1 = date.fromisoformat(b.strip()) if b else d0
        if d1 < d0 or (d1 - d0).days > 30:
            raise ValueError(f"диапазон {item}")
        out |= {(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)}
    return out


def parse_time(v):
    if not v:
        return None
    a, b = str(v).split("-")
    f = lambda s: int(s[:2]) * 60 + int(s[3:5])
    return f(a.strip()), f(b.strip())


def load_subs(path, codes):
    """-> (подписки, проблемы). Битая подписка пропускается, остальные работают."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    subs, problems = [], []
    for i, s in enumerate(data.get("subs", [])):
        who_ = s.get("name") or s.get("chat")
        try:
            dep, arv = codes[s["from"]], codes[s["to"]]
            subs.append(dict(chat=int(s["chat"]), name=str(s.get("name", s["chat"])),
                             dep=dep, arv=arv, dates=parse_dates(s["dates"]),
                             tr=parse_time(s.get("time")), min=max(int(s.get("min", 1)), 1),
                             cool=int(s.get("cooldown", 600)), new=bool(s.get("new", False))))
        except Exception as e:
            problems.append(f"subs.json: подписка №{i + 1} ({who_}) пропущена: "
                            f"{type(e).__name__} {e}")
    return subs, problems


# ---------------------------------------------------------------- охотник

class Hunter:
    def __init__(self, codes, token, send=None, alert=print):
        self.codes = codes
        self.names = {v: k for k, v in codes.items()}
        self.send = send or (lambda chat, text: tg_send(token, chat, text))
        self.alert = alert                     # сообщение владельцу (сбои конфига, чат недоступен)
        self.subs, self.mtime, self.checked = [], None, 0
        self.active, self.active_key = {}, None
        self.cool, self.said, self.dead = {}, set(), set()

    # --- конфиг и список опрашиваемых задач
    def reload(self):
        if time.time() - self.checked < 60:
            return
        self.checked = time.time()
        try:
            m = os.stat(CONF).st_mtime
        except FileNotFoundError:
            self.subs, self.mtime, self.active_key = [], None, None
            return
        if m == self.mtime:
            return
        self.mtime, self.active_key = m, None
        try:
            subs, problems = load_subs(CONF, self.codes)
            self.subs = subs
        except Exception as e:                 # файл битый — остаёмся на прежних подписках
            problems = [f"subs.json не разобран, работаю по прежним подпискам: {type(e).__name__} {e}"]
        for p in problems:
            self.warn(p)

    def warn(self, text):
        if text not in self.said:
            self.said.add(text)
            self.alert(text)

    def tasks(self, today):
        """-> {«откуда|куда|дата»: [подписки]} — что опрашивать «охотой» сегодня."""
        self.reload()
        if self.active_key == (today, self.mtime):
            return self.active
        per_chat, chosen = {}, {}
        lim = today + timedelta(days=HORIZON)
        for s in self.subs:
            for d in sorted(s["dates"]):
                if today.isoformat() <= d <= lim.isoformat():
                    per_chat.setdefault(s["chat"], []).append((d, s))
        for chat, lst in per_chat.items():
            lst.sort(key=lambda x: x[0])
            if len(lst) > MAX_PER_CHAT:
                self.warn(f"subs.json: у чата {chat} {len(lst)} дат, опрашиваю первые {MAX_PER_CHAT}")
            for d, s in lst[:MAX_PER_CHAT]:
                task = f"{s['dep']}|{s['arv']}|{d}"
                chosen.setdefault(task, []).append(s)
        if len(chosen) > MAX_TOTAL:
            self.warn(f"subs.json: {len(chosen)} уникальных пар «направление+дата», потолок "
                      f"{MAX_TOTAL}: опрашиваю ближайшие по дате")
            keep = sorted(chosen, key=lambda k: k.split("|")[2])[:MAX_TOTAL]
            chosen = {k: chosen[k] for k in keep}
        self.active, self.active_key = chosen, (today, self.mtime)
        return chosen

    # --- события
    @staticmethod
    def total(seats, key):
        return sum(v for k, v in seats.get(key, {}).items() if k.startswith("car:"))

    def snapshot(self, seats, task):
        pre = task + "|"
        return {k: self.total(seats, k) for k in seats if k.startswith(pre)}

    def _match(self, s, key, tot):
        if tot < s["min"]:
            return False
        if s["tr"]:
            h, m = key.split("|")[4][:2], key.split("|")[4][3:5]
            if not (s["tr"][0] <= int(h) * 60 + int(m) <= s["tr"][1]):
                return False
        return True

    def _text(self, task, items, seats, resumed=False):
        """Одно сообщение на пару+дату: что случилось за этот опрос (полночная пачка — один абзац)."""
        dep, arv, d = task.split("|")
        dd = date.fromisoformat(d)
        titles = {"opened": "Открылась продажа" + (" после паузы" if resumed else ""),
                  "seats": "Появились места", "new": "Новый поезд в выдаче (возможно, замена номера)"}
        out = [f"{self.names.get(dep, dep)} → {self.names.get(arv, arv)}, {WD[dd.weekday()]} {dd:%d.%m}"]
        for kind in ("opened", "seats", "new"):
            rows = [(k, tot, prev) for kd, k, tot, prev in items if kd == kind]
            if not rows:
                continue
            out.append(titles[kind] + ":")
            for key, tot, prev in sorted(rows, key=lambda r: r[0].split("|")[4]):
                num, tm = key.split("|")[3:5]
                cls = {k[4:]: v for k, v in seats.get(key, {}).items() if k.startswith("cls:")}
                row = f"  {num} {tm} — {tot} мест" + (f" (было {prev})" if kind == "seats" and prev is not None else "")
                if cls and sum(cls.values()) == tot:      # разбивка по классам верна не у всех поездов
                    row += " · " + ", ".join(f"{k} {v}" for k, v in sorted(cls.items()))
                out.append(row)
        out.append(f"Замечено {datetime.now(TZ):%H:%M:%S}."
                   + (" Такие места обычно разбирают за считаные минуты."
                      if any(kd == "seats" for kd, *_ in items) else ""))
        out.append(SITE)
        return "\n".join(out)

    def _deliver(self, s, task, items, seats, resumed=False, cool=True):
        """-> число отправленных сообщений (0 или 1)."""
        if s["chat"] in self.dead:
            return 0
        now = time.time()
        if cool:   # не повторять про тот же поезд (мигающие места), если мест много — всё равно сообщаем
            items = [i for i in items
                     if now - self.cool.get((s["chat"], i[1]), 0) >= s["cool"] or i[2] >= 10]
        if not items:
            return 0
        ok, why = self.send(s["chat"], self._text(task, items, seats, resumed))
        if ok:
            for _, key, _, _ in items:
                self.cool[(s["chat"], key)] = now
            return 1
        if why.startswith("HTTP 403"):          # пользователь заблокировал бота / не нажал /start
            self.dead.add(s["chat"])
        self.warn(f"в чат {s['chat']} ({s['name']}) не доставлено: {why}")
        return 0

    def after(self, task, before, was_empty, seats):
        """Вызывать после применения ответа по задаче. before — snapshot до diff,
        was_empty — раньше на эту пару+дату приходил пустой список. -> число отправленных."""
        dep, arv, d = task.split("|")
        now_t = self.snapshot(seats, task)
        had = bool(before)
        ev = []                                  # (вид, ключ, всего, было)
        for key, tot in sorted(now_t.items()):
            pt = before.get(key)
            if pt is None and was_empty and not had:
                if tot > 0:
                    ev.append(("opened", key, tot, None))
            elif pt is None and had and tot > 0:
                ev.append(("new", key, tot, None))
            elif pt == 0 and tot > 0:
                ev.append(("seats", key, tot, 0))
        if not ev:
            return 0
        sent, told = 0, set()
        prevday = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        resumed = not any(k.startswith(f"{dep}|{arv}|{prevday}|") for k in seats)
        for s in self.active.get(task, []):      # подписки на эту дату
            mine = [e for e in ev if self._match(s, e[1], e[2])]
            if mine:
                sent += self._deliver(s, task, mine, seats, resumed)
                told.add(s["chat"])
        opened = [e for e in ev if e[0] == "opened"]
        for s in self.subs:                      # «новые даты» по направлению
            if s["new"] and s["dep"] == dep and s["arv"] == arv and s["chat"] not in told and opened:
                mine = [e for e in opened if self._match(s, e[1], e[2])]
                if mine:
                    sent += self._deliver(s, task, mine, seats, resumed, cool=False)
        return sent
