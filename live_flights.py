#!/usr/bin/env python3
"""
Живое табло аэропортов Узбекистана — uzairports.com.

Один маршрут на все аэропорты и оба направления:
    /flights/{event}?status={event}&airport={IATA}

Отдаёт плановое и фактическое время, статус, терминал, багажную ленту.
Пишет только в data/flights_live.json — железнодорожных данных
не касается.

    python3 live_flights.py                    # все аэропорты
    python3 live_flights.py --airport TAS      # один
    python3 live_flights.py --file /tmp/tas.html --event arrival
"""

import argparse
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://uzairports.com/flights"
OUT = "data/flights_live.json"

# Коды взяты со страницы фильтра. Ташкент, Самарканд, Бухара, Ургенч,
# Наманган, Андижан, Фергана, Карши, Нукус, Термез, Навои, Зарафшан.
AIRPORTS = ["TAS", "SKD", "BHK", "UGC", "NMA", "AZN",
            "FEG", "KSQ", "NCU", "TMJ", "NVI",
            # вертолётные площадки Silk Avia, свои страницы табло есть
            "RBZ", "OMN", "IYO"]

UA = "tabloda.uz/1.0 (+https://tabloda.uz)"

# Время на табло местное, Узбекистан круглый год UTC+5.
TZ = timezone(timedelta(hours=5))

# Строка табло. Разметка на bootstrap-классах, но узлы семантические,
# поэтому цепляемся за имена классов, а не за порядок ячеек.
ROW = re.compile(r'<div class="flight-result__content"[^>]*'
                 r'data-date="(?P<date>[^"]*)"(?P<html>.*?)'
                 r'(?=<div class="flight-result__content"|$)', re.S)

FIELDS = {
    # плановое время
    "scheduled": r'<span class="color-custom-1">\s*([\d:]+)',
    # фактическое или ожидаемое
    "estimated": r'<span class="estimated-time">\s*([\d:]+)',
    # дата факта — непусто при переходе через полночь
    "estimated_date": r'<span class="estimated-date">\s*([^<\s][^<]*?)\s*</span>',
    "status": r'<div class="flight-status"[^>]*>\s*<span[^>]*>\s*(.*?)\s*</span>',
    "number": r'<span\s+class="highlight">\s*(.*?)\s*</span>',
    "carrier": r'<span\s+class="desc[^"]*">\s*(.*?)\s*</span>',
    "terminal": r'<div class="terminal[^"]*">.*?<span>\s*(.*?)\s*</span>',
    "airline_iata": r'/airlines/([A-Z0-9]{2})\.png',
    "baggage": r'<p class="check-in[^"]*">.*?<span[^>]*>\s*(.*?)\s*</span>',
    "codeshare": r'<div\s+class="flight-code-share[^"]*">\s*(.*?)\s*</div>',
}

# Город: видимое название плюс скрытый IATA-код рядом. Код надёжнее
# названия — оно приходит на языке страницы и в трёх вариантах
# написания. Сшивать с Яндексом нужно по коду.
CITY = re.compile(r'<div class="flight-name[^"]*">\s*(?P<name>.*?)\s*'
                  r'<span style="display:none;">(?P<iata>[A-Z]{3})</span>', re.S)


# Статусы приходят на языке страницы (узбекский). Их конечное число:
# пять фиксированных и два шаблона с подстановкой времени. Приводим к
# коду, чтобы страница переводила сама, а не разбирала текст.
STATUSES = [
    (re.compile(r"^Jadval bo.?yicha$", re.I), "on_time"),
    (re.compile(r"^Parvoz yetib keldi$", re.I), "arrived"),
    (re.compile(r"^Parvoz uchib\s?ketdi$", re.I), "departed"),
    (re.compile(r"^Parvoz bekor qilindi$", re.I), "cancelled"),
    (re.compile(r"^Parvoz kechiktirildi$", re.I), "delayed"),
    # «12:45 da kutilmoqda» — ожидается в
    (re.compile(r"^(\S+)\s+da kutilmoqda$", re.I), "expected"),
    # «22:00 gacha kechiktirildi» — задержан до.
    # Бывает «xx:xx», когда время неизвестно.
    (re.compile(r"^(\S+)\s+gacha kechiktirildi$", re.I), "delayed_until"),
]


