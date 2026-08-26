#!/usr/bin/env python3
"""
Добавляет в seed.json машиночитаемые ссылки на города, чтобы сайт мог
показывать заголовки на трёх языках.

    python i18n.py

К каждой записи добавляется поле cities — список городов в именительном
падеже. Русский заголовок ('из Бухары') остаётся как есть, а узбекский и
английский собираются на стороне сайта из cities и словаря NAMES.

Также пишет data/names.json — справочник ru/uz/en для всех встреченных
городов.
"""

import json
import re
from pathlib import Path

from from_table import genitive

ROOT = Path(__file__).parent

# Города, которых нет в stations.json (конечные пункты и заграница).
EXTRA = {
    "Кунград":    ("Qo'ng'irot", "Kungrad"),
    "Шават":      ("Shovot", "Shovot"),
    "Алат":       ("Olot", "Olot"),
    "Сарыасия":   ("Sariosiyo", "Sariosiyo"),
    "Сарыассия":  ("Sariosiyo", "Sariosiyo"),
    "Шахрисабз":  ("Shahrisabz", "Shakhrisabz"),
    "Москва":     ("Moskva", "Moscow"),
    "Волгоград":  ("Volgograd", "Volgograd"),
    "Алматы":     ("Almati", "Almaty"),
    "Казань":     ("Qozon", "Kazan"),
    "Душанбе":    ("Dushanbe", "Dushanbe"),
    "Китаб":      ("Kitob", "Kitab"),
    "Волжский":   ("Voljskiy", "Volzhsky"),
    "Ангрен":     ("Angren", "Angren"),
    "Пап":        ("Pop", "Pop"),
    "Чартак":     ("Chortoq", "Chartak"),
    "Чуст":       ("Chust", "Chust"),
    "Каттакурган": ("Kattaqo'rg'on", "Kattakurgan"),
    "Кызылтепа":  ("Qiziltepa", "Kyzyltepa"),
    "Даштобод":   ("Dashtobod", "Dashtobod"),
    "Хазарасп":   ("Hazorasp", "Khazarasp"),
    "Кува":       ("Quva", "Kuva"),
    "Алтыарык":   ("Oltiariq", "Oltiariq"),
    "Денау":      ("Denov", "Denau"),
    "Кумкурган":  ("Qumqo'rg'on", "Kumkurgan"),
    "Тукимачи":   ("To'qimachi", "Tukimachi"),
}


# Уточнение вокзала: хранится отдельно от города, чтобы не потерялось
# при переводе. 'Ташкент-Южный' -> город 'Ташкент' + уточнение 'south'.
QUALIFIERS = [
    (r"центральн(ый|ого)|central|markaziy", "central"),
    (r"южн(ый|ого)|janubiy|south",          "south"),
    (r"северн(ый|ого)|shimoliy|north",      "north"),
    (r"пассажирск(ий|ого)|пасс\.?",         "pass"),
]


def qualifier(name):
    """Вытаскивает уточнение вокзала: 'Ташкент-Южный' -> 'south',
    'Бухара 1' -> '1'. Возвращает пустую строку, если уточнения нет."""
    low = re.sub(r"\s*\([^)]*\)", "", name).strip().lower()
    for pattern, key in QUALIFIERS:
        if re.search(r"[-\s](" + pattern + r")$", low):
            return key
    m = re.search(r"\s(\d+)$", low)
    return m.group(1) if m else ""


def normalize(name):
    """'Бухара 1' -> 'Бухара', 'Ташкент-Южный' -> 'Ташкент',
    'Москва (Павелецкий вокзал)' -> 'Москва', 'Каттакурган-Пасс.' -> 'Каттакурган'."""
    n = re.sub(r"\s*\([^)]*\)", "", name).strip()          # скобки
    n = re.sub(r"[-\s](Пасс\.?|Центр\.?|Центральн(ый|ого)|Южн(ый|ого)|"
               r"Северн(ый|ого)|Пассажирск(ий|ого)|Тенизи)$", "",
               n, flags=re.I).strip()
    n = re.sub(r"\s*\d+$", "", n).strip()                   # 'Бухара 1'
    return n


