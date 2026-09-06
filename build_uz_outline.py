"""Разовая генерация assets/uz-outline.json — контур Узбекистана для
train-map.html. Не часть автосборки: границы стран не меняются,
перегенерировать нет нужды.

Источник: Natural Earth (admin-1, разрешение 10m) через зеркало
BenPortner/geojson-atlas, лицензия CC0 1.0 — общественное достояние,
атрибуция не обязательна по лицензии (но source ниже сохранён на
всякий случай). https://github.com/BenPortner/geojson-atlas

    pip3 install shapely --break-system-packages
    python3 build_uz_outline.py
"""
import json
from pathlib import Path

from shapely.geometry import shape
from shapely.ops import unary_union

# Скачано отдельно через curl (urllib на этом Маке не видит системные
# сертификаты — известная особенность сборки Python с python.org):
#   curl -sL -o /tmp/UZ.geojson "https://raw.githubusercontent.com/BenPortner/geojson-atlas/main/geojson/natural_earth/countries/10m/UZ.geojson"
print("Читаю /tmp/UZ.geojson")
data = json.load(open("/tmp/UZ.geojson"))

geoms = [shape(f["geometry"]) for f in data["features"]]
merged = unary_union(geoms)  # 14 областей -> один контур страны, без внутренних границ


def rings_of(geom):
    polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    out = []
    for p in polys:
        out.append(list(p.exterior.coords))
        for interior in p.interiors:
            out.append(list(interior.coords))
    return out


rings = [[[round(lon, 3), round(lat, 3)] for lon, lat in ring]
         for ring in rings_of(merged)]

Path("assets").mkdir(exist_ok=True)
out = {
    "source": "Natural Earth via BenPortner/geojson-atlas, CC0 1.0",
    "rings": rings,
}
Path("assets/uz-outline.json").write_text(
    json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

total_points = sum(len(r) for r in rings)
print(f"Записано колец: {len(rings)}, точек: {total_points}")
print("Готово: assets/uz-outline.json")
