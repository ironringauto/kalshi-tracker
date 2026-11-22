"""One-off / occasional: extract run + lift geometry for our resorts from OpenSkiMap
(OpenStreetMap-derived) into compact per-resort JSON in snow/trailmaps/.

Usage: python3 build_trailmaps.py <dir with runs.geojson lifts.geojson>
Source: https://tiles.openskimap.org/geojson/{runs,lifts}.geojson  (ODbL, (c) OpenStreetMap contributors)
Geometry changes rarely, so these files are committed and NOT rebuilt on scheduled runs.
"""
import json, os, sys

AREAS = {
    "snowbird": ["8cbbb46cdace32e91492bfe6abd8014ed16328e6"],
    "brighton": ["ab2b5807422f8a412c013e32e73b521f76a65d98"],
    "solitude": ["b40e925163e2330550f940711b8571409fafb3a4"],
    "snowbasin": ["423504a1fa53bcfbe5bf956f0cd48c12868c7f06"],
    "jackson": ["2b812055300b5107fc2ffed8f5ae27a4c4f94f0a"],
    "targhee": ["84ddf46f67c0fca577dc969188dc189f1c7a040f"],
    "winterpark": ["eac3283b22abc1d56081b11e2c4867209961f982"],
    "copper": ["0bab3102857cf4c7243d3db844c283a24413fa28"],
    "abasin": ["1e281ff389476a82150ad7ef7be5f1ea48edd5e5"],
    "steamboat": ["5267096488ef4504f32f94de066e28cd88b7830e"],
    "eldora": ["a2909bf21b379e545b5be4f1dbe9b6c334e9e41c"],
    "snowmass": ["03b56bd47b26c384d82474a2fb4b3fbc3a0c4a31"],
    "palisades": ["fa7bea0bfe372c06b3cccc3e8113949837d688fb", "01fc91766cf1ba8156fdc3d233e90205b0dd3001"],
    "sugarbowl": ["fa95641c38a689ebebd96aa62b6ee049115fbeee"],
    "sierra": ["0558a46359e12971010ba1ba17046a6bf304e6af"],
    "mammoth": ["ba4944ecd2e640acedf666287f228427b607680e"],
}
IDX = {a: r for r, ids in AREAS.items() for a in ids}


