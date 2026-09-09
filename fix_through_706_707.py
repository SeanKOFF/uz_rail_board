"""
Фикс бага из предыдущего патча (patch_khiva_bukhara.py): у записей
706Ф/707Ф на станции Ургенч поле through было жёстко записано как
False, хотя Ургенч для них - проходящая станция (706Ф едет дальше на
Хиву, 707Ф - дальше на Бухару). Из-за этого на сайте пропадала метка
«проходящий».

Правит ровно 4 записи: (706Ф, urgench, arrival), (706Ф, urgench,
departure), (707Ф, urgench, arrival), (707Ф, urgench, departure).
Больше ничего не трогает - у остальных 8 записей (Хива/Бухара/Ташкент)
through=False верен, это настоящие конечные точки.

Не для коммита в репозиторий. Делает .bak перед перезаписью.

Запускать из корня репозитория:
    python3 fix_through_706_707.py
"""
import json
import shutil
from pathlib import Path

SCHEDULE_PATH = Path("data/schedule.json")

TARGETS = {
    ("706Ф", "urgench", "arrival"),
    ("706Ф", "urgench", "departure"),
    ("707Ф", "urgench", "arrival"),
    ("707Ф", "urgench", "departure"),
}


def main():
    data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    fixed = set()
    for t in data["trains"]:
        key = (t["number"], t["station"], t["direction"])
        if key in TARGETS:
            if t["through"] is True:
                print(f"{key}: уже True, пропускаю")
            else:
                t["through"] = True
                print(f"{key}: through False -> True")
            fixed.add(key)

    missing = TARGETS - fixed
    if missing:
        raise SystemExit(f"Не нашлись записи: {sorted(missing)}")

    shutil.copy2(SCHEDULE_PATH, SCHEDULE_PATH.with_suffix(".json.bak"))
    SCHEDULE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("schedule.json обновлён. Резервная копия:", SCHEDULE_PATH.with_suffix(".json.bak"))


if __name__ == "__main__":
    main()
