#!/usr/bin/env python3
"""
Снимок API elektropoyezd.uz от 28.09.2026 в сжатом текстовом виде
(снят из браузера) -> фикстура в формате ответов API для
`elektropoyezd.py --fixtures`.

    python3 tests/make_fixture.py tests/snapshot_2026-09-28.txt tests/fixture.json.gz
"""
import gzip
import json
import sys

TODAY, TOMORROW = "2026-09-28", "2026-09-29"


def main(src, dst):
    lines = open(src, encoding="utf-8").read().splitlines()
    regs, st, rn, cr = {}, {"ru": {}, "uz": {}, "en": {}}, {"uz": {}, "en": {}}, {}
    listing, routes, tomorrow = {}, [], {}
    cur = None
    for ln in lines:
        if ln.startswith("LIST "):
            for part in ln[5:].split():
                slug, ids = part.split(":")
                listing[slug] = [int(x) for x in ids.split(",")]
        elif ln.startswith("R|"):
            f = ln.split("|")
            cur = {"id": int(f[1]), "region": f[2], "regions": f[3].split(","), "name": f[4],
                   "from": int(f[5]), "to": int(f[6]),
                   "tariffs": [{"percent": int(a), "price": int(b)}
                               for a, b in (x.split(":") for x in f[7].split(",") if x)],
                   "fleet": f[8] or None, "active": f[9] == "1", "trains": []}
            routes.append(cur)
        elif ln.startswith("T|"):
            f = ln.split("|")
            cur["trains"].append({
                "id": int(f[1]), "train_number": f[2], "direction": f[3],
                "o": int(f[4]), "d": int(f[5]), "departs_at": f[6], "arrives_at": f[7],
                "duration_minutes": int(f[8]),
                "operating_day_numbers": [int(c) for c in f[9]],
                "is_active": f[10] == "1", "is_cancelled": f[11] == "1",
                "cancel_reason": f[12] or None,
                "delay_minutes": int(f[13]) if f[13] else None,
                "delay_reason": f[14] or None})
        elif ln.startswith("N"):
            for item in ln[1:].strip().split(";"):
                if item:
                    tid, c, dl, why = item.split(":", 3)
                    tomorrow[int(tid)] = (c == "1", int(dl) if dl else None, why or None)
        elif ln.startswith("CR "):
            _, lang, tid, why = ln.split(" ", 3)
            cr[(lang, int(tid))] = why
        elif ln.startswith("REG "):
            for item in ln[4:].split(";"):
                i, slug, ru, uz, en = item.split(":")
                regs[slug] = {"id": int(i), "slug": slug, "ru": ru, "uz": uz, "en": en}
        elif ln.startswith("ST "):
            _, lang, rest = ln.split(" ", 2)
            for item in rest.split(";"):
                i, n = item.split("~", 1)
                st[lang][int(i)] = n
        elif ln.startswith("RN "):
            _, lang, rest = ln.split(" ", 2)
            for item in rest.split(";"):
                i, n = item.split("~", 1)
                rn[lang][int(i)] = n

    def reg(slug, lang):
        r = regs[slug]
        return {"id": r["id"], "name": r[lang], "slug": slug}

    def station(i, lang):
        return {"id": i, "name": st[lang].get(i) or st["ru"].get(i)}

    fx = {}
    for lang in ("ru", "uz", "en"):
        fx[f"list_{lang}"] = {"data": [{"region": reg(slug, lang), "routes": [{"id": i} for i in ids]}
                                       for slug, ids in listing.items()]}

    for r in routes:
        for lang, date in (("ru", TODAY), ("ru", TOMORROW), ("uz", TODAY), ("en", TODAY)):
            trains = []
            for t in r["trains"]:
                tt = {k: v for k, v in t.items() if k not in ("o", "d")}
                tt["origin_station"] = station(t["o"], lang)
                tt["destination_station"] = station(t["d"], lang)
                if date == TOMORROW:
                    c, dl, why = tomorrow.get(t["id"], (False, None, None))
                    tt.update(is_cancelled=c, delay_minutes=dl, cancel_reason=why)
                elif lang != "ru" and t["is_cancelled"]:
                    tt["cancel_reason"] = cr.get((lang, t["id"]), t["cancel_reason"])
                trains.append(tt)
            fx[f"r{r['id']}_{lang}_{date}"] = {"data": {
                "id": r["id"],
                "name": r["name"] if lang == "ru" else rn[lang].get(r["id"], r["name"]),
                "region": reg(r["region"], lang),
                "regions": [reg(s, lang) for s in r["regions"]],
                "origin_station": station(r["from"], lang),
                "destination_station": station(r["to"], lang),
                "tariffs": r["tariffs"], "fleet_type": r["fleet"], "is_active": r["active"],
                "trains": trains}}

    with gzip.open(dst, "wt", encoding="utf-8") as f:
        json.dump(fx, f, ensure_ascii=False)
    print(f"{dst}: {len(fx)} ответов, {len(routes)} маршрутов")


if __name__ == "__main__":
    main(*sys.argv[1:3])