def build_names():
    ref = json.loads((ROOT / "stations.json").read_text(encoding="utf-8"))
    names = {st["ru"]: {"ru": st["ru"], "uz": st["uz"], "en": st["en"]}
             for st in ref["stations"]}
    for ru, (uz, en) in EXTRA.items():
        names.setdefault(ru, {"ru": ru, "uz": uz, "en": en})
    return names


def merge_extra(seed):
    """Добавляет рейсы из extra.json, которых нет в выдаче Яндекса.

    build_yandex.py --apply перезаписывает seed.json целиком, поэтому
    ручные записи должны жить отдельным файлом и подмешиваться заново
    после каждой сборки.

    Если рейс появился у Яндекса — запись из extra.json пропускается,
    иначе он задвоится на табло.
    """
    f = ROOT / "extra.json"
    if not f.exists():
        return 0, []

    data = json.loads(f.read_text(encoding="utf-8"))
    have = {(re.match(r"^\d+", r["number"]).group(0), r["direction"], r["station"])
            for r in seed}

    added, skipped = 0, []
    for r in data.get("trains", []):
        key = (re.match(r"^\d+", r["number"]).group(0), r["direction"], r["station"])
        if key in have:
            skipped.append(r["number"])
            continue
        seed.append(dict(r))
        added += 1
    return added, skipped


def apply_brands(seed):
    """Проставляет бренды из brands.json — Яндекс их не отдаёт.

    Сшивка по цифрам номера: суффиксы у одного поезда плавают
    (054Ф, 054Щ, 054 — один и тот же рейс).
    """
    f = ROOT / "brands.json"
    if not f.exists():
        return 0
    table = {k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items()
             if not k.startswith("_")}

    n = 0
    for r in seed:
        m = re.match(r"^\d+", r["number"])
        brand = table.get(m.group(0)) if m else None
        if brand and r.get("brand") != brand:
            r["brand"] = brand
            n += 1
        # Скоростным рисуем плашку акцентным цветом.
        r["fast"] = brand in ("Afrosiyob", "Jaloliddin Manguberdi")
    return n


def main():
    names = build_names()

    # Обратный словарь: и именительный, и родительный падеж -> канон.
    lookup = {}
    for ru in names:
        for form in (ru, genitive(ru)):
            lookup[form.lower()] = ru
            lookup[normalize(form).lower()] = ru

    seed = json.loads((ROOT / "seed.json").read_text(encoding="utf-8"))
    extra, dupes = merge_extra(seed)
    branded = apply_brands(seed)
    unknown = set()

    for r in seed:
        title = (r.get("title") or "").strip()
        body = re.sub(r"^из\s+", "", title)
        # Режем только по тире-разделителю маршрута, не по дефису в названии.
        parts = [p.strip() for p in re.split(r"\s+—\s+|\s+-\s+", body) if p.strip()]

        cities, quals = [], []
        for p in parts:
            canon = lookup.get(p.lower()) or lookup.get(normalize(p).lower())
            if canon:
                cities.append(canon)
                quals.append(qualifier(p))
            elif p != "—":
                unknown.add(p)
        r["cities"] = cities
        r["quals"] = quals

    (ROOT / "seed.json").write_text(
        json.dumps(seed, ensure_ascii=False, indent=1), encoding="utf-8")

    out = ROOT / "data" / "names.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(names, ensure_ascii=False, indent=1), encoding="utf-8")

    filled = sum(1 for r in seed if r["cities"])
    print(f"Записей: {len(seed)}, с распознанными городами: {filled}")
    with_brand = sum(1 for r in seed if r.get("brand"))
    print(f"Брендов проставлено: {branded} (всего с брендом: {with_brand})")
    if extra:
        print(f"Добавлено из extra.json: {extra}")
    if dupes:
        print(f"Пропущено — уже есть у Яндекса: {', '.join(sorted(set(dupes)))}")
        print("Такие записи можно убрать из extra.json.")
    print(f"Справочник названий: {len(names)} городов → {out}")
    if unknown:
        print(f"Не распознано: {', '.join(sorted(unknown))}")


if __name__ == "__main__":
    main()
