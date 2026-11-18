"""Future-radar grids for the Snow Tracker map, from NOAA's GFS model (0.25 degree, CONUS).

Downloads a CONUS subset of each forecast hour through the NOMADS GRIB filter, then writes:
  radar.png   - precipitation type + intensity per frame (8-bit gray sprite, frames stacked vertically)
  snowacc.png - accumulated snow (inches * 2) from the first frame to each frame
and returns metadata (grid, frame times) for the page.
"""
import io, math, os, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "RyanSnowTracker/1.0 (personal snow alerts)"}
BASE = "https://nomads.ncep.noaa.gov"
BOX = dict(toplat=56, bottomlat=18, leftlon=-142, rightlon=-56)
HOURS = list(range(3, 121, 3)) + list(range(126, 241, 6))
RATE_BINS = [0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8]  # mm per hour


def ensure_libs():
    for mod, pkg in (("pygrib", "pygrib"), ("numpy", "numpy"), ("PIL", "pillow")):
        try:
            __import__(mod)
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--break-system-packages", pkg], check=False)


def http(url, tries=3, timeout=90):
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as e:
            err = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]} -> {err}")


def latest_run(now):
    """Newest GFS run whose last needed hour is already posted."""
    t = now.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    for _ in range(6):
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        idx = f"{BASE}/pub/data/nccf/com/gfs/prod/gfs.{d}/{h}/atmos/gfs.t{h}z.pgrb2.0p25.f{HOURS[-1]:03d}.idx"
        try:
            http(idx, tries=1, timeout=30)
            return t
        except Exception:
            t -= timedelta(hours=6)
    raise RuntimeError("no complete GFS run found")


def fetch_hour(run, fh):
    d, h = run.strftime("%Y%m%d"), run.strftime("%H")
    q = (f"dir=%2Fgfs.{d}%2F{h}%2Fatmos&file=gfs.t{h}z.pgrb2.0p25.f{fh:03d}"
         "&var_APCP=on&var_CSNOW=on&var_CRAIN=on&var_CFRZR=on&var_CICEP=on&var_TMP=on"
         "&lev_surface=on&lev_2_m_above_ground=on&subregion="
         + "".join(f"&{k}={v}" for k, v in BOX.items()))
    return http(f"{BASE}/cgi-bin/filter_gfs_0p25.pl?{q}")


def decode(blob):
    import pygrib, numpy as np
    path = f"/tmp/gfs_{os.getpid()}_{id(blob)}.grb2"
    with open(path, "wb") as f:
        f.write(blob)
    out, apcp = {}, []
    try:
        for m in pygrib.open(path):
            v = m.values
            if hasattr(v, "filled"):
                v = v.filled(0)
            lats = m.latlons()[0]
            if lats[0, 0] < lats[-1, 0]:
                v = v[::-1]
            sn = m.shortName
            if sn == "tp":
                apcp.append((int(m.startStep), int(m.endStep), np.asarray(v, dtype="float32")))
            elif sn in ("csnow", "crain", "cfrzr", "cicep"):
                out[sn] = np.asarray(v, dtype="float32")
            elif sn in ("2t", "t") and m.typeOfLevel == "heightAboveGround":
                out["t2"] = np.asarray(v, dtype="float32") - 273.15
        lat, lon = m.latlons()
        out["grid"] = (float(lat.max()), float(lat.min()), float(lon.min()), float(lon.max()))
    finally:
        os.remove(path)
    out["apcp"] = apcp
    return out


def slr(t):
    import numpy as np
    r = np.full(t.shape, 15.0, dtype="float32")
    r[t > -12] = 17
    r[t > -7] = 14
    r[t > -3] = 11
    r[t > 0] = 8
    r[t > 1] = 0
    return r


