"""
Разовый патч: новые рейсы 707Ф/706Ф (Хива - Бухара 1) и 701Ф/702Ф
(Бухара 1 - Ташкент), подтверждены четырежды на eticket.railway.uz
(поиском по Коканду/Андижану/Ургенчу/Бухаре/Ташкенту), 09.09.2026.

В отличие от патча 730/729, тут не правка существующих записей, а
ДОБАВЛЕНИЕ новых - ключей 707/706/701/702 в data/routes.json раньше
не было вообще.

ВАЖНО - что НЕ проверено и сознательно не включено:
  - 707Ф/706Ф: между Ургенчем и Бухарой почти наверняка есть Хазарасп
    (там останавливаются все остальные поезда на этом участке), но
    подтверждённого времени для него нет - в routes.json НЕ добавляю,
    чтобы не выдумывать цифры. Маршрут в раскрытии на сайте будет
    показывать 3 точки вместо вероятных 4.
  - 701Ф/702Ф: известны только конечные точки (Бухара 1 и Ташкент).
    Реальный маршрут наверняка идёт через Навои/Самарканд/Джизак/
    Гулистан (как и все остальные бухарские поезда), но ни одной
    промежуточной остановки не подтверждено. Поэтому 701Ф/702Ф
    сознательно НЕ добавляются в routes.json - двухточечный маршрут
    выглядел бы как выдуманный "прямой" поезд без остановок, что
    неправда. В data/schedule.json (табло по городам) они всё равно
    добавлены - там достаточно только конечных времён, это не вводит
    в заблуждение.
  - brand и fast везде null/false - реальных данных о бренде и
    категории скорости от Яндекса ещё не было, гадать не стал.
    (t.brand ? ... : '' в index.html и так спрячет плашку бренда.)

Дни недели - "1"=понедельник ... "7"=воскресенье (как в build_yandex.py,
через date.isoweekday()), взяты из текста новости, а не подобраны:
  707Ф - ежедневно, кроме вторника  -> 134567
  706Ф - ежедневно, кроме понедельника -> 234567
  701Ф - только понедельник -> 1
  702Ф - только вторник -> 2

Не для коммита в репозиторий - одноразовый инструмент. Делает .bak
рядом с оригиналами перед перезаписью.

Запускать из корня репозитория:
    python3 patch_khiva_bukhara.py
"""
import json
import shutil
from pathlib import Path

ROUTES_PATH = Path("data/routes.json")
SCHEDULE_PATH = Path("data/schedule.json")

KHIVA = {"code": "2900172", "title": "Хива",
         "names": {"ru": "Хива", "uz": "Xiva", "en": "Khiva"}}
URGENCH = {"code": "2900790", "title": "Ургенч",
           "names": {"ru": "Ургенч", "uz": "Urganch", "en": "Urgench"}}
BUKHARA = {"code": "2900800", "title": "Бухара 1",
           "names": {"ru": "Бухара 1", "uz": "Buxoro 1", "en": "Bukhara 1"}}
TASHKENT = {"code": "2900001", "title": "Ташкент-Центральный",
            "names": {"ru": "Ташкент-Центральный", "uz": "Toshkent-Markaziy", "en": "Tashkent-Central"}}

# (станция, arrival, departure) - как и в патче 730/729: если из
# подтверждённых данных известно только одно время на промежуточной
# станции, кладём его как departure, а arrival оставляем пустым, чтобы
# в развороте маршрута не дублировалось "10:20 - 10:20".
ROUTE_707 = [
    (KHIVA, None, "09:55"),
    (URGENCH, None, "10:20"),
    (BUKHARA, "14:32", None),
]
ROUTE_706 = [
    (BUKHARA, None, "15:45"),
    (URGENCH, None, "19:49"),
    (KHIVA, "20:25", None),
]