def normalize_status(text):
    """Возвращает (код, время) — время только у expected/delayed_until."""
    if not text:
        return None, None
    text = text.strip()
    for pattern, code in STATUSES:
        m = pattern.match(text)
        if m:
            value = m.group(1) if m.groups() else None
            # Время неизвестно — источник пишет заглушку.
            if value and not re.fullmatch(r"\d{1,2}:\d{2}", value):
                value = None
            return code, value
    return "unknown", None


def to_iso(date_str, time_str):
    """29.08.2026 + 08:55 → 2026-08-29T08:55:00+05:00 (время местное)."""
    if not date_str or not time_str:
        return None
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
    except ValueError:
        return None
    return dt.replace(tzinfo=TZ).isoformat()


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse(html, airport, event):
    body = html[html.rfind("</style>"):] if "</style>" in html else html
    rows = []
    for m in ROW.finditer(body):
        block = m.group("html")
        row = {"airport": airport, "event": event, "date": m.group("date")}

        for name, pattern in FIELDS.items():
            found = re.search(pattern, block, re.S)
            row[name] = clean(found.group(1)) if found else None

        row["status_code"], row["status_time"] = normalize_status(
            row["status"])
        row["scheduled_at"] = to_iso(row["date"], row["scheduled"])
        # Фактическое время может уехать на следующие сутки.
        row["estimated_at"] = to_iso(
            row["estimated_date"] or row["date"], row["estimated"])

        city = CITY.search(block)
        row["city"] = clean(city.group("name")) if city else None
        row["city_iata"] = city.group("iata") if city else None

        # Пустая карточка — пропускаем, чтобы мусор не попал в выдачу.
        if row["number"] or row["scheduled"]:
            rows.append(row)
    return rows


def fetch(airport, event):
    url = f"{BASE}/{event}?status={event}&airport={airport}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--airport", help="один аэропорт вместо всех")
    p.add_argument("--event", default=None,
                   choices=["arrival", "departure"])
    p.add_argument("--file", help="разобрать сохранённый HTML, без сети")
    p.add_argument("--out", default=OUT)
    a = p.parse_args()

    if a.file:
        html = open(a.file, encoding="utf-8").read()
        rows = parse(html, a.airport or "?", a.event or "?")
        print(f"строк: {len(rows)}")
        if rows:
            print(json.dumps(rows[0], ensure_ascii=False, indent=2))
        return

    airports = [a.airport] if a.airport else AIRPORTS
    events = [a.event] if a.event else ["arrival", "departure"]

    result, failed = [], []
    for code in airports:
        for event in events:
            try:
                rows = parse(fetch(code, event), code, event)
                print(f"  {code} {event:<10} {len(rows)}")
                result.extend(rows)
            except Exception as e:
                print(f"  {code} {event:<10} ОШИБКА: {e}")
                failed.append(f"{code}/{event}")
            time.sleep(1)

    # Неполную выдачу не применяем — на этом уже обжигались с ЖД.
    if failed:
        sys.exit(f"\nне загрузилось: {', '.join(failed)} — не пишу")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({
            "updated": datetime.now(timezone.utc).isoformat(),
            "flights": result,
        }, f, ensure_ascii=False, indent=2)
    print(f"\nвсего {len(result)} → {a.out}")

    # Незнакомый код направления показывается на сайте как есть,
    # поэтому лучше узнать о нём здесь, а не от посетителя.
    try:
        with open('cities.json', encoding='utf-8') as f:
            known = json.load(f)
        missing = sorted({r['city_iata'] for r in result
                          if r['city_iata'] and r['city_iata'] not in known})
        if missing:
            print(f"\nнет в cities.json: {', '.join(missing)}")
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    main()
