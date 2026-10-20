#!/usr/bin/env python3
"""
Охотник за билетами: уведомления в Telegram о появлении мест. Работает внутри
seats_windows.py (тот же опрос, те же события — отдельного опроса «для бота» нет),
только на стандартной библиотеке.

Подписки — файл subs.json в каталоге состояния (/var/lib/tabloda-seats/subs.json, путь
задаёт HUNT_CONFIG), правится ботом (/watch, /unwatch) или вручную через sudo,
перечитывается раз в минуту без перезапуска сервиса:

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

Опрос подписок — «hunt», раз в HUNT_EVERY секунд (по умолчанию 120: одиночные возвраты
живут 1–5 минут, при шаге 4 минуты половина пролетала бы мимо), только даты в пределах
горизонта продаж (0…59 дней). Потолки: MAX_PER_CHAT пар «направление+дата» на
человека и MAX_TOTAL всего, лишнее не опрашивается (владельцу — одно ⚠).

    python3 seats_windows.py --who            # кто писал боту: id чатов для subs.json
    python3 seats_windows.py --hunt-status    # что сейчас по подпискам (--send — разослать)

Бот слушает сообщения (поток Listener внутри сервиса, getUpdates):
  кнопка «Купил — снять эту дату» под сообщением — пара+дата снимается у этого
          человека (mutes.json в каталоге состояния), опрос по ней прекращается,
          если больше никому не нужна; остальные подписки и подписчики не затронуты
  /start  отвечает человеку его id, владельцу (TG_CHAT) сообщает, кто пришёл
  /list   подписки человека и снятые даты
  /resume вернуть снятые даты
Пока работает сервис, `--who` может получить 409: обновления разбирает сервис —
достаточно, чтобы человек нажал /start.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.environ.get("HUNT_CONFIG") or os.path.join(os.environ.get("STATE_DIRECTORY") or HERE, "subs.json")
TG_API = os.environ.get("TG_API", "https://api.telegram.org")
SITE = "https://eticket.uzrailpass.uz"
TZ = timezone(timedelta(hours=5))
HUNT_EVERY = int(os.environ.get("HUNT_EVERY", 120))   # сек между опросами одной пары+даты (HUNT_EVERY в alert.env)
STALE = 900               # сек: если прошлый опрос пары+даты был раньше — «было 0» не доверяем
HORIZON = 59              # дней вперёд, где продажа открыта
MAX_PER_CHAT = 8
MAX_TOTAL = 12
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def seats_word(n):
    """1 место, 2–4 места, 5+ мест (11–14 — мест)."""
    n = abs(int(n))
    if n % 100 in range(11, 15):
        return f"{n} мест"
    return f"{n} " + ("место" if n % 10 == 1 else "места" if n % 10 in (2, 3, 4) else "мест")


# ---------------------------------------------------------------- Telegram

def tg(token, method, body, timeout=15):
    req = urllib.request.Request(f"{TG_API}/bot{token}/{method}", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def tg_send(token, chat, text, markup=None):
    """-> (ok, пояснение). Токена в пояснении нет. markup — inline-клавиатура."""
    body_ = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if markup:
        body_["reply_markup"] = markup
    for attempt in (1, 2):
        try:
            tg(token, "sendMessage", body_)
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
    def __init__(self, codes, token, send=None, alert=print, api=None, owner=None, state_dir=None):
        self.codes = codes
        self.names = {v: k for k, v in codes.items()}
        self.send = send or (lambda chat, text, markup=None: tg_send(token, chat, text, markup))
        self.api = api or (lambda method, body: tg(token, method, body))
        self.alert = alert                     # сообщение владельцу (сбои конфига, чат недоступен)
        self.owner = int(owner) if str(owner or "").lstrip("-").isdigit() else None
        self.subs, self.mtime, self.checked = [], None, 0
        self.active, self.active_key = {}, None
        self.cool, self.said, self.dead = {}, set(), set()
        self.lock = threading.Lock()
        self.mutes_path = os.path.join(state_dir or os.environ.get("STATE_DIRECTORY") or HERE, "mutes.json")
        self.mutes = self._load_mutes()

    # --- снятые кнопкой даты: (чат, откуда, куда, дата)
    def _load_mutes(self):
        try:
            with open(self.mutes_path, encoding="utf-8") as f:
                today = datetime.now(TZ).date().isoformat()
                return {tuple(x) for x in json.load(f) if x[3] >= today}
        except (OSError, ValueError, IndexError, TypeError):
            return set()

    def _save_mutes(self):
        tmp = self.mutes_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(self.mutes), f, ensure_ascii=False)
        os.replace(tmp, self.mutes_path)

    def set_mute(self, chat, dep, arv, d, on=True):
        with self.lock:
            (self.mutes.add if on else self.mutes.discard)((chat, dep, arv, d))
            self.active_key = None            # список задач пересчитается
            try:
                self._save_mutes()
            except OSError as e:
                self.warn(f"mutes.json не записан: {e}")

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
                if (s["chat"], s["dep"], s["arv"], d) in self.mutes:
                    continue
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
                  "seats": "Появились места", "new": "Новый поезд в выдаче (возможно, замена номера)",
                  "now": "Сейчас есть места (прошлая проверка была давно — когда они появились, неизвестно)"}
        out = [f"{self.names.get(dep, dep)} → {self.names.get(arv, arv)}, {WD[dd.weekday()]} {dd:%d.%m}"]
        for kind in ("opened", "seats", "new", "now"):
            rows = [(k, tot, prev) for kd, k, tot, prev in items if kd == kind]
            if not rows:
                continue
            out.append(titles[kind] + ":")
            for key, tot, prev in sorted(rows, key=lambda r: r[0].split("|")[4]):
                num, tm = key.split("|")[3:5]
                cls = {k[4:]: v for k, v in seats.get(key, {}).items() if k.startswith("cls:")}
                row = f"  {num} {tm} — {seats_word(tot)}" + (f" (было {prev})" if kind == "seats" and prev is not None else "")
                if cls and sum(cls.values()) == tot:      # разбивка по классам верна не у всех поездов
                    row += " · " + ", ".join(f"{k} {v}" for k, v in sorted(cls.items()))
                out.append(row)
        out.append(f"Замечено {datetime.now(TZ):%H:%M:%S}."
                   + (" Такие места обычно разбирают за считаные минуты."
                      if any(kd == "seats" for kd, *_ in items) else ""))
        out.append(SITE)
        return "\n".join(out)

    def _deliver(self, s, task, items, seats, resumed=False, cool=True, button=False):
        """-> число отправленных сообщений (0 или 1)."""
        if s["chat"] in self.dead:
            return 0
        now = time.time()
        if cool:   # не повторять про тот же поезд (мигающие места), если мест много — всё равно сообщаем
            items = [i for i in items
                     if now - self.cool.get((s["chat"], i[1]), 0) >= s["cool"] or i[2] >= 10]
        if not items:
            return 0
        markup = None
        if button:                               # снять эту дату после покупки
            dep_, arv_, d_ = task.split("|")
            markup = {"inline_keyboard": [[{"text": "Купил — снять эту дату",
                                            "callback_data": f"d|{dep_}|{arv_}|{d_}"}]]}
        ok, why = self.send(s["chat"], self._text(task, items, seats, resumed), markup)
        if ok:
            for _, key, _, _ in items:
                self.cool[(s["chat"], key)] = now
            return 1
        if why.startswith("HTTP 403"):          # пользователь заблокировал бота / не нажал /start
            self.dead.add(s["chat"])
        self.warn(f"в чат {s['chat']} ({s['name']}) не доставлено: {why}")
        return 0

    def after(self, task, before, was_empty, seats, stale=False):
        """Вызывать после применения ответа по задаче. before — snapshot до diff,
        was_empty — раньше на эту пару+дату приходил пустой список, stale — прошлый
        опрос был слишком давно (первый после подписки, рестарта или сбоя): тогда «было 0»
        устарело, и времени появления мест мы не знаем. -> число отправленных."""
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
        if stale:                                # честно: «есть», а не «появились»
            ev = [("now", key, tot, None) for _, key, tot, _ in ev]
        sent, told = 0, set()
        prevday = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        resumed = not any(k.startswith(f"{dep}|{arv}|{prevday}|") for k in seats)
        for s in self.active.get(task, []):      # подписки на эту дату
            mine = [e for e in ev if self._match(s, e[1], e[2])]
            if mine:
                sent += self._deliver(s, task, mine, seats, resumed, button=True)
                told.add(s["chat"])
        opened = [e for e in ev if e[0] == "opened"]
        for s in self.subs:                      # «новые даты» по направлению
            if s["new"] and s["dep"] == dep and s["arv"] == arv and s["chat"] not in told and opened:
                mine = [e for e in opened if self._match(s, e[1], e[2])]
                if mine:
                    sent += self._deliver(s, task, mine, seats, resumed, cool=False)
        return sent


    # --- сообщения и кнопки от людей
    def handle_update(self, u):
        cq = u.get("callback_query")
        if cq:
            if (cq.get("data") or "").startswith("w|"):
                return self._on_menu(cq)
            return self._on_button(cq)
        m = u.get("message") or {}
        chat = (m.get("chat") or {}).get("id")
        text = (m.get("text") or "").strip()
        text = self.MENU_TEXTS.get(text, text)      # кнопки под полем ввода
        if chat is None or not text.startswith("/"):
            return
        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/start":
            known = self._allowed(chat)
            self.send(chat, f"Закрытая бета: бот сообщает о появлении мест на скоростные поезда. "
                            f"Ваш id: {chat}." + ("" if known else " Передайте его владельцу бота, "
                            "если хотите получать уведомления."),
                      self.MENU_KB if known else None)
            if not known:
                f = m.get("from") or {}
                self.warn(f"/start от нового пользователя: id {chat}, {f.get('first_name', '')} "
                          f"@{f.get('username', '')}")
        elif cmd == "/menu" or (cmd == "/watch" and len(text.split()) == 1):
            if not self._allowed(chat):
                self.send(chat, f"Доступ по приглашению. Ваш id: {chat} — передайте его владельцу бота.")
            elif cmd == "/watch":
                self._w()[chat] = {"f": None, "t": None, "ds": set(), "pg": 0}
                t_, rows_ = self._from_screen()
                self.send(chat, t_, {"inline_keyboard": rows_})
            else:
                self.send(chat, "Кнопки меню — под полем ввода.", self.MENU_KB)
        elif cmd == "/list":
            self.send(chat, self._list_text(chat), self._kb(self._list_markup(chat)))
        elif cmd == "/resume":
            with self.lock:
                mine = [x for x in self.mutes if x[0] == chat]
            for x in mine:
                self.set_mute(*x, on=False)
            self.send(chat, f"Возвращено дат: {len(mine)}." if mine else "Снятых дат нет.")
        elif cmd in ("/invite", "/uninvite"):
            try:
                reply = self._invite(chat, text.split()[1:], add=(cmd == "/invite"))
            except Exception as e:
                print(f"✗ {cmd}: {type(e).__name__} {e}", flush=True)
                reply = "Не получилось обработать команду."
            self.send(chat, reply)
        elif cmd in ("/watch", "/unwatch"):
            try:
                reply = self._edit_subs(chat, m, text.split()[1:], add=(cmd == "/watch"))
            except Exception as e:
                print(f"✗ {cmd}: {type(e).__name__} {e}", flush=True)
                reply = "Не получилось обработать команду."
            self.send(chat, reply)
        else:
            self.send(chat, "Команды: /list — ваши подписки, /watch — добавить, /unwatch — убрать, "
                            "/resume — вернуть снятые даты, /menu — кнопки, /start — ваш id.")

    # --- подписки из бота: /watch и /unwatch (патч seats-watch-cmd)
    def _allowed(self, chat):
        return (chat == self.owner or any(s["chat"] == chat for s in self.subs)
                or chat in self._invited_ids())

    def _station_key(self, word):
        norm = lambda x: x.strip().lower().replace("ё", "е")
        w = norm(word)
        cand = [k for k in self.codes if norm(k) == w] or [k for k in self.codes if norm(k).startswith(w)]
        return cand[0] if cand and len({self.codes[k] for k in cand}) == 1 else None

    def _user_dates(self, tokens, today, past_ok=False):
        """«25.10» или «25.10..27.10» -> множество ISO-дат. ValueError — текст для человека."""
        import re
        out = set()
        for t in tokens:
            a, _, b = t.partition("..")
            ds = []
            for part in (a, b or a):
                m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", part)
                if not m:
                    raise ValueError(f"Не понял дату «{t}», нужен вид 25.10 или 25.10..27.10.")
                try:
                    d = date(today.year, int(m.group(2)), int(m.group(1)))
                except ValueError:
                    raise ValueError(f"Нет такой даты: «{part}».")
                if d < today and (today - d).days > 200:     # 05.01 в конце декабря — это следующий год
                    d = d.replace(year=d.year + 1)
                ds.append(d)
            if ds[1] < ds[0] or (ds[1] - ds[0]).days > 30:
                raise ValueError(f"Диапазон «{t}» неверный или длиннее 31 дня.")
            if ds[0] < today and not past_ok:
                raise ValueError(f"Дата {ds[0]:%d.%m} уже прошла.")
            out |= {(ds[0] + timedelta(days=i)).isoformat() for i in range((ds[1] - ds[0]).days + 1)}
        return out

    @staticmethod
    def _fmt_dates(ds):
        ds = sorted(ds)
        f = lambda d: f"{d[8:10]}.{d[5:7]}"
        return ", ".join(f(d) for d in ds) if len(ds) <= 6 else f"{f(ds[0])}…{f(ds[-1])} ({len(ds)} дат)"

    def _edit_subs(self, chat, m, args, add):
        """/watch Откуда Куда дата [мин_мест] и /unwatch Откуда Куда [дата] -> текст ответа."""
        import re
        if not self._allowed(chat):
            return f"Доступ по приглашению. Ваш id: {chat} — передайте его владельцу бота."
        today = datetime.now(TZ).date()
        usage = ("Формат: /watch Откуда Куда дата [мин_мест]\n"
                 "Пример: /watch Ташкент Самарканд 25.10 2\n"
                 "Диапазон дат: 25.10..27.10. Убрать: /unwatch Откуда Куда [дата].")
        dtok = [a for a in args if re.fullmatch(r"\d{1,2}\.\d{1,2}(\.\.\d{1,2}\.\d{1,2})?", a)]
        rest = [a for a in args if a not in dtok]
        nums = [a for a in rest if a.isdigit()]
        words = [a for a in rest if not a.isdigit()]
        if len(words) != 2 or len(nums) > 1 or (add and not dtok) or (nums and not add):
            return usage
        n = int(nums[0]) if nums else None
        if n is not None and not 1 <= n <= 50:
            return "Мин. мест — число от 1 до 50."
        keys = [self._station_key(w) for w in words]
        for w, k in zip(words, keys):
            if k is None:
                return f"Не знаю станцию «{w}». Доступные: " + ", ".join(sorted(self.codes))[:600]
        fk, tk = keys
        dep, arv = self.codes[fk], self.codes[tk]
        if dep == arv:
            return "Откуда и куда совпадают."
        try:
            dates = self._user_dates(dtok, today, past_ok=not add)
        except ValueError as e:
            return str(e)
        iso_today = today.isoformat()
        with self.lock:
            try:
                with open(CONF, encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:               # свежий сервер: файла ещё нет, начинаем с пустого
                data = {"subs": []}
            except (OSError, ValueError) as e:
                self.warn(f"/watch: subs.json не прочитан: {type(e).__name__} {e}")
                return "Не получилось прочитать файл подписок, владельцу сообщено."
            raw = data.setdefault("subs", [])
            same = [x for x in raw if str(x.get("chat")) == str(chat)
                    and self.codes.get(x.get("from")) == dep and self.codes.get(x.get("to")) == arv]
            if add:
                if same:
                    e0 = same[0]
                    try:
                        old = parse_dates(e0["dates"])
                    except Exception:
                        old = set()
                    e0["dates"] = sorted(d for d in old | dates if d >= iso_today)
                    if n:
                        e0["min"] = n
                    shown_min = e0.get("min", 1)
                else:
                    nm = (next((x.get("name") for x in raw if str(x.get("chat")) == str(chat) and x.get("name")), None)
                          or (m.get("from") or {}).get("first_name") or str(chat))
                    raw.append({"chat": chat, "name": nm, "from": fk, "to": tk,
                                "dates": sorted(dates), "min": n or 1})
                    shown_min = n or 1
                done = f"Добавлено: {self.names.get(dep, dep)} → {self.names.get(arv, arv)}, " \
                       f"{self._fmt_dates(dates)}. Мест от {shown_min}."
            else:
                if not same:
                    return "Такой подписки нет."
                removed = set()
                for x in same:
                    try:
                        old = parse_dates(x["dates"])
                    except Exception:
                        old = set()
                    if dates:
                        left = old - dates
                        removed |= old & dates
                        if left:
                            x["dates"] = sorted(left)
                        else:
                            raw.remove(x)
                    else:
                        removed |= old
                        raw.remove(x)
                if dates and not removed:
                    return "Этих дат в подписке нет."
                done = f"Снято: {self.names.get(dep, dep)} → {self.names.get(arv, arv)}" + \
                       (f", {self._fmt_dates(removed)}." if dates else ", вся подписка.")
            tmp = CONF + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                subs, _ = load_subs(tmp, self.codes)
                if add:                           # потолки — те же, что в tasks()
                    lim = (today + timedelta(days=HORIZON)).isoformat()
                    trip = lambda ss: {(s["dep"], s["arv"], d) for s in ss for d in s["dates"]
                                       if iso_today <= d <= lim
                                       and (s["chat"], s["dep"], s["arv"], d) not in self.mutes}
                    mine = trip([s for s in subs if s["chat"] == chat])
                    if len(mine) > MAX_PER_CHAT:
                        os.remove(tmp)
                        return (f"Потолок: не больше {MAX_PER_CHAT} дат на человека, с этой подпиской вышло "
                                f"{len(mine)}. Уберите лишнее: /unwatch Откуда Куда дата.")
                    if len(trip(subs)) > MAX_TOTAL:
                        os.remove(tmp)
                        return "Общий потолок опроса исчерпан, подписка не добавлена. Напишите владельцу бота."
                os.replace(tmp, CONF)
            except OSError as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                self.warn(f"/watch: subs.json не записан: {type(e).__name__} {e}")
                return "Не получилось сохранить подписку, владельцу сообщено."
            self.subs, self.mtime, self.checked, self.active_key = subs, None, 0, None
        return done + "\n\n" + self._list_text(chat)

    # --- приглашения (патч seats-invite): владелец пускает людей без правки файла
    def _invited_ids(self):
        try:
            with open(CONF, encoding="utf-8") as f:
                return {int(x["chat"]) for x in json.load(f).get("invited", [])}
        except Exception:
            return set()

    def _invite(self, chat, args, add):
        """/invite id [имя], /invite (список), /uninvite id — только владельцу -> текст ответа."""
        if self.owner is None or chat != self.owner:
            return "Команда только для владельца бота."
        usage = "/invite id [имя] — пригласить, /invite — список, /uninvite id — убрать вместе с подписками."
        target = None
        if args or not add:
            try:
                target = int(args[0])
            except (ValueError, IndexError):
                return usage
        name = " ".join(args[1:]).strip()
        notify = False
        with self.lock:
            try:
                with open(CONF, encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                data = {"subs": []}
            except (OSError, ValueError) as e:
                self.warn(f"/invite: subs.json не прочитан: {type(e).__name__} {e}")
                return "Не получилось прочитать файл подписок, владельцу сообщено."
            inv = data.setdefault("invited", [])
            raw = data.setdefault("subs", [])
            if add and target is None:               # список
                names = {}
                for x in raw:
                    names.setdefault(str(x.get("chat")), x.get("name") or "")
                lines = [f"{x.get('chat')} {x.get('name') or ''}".strip() for x in inv]
                out = "Приглашены:\n" + "\n".join(lines) if lines else "Приглашённых нет."
                if names:
                    out += "\n\nЕсть подписки у:\n" + "\n".join(f"{c} {n}".strip() for c, n in names.items())
                return out
            if add:
                found = [x for x in inv if str(x.get("chat")) == str(target)]
                if found:
                    if name:
                        found[0]["name"] = name
                    done = f"{target} уже приглашён."
                else:
                    inv.append({"chat": target, "name": name or str(target)})
                    done = f"Приглашён: {target}" + (f" ({name})." if name else ".")
                    notify = True
            else:
                if target == self.owner:
                    return "Владельца убирать нельзя."
                n_inv = len(inv)
                data["invited"] = [x for x in inv if str(x.get("chat")) != str(target)]
                n_subs = sum(1 for x in raw if str(x.get("chat")) == str(target))
                data["subs"] = [x for x in raw if str(x.get("chat")) != str(target)]
                if n_inv == len(data["invited"]) and not n_subs:
                    return "Такого id нет среди приглашённых и подписчиков."
                done = f"Убрано: {target}, подписок удалено: {n_subs}."
            tmp = CONF + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                subs, _ = load_subs(tmp, self.codes)
                os.replace(tmp, CONF)
            except OSError as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                self.warn(f"/invite: subs.json не записан: {type(e).__name__} {e}")
                return "Не получилось сохранить, владельцу сообщено."
            self.subs, self.mtime, self.checked, self.active_key = subs, None, 0, None
        if notify:                                   # сообщить приглашённому (если он уже нажал /start)
            res = self.send(target, "Вас пригласили в закрытую бету бота: он сообщает о появлении мест на "
                                    "скоростные поезда. Добавить подписку: /watch Откуда Куда дата, например "
                                    "/watch Ташкент Самарканд 25.10. Ваши подписки: /list.", self.MENU_KB)
            if isinstance(res, tuple) and not res[0]:
                done += (f"\nБот не смог написать ему ({res[1]}): пусть сначала нажмёт /start, "
                         "приглашение уже сохранено.")
        return done

    # --- кнопочное меню (патч seats-menu): станции и даты выбираются нажатиями
    MENU_KB = {"keyboard": [[{"text": "➕ Добавить подписку"}, {"text": "📋 Мои подписки"}]],
               "resize_keyboard": True, "is_persistent": True}
    MENU_TEXTS = {"➕ Добавить подписку": "/watch", "📋 Мои подписки": "/list"}
    MENU_CITIES = ("Ташкент", "Самарканд", "Бухара", "Ургенч")    # где ходят скоростные поезда
    PAGE_DAYS = 14

    def _w(self):
        if not hasattr(self, "_wiz"):
            self._wiz = {}                      # чат -> выбор в процессе; после рестарта старые кнопки «устарели»
        return self._wiz

    def _cities(self):
        return [k for k in self.MENU_CITIES if k in self.codes]

    def _screen(self, chat, mid, text, rows):
        body = {"chat_id": chat, "message_id": mid, "text": text, "disable_web_page_preview": True}
        if rows is not None:
            body["reply_markup"] = {"inline_keyboard": rows}
        try:
            self.api("editMessageText", body)
        except Exception as e:                  # «message is not modified» и т. п. — не страшно
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            print(f"✗ меню: {type(e).__name__} {getattr(e, 'code', '')} {detail}", flush=True)

    @staticmethod
    def _kb(rows):
        return None if rows is None else {"inline_keyboard": rows}

    @staticmethod
    def _grid(btns, n):
        return [btns[j:j + n] for j in range(0, len(btns), n)]

    def _from_screen(self):
        btns = [{"text": c, "callback_data": f"w|f|{i}"} for i, c in enumerate(self._cities())]
        rows = self._grid(btns, 2) + [[{"text": "Отмена", "callback_data": "w|c"}]]
        return "Откуда едем?", rows

    def _to_screen(self, st):
        cs = self._cities()
        btns = [{"text": c, "callback_data": f"w|t|{i}"} for i, c in enumerate(cs) if i != st["f"]]
        rows = self._grid(btns, 2) + [[{"text": "‹ Назад", "callback_data": "w|b"},
                                        {"text": "Отмена", "callback_data": "w|c"}]]
        return f"Откуда: {cs[st['f']]}. Куда?", rows

    def _dates_screen(self, st, note=""):
        cs = self._cities()
        today = datetime.now(TZ).date()
        wd = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
        total = HORIZON + 1
        a = st["pg"] * self.PAGE_DAYS
        b = min(a + self.PAGE_DAYS, total)
        btns = []
        for k in range(a, b):
            d = today + timedelta(days=k)
            mark = "✓ " if d.isoformat() in st["ds"] else ""
            btns.append({"text": f"{mark}{wd[d.weekday()]} {d:%d.%m}", "callback_data": f"w|d|{d.isoformat()}"})
        rows = self._grid(btns, 4)
        nav = []
        if st["pg"] > 0:
            nav.append({"text": "‹ Раньше", "callback_data": f"w|p|{st['pg'] - 1}"})
        if b < total:
            nav.append({"text": "Позже ›", "callback_data": f"w|p|{st['pg'] + 1}"})
        if nav:
            rows.append(nav)
        rows.append([{"text": f"Готово ({len(st['ds'])})", "callback_data": "w|ok"}])
        rows.append([{"text": "‹ Назад", "callback_data": "w|b"}, {"text": "Отмена", "callback_data": "w|c"}])
        chosen = self._fmt_dates(st["ds"]) if st["ds"] else "пока ничего"
        text = ((note + "\n\n") if note else "") + (f"{cs[st['f']]} → {cs[st['t']]}\n"
                "Отметьте даты (повторное нажатие снимает), затем «Готово».\n" f"Выбрано: {chosen}")
        return text, rows

    def _list_markup(self, chat):
        if not self._allowed(chat):
            return None
        today = datetime.now(TZ).date().isoformat()
        rows = []
        for s in self.subs:
            if s["chat"] != chat:
                continue
            for d in sorted(x for x in s["dates"] if x >= today):
                label = (f"✖ {self.names.get(s['dep'], s['dep'])} → {self.names.get(s['arv'], s['arv'])} "
                         f"{d[8:10]}.{d[5:7]}")
                rows.append([{"text": label, "callback_data": f"w|u|{s['dep']}|{s['arv']}|{d}"}])
        rows = rows[:12]
        rows.append([{"text": "➕ Добавить подписку", "callback_data": "w|n"}])
        return rows

    def _on_menu(self, cq):
        t0 = time.time()
        try:
            return self._on_menu_inner(cq)
        finally:
            chat_ = ((cq.get("message") or {}).get("chat") or {}).get("id")
            print(f"меню: {cq.get('data')} чат {chat_} за {int((time.time() - t0) * 1000)} мс", flush=True)

    def _on_menu_inner(self, cq):
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        mid = msg.get("message_id")

        def ack(text=None):
            body = {"callback_query_id": cq.get("id")}
            if text:
                body["text"] = text
                print(f"меню: всплывающий ответ «{text}»", flush=True)
            def send_ack():                 # не держим Listener, если Telegram отвечает медленно
                t1 = time.time()
                try:
                    self.api("answerCallbackQuery", body)
                except Exception as e:
                    print(f"✗ меню: ответ на нажатие не ушёл ({type(e).__name__}) "
                          f"за {int((time.time() - t1) * 1000)} мс", flush=True)
            threading.Thread(target=send_ack, daemon=True).start()

        if chat is None or mid is None or not self._allowed(chat):
            return ack("Доступ по приглашению")
        p = data.split("|")
        cs, st = self._cities(), self._w().get(chat)
        today = datetime.now(TZ).date()
        who = {"from": cq.get("from") or {}}
        try:
            act = p[1]
            if act == "n":
                self._w()[chat] = st = {"f": None, "t": None, "ds": set(), "pg": 0}
                text, rows = self._from_screen()
            elif act == "l":
                text, rows = self._list_text(chat), self._list_markup(chat)
            elif act == "c":
                self._w().pop(chat, None)
                text, rows = "Отменено. /menu — открыть заново.", None
            elif act == "u" and len(p) == 5:
                dep, arv, d = p[2], p[3], p[4]
                date.fromisoformat(d)
                nm = lambda c: self.names.get(c, c)
                text = self._edit_subs(chat, who, [nm(dep), nm(arv), f"{d[8:10]}.{d[5:7]}"], add=False)
                rows = self._list_markup(chat)
            elif act in ("f", "t", "d", "p", "b", "ok"):
                if st is None:
                    return ack("Меню устарело — откройте заново: /watch")
                if act == "f":
                    i = int(p[2])
                    if not 0 <= i < len(cs):
                        raise ValueError
                    st["f"], st["t"] = i, None
                    text, rows = self._to_screen(st)
                elif act == "t":
                    i = int(p[2])
                    if st["f"] is None or i == st["f"] or not 0 <= i < len(cs):
                        raise ValueError
                    st["t"], st["pg"] = i, 0
                    text, rows = self._dates_screen(st)
                elif st["f"] is None or (act != "b" and st["t"] is None):
                    raise ValueError
                elif act == "d":
                    d = date.fromisoformat(p[2])
                    if not today <= d <= today + timedelta(days=HORIZON):
                        raise ValueError
                    st["ds"] ^= {d.isoformat()}
                    text, rows = self._dates_screen(st)
                elif act == "p":
                    n = int(p[2])
                    if not 0 <= n * self.PAGE_DAYS <= HORIZON:
                        raise ValueError
                    st["pg"] = n
                    text, rows = self._dates_screen(st)
                elif act == "b":
                    if st["t"] is not None:
                        st["t"] = None
                        text, rows = self._to_screen(st)
                    else:
                        st["f"] = None
                        text, rows = self._from_screen()
                else:                                         # ok
                    if not st["ds"]:
                        return ack("Сначала отметьте хотя бы одну дату")
                    toks = [cs[st["f"]], cs[st["t"]]] + [f"{x[8:10]}.{x[5:7]}" for x in sorted(st["ds"])]
                    res = self._edit_subs(chat, who, toks, add=True)
                    if res.startswith("Добавлено"):
                        self._w().pop(chat, None)
                        text, rows = res, self._list_markup(chat)
                    else:                                     # потолок, сбой записи и т. п. — остаёмся на датах
                        text, rows = self._dates_screen(st, note=res)
            else:
                raise ValueError
        except (ValueError, IndexError, TypeError):
            return ack("Кнопка не распознана")
        except Exception as e:
            print(f"✗ меню: {type(e).__name__} {e}", flush=True)
            return ack("Не получилось, попробуйте ещё раз")
        ack()
        self._screen(chat, mid, text, rows)

    def _list_text(self, chat):
        mine = [s for s in self.subs if s["chat"] == chat]
        if not mine:
            return "Подписок нет."
        today = datetime.now(TZ).date().isoformat()
        lines = []
        for s in mine:
            ds = sorted(d for d in s["dates"] if d >= today)
            off = [d for d in ds if (chat, s["dep"], s["arv"], d) in self.mutes]
            on = [d for d in ds if d not in off]
            fmt = lambda xs: ", ".join(f"{d[8:10]}.{d[5:7]}" for d in xs) or "—"
            lines.append(f"{self.names.get(s['dep'], s['dep'])} → {self.names.get(s['arv'], s['arv'])}: "
                         f"{fmt(on)}" + (f" (снято: {fmt(off)})" if off else "")
                         + (" · новые даты" if s["new"] else ""))
        return "Ваши подписки:\n" + "\n".join(lines)

    def _on_button(self, cq):
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        try:
            kind, dep, arv, d = data.split("|")
            date.fromisoformat(d)
            if kind != "d" or chat is None:
                raise ValueError
        except ValueError:
            self.api("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": "Кнопка не распознана"})
            return
        self.set_mute(chat, dep, arv, d)
        label = f"{self.names.get(dep, dep)} → {self.names.get(arv, arv)}, {d[8:10]}.{d[5:7]}"
        print(f"кнопка: чат {chat} снял {label}", flush=True)
        self.api("answerCallbackQuery", {"callback_query_id": cq.get("id"),
                                         "text": f"Готово: {label} снято"})
        if msg.get("message_id") and msg.get("text"):     # убрать кнопку, оставить текст
            try:
                self.api("editMessageText", {"chat_id": chat, "message_id": msg["message_id"],
                                             "text": msg["text"] + f"\n\nПодписка на эту дату снята ({label}). "
                                                                   "Вернуть: /resume",
                                             "disable_web_page_preview": True})
            except Exception as e:
                print(f"✗ сообщение не обновлено: {type(e).__name__}", flush=True)


class Listener(threading.Thread):
    """Слушает сообщения и кнопки (getUpdates, long polling) и передаёт их охотнику."""

    def __init__(self, hunter, token):
        super().__init__(daemon=True)
        self.hunter, self.token, self.stopped, self.offset = hunter, token, False, None

    def run(self):
        errs = 0
        while not self.stopped:
            try:
                body = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if self.offset:
                    body["offset"] = self.offset
                t0 = time.time()
                res = tg(self.token, "getUpdates", body, timeout=40).get("result", [])
                errs = 0
                if not res and time.time() - t0 < 1:      # долгий опрос не держится — не крутиться вхолостую
                    time.sleep(2)
            except Exception as e:
                errs += 1
                if errs in (1, 10):
                    print(f"✗ getUpdates: {type(e).__name__} {getattr(e, 'code', '')}", flush=True)
                time.sleep(min(5 * errs, 60))
                continue
            for u in res:
                self.offset = u["update_id"] + 1
                try:
                    self.hunter.handle_update(u)
                except Exception as e:       # один плохой апдейт не должен убить слушателя
                    print(f"✗ обработка обновления: {type(e).__name__} {e}", flush=True)
