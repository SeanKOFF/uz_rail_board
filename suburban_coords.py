"""Координаты станций электричек: data/suburban_coords.json.

Разовый инструмент, как station_coords.py, не часть автосборки. У станций
в data/suburban.json есть только названия (id из elektropoyezd.uz), а
для карты и видео нужны координаты.

    export YANDEX_RASP_KEY=...
    python3 suburban_coords.py            # разведка: что нашлось, ничего не пишет
    python3 suburban_coords.py --apply    # записать, если нашлось всё

Источник — тот же stations_list Яндекса, что у station_coords.py. Ответ
большой (весь мир), поэтому сохраняется в .cache/stations_list.json;
--refresh скачивает заново.

Названия у elektropoyezd.uz и Яндекса расходятся («Ташкент Южный» и
«Ташкент-Южный», «Сариасия» и «Сарыасия», «Коканд (вокзал)» и «Коканд 1»),
поэтому сравниваются нормализованные. Если кандидатов несколько или нет
ни одного, решение принимается руками в YANDEX_CODE или MANUAL ниже —
угадывать скрипт не будет. Запись только когда у каждой станции, где
останавливается действующая электричка, ровно одна точка, и все
проверки пройдены.
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

SUBURBAN = Path("data/suburban.json")
LONG_COORDS = Path("data/station_coords.json")
CACHE = Path(".cache/stations_list.json")
OUT = Path("data/suburban_coords.json")

# id станции elektropoyezd.uz -> код Яндекса, когда кандидатов несколько
# или название не совпало. Код брать из вывода разведки.
YANDEX_CODE = {
    41: "s9620702",     # Ховас → «Хаваст»; ЗП и СП — парки той же станции
    35: "s9620651",     # Сырдарья → «Сырдарьинская», единственная с таким именем
}

# id станции -> (широта, долгота, откуда взято), если у Яндекса её нет.
# Только проверенные координаты (OSM, карта станции), не по памяти.
MANUAL = {
    25: (41.62404, 69.93762, "osm:node/12618008915 Chinorkent"),
    142: (41.30047, 69.66655, "osm:node/13766680940 Parkent"),
    124: (39.43951, 67.22976, "osm:node/13717491021 Urgut"),
    141: (41.58370, 60.63472, "osm:node/14110153095 Urganch Aeroporti"),
    # Яндекс отдаёт координаты только «Учкудук-2», и они совпадают с OSM
    # «Учкудук», а не с OSM «Uchquduq-2» (в 11 км южнее). Берём OSM.
    137: (42.11750, 63.65708, "osm:node/1592211596 Учкудук"),
    # Станции «Шахрисабз» в OSM нет; ветка из Карши кончается станцией
    # Китаб, в 4 км от Шахрисабза. 94 км от Карши за 142 мин — сходится.
    68: (39.09028, 66.83745, "osm:node/245607879 Kitob, конечная ветки"),
}

# id станции -> почему не записываем. Коканд (Кольцевой) сопоставляется
# с «Коканд 1», но может оказаться «Коканд-2» (у Яндекса без координат).
# Поездов от неё сейчас нет, неверная точка хуже отсутствующей.
SKIP = {
    125: "неясно, Коканд 1 или Коканд-2; поездов нет",
    # Термез — Учкызыл — Наушахар — Шерабад — Болдыр — Сурхонобод (elektropoyezd.uz).
    # За Болдыром подходят по времени разъезды 162 и 161 и ветка к Sherobod Sement;
    # посёлок Сурхонобод, который знает OSM, — в Зааминском районе, не тот.
    105: "не определена: три кандидата за Болдыром",
}

BBOX = (55.9, 37.1, 73.2, 45.6)          # lon0, lat0, lon1, lat1 — Узбекистан
SAME_NAME_KM = 3.0                        # расхождение с station_coords.json
MAX_KMH = 110                             # по прямой между концами: быстрее — перепутана точка


def norm(s):
    s = s.lower().replace("ё", "е").replace("ы", "и")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[-—–.,]", " ", s)
    words = [w for w in s.split()
             if w not in {"вокзал", "станция", "ст", "пасс", "пассажирский", "о", "п"}
             and not w.isdigit()]
    return " ".join(words)


def km(a, b):
    (la1, lo1), (la2, lo2) = a, b
    x = math.radians(lo2 - lo1) * math.cos(math.radians((la1 + la2) / 2))
    y = math.radians(la2 - la1)
    return 6371 * math.hypot(x, y)


def load_yandex(refresh):
    if CACHE.exists() and not refresh:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    from yandex import call, RaspError
    try:
        data = call("stations_list")
    except RaspError as e:
        sys.exit(f"Ошибка: {e}")
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def uz_rail_stations(data):
    """Железнодорожные станции и платформы Узбекистана из stations_list."""
    uz = next((c for c in data["countries"] if "Узбекистан" in c.get("title", "")), None)
    if uz is None:
        sys.exit("Узбекистан не найден в ответе stations_list")
    out = []
    for region in uz.get("regions", []):
        for settlement in region.get("settlements", []):
            for st in settlement.get("stations", []):
                if st.get("transport_type") not in ("train", "suburban"):
                    continue
                lat, lon = st.get("latitude"), st.get("longitude")
                if lat in (None, "") or lon in (None, ""):
                    continue
                out.append(dict(title=st["title"], code=st["codes"].get("yandex_code"),
                                settlement=settlement.get("title", ""),
                                region=region.get("title", ""),
                                type=st.get("station_type", ""),
                                lat=float(lat), lon=float(lon)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="записать data/suburban_coords.json")
    ap.add_argument("--refresh", action="store_true", help="скачать stations_list заново")
    args = ap.parse_args()

    sub = json.loads(SUBURBAN.read_text(encoding="utf-8"))
    names = {int(k): v["ru"] for k, v in sub["stations"].items()}
    used = set()
    for r in sub["routes"]:
        if not (r["active"] and r["listed"]):
            continue
        for t in r["trains"]:
            if t["active"]:
                used.update((t["from"], t["to"]))

    yandex = uz_rail_stations(load_yandex(args.refresh))
    by_code = {s["code"]: s for s in yandex}
    by_norm = {}
    for s in yandex:
        by_norm.setdefault(norm(s["title"]), []).append(s)

    long_coords = json.loads(LONG_COORDS.read_text(encoding="utf-8"))
    long_by_norm = {norm(v["title"]): (float(v["lat"]), float(v["lon"]))
                    for v in long_coords.values() if v["lat"] not in (None, "")}

    result, problems = {}, []
    print(f"Станций электричек: {len(names)}, из них с действующими поездами: {len(used)}\n")
    for sid in sorted(names, key=lambda i: names[i]):
        title = names[sid]
        mark = "" if sid in used else "  (поездов нет, не обязательна)"
        if sid in SKIP:
            print(f"  {sid:>3} {title}: пропущена — {SKIP[sid]}")
            if sid in used:
                lost = [f"{t['number']} ({r['name']['ru']})" for r in sub["routes"]
                        for t in r["trains"] if t["active"] and sid in (t["from"], t["to"])]
                print(f"        ВНИМАНИЕ: без неё на карте не будет поездов: {', '.join(lost)}")
                used.discard(sid)
            continue
        if sid in MANUAL:
            lat, lon, src = MANUAL[sid]
            result[sid] = dict(title=title, lat=lat, lon=lon, source=f"manual: {src}")
            print(f"  {sid:>3} {title}: вручную ({lat}, {lon}){mark}")
            continue
        if sid in YANDEX_CODE:
            s = by_code.get(YANDEX_CODE[sid])
            if s is None:
                problems.append(f"{title}: код {YANDEX_CODE[sid]} не найден в stations_list")
                continue
            cands = [s]
        else:
            cands = by_norm.get(norm(title), [])
        if len(cands) == 1:
            s = cands[0]
            result[sid] = dict(title=title, lat=s["lat"], lon=s["lon"],
                               source=f"yandex:{s['code']}")
            same = "" if s["title"] == title else f" ← «{s['title']}»"
            print(f"  {sid:>3} {title}: {s['code']} ({s['lat']:.4f}, {s['lon']:.4f}){same}{mark}")
            continue
        print(f"  {sid:>3} {title}: {'нет совпадений' if not cands else f'{len(cands)} кандидата'}{mark}")
        pool = cands
        if not pool:
            # подсказка: станции, чьё название содержит первое слово нашего
            head = norm(title).split()[0] if norm(title) else ""
            pool = [s for s in yandex if head and head[:5] in norm(s["title"])][:8]
            if pool:
                print("        похожие по названию:")
        for s in pool:
            print(f"        {s['code']}  {s['title']} · {s['settlement']}, {s['region']} "
                  f"· {s['type']} ({s['lat']:.4f}, {s['lon']:.4f})")
        if sid in used:
            problems.append(f"{title}: {'нет совпадений' if not cands else 'несколько кандидатов'}")

    # Проверки на данных
    print()
    for sid, v in result.items():
        lat, lon = v["lat"], v["lon"]
        if not (BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]):
            problems.append(f"{v['title']}: ({lat}, {lon}) за пределами Узбекистана")
        ref = long_by_norm.get(norm(v["title"]))
        if ref:
            d = km((lat, lon), ref)
            flag = "  ← РАСХОЖДЕНИЕ" if d > SAME_NAME_KM else ""
            print(f"  сверка с station_coords.json: {v['title']} — {d:.1f} км{flag}")
            if d > SAME_NAME_KM:
                problems.append(f"{v['title']}: {d:.1f} км от одноимённой станции дальних поездов")
    print()
    for r in sub["routes"]:
        for t in r["trains"]:
            if not t["active"] or t["from"] not in result or t["to"] not in result:
                continue
            a, b = result[t["from"]], result[t["to"]]
            d = km((a["lat"], a["lon"]), (b["lat"], b["lon"]))
            v = d / (t["dur"] / 60)
            line = (f"{t['number']} {a['title']} → {b['title']}: {d:.0f} км по прямой "
                    f"за {t['dur']} мин = {v:.0f} км/ч")
            if v > MAX_KMH:
                problems.append(line + " — быстрее поезда, точка перепутана")
            elif v < 15:
                # не ошибка: кольцевой маршрут или путь сильно в обход
                print(f"  медленно по прямой, проверить глазами: {line}")
            break                                   # хватает одного поезда на маршрут

    seen = {}
    for sid, v in result.items():
        seen.setdefault(v["source"], []).append(v["title"])
    for src, titles in seen.items():
        if len(titles) > 1 and src.startswith("yandex:"):
            print(f"  одна точка у нескольких станций ({src}): {', '.join(titles)} — проверить")

    missing = sorted(names[s] for s in used if s not in result)
    if missing:
        problems.append(f"без координат: {', '.join(missing)}")
    if problems:
        print("Не записано. Разобрать:")
        for p in problems:
            print(f"  • {p}")
        print("\nНеоднозначные — код в YANDEX_CODE, отсутствующие — в MANUAL, затем повторить.")
        sys.exit(1)
    if not args.apply:
        print(f"Всё сошлось: {len(result)} станций. Записать: python3 suburban_coords.py --apply")
        return
    OUT.write_text(json.dumps({str(k): v for k, v in sorted(result.items())},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Записано: {OUT} ({len(result)} станций)")


if __name__ == "__main__":
    main()
