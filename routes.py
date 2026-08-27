#!/usr/bin/env python3
"""
Собирает маршруты рейсов для раскрытия на сайте.

    python3 routes.py

Данные берутся из `.cache/threads` — их скачал build_yandex.py, когда
собирал расписание. Ни одного нового запроса к API: маршруты уже лежат
на диске.

Оставляем только узбекские станции: у московского поезда сорок остановок
по Казахстану и России, и список превращается в простыню. Отброшенные
участки помечаются, чтобы на сайте было честно видно — маршрут длиннее.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
CACHE = ROOT / ".cache" / "threads"
OUT = ROOT / "data" / "routes.json"

# Узбекские станции: express-коды начинаются с 29.
UZ = re.compile(r"^29\d{5}$")


# Станции маршрутов на трёх языках. Дополняет data/names.json: там только
# города с табло, а в маршрутах встречаются и мелкие станции.
STATION_NAMES = {
    "Ташкент-Центральный": ("Toshkent-Markaziy", "Tashkent-Central"),
    "Ташкент-Южный": ("Toshkent-Janubiy", "Tashkent-South"),
    "Тукимачи": ("To'qimachi", "Tukimachi"),
    "Гулистан": ("Guliston", "Gulistan"),
    "Джизак": ("Jizzax", "Jizzakh"),
    "Даштобод": ("Dashtobod", "Dashtobod"),
    "Самарканд": ("Samarqand", "Samarkand"),
    "Каттакурган-Пасс.": ("Kattaqo'rg'on", "Kattakurgan"),
    "Зирабулак": ("Zirabuloq", "Zirabulak"),
    "Зиевуддин": ("Ziyovuddin", "Ziyovuddin"),
    "Навои": ("Navoiy", "Navoi"),
    "Кызылтепа": ("Qiziltepa", "Kyzyltepa"),
    "Бухара 1": ("Buxoro 1", "Bukhara 1"),
    "Якатут": ("Yakkatut", "Yakkatut"),
    "Каракуль": ("Qorako'l", "Karakul"),
    "Алат": ("Olot", "Olot"),
    "Каган": ("Kogon", "Kagan"),
    "Карши": ("Qarshi", "Karshi"),
    "Китаб": ("Kitob", "Kitab"),
    "Шахрисабз": ("Shahrisabz", "Shakhrisabz"),
    "Термез": ("Termiz", "Termez"),
    "Денау": ("Denov", "Denau"),
    "Кумкурган": ("Qumqo'rg'on", "Kumkurgan"),
    "Сарыасия": ("Sariosiyo", "Sariosiyo"),
    "Ургенч": ("Urganch", "Urgench"),
    "Хива": ("Xiva", "Khiva"),
    "Хазарасп": ("Hazorasp", "Khazarasp"),
    "Шават": ("Shovot", "Shovot"),
    "Нукус": ("Nukus", "Nukus"),
    "Кунград": ("Qo'ng'irot", "Kungrad"),
    "Каракалпакстан": ("Qoraqalpog'iston", "Karakalpakstan"),
    "Андижан 1": ("Andijon 1", "Andijan 1"),
    "Наманган": ("Namangan", "Namangan"),
    "Чартак": ("Chortoq", "Chartak"),
    "Чуст": ("Chust", "Chust"),
    "Пап": ("Pop", "Pop"),
    "Ангрен": ("Angren", "Angren"),
    "Коканд 1": ("Qo'qon 1", "Kokand 1"),
    "Маргилан": ("Marg'ilon", "Margilan"),
    "Кува": ("Quva", "Kuva"),
    "Алтыарык": ("Oltiariq", "Oltiariq"),
    "Назархан": ("Nazarxon", "Nazarkhan"),
    # Сурхандарья и юг
    "Бойсун": ("Boysun", "Boysun"),
    "Дарбанд": ("Darband", "Darband"),
    "Джаркурган": ("Jarqo'rg'on", "Jarkurgan"),
    "Шурчи": ("Sho'rchi", "Shurchi"),
    "Гумбаз": ("Gumbaz", "Gumbaz"),
    "Айрытам": ("Ayritom", "Ayritom"),
    "Сурханы": ("Surxon", "Surkhon"),
    "Тангимуш": ("Tangimush", "Tangimush"),
    "Дехконобод": ("Dehqonobod", "Dehkanabad"),
    "Окработ": ("Oqrabot", "Okrabot"),
    # Каракалпакстан
    "Ходжейли": ("Xo'jayli", "Khojeyli"),
    "Тахиаташ-Пристань": ("Taxiatosh", "Takhiatash"),
    "Элликала": ("Ellikqal'a", "Ellikkala"),
    "Туртгул": ("To'rtko'l", "Turtkul"),
    "Караозек": ("Qorao'zak", "Karauzyak"),
    "Мискин": ("Miskin", "Miskin"),
    "Жайхун": ("Jayxun", "Jayhun"),
    # Ферганская долина и центр
    "Алтынкуль": ("Oltinko'l", "Oltinkul"),
    "Джума": ("Juma", "Juma"),
    "Даутепа": ("Dovtepa", "Davtepa"),
    "Янгиабад": ("Yangiobod", "Yangiobod"),
}

# Для остальных — механическая транслитерация. Приблизительная: узбекские
# q, o', g' по русскому написанию не восстанавливаются.
TRANSLIT = {
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"yo","ж":"j","з":"z",
    "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
    "с":"s","т":"t","у":"u","ф":"f","х":"x","ц":"ts","ч":"ch","ш":"sh",
    "щ":"sh","ъ":"","ы":"i","ь":"","э":"e","ю":"yu","я":"ya",
}


def translit(name):
    out = []
    for ch in name:
        rep = TRANSLIT.get(ch.lower(), ch)
        if ch.isupper() and rep:
            rep = rep[0].upper() + rep[1:]      # «Я» -> «Ya», а не «YA»
        out.append(rep)
    return "".join(out)


GUESSED = set()


def names_of(title):
    known = STATION_NAMES.get(title)
    if known:
        return dict(ru=title, uz=known[0], en=known[1])
    GUESSED.add(title)
    guess = translit(title)
    return dict(ru=title, uz=guess, en=guess)


def hhmm(iso):
    return iso[11:16] if iso and len(iso) >= 16 else None


def day_of(iso):
    return iso[:10] if iso and len(iso) >= 10 else None


def build_route(info):
    """Маршрут: только узбекские остановки, с временем и стоянкой."""
    raw = info.get("stops", [])
    first_day = next((day_of(st.get("departure") or st.get("arrival"))
                      for st in raw if st.get("departure") or st.get("arrival")), None)

    stops, dropped_before, dropped_after = [], 0, 0
    for st in raw:
        s = st.get("station", {})
        code = (s.get("codes") or {}).get("express") or ""
        arr, dep = hhmm(st.get("arrival")), hhmm(st.get("departure"))
        if not (arr or dep):
            continue

        if not UZ.match(code):
            if stops:
                dropped_after += 1
            else:
                dropped_before += 1
            continue

        this_day = day_of(st.get("arrival") or st.get("departure"))
        offset = 0
        if this_day and first_day:
            offset = (int(this_day[8:10]) - int(first_day[8:10])) % 31

        # Стоянка: сколько поезд стоит на этой станции.
        stop_min = None
        if arr and dep and arr != dep:
            a = int(arr[:2]) * 60 + int(arr[3:])
            d = int(dep[:2]) * 60 + int(dep[3:])
            stop_min = (d - a) % 1440

        title = s.get("title") or ""
        stops.append(dict(code=code, title=title, names=names_of(title),
                          arrival=arr, departure=dep,
                          stop=stop_min, offset=offset))

    return stops, dropped_before, dropped_after


def main():
    if not CACHE.exists():
        raise SystemExit("Нет папки .cache/threads — сначала запусти build_yandex.py")

    routes = {}
    for f in sorted(CACHE.glob("*.json")):
        info = json.loads(f.read_text(encoding="utf-8"))
        stops, before, after = build_route(info)
        if len(stops) < 2:
            continue

        number = (info.get("thread", {}) or info).get("number") or ""
        number = number.strip() or f.stem
        digits = (re.match(r"^\d+", number) or [""])[0]
        if not digits:
            continue

        # У одних и тех же цифр бывают встречные рейсы: 056Ч идёт
        # Ташкент — Хива, 056Ж обратно. Маршруты у них разные, поэтому
        # держим все варианты, а нужный выбирается на сайте по времени.
        variants = routes.setdefault(digits, [])
        signature = tuple((st["code"], st["arrival"], st["departure"]) for st in stops)
        if any(v["signature"] == list(signature) for v in variants):
            continue

        variants.append(dict(number=number, stops=stops,
                             cut_before=before, cut_after=after,
                             signature=[list(x) for x in signature]))

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(routes, ensure_ascii=False, indent=1), encoding="utf-8")

    # Подпись нужна была только для отсева дублей — в файл её не пишем.
    for variants in routes.values():
        for v in variants:
            v.pop("signature", None)

    flat = [v for variants in routes.values() for v in variants]
    total = sum(len(v["stops"]) for v in flat)
    cut = sum(v["cut_before"] + v["cut_after"] for v in flat)
    print(f"Номеров: {len(routes)}, маршрутов: {len(flat)}, остановок: {total}")

    both = [d for d, v in routes.items() if len(v) > 1]
    if both:
        print(f"Встречные направления под одним номером: {', '.join(sorted(both))}")
    print(f"Отброшено зарубежных остановок: {cut}")
    if GUESSED:
        print(f"\nПереведено транслитерацией ({len(GUESSED)}) — проверь и при "
              f"необходимости впиши в STATION_NAMES:")
        for t in sorted(GUESSED):
            print(f"  {t:<26} → {translit(t)}")
    print(f"→ {OUT}  ({OUT.stat().st_size // 1024} КБ)")

    longest = max(flat, key=lambda r: len(r["stops"]))
    print(f"\nСамый длинный — {longest['number']}, {len(longest['stops'])} остановок:")
    for st in longest["stops"][:6]:
        t = f"{st['arrival'] or '  —  '} / {st['departure'] or '  —  '}"
        extra = f"  стоянка {st['stop']} мин" if st["stop"] else ""
        print(f"  {t}  {st['title']}{extra}")


if __name__ == "__main__":
    main()
