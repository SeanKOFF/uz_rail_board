"""
Разовый патч: 730Ф/729Ф (Ташкент-Андижан) на электропоезд CRRC-Далянь,
с 10.09.2026. Кува убирается из маршрута, Ангрен/Пап/Кокан/Маргилан —
только одно время (объявление не даёт отдельно приход/отход, поэтому
для этих станций arrival = departure, стоянка считается технической).

Не для коммита в репозиторий — одноразовый инструмент. Перед запуском:
делает .bak-копии обоих файлов рядом с оригиналами.

Запускать из корня репозитория:
    python3 patch_730_729.py
"""
import json
import shutil
from pathlib import Path

ROUTES_PATH = Path("data/routes.json")
SCHEDULE_PATH = Path("data/schedule.json")

TASHKENT = {
    "code": "2900001",
    "title": "Ташкент-Центральный",
    "names": {"ru": "Ташкент-Центральный", "uz": "Toshkent-Markaziy", "en": "Tashkent-Central"},
}
ANGREN = {
    "code": "2900679",
    "title": "Ангрен",
    "names": {"ru": "Ангрен", "uz": "Angren", "en": "Angren"},
}
PAP = {
    "code": "2900693",
    "title": "Пап",
    "names": {"ru": "Пап", "uz": "Pop", "en": "Pop"},
}
KOKAND = {
    "code": "2900880",
    "title": "Коканд 1",
    "names": {"ru": "Коканд 1", "uz": "Qo'qon 1", "en": "Kokand 1"},
}
MARGILAN = {
    "code": "2900920",
    "title": "Маргилан",
    "names": {"ru": "Маргилан", "uz": "Marg'ilon", "en": "Margilan"},
}
ANDIJAN = {
    "code": "2900680",
    "title": "Андижан 1",
    "names": {"ru": "Андижан 1", "uz": "Andijon 1", "en": "Andijan 1"},
}

# (станция, приход, отход). Для промежуточных станций объявление даёт
# только одно время — храним его как departure, а arrival оставляем
# пустым: если заполнить оба одним и тем же значением, в развороте
# маршрута на сайте (routeHtml в index.html) это отрисуется как
# дублирующееся "10:25 — 10:25". train-map.html на такой пропуск не
# ломается — там уже есть свой fallback (departureDt || arrivalDt).
NEW_730_STOPS = [
    (TASHKENT, None, "09:20"),
    (ANGREN, None, "10:25"),
    (PAP, None, "12:00"),
    (KOKAND, None, "12:28"),
    (MARGILAN, None, "13:00"),
    (ANDIJAN, "13:35", None),
]
NEW_729_STOPS = [
    (ANDIJAN, None, "15:38"),
    (MARGILAN, None, "16:11"),
    (KOKAND, None, "16:43"),
    (PAP, None, "17:11"),
    (ANGREN, None, "18:50"),
    (TASHKENT, "19:53", None),
]


def build_route_entry(number, stop_defs):
    stops = []
    for i, (place, arr, dep) in enumerate(stop_defs):
        stops.append({
            "code": place["code"],
            "title": place["title"],
            "names": place["names"],
            "arrival": arr,
            "departure": dep,
            "stop": None,
            "offset": 0,
        })
    signature = [[s["code"], s["arrival"], s["departure"]] for s in stops]
    return {
        "number": number,
        "stops": stops,
        "cut_before": 0,
        "cut_after": 0,
        "signature": signature,
    }


def patch_routes():
    data = json.loads(ROUTES_PATH.read_text(encoding="utf-8"))
    before_730 = json.dumps(data.get("730"), ensure_ascii=False)
    before_729 = json.dumps(data.get("729"), ensure_ascii=False)

    data["730"] = [build_route_entry("730Ф", NEW_730_STOPS)]
    data["729"] = [build_route_entry("729Ф", NEW_729_STOPS)]

    shutil.copy2(ROUTES_PATH, ROUTES_PATH.with_suffix(".json.bak"))
    ROUTES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("routes.json: было 730 ->", before_730[:80], "...")
    print("routes.json: было 729 ->", before_729[:80], "...")
    print("routes.json обновлён, резервная копия:", ROUTES_PATH.with_suffix(".json.bak"))


# (number, station, direction) -> новое time_local
SCHEDULE_UPDATES = {
    ("730Ф", "tashkent-central", "departure"): "09:20",
    ("730Ф", "kokand", "arrival"): "12:28",
    ("730Ф", "kokand", "departure"): "12:28",
    ("730Ф", "margilan", "arrival"): "13:00",
    ("730Ф", "margilan", "departure"): "13:00",
    ("730Ф", "andijan", "arrival"): "13:35",
    ("729Ф", "andijan", "departure"): "15:38",
    ("729Ф", "margilan", "arrival"): "16:11",
    ("729Ф", "margilan", "departure"): "16:11",
    ("729Ф", "kokand", "arrival"): "16:43",
    ("729Ф", "kokand", "departure"): "16:43",
    ("729Ф", "tashkent-central", "arrival"): "19:53",
}


def patch_schedule():
    data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    applied = 0
    missing = set(SCHEDULE_UPDATES.keys())
    for t in data["trains"]:
        key = (t["number"], t["station"], t["direction"])
        if key in SCHEDULE_UPDATES:
            old = t["time_local"]
            new = SCHEDULE_UPDATES[key]
            t["time_local"] = new
            missing.discard(key)
            applied += 1
            print(f"schedule.json: {key} {old} -> {new}")

    if missing:
        raise SystemExit(f"Не нашлись записи в schedule.json: {sorted(missing)}")

    shutil.copy2(SCHEDULE_PATH, SCHEDULE_PATH.with_suffix(".json.bak"))
    SCHEDULE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"schedule.json обновлён, записей изменено: {applied}, резервная копия: {SCHEDULE_PATH.with_suffix('.json.bak')}")


if __name__ == "__main__":
    patch_routes()
    patch_schedule()