def build(out_dir, now=None, log=print):
    ensure_libs()
    import numpy as np
    from PIL import Image
    now = now or datetime.now(timezone.utc)
    run = latest_run(now)
    with ThreadPoolExecutor(4) as ex:
        blobs = list(ex.map(lambda fh: fetch_hour(run, fh), HOURS))
    frames = [decode(b) for b in blobs]
    ny, nx = frames[0]["t2"].shape
    cum_run = {0: np.zeros((ny, nx), "float32")}  # run-total precip (mm) by forecast hour
    radar, snowacc, times = [], [], []
    acc = np.zeros((ny, nx), "float32")
    started = False
    prev_h = 0
    for fh, fr in zip(HOURS, frames):
        total = None
        for a, b, v in fr["apcp"]:
            if a == 0 and b == fh:
                total = v
        if total is None:
            for a, b, v in fr["apcp"]:
                if b == fh and a in cum_run:
                    total = cum_run[a] + v
        if total is None:
            log(f"radar: no usable precip at f{fh:03d}")
            total = cum_run[prev_h]
        cum_run[fh] = total
        step = np.clip(total - cum_run[prev_h], 0, None)
        hrs = fh - prev_h
        prev_h = fh
        valid = run + timedelta(hours=fh)
        if valid < now - timedelta(hours=2):
            continue
        t2 = fr["t2"]
        cats = [fr.get(k, np.zeros_like(t2)) for k in ("csnow", "crain", "cfrzr", "cicep")]
        s = cats[0] + cats[1] + cats[2] + cats[3]
        snowfrac = np.where(s > 0, cats[0] / np.maximum(s, 1e-6), (t2 <= 0.5).astype("float32"))
        icefrac = np.where(s > 0, (cats[2] + cats[3]) / np.maximum(s, 1e-6), 0)
        snow_in = step * snowfrac / 25.4 * slr(t2)
        if started:
            acc = acc + snow_in
        started = True
        rate = step / max(hrs, 1)
        lvl = np.zeros((ny, nx), "uint8")
        for i, b in enumerate(RATE_BINS):
            lvl[rate >= b] = i + 1
        kind = np.where(icefrac > 0.3, 3, np.where(snowfrac >= 0.7, 2, np.where(snowfrac > 0.3, 3, 1)))
        radar.append(np.where(lvl > 0, kind * 16 + lvl, 0).astype("uint8"))
        snowacc.append(np.clip(np.round(acc * 2), 0, 255).astype("uint8"))
        times.append(valid.isoformat())
    os.makedirs(out_dir, exist_ok=True)
    Image.fromarray(np.vstack(radar), "L").save(os.path.join(out_dir, "radar.png"), optimize=True)
    Image.fromarray(np.vstack(snowacc), "L").save(os.path.join(out_dir, "snowacc.png"), optimize=True)
    n, s_, w, e = frames[0]["grid"]
    if w > 180:
        w, e = w - 360, e - 360
    return {"run": run.isoformat(), "times": times, "nx": nx, "ny": ny,
            "north": n, "south": s_, "west": w, "east": e}


# ---------- HRRR: 3 km model, next 48 hours, whole CONUS (future radar only) ----------
# Regridded to two lat/lon grids: sharp in the West, coarser in the East. 255 = outside HRRR coverage.
HR_BOX = dict(toplat=53.5, bottomlat=20.5, leftlon=-135, rightlon=-60)
HR_PARTS = [("W", 53.0, 21.0, -135.0, -100.0, 0.04, 8), ("E", 53.0, 21.0, -100.0, -60.0, 0.08, 12)]
HR_HOURS = list(range(1, 25)) + list(range(26, 49, 2))


def latest_hrrr(now):
    t = now.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    for _ in range(4):
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        try:
            http(f"{BASE}/pub/data/nccf/com/hrrr/prod/hrrr.{d}/conus/hrrr.t{h}z.wrfsfcf48.grib2.idx", tries=1, timeout=30)
            return t
        except Exception:
            t -= timedelta(hours=6)
    raise RuntimeError("no complete HRRR run found")