def simplify(coords, tol=0.00004):
    """Douglas-Peucker on [lon,lat] (~4 m tolerance)."""
    pts = [(round(c[0], 5), round(c[1], 5)) for c in coords]
    if len(pts) < 3:
        return [list(p) for p in pts]
    keep = [False] * len(pts); keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1; L = (dx * dx + dy * dy) or 1e-18
        best, bi = 0, None
        for i in range(a + 1, b):
            x, y = pts[i]
            t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / L))
            d = (x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2
            if d > best: best, bi = d, i
        if bi is not None and best > tol * tol:
            keep[bi] = True; stack += [(a, bi), (bi, b)]
    return [list(p) for p, k in zip(pts, keep) if k]


def lines(geom):
    t, c = geom["type"], geom["coordinates"]
    if t == "LineString": return [c]
    if t == "MultiLineString": return c
    if t == "Polygon": return [c[0]]
    if t == "MultiPolygon": return [p[0] for p in c]
    return []


def scan(path, kind, out):
    with open(path) as f:
        for ln in f:
            ln = ln.strip().rstrip(",")
            if not ln.startswith('{"type":"Feature"'): continue
            if not any(i in ln for i in IDX): continue
            ft = json.loads(ln); p = ft["properties"]
            aid = lambda a: (a.get("properties") or a).get("id") if isinstance(a, dict) else a
            rs = {IDX[aid(a)] for a in (p.get("skiAreas") or []) if aid(a) in IDX}
            if not rs: continue
            if p.get("status") not in (None, "operating"): continue
            if kind == "run":
                if "downhill" not in (p.get("uses") or []): continue
                item = {"n": p.get("name") or "", "d": p.get("difficulty") or "", "g": [simplify(l) for l in lines(ft["geometry"])]}
            else:
                lt = p.get("liftType") or ""
                if lt in ("magic_carpet", "rope_tow", "platter", "t-bar", "j-bar", "zip_line", "station"): continue
                item = {"n": p.get("name") or "", "t": lt, "g": [simplify(l, 0.00002) for l in lines(ft["geometry"])]}
            item["g"] = [g for g in item["g"] if len(g) >= 2]
            if not item["g"]: continue
            for r in rs:
                out[r][kind + "s"].append(item)


def main(src):
    out = {r: {"runs": [], "lifts": []} for r in AREAS}
    scan(os.path.join(src, "lifts.geojson"), "lift", out)
    scan(os.path.join(src, "runs.geojson"), "run", out)
    od = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trailmaps"); os.makedirs(od, exist_ok=True)
    for r, d in out.items():
        xs = [c[0] for k in ("runs", "lifts") for it in d[k] for g in it["g"] for c in g]
        ys = [c[1] for k in ("runs", "lifts") for it in d[k] for g in it["g"] for c in g]
        d["bbox"] = [min(xs), min(ys), max(xs), max(ys)] if xs else None
        d["src"] = "OpenSkiMap / (c) OpenStreetMap contributors (ODbL)"
        json.dump(d, open(os.path.join(od, r + ".json"), "w"), separators=(",", ":"))
        print(r, len(d["runs"]), "runs", len(d["lifts"]), "lifts", os.path.getsize(os.path.join(od, r + ".json")) // 1024, "KB")


if __name__ == "__main__":
    main(sys.argv[1])


# ---------- satellite background per resort (USGS imagery, public domain) ----------
USGS = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"


def bake_images(maxpx=2400, pad=0.08):
    import math, io, urllib.request
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image
    od = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trailmaps")
    def wx(lon, z): return (lon + 180) / 360 * 256 * 2 ** z
    def wy(lat, z):
        s = math.sin(math.radians(lat)); return (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * 256 * 2 ** z
    def get(z, x, y):
        for _ in range(3):
            try:
                b = urllib.request.urlopen(urllib.request.Request(USGS.format(z=z, x=x, y=y), headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read()
                return Image.open(io.BytesIO(b)).convert("RGB")
            except Exception:
                pass
        return Image.new("RGB", (256, 256), (40, 48, 56))
    for r in AREAS:
        d = json.load(open(os.path.join(od, r + ".json")))
        x0, y0, x1, y1 = d["bbox"]; dx, dy = (x1 - x0) * pad, (y1 - y0) * pad
        x0, x1, y0, y1 = x0 - dx, x1 + dx, y0 - dy, y1 + dy
        z = 16
        while z > 11 and max(wx(x1, z) - wx(x0, z), wy(y0, z) - wy(y1, z)) > maxpx:
            z -= 1
        px0, px1, py0, py1 = wx(x0, z), wx(x1, z), wy(y1, z), wy(y0, z)
        tx0, tx1, ty0, ty1 = int(px0 // 256), int(px1 // 256), int(py0 // 256), int(py1 // 256)
        canvas = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
        jobs = [(x, y) for x in range(tx0, tx1 + 1) for y in range(ty0, ty1 + 1)]
        with ThreadPoolExecutor(8) as ex:
            for (x, y), im in zip(jobs, ex.map(lambda t: get(z, *t), jobs)):
                canvas.paste(im, ((x - tx0) * 256, (y - ty0) * 256))
        crop = canvas.crop((int(px0 - tx0 * 256), int(py0 - ty0 * 256), int(px1 - tx0 * 256), int(py1 - ty0 * 256)))
        fn = os.path.join(od, r + ".jpg"); crop.save(fn, quality=68, optimize=True, progressive=True)
        d["img"] = {"file": f"trailmaps/{r}.jpg", "bounds": [[y0, x0], [y1, x1]], "z": z}
        json.dump(d, open(os.path.join(od, r + ".json"), "w"), separators=(",", ":"))
        print(r, "z", z, crop.size, os.path.getsize(fn) // 1024, "KB")


HILL = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSShadedReliefOnly/MapServer/tile/{z}/{y}/{x}"


def bake_hill(maxpx=1800):
    """Snowy shaded-relief background, same bounds as the satellite image (cleaner for reading labels)."""
    import math, io, urllib.request
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image, ImageOps
    od = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trailmaps")
    def wx(lon, z): return (lon + 180) / 360 * 256 * 2 ** z
    def wy(lat, z):
        s = math.sin(math.radians(lat)); return (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * 256 * 2 ** z
    def get(z, x, y):
        for _ in range(3):
            try:
                b = urllib.request.urlopen(urllib.request.Request(HILL.format(z=z, x=x, y=y), headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read()
                return Image.open(io.BytesIO(b)).convert("L")
            except Exception:
                pass
        return Image.new("L", (256, 256), 220)
    for r in AREAS:
        fn = os.path.join(od, r + ".json"); d = json.load(open(fn))
        (y0, x0), (y1, x1) = d["img"]["bounds"]
        z = 16
        while z > 11 and max(wx(x1, z) - wx(x0, z), wy(y0, z) - wy(y1, z)) > maxpx:
            z -= 1
        px0, px1, py0, py1 = wx(x0, z), wx(x1, z), wy(y1, z), wy(y0, z)
        tx0, tx1, ty0, ty1 = int(px0 // 256), int(px1 // 256), int(py0 // 256), int(py1 // 256)
        canvas = Image.new("L", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
        jobs = [(x, y) for x in range(tx0, tx1 + 1) for y in range(ty0, ty1 + 1)]
        with ThreadPoolExecutor(8) as ex:
            for (x, y), im in zip(jobs, ex.map(lambda t: get(z, *t), jobs)):
                canvas.paste(im, ((x - tx0) * 256, (y - ty0) * 256))
        g = canvas.crop((int(px0 - tx0 * 256), int(py0 - ty0 * 256), int(px1 - tx0 * 256), int(py1 - ty0 * 256)))
        g = ImageOps.autocontrast(g, cutoff=1)
        # snowy palette: shadows slate blue, lit slopes near white
        col = ImageOps.colorize(g, black=(96, 116, 140), mid=(196, 208, 222), white=(250, 252, 255), midpoint=150)
        out = os.path.join(od, r + "_hill.jpg"); col.save(out, quality=70, optimize=True, progressive=True)
        d["hill"] = {"file": f"trailmaps/{r}_hill.jpg", "bounds": d["img"]["bounds"]}
        json.dump(d, open(fn, "w"), separators=(",", ":"))
        print(r, "z", z, col.size, os.path.getsize(out) // 1024, "KB")
