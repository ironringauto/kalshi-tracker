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
BOX = dict(toplat=50, bottomlat=24, leftlon=-125, rightlon=-66)
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