def build_route_entry(number, stop_defs):
    stops = []
    for place, arr, dep in stop_defs:
        stops.append({
            "code": place["code"], "title": place["title"], "names": place["names"],
            "arrival": arr, "departure": dep, "stop": None, "offset": 0,
        })
    signature = [[s["code"], s["arrival"], s["departure"]] for s in stops]
    return {"number": number, "stops": stops, "cut_before": 0, "cut_after": 0, "signature": signature}


def patch_routes():
    data = json.loads(ROUTES_PATH.read_text(encoding="utf-8"))
    for key in ("707", "706"):
        if key in data:
            raise SystemExit(f"Ключ {key} уже есть в routes.json - патч рассчитан на добавление, а не перезапись")

    data["707"] = [build_route_entry("707Ф", ROUTE_707)]
    data["706"] = [build_route_entry("706Ф", ROUTE_706)]

    shutil.copy2(ROUTES_PATH, ROUTES_PATH.with_suffix(".json.bak"))
    ROUTES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("routes.json: добавлены ключи 707, 706. Резервная копия:", ROUTES_PATH.with_suffix(".json.bak"))


def make_record(number, station, direction, time_local, days, title, cities, quals):
    return {
        "number": number, "direction": direction, "title": title,
        "brand": None, "fast": False, "time_local": time_local, "days": days,
        "via": None, "station": station, "through": False,
        "cities": cities, "quals": quals,
    }


NEW_SCHEDULE_RECORDS = [
    # 707Ф Хива -> Бухара 1, ежедневно кроме вторника (134567)
    make_record("707Ф", "khiva", "departure", "09:55", "134567", "Бухара 1", ["Бухара"], ["1"]),
    make_record("707Ф", "urgench", "arrival", "10:20", "134567", "из Хивы", ["Хива"], []),
    make_record("707Ф", "urgench", "departure", "10:20", "134567", "Бухара 1", ["Бухара"], ["1"]),
    make_record("707Ф", "bukhara", "arrival", "14:32", "134567", "из Хивы", ["Хива"], []),
    # 706Ф Бухара 1 -> Хива, ежедневно кроме понедельника (234567)
    make_record("706Ф", "bukhara", "departure", "15:45", "234567", "Хива", ["Хива"], []),
    make_record("706Ф", "urgench", "arrival", "19:49", "234567", "из Бухары 1", ["Бухара"], ["1"]),
    make_record("706Ф", "urgench", "departure", "19:49", "234567", "Хива", ["Хива"], []),
    make_record("706Ф", "khiva", "arrival", "20:25", "234567", "из Бухары 1", ["Бухара"], ["1"]),
    # 701Ф Бухара 1 -> Ташкент, только понедельник (1)
    make_record("701Ф", "bukhara", "departure", "15:17", "1", "Ташкент-Центральный", ["Ташкент"], ["central"]),
    make_record("701Ф", "tashkent-central", "arrival", "21:22", "1", "из Бухары 1", ["Бухара"], ["1"]),
    # 702Ф Ташкент -> Бухара 1, только вторник (2)
    make_record("702Ф", "tashkent-central", "departure", "10:05", "2", "Бухара 1", ["Бухара"], ["1"]),
    make_record("702Ф", "bukhara", "arrival", "15:17", "2", "из Ташкента-Центрального", ["Ташкент"], ["central"]),
]


def patch_schedule():
    data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    existing_keys = {(t["number"], t["station"], t["direction"]) for t in data["trains"]}

    added = 0
    for rec in NEW_SCHEDULE_RECORDS:
        key = (rec["number"], rec["station"], rec["direction"])
        if key in existing_keys:
            raise SystemExit(f"Запись {key} уже есть в schedule.json - патч рассчитан на добавление")
        data["trains"].append(rec)
        added += 1

    shutil.copy2(SCHEDULE_PATH, SCHEDULE_PATH.with_suffix(".json.bak"))
    SCHEDULE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"schedule.json: добавлено записей: {added}. Резервная копия:", SCHEDULE_PATH.with_suffix(".json.bak"))


if __name__ == "__main__":
    patch_routes()
    patch_schedule()