def fetch_hrrr(run, fh):
    d, h = run.strftime("%Y%m%d"), run.strftime("%H")
    q = (f"dir=%2Fhrrr.{d}%2Fconus&file=hrrr.t{h}z.wrfsfcf{fh:02d}.grib2"
         "&var_REFC=on&var_CSNOW=on&var_CRAIN=on&var_CFRZR=on&var_CICEP=on&var_TMP=on"
         "&lev_entire_atmosphere=on&lev_surface=on&lev_2_m_above_ground=on&subregion="
         + "".join(f"&{k}={v}" for k, v in HR_BOX.items()))
    return http(f"{BASE}/cgi-bin/filter_hrrr_2d.pl?{q}", timeout=180)


def build_hrrr(out_dir, now=None, log=print):
    ensure_libs()
    import numpy as np, pygrib
    from PIL import Image
    try:
        from scipy.spatial import cKDTree
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--break-system-packages", "scipy"], check=False)
        from scipy.spatial import cKDTree
    now = now or datetime.now(timezone.utc)
    run = latest_hrrr(now)
    hours = [fh for fh in HR_HOURS if run + timedelta(hours=fh) >= now - timedelta(hours=1)]
    k = math.cos(math.radians(40))
    parts = []
    for name, n, s_, w, e, res, chunk in HR_PARTS:
        la = np.arange(n, s_ - 1e-9, -res)
        lo = np.arange(w, e + 1e-9, res)
        parts.append({"name": name, "lat": la, "lon": lo, "res": res, "chunk": chunk, "frames": [], "idx": None, "far": None})
    times = []

    def one(fh):
        return fh, fetch_hrrr(run, fh)
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(one, hours))
    for fh, blob in results:
        path = f"/tmp/hrrr_{os.getpid()}_{fh}.grb2"
        with open(path, "wb") as f:
            f.write(blob)
        v = {}
        try:
            for m in pygrib.open(path):
                sn = m.shortName
                if sn == "refc":
                    v["refc"] = m.values
                elif sn in ("csnow", "crain", "cfrzr", "cicep"):
                    v[sn] = m.values
                elif sn == "2t":
                    v["t2"] = m.values - 273.15
                if parts[0]["idx"] is None:
                    sla, slo = m.latlons()
                    tree = cKDTree(np.column_stack([sla.ravel(), slo.ravel() * k]))
                    for p in parts:
                        gy, gx = np.meshgrid(p["lat"], p["lon"], indexing="ij")
                        dist, p["idx"] = tree.query(np.column_stack([gy.ravel(), gx.ravel() * k]))
                        p["far"] = dist > 0.05
        finally:
            os.remove(path)
        if "refc" not in v:
            log(f"HRRR f{fh:02d}: no reflectivity")
            continue
        for p in parts:
            g = lambda nm: np.asarray(v.get(nm, np.zeros_like(v["refc"]))).ravel()[p["idx"]]
            dbz, t2 = g("refc"), g("t2")
            sn, rn, ice = g("csnow"), g("crain"), g("cfrzr") + g("cicep")
            lvl = np.clip(((dbz - 12) // 5 + 1), 0, 8).astype("uint8")
            lvl[dbz < 12] = 0
            kind = np.where(ice > 0.5, 3, np.where(sn > 0.5, np.where(rn > 0.5, 3, 2), np.where(rn > 0.5, 1, np.where(t2 <= 0.5, 2, 1))))
            b = np.where(lvl > 0, kind * 16 + lvl, 0).astype("uint8")
            b[p["far"]] = 255
            p["frames"].append(b.reshape(len(p["lat"]), len(p["lon"])))
        times.append((run + timedelta(hours=fh)).isoformat())
    os.makedirs(out_dir, exist_ok=True)
    meta = []
    from scipy.ndimage import distance_transform_edt
    for p in parts:
        far2 = p["far"].reshape(len(p["lat"]), len(p["lon"]))
        # treat the outer box border as "no HRRR" too (but not the seam shared with the other part)
        padded = np.pad(far2, 1, constant_values=True)
        if p["name"] == "W":
            padded[:, -1] = padded[:, -2]
        else:
            padded[:, 0] = padded[:, 1]
        edge = np.clip(distance_transform_edt(~padded)[1:-1, 1:-1] / (40.0 if p["name"] == "W" else 20.0), 0, 1)
        Image.fromarray((edge * 255).astype("uint8"), "L").save(os.path.join(out_dir, f"hrrr{p['name']}_edge.png"), optimize=True)
        files = []
        for c in range(0, len(p["frames"]), p["chunk"]):
            fname = f"hrrr{p['name']}{c // p['chunk']}.png"
            Image.fromarray(np.vstack(p["frames"][c:c + p["chunk"]]), "L").save(os.path.join(out_dir, fname), optimize=True)
            files.append({"file": fname, "n": len(p["frames"][c:c + p["chunk"]])})
        meta.append({"name": p["name"], "nx": len(p["lon"]), "ny": len(p["lat"]), "north": float(p["lat"][0]), "south": float(p["lat"][-1]),
                     "west": float(p["lon"][0]), "east": float(p["lon"][-1]), "chunks": files, "edge": f"hrrr{p['name']}_edge.png"})
    return {"run": run.isoformat(), "times": times, "parts": meta}


# ---------- NBM (National Blend of Models): 2.5 km, 6-hourly, ~10 days, CONUS ----------
# Precip type comes from the ratio of NBM snowfall to liquid. Two layers: future radar (6-hr rate) and snow piled up.
NB_PARTS = [("W", 53.0, 21.0, -135.0, -100.0, 0.06, 10), ("E", 53.0, 21.0, -100.0, -60.0, 0.12, 20)]


def latest_nbm(now):
    t = now.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    for _ in range(4):
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        try:
            http(f"{BASE}/pub/data/nccf/com/blend/prod/blend.{d}/{h}/core/blend.t{h}z.core.f240.co.grib2.idx", tries=1, timeout=30)
            return t
        except Exception:
            t -= timedelta(hours=6)
    raise RuntimeError("no complete NBM run found")


def ranged(url, start, end):
    req = urllib.request.Request(url, headers=dict(UA, Range=f"bytes={start}-{end}"))
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:
            err = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url} range -> {err}")


def fetch_nbm(run, fh):
    d, h = run.strftime("%Y%m%d"), run.strftime("%H")
    url = f"{BASE}/pub/data/nccf/com/blend/prod/blend.{d}/{h}/core/blend.t{h}z.core.f{fh:03d}.co.grib2"
    lines = http(url + ".idx").decode().splitlines()
    want = {f"APCP:surface:{fh - 6}-{fh} hour acc fcst", f"ASNOW:surface:{fh - 6}-{fh} hour acc fcst"}
    out = b""
    for i, ln in enumerate(lines):
        p = ln.split(":")
        if len(p) == 7 and p[6] == "" and ":".join(p[3:6]) in want:
            start = int(p[1])
            end = int(lines[i + 1].split(":")[1]) - 1 if i + 1 < len(lines) else ""
            out += ranged(url, start, end)
    return out


def build_nbm(out_dir, now=None, log=print):
    ensure_libs()
    import numpy as np, pygrib
    from PIL import Image
    from scipy.spatial import cKDTree
    from scipy.ndimage import distance_transform_edt
    now = now or datetime.now(timezone.utc)
    run = latest_nbm(now)
    hours = [fh for fh in range(6, 241, 6) if run + timedelta(hours=fh) > now]
    k = math.cos(math.radians(40))
    parts = []
    for name, n, s_, w, e, res, chunk in NB_PARTS:
        parts.append({"name": name, "lat": np.arange(n, s_ - 1e-9, -res), "lon": np.arange(w, e + 1e-9, res),
                      "chunk": chunk, "radar": [], "snow": [], "idx": None, "far": None})

    def one(fh):
        try:
            return fh, fetch_nbm(run, fh)
        except Exception as e:
            log(f"NBM f{fh:03d}: {e}")
            return fh, b""
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(one, hours))
    acc = None
    times = []
    for fh, blob in results:
        if not blob:
            continue
        path = f"/tmp/nbm_{os.getpid()}_{fh}.grb2"
        with open(path, "wb") as f:
            f.write(blob)
        v = {}
        try:
            for m in pygrib.open(path):
                key = "qpf" if m.shortName in ("tp", "apcp") or "precip" in m.name.lower() else "snow"
                v[key] = np.asarray(m.values.filled(0) if hasattr(m.values, "filled") else m.values, dtype="float32")
                if parts[0]["idx"] is None:
                    sla, slo = m.latlons()
                    tree = cKDTree(np.column_stack([sla.ravel(), slo.ravel() * k]))
                    for p in parts:
                        gy, gx = np.meshgrid(p["lat"], p["lon"], indexing="ij")
                        dist, p["idx"] = tree.query(np.column_stack([gy.ravel(), gx.ravel() * k]))
                        p["far"] = dist > 0.04
        finally:
            os.remove(path)
        if "qpf" not in v or "snow" not in v:
            log(f"NBM f{fh:03d}: missing fields")
            continue
        for p in parts:
            q = v["qpf"].ravel()[p["idx"]]
            sn_in = v["snow"].ravel()[p["idx"]] * 39.37
            if "cum" not in p:
                p["cum"] = np.zeros_like(q)
            p["cum"] = p["cum"] + np.clip(sn_in, 0, None)
            rate = q / 6.0
            lvl = np.zeros(q.shape, "uint8")
            for i, b in enumerate(RATE_BINS):
                lvl[rate >= b] = i + 1
            ratio = np.where(q > 0.05, (sn_in * 25.4) / np.maximum(q, 0.05), 0)
            kind = np.where(ratio >= 6, 2, np.where(ratio >= 1.5, 3, 1))
            rb = np.where(lvl > 0, kind * 16 + lvl, 0).astype("uint8")
            sb = np.clip(np.round(p["cum"] * 2), 0, 254).astype("uint8")
            rb[p["far"]] = 255
            sb[p["far"]] = 255
            shp = (len(p["lat"]), len(p["lon"]))
            p["radar"].append(rb.reshape(shp))
            p["snow"].append(sb.reshape(shp))
        times.append((run + timedelta(hours=fh)).isoformat())
    if not times:
        raise RuntimeError("no NBM frames")
    os.makedirs(out_dir, exist_ok=True)
    meta = []
    for p in parts:
        far2 = p["far"].reshape(len(p["lat"]), len(p["lon"]))
        padded = np.pad(far2, 1, constant_values=True)
        if p["name"] == "W":
            padded[:, -1] = padded[:, -2]
        else:
            padded[:, 0] = padded[:, 1]
        edge = np.clip(distance_transform_edt(~padded)[1:-1, 1:-1] / (25.0 if p["name"] == "W" else 12.0), 0, 1)
        ename = f"nbm{p['name']}_edge.png"
        Image.fromarray((edge * 255).astype("uint8"), "L").save(os.path.join(out_dir, ename), optimize=True)
        layers = {}
        for layer in ("radar", "snow"):
            files = []
            fr = p[layer]
            for c in range(0, len(fr), p["chunk"]):
                fname = f"nbm{p['name']}_{layer}{c // p['chunk']}.png"
                Image.fromarray(np.vstack(fr[c:c + p["chunk"]]), "L").save(os.path.join(out_dir, fname), optimize=True)
                files.append({"file": fname, "n": len(fr[c:c + p["chunk"]])})
            layers[layer] = files
        meta.append({"name": p["name"], "nx": len(p["lon"]), "ny": len(p["lat"]), "north": float(p["lat"][0]),
                     "south": float(p["lat"][-1]), "west": float(p["lon"][0]), "east": float(p["lon"][-1]),
                     "layers": layers, "edge": ename})
    return {"run": run.isoformat(), "times": times, "parts": meta}
