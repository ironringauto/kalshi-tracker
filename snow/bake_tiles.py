"""One-time: download basemap imagery tiles for the Snow Tracker map (Web Mercator XYZ).
Wide views: NASA GIBS Blue Marble shaded relief (z3-7, CONUS). Detail: USGS Imagery Only (z8-11) for ski regions.
Usage: python3 bake_tiles.py OUTDIR [--count]"""
import math, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

CONUS = (18.0, 56.0, -142.0, -56.0)   # z3-6
CONUS7 = (21.0, 53.0, -134.0, -62.0)  # z7
REGIONS = {  # (south, north, west, east)
    "wasatch": (39.85, 41.65, -112.55, -110.85),
    "tahoe": (38.55, 39.60, -120.75, -119.65),
    "mammoth": (37.35, 37.90, -119.35, -118.65),
    "tetons": (43.25, 44.10, -111.45, -110.45),
    "colorado": (39.05, 40.65, -107.20, -105.25),
}
WEST = (31.0, 49.5, -125.0, -102.0)  # USGS imagery at z8 and z9 across the West
WIDE = {  # USGS imagery at z10 around each ski region, so zooming in stays sharp past the resorts
    "utah": (38.8, 42.3, -114.2, -109.3),
    "sierra": (37.2, 40.2, -121.6, -118.3),
    "tetons": (42.8, 44.6, -112.3, -109.8),
    "colorado": (38.6, 40.9, -107.8, -104.8),
}
BM = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/BlueMarble_ShadedRelief_Bathymetry/default/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg"
USGS = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"


def tile(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def tiles(box, z):
    s, n, w, e = box
    x0, y0 = tile(n, w, z)
    x1, y1 = tile(s, e, z)
    return [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def plan(region_max=11):
    jobs = [("bm", BM, t) for z in range(3, 7) for t in tiles(CONUS, z)] + [("bm", BM, t) for t in tiles(CONUS7, 7)]
    seen = set(tiles(WEST, 8)) | set(tiles(WEST, 9))
    for box in WIDE.values():
        seen |= set(tiles(box, 10))
    jobs += [("usgs", USGS, t) for t in sorted(seen)]
    for box in REGIONS.values():
        for z in range(9, region_max + 1):
            for t in tiles(box, z):
                if t not in seen:
                    seen.add(t)
                    jobs.append(("usgs", USGS, t))
    return jobs


def get(url):
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "RyanSnowTracker/1.0"}), timeout=60) as r:
                return r.read()
        except Exception as e:
            err = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url} -> {err}")


if __name__ == "__main__":
    out = sys.argv[1]
    jobs = plan()
    if "--count" in sys.argv:
        from collections import Counter
        print(len(jobs), Counter((j[0], j[2][0]) for j in jobs))
        sys.exit()

    def one(j):
        name, tmpl, (z, x, y) = j
        p = os.path.join(out, name, str(z), str(x), f"{y}.jpg")
        if os.path.exists(p):
            return 0
        os.makedirs(os.path.dirname(p), exist_ok=True)
        try:
            b = get(tmpl.format(z=z, x=x, y=y))
        except Exception as e:
            print(e, file=sys.stderr)
            return 0
        open(p, "wb").write(b)
        return len(b)
    with ThreadPoolExecutor(6) as ex:
        sizes = list(ex.map(one, jobs))
    print(len(jobs), sum(1 for s in sizes if s), sum(sizes) // 1024, "KB")
