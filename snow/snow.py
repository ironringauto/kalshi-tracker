#!/usr/bin/env python3
"""Snow Tracker: weather-model snow forecasts for Ryan's resorts.

Sources (no resort-reported numbers):
  - Open-Meteo: hourly forecasts from 5 models (ECMWF, GFS/HRRR, ICON, GEM, NBM)
  - NWS (api.weather.gov): official forecaster-edited snowfall grid + SLC forecast discussion
  - SNOTEL (USDA): measured snow depth at the nearest station

Writes OUT/data.json and OUT/index.html. Standard library only.
Usage: python3 snow.py [--out DIR]
"""
import json, math, os, statistics, sys, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
import urllib.error

UA = {"User-Agent": "RyanSnowTracker/1.0 (personal snow alerts)", "Accept": "application/json"}
OM_MODELS = {"ecmwf_ifs025": "ECMWF", "gfs_seamless": "GFS/HRRR", "icon_seamless": "ICON",
             "gem_seamless": "GEM", "ncep_nbm_conus": "NBM"}
# id, name, region, lat, lon, model elevation ft (mid/upper mountain), drive from Provo
RESORTS = [
    ("snowbird", "Snowbird", "UT", 40.5829, -111.6556, 9300, "55 min"),
    ("brighton", "Brighton", "UT", 40.5980, -111.5832, 9300, "1 hr"),
    ("solitude", "Solitude", "UT", 40.6199, -111.5919, 9000, "55 min"),
    ("snowbasin", "Snowbasin", "UT", 41.2160, -111.8569, 7800, "1 hr 45"),
    ("targhee", "Grand Targhee", "WY", 43.7896, -110.9581, 8800, "4 hr 45"),
    ("jackson", "Jackson Hole", "WY", 43.5950, -110.8500, 8500, "4 hr 45"),
    ("winterpark", "Winter Park", "CO", 39.8868, -105.7625, 10500, None),
    ("copper", "Copper", "CO", 39.5022, -106.1497, 11000, None),
    ("abasin", "Arapahoe Basin", "CO", 39.6425, -105.8719, 11800, None),
    ("steamboat", "Steamboat", "CO", 40.4572, -106.8045, 9000, None),
    ("eldora", "Eldora", "CO", 39.9372, -105.5827, 10000, None),
    ("snowmass", "Aspen Snowmass", "CO", 39.2084, -106.9490, 10500, None),
    ("palisades", "Palisades Tahoe", "CA", 39.1970, -120.2357, 7800, None),
    ("sugarbowl", "Sugar Bowl", "CA", 39.3045, -120.3360, 7600, None),
    ("sierra", "Sierra-at-Tahoe", "CA", 38.7990, -120.0800, 7800, None),
    ("mammoth", "Mammoth", "CA", 37.6308, -119.0326, 9500, None),
]
PROVO = ("provo", "Provo (valley)", "UT", 40.2338, -111.6585, 4550, None)
REGION_NAMES = {"UT": "Utah", "WY": "Wyoming / Idaho", "CO": "Colorado", "CA": "California"}
REGION_TZ = {"UT": "America/Denver", "WY": "America/Denver", "CO": "America/Denver", "CA": "America/Los_Angeles"}
DAYS = 9
LOG = []
UTC = ZoneInfo("UTC")


def log(msg):
    LOG.append(msg)
    print(msg, file=sys.stderr)


def get(url, tries=3):
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=45) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            err = e
            if e.code in (400, 403, 404, 429):
                break
        except Exception as e:
            err = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]} -> {err}")


def slr(t_c):
    """Snow-to-liquid ratio from temperature at resort elevation; None means rain."""
    if t_c is None or t_c > 1.0:
        return None
    if t_c > 0: return 8
    if t_c > -3: return 11
    if t_c > -7: return 14
    if t_c > -12: return 17
    return 15


def row(t, snow, pr, tc, gust, lvl):
    return {"t": t, "snow": snow, "pr": pr, "tc": tc, "gust": gust, "lvl": lvl}


def model_row(t, pr_mm, tc, gust, lvl):
    r = slr(tc)
    return row(t, pr_mm / 25.4 * r if r else 0.0, pr_mm / 25.4, tc, gust, lvl)


# ---------- fetch ----------
def fetch_open_meteo(points):
    """Several models in one request each. Free tier is capped per IP, so stop at the first 429."""
    lat = ",".join(str(p[3]) for p in points)
    lon = ",".join(str(p[4]) for p in points)
    elev = ",".join(str(round(p[5] * 0.3048)) for p in points)
    out = {}
    for m, name in OM_MODELS.items():
        for hourly in ("precipitation,temperature_2m,wind_gusts_10m,freezing_level_height",
                       "precipitation,temperature_2m,wind_gusts_10m"):
            q = dict(latitude=lat, longitude=lon, elevation=elev, hourly=hourly, models=m,
                     timezone="GMT", forecast_days=DAYS + 2, wind_speed_unit="mph")
            try:
                res = get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q), tries=2)
            except Exception as e:
                log(f"Open-Meteo {name}: {e}")
                if "429" in str(e):
                    return out
                continue
            res = res if isinstance(res, list) else [res]
            for p, r in zip(points, res):
                h = r["hourly"]
                frz = h.get("freezing_level_height") or [None] * len(h["time"])
                out.setdefault(p[0], {})[name] = [
                    model_row(datetime.fromisoformat(t).replace(tzinfo=UTC), h["precipitation"][i] or 0,
                              h["temperature_2m"][i], h["wind_gusts_10m"][i],
                              frz[i] * 3.281 - 1000 if frz[i] is not None else None)
                    for i, t in enumerate(h["time"])]
            break
    return out


def fetch_met(p):
    """MET Norway locationforecast (ECMWF-based). Hourly to ~60 h, then 6-hourly (spread evenly)."""
    q = dict(lat=round(p[3], 4), lon=round(p[4], 4), altitude=round(p[5] * 0.3048))
    js = get("https://api.met.no/weatherapi/locationforecast/2.0/complete?" + urllib.parse.urlencode(q))
    rows = []
    for ts in js["properties"]["timeseries"]:
        t0 = datetime.fromisoformat(ts["time"].replace("Z", "+00:00"))
        d = ts["data"]
        inst = d["instant"]["details"]
        tc, gust = inst.get("air_temperature"), inst.get("wind_speed_of_gust")
        gust = gust * 2.237 if gust is not None else None
        if "next_1_hours" in d:
            rows.append(model_row(t0 + timedelta(hours=1), d["next_1_hours"]["details"].get("precipitation_amount", 0), tc, gust, None))
        elif "next_6_hours" in d:
            pr = d["next_6_hours"]["details"].get("precipitation_amount", 0) / 6
            rows += [model_row(t0 + timedelta(hours=k + 1), pr, tc, gust, None) for k in range(6)]
    seen, out = set(), []
    for r in rows:
        if r["t"] not in seen:
            seen.add(r["t"]); out.append(r)
    return out


def iso_hours(d):  # ISO8601 duration like PT6H, P1DT3H
    days = int(d.split("P")[1].split("D")[0]) if "D" in d else 0
    hrs = int(d.split("T")[1].split("H")[0]) if "T" in d and "H" in d else 0
    return days * 24 + hrs


def expand(values, scale=1.0, split=False):
    """NWS layer -> {utc hour (end of hour): value}. split=True divides totals across the hours."""
    out = {}
    for v in values:
        start, dur = v["validTime"].split("/")
        t0, n = datetime.fromisoformat(start), max(iso_hours(dur), 1)
        if v["value"] is None:
            continue
        val = v["value"] * scale / (n if split else 1)
        for i in range(n):
            out[t0 + timedelta(hours=i + 1)] = val
    return out


def fetch_nws(p):
    """Official NWS forecast grid (forecaster-edited). Returns (tz name, rows)."""
    pt = get(f"https://api.weather.gov/points/{p[3]:.4f},{p[4]:.4f}")["properties"]
    g = get(pt["forecastGridData"])["properties"]
    snow = expand(g["snowfallAmount"]["values"], 1 / 25.4, True)
    pr = expand(g["quantitativePrecipitation"]["values"], 1 / 25.4, True)
    tc = expand(g["temperature"]["values"])
    gust = expand(g["windGust"]["values"], 0.6214)
    lvl = expand(g.get("snowLevel", {}).get("values", []), 3.281)
    rows = [row(t, snow.get(t, 0.0), pr.get(t, 0.0), tc.get(t), gust.get(t), lvl.get(t)) for t in sorted(snow)]
    return pt.get("timeZone"), rows


def fetch_snotel(points):
    base = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
    st = get(base + "/stations?" + urllib.parse.urlencode(
        {"stationTriplets": "*:UT:SNTL,*:WY:SNTL,*:CO:SNTL,*:CA:SNTL,*:ID:SNTL", "activeOnly": "true"}))
    near = {}
    for p in points:
        best = None
        for s in st:
            try:
                d = km(p[3], p[4], float(s["latitude"]), float(s["longitude"]))
            except (KeyError, TypeError, ValueError):
                continue
            if d < 15 and (best is None or d < best[0]):
                best = (d, s["stationTriplet"], s.get("name", ""), s.get("elevation"))
        if best:
            near[p[0]] = best
    if not near:
        return {}
    today = datetime.now(ZoneInfo("America/Denver")).date()
    data = get(base + "/data?" + urllib.parse.urlencode({
        "stationTriplets": ",".join(sorted({v[1] for v in near.values()})), "elements": "SNWD",
        "duration": "DAILY", "beginDate": str(today - timedelta(days=4)), "endDate": str(today)}))
    depth = {}
    for r in data:
        vals = sorted((x["date"], x["value"]) for el in r.get("data", []) for x in el.get("values", [])
                      if x.get("value") is not None)
        if vals:
            depth[r["stationTriplet"]] = vals
    out = {}
    for rid, (d, trip, name, el) in near.items():
        v = depth.get(trip)
        if v:
            out[rid] = {"station": name, "km": round(d, 1), "elev_ft": el, "depth_in": v[-1][1], "date": v[-1][0],
                        "chg24": v[-1][1] - v[-2][1] if len(v) >= 2 else None,
                        "chg72": v[-1][1] - v[-4][1] if len(v) >= 4 else None}
    return out


def km(a, b, c, d):
    r = math.pi / 180
    return 6371 * math.hypot((d - b) * r * math.cos((a + c) / 2 * r), (c - a) * r)


def fetch_afd(office="SLC"):
    pid = get(f"https://api.weather.gov/products/types/AFD/locations/{office}")["@graph"][0]["id"]
    return get(f"https://api.weather.gov/products/{pid}")["productText"][:6000]


def gather():
    pts = RESORTS + [PROVO]
    members = {p[0]: {} for p in pts}
    tzs = {p[0]: REGION_TZ[p[2]] for p in pts}
    with ThreadPoolExecutor(6) as ex:
        nws_f = {p[0]: ex.submit(fetch_nws, p) for p in pts}
        met_f = {p[0]: ex.submit(fetch_met, p) for p in pts}
        om_f = ex.submit(fetch_open_meteo, pts)
        sn_f = ex.submit(fetch_snotel, RESORTS)
        afd_f = ex.submit(fetch_afd, "SLC")
        for rid, f in nws_f.items():
            try:
                tz, rows = f.result()
                members[rid]["NWS"] = rows
                tzs[rid] = tz or tzs[rid]
            except Exception as e:
                log(f"NWS {rid}: {e}")
        for rid, f in met_f.items():
            try:
                members[rid]["MET Norway (ECMWF)"] = f.result()
            except Exception as e:
                log(f"MET Norway {rid}: {e}")
        try:
            for rid, ms in om_f.result().items():
                members[rid].update(ms)
        except Exception as e:
            log(f"Open-Meteo: {e}")
        try:
            snotel = sn_f.result()
        except Exception as e:
            snotel = {}
            log(f"SNOTEL: {e}")
        try:
            afd = afd_f.result()
        except Exception as e:
            afd = None
            log(f"NWS discussion: {e}")
    return members, tzs, snotel, afd


# ---------- analysis ----------
def wsum(rows, s, e, key):
    xs = [r[key] for r in rows if s < r["t"] <= e]
    return sum(xs), len(xs)


def analyze(members, tzs, snotel, now_utc):
    resorts = []
    for p in RESORTS:
        rid, mem = p[0], members[p[0]]
        if not mem:
            continue
        tz = ZoneInfo(tzs[rid])
        now = now_utc.astimezone(tz)
        first = now.date() + timedelta(days=1 if now.hour >= 9 else 0)
        days = []
        for k in range(DAYS):
            d = first + timedelta(days=k)
            am = datetime(d.year, d.month, d.day, 9, tzinfo=tz)
            ovn_s, day_e = am - timedelta(hours=17), am + timedelta(hours=7)
            ovn, dsn = {}, {}
            for name, rows in mem.items():
                o, n1 = wsum(rows, ovn_s, am, "snow")
                dd, n2 = wsum(rows, am, day_e, "snow")
                if n1 >= 15:
                    ovn[name] = o
                    if n2 >= 6:
                        dsn[name] = dd
            if not ovn:
                continue
            nws_o, nws_d = ovn.get("NWS"), dsn.get("NWS")
            mods = sorted(v for n, v in ovn.items() if n != "NWS")
            dmods = [v for n, v in dsn.items() if n != "NWS"]
            med = statistics.median(mods) if mods else None
            dmed = statistics.median(dmods) if dmods else None
            best = statistics.mean([x for x in (med, nws_o) if x is not None])
            dbest = statistics.mean([x for x in (dmed, nws_d) if x is not None] or [0])
            allv = sorted(ovn.values())
            conf = None
            if best >= 2:
                if len(allv) == 1:
                    conf = "Single source"
                else:
                    agree = sum(1 for v in allv if v >= 0.55 * best) / len(allv)
                    conf = "High" if agree >= 0.8 else "Med" if agree >= 0.6 else "Low"
            ref = mem.get("NWS") or next(iter(mem.values()))
            gusts = [r["gust"] for r in ref if am < r["t"] <= day_e and r["gust"] is not None]
            temps = [r["tc"] for rows in mem.values() for r in rows if r["t"] == am and r["tc"] is not None]
            t9 = statistics.median(temps) if temps else None
            lv_src = mem.get("NWS") if any(r["lvl"] for r in mem.get("NWS", [])) else mem.get("GFS/HRRR", [])
            lv = [r["lvl"] for r in lv_src if ovn_s < r["t"] <= day_e and r["pr"] > 0.005 and r["lvl"]]
            g = max(gusts) if gusts else 0
            score = 10 * (1 - math.exp(-best / 7))
            score -= {"Low": 1.0, "Med": 0.3, "Single source": 0.3}.get(conf, 0)
            score -= 1.5 if g > 50 else 0.5 if g > 35 else 0
            score -= 1.0 if (t9 is not None and t9 > 0 and best > 1) else 0
            score -= 0.5 if (d.weekday() >= 5 and p[2] == "UT") else 0
            days.append({
                "date": str(d), "dow": d.strftime("%a"), "label": d.strftime("%a %-d"), "dnum": d.strftime("%-d"),
                "ovn": round(best, 1), "ovn_lo": round(allv[0], 1), "ovn_hi": round(allv[-1], 1),
                "sources": {n: round(v, 1) for n, v in ovn.items()},
                "nws_ovn": None if nws_o is None else round(nws_o, 1), "day": round(dbest, 1),
                "conf": conf, "gust": round(g) if gusts else None,
                "t9_f": None if t9 is None else round(t9 * 9 / 5 + 32),
                "snow_level_ft": int(round(min(lv), -2)) if lv else None,
                "score": round(max(0, min(10, score)), 1)})
        if not days:
            continue
        tot5 = sum(x["ovn"] + x["day"] for x in days[:5])
        resorts.append({"id": rid, "name": p[1], "region": p[2], "drive": p[6], "elev_ft": p[5],
                        "sources": sorted(mem), "snotel": snotel.get(rid), "days": days,
                        "total5": round(tot5, 1)})
    return resorts


def provo_outlook(members, now_utc):
    m = members.get("provo", {})
    rows = m.get("NWS") or next(iter(m.values()), None)
    if not rows:
        return None
    wet = [r for r in rows if now_utc < r["t"] <= now_utc + timedelta(hours=48) and r["pr"] > 0.003]
    if not wet:
        return {"text": "Provo next 48 hrs: dry.", "precip_in": 0}
    pr, sn = sum(r["pr"] for r in wet), sum(r["snow"] for r in wet)
    kind = "rain" if sn < 0.3 else ("snow" if sum(r["pr"] for r in wet if r["snow"] == 0) < 0.05 else "rain and snow")
    lv = [r["lvl"] for r in wet if r["lvl"]]
    lvl = f" Mountain snow level around {round(statistics.median(lv), -2):,.0f} ft." if lv else ""
    sn_txt = f" ({sn:.0f} in of snow in town)" if sn >= 0.5 else ""
    return {"text": f"Provo next 48 hrs: {kind}, {pr:.2f} in of precipitation{sn_txt}.{lvl}",
            "precip_in": round(pr, 2)}


def alerts_and_call(resorts, now_denver):
    alerts = []
    home = [r for r in resorts if r["region"] == "UT"]
    pw = sorted(((r["days"][0], r["name"]) for r in home if r["days"][0]["ovn"] >= 6), key=lambda x: -x[0]["ovn"])
    if pw:
        alerts.append({"level": "powder", "text": f"Powder {pw[0][0]['label']} morning: " + ", ".join(
            f'{n} ~{d["ovn"]:.0f}" ({d["ovn_lo"]:.0f}-{d["ovn_hi"]:.0f}", {d["conf"] or "?"})' for d, n in pw)})
    for reg in ("UT", "WY", "CO", "CA"):
        hits = []
        for r in (x for x in resorts if x["region"] == reg):
            need = 10 if reg == "UT" else 12
            for k in range(1, min(7, len(r["days"]))):
                two = r["days"][k]["ovn"] + r["days"][k - 1]["day"] + r["days"][k - 1]["ovn"]
                if two >= need and r["days"][k]["conf"] in ("High", "Med"):
                    hits.append((two, r["name"], r["days"][k]))
                    break
        if hits:
            hits.sort(key=lambda h: -h[0])
            top = ", ".join(f'{n} ~{t:.0f}"' for t, n, _ in hits[:4])
            d = hits[0][2]
            alerts.append({"level": "storm" if reg == "UT" else "away",
                           "text": f"Storm building in {REGION_NAMES[reg]} by {d['label']} (48-hr totals, "
                                   f"{d['conf']} confidence): {top}"})
    early = [(d, r["name"]) for r in home for d in r["days"][6:] if d["nws_ovn"] is None and d["ovn"] >= 10]
    if early:
        d, n = max(early, key=lambda x: x[0]["ovn"])
        alerts.append({"level": "early", "text": f'Early look: models show ~{d["ovn"]:.0f}" at {n} by {d["label"]} (past the NWS range, will change)'})
    call = "No standout powder morning in Utah this week."
    if home:
        best = max(((r, d) for r in home for d in r["days"] if d["nws_ovn"] is not None or d is r["days"][0]), key=lambda x: x[1]["score"])
        r, d = best
        if d["ovn"] >= 3:
            call = (f"Best Utah morning: {d['label']} at {r['name']}, ~{d['ovn']:.0f}\" overnight "
                    f"(range {d['ovn_lo']:.0f}-{d['ovn_hi']:.0f}\", {d['conf'] or '?'} confidence, score {d['score']}).")
            if d["date"] != home[0]["days"][0]["date"] and max(x["days"][0]["score"] for x in home) < d["score"] - 3:
                call += f" Tomorrow is weak by comparison. Save the day for {d['dow']}."
    return alerts, call, now_denver.weekday() == 6


# ---------- render ----------
def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def color(score):
    return "s3" if score >= 6 else "s2" if score >= 3.5 else "s1" if score >= 1 else "s0"


POS = {p[0]: (p[3], p[4]) for p in RESORTS}
# short map label at wide zoom, and the zoom level at which the full label appears
MAPLBL = {"snowbird": ("Cottonwoods", 5, 8), "brighton": ("", 99, 8), "solitude": ("", 99, 8),
          "snowbasin": ("Snowbasin", 5, 7), "targhee": ("Targhee", 5, 7), "jackson": ("", 99, 7),
          "steamboat": ("Steamboat", 5, 7), "snowmass": ("Aspen", 5, 7), "copper": ("Summit Co.", 5, 8),
          "abasin": ("", 99, 8), "winterpark": ("", 99, 7), "eldora": ("", 99, 7),
          "palisades": ("Tahoe", 5, 8), "sugarbowl": ("", 99, 8), "sierra": ("", 99, 8), "mammoth": ("Mammoth", 5, 7)}


def point_timeline(r, tzname):
    """Cumulative blended snow at the end of each 4pm-9am and 9am-4pm window: [[iso_end, inches], ...]."""
    tz = ZoneInfo(tzname)
    out, cum = [], 0.0
    for d in r["days"]:
        y, m, dd = map(int, d["date"].split("-"))
        am = datetime(y, m, dd, 9, tzinfo=tz)
        cum += d["ovn"]
        out.append([am.astimezone(UTC).isoformat(), round(cum, 1)])
        cum += d["day"]
        out.append([(am + timedelta(hours=7)).astimezone(UTC).isoformat(), round(cum, 1)])
    return out


def render_chart(rs):
    """Cumulative projected snow by morning for one region, as inline SVG."""
    pal = ["#4f9be6", "#e8a33d", "#3fb68b", "#d9578f", "#9b7be0", "#7fb3c8"]
    days = rs[0]["days"]
    n = len(days)
    W, H, L, R, T, B = 660, 230, 34, 150, 14, 34
    series = []
    for r in rs:
        cum, pts = 0.0, []
        for d in r["days"]:
            cum += d["ovn"]
            pts.append(cum)
            cum += d["day"]
        series.append((r, pts))
    top = max([6] + [p for _, ps in series for p in ps])
    step = 2 if top <= 12 else 6 if top <= 36 else 12
    top = math.ceil(top / step) * step
    X = lambda i: L + (W - L - R) * (i / max(n - 1, 1))
    Y = lambda v: T + (H - T - B) * (1 - v / top)
    o = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Projected snow piling up by morning">']
    far0 = next((i for i, d in enumerate(days) if d["nws_ovn"] is None), None)
    if far0 is not None:
        o.append(f'<rect x="{X(far0) - 8:.0f}" y="{T}" width="{X(n - 1) - X(far0) + 16:.0f}" height="{H - T - B}" class="farbg"/>'
                 f'<text x="{X(n - 1) + 4:.0f}" y="{T + 10}" class="ax" text-anchor="end">models only</text>')
    v = 0
    while v <= top:
        o.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.0f}" y2="{Y(v):.0f}" class="gl"/><text x="{L - 6}" y="{Y(v) + 4:.0f}" class="ax" text-anchor="end">{v}"</text>')
        v += step
    for i, d in enumerate(days):
        o.append(f'<text x="{X(i):.0f}" y="{H - 16}" class="ax" text-anchor="middle">{d["dow"][:2]}</text>'
                 f'<text x="{X(i):.0f}" y="{H - 4}" class="ax" text-anchor="middle">{d["dnum"]}</text>')
    ends = []
    for k, (r, pts) in enumerate(series):
        c = pal[k % len(pal)]
        path = " ".join(f'{"M" if i == 0 else "L"}{X(i):.1f},{Y(p):.1f}' for i, p in enumerate(pts))
        o.append(f'<path d="{path}" class="ln" style="stroke:{c}"/>')
        o.extend(f'<circle cx="{X(i):.1f}" cy="{Y(p):.1f}" r="2.6" style="fill:{c}"><title>{esc(r["name"])} by {days[i]["dow"]} {days[i]["dnum"]} 9 am: {p:.0f}"</title></circle>' for i, p in enumerate(pts))
        ends.append([Y(pts[-1]), r["name"], pts[-1], c])
    ends.sort()
    for i in range(1, len(ends)):
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 14)
    for y, name, val, c in ends:
        o.append(f'<text x="{W - R + 8}" y="{y + 4:.0f}" class="el" style="fill:{c}">{esc(name)} {val:.0f}"</text>')
    o.append("</svg>")
    return "".join(o)


def render_card(r, scale):
    bars = []
    for d in r["days"]:
        h = max(2, round(d["ovn"] / scale * 100))
        far = d["nws_ovn"] is None
        nws_txt = f', NWS {d["nws_ovn"]}"' if not far else ", models only"
        tip = (f'{d["label"]}: {d["ovn"]}" overnight (range {d["ovn_lo"]}-{d["ovn_hi"]}"{nws_txt}), '
               f'+{d["day"]}" during the day, score {d["score"]}')
        rng = f'{d["ovn_lo"]:.0f}-{d["ovn_hi"]:.0f}' if d["ovn_hi"] >= 1 and d["ovn_hi"] != d["ovn_lo"] else "&nbsp;"
        bars.append(f'<div class="bar{" far" if far else ""}" title="{esc(tip)}"><div class="v">{d["ovn"]:.0f}"</div>'
                    f'<div class="track"><div class="fill {color(d["score"])}" style="height:{h}%"></div></div>'
                    f'<div class="rng">{rng}</div><div class="dl">{d["dow"][:2]}<br>{d["dnum"]}</div></div>')
    s = r["snotel"]
    sub = [r["drive"]] if r["drive"] else []
    if s:
        sub.append(f'{s["depth_in"]:.0f}" on the ground')
    best = max(r["days"], key=lambda d: d["score"])
    nxt = r["days"][0]
    lvl = next((d["snow_level_ft"] for d in r["days"][:3] if d["snow_level_ft"] and d["ovn"] + d["day"] >= 1), None)
    lvl_txt = f' · snow level ~{lvl:,} ft' if lvl else ""
    best_txt = (f'best {best["dow"]} {best["dnum"]} ({best["ovn"]:.0f}")' if best["ovn"] >= 1 else "no real snow in sight")
    return (f'<section class="card" id="{r["id"]}"><div class="ch"><div><h3>{esc(r["name"])}</h3>'
            f'<div class="sub">{esc(" · ".join(sub))}</div></div>'
            f'<div class="badge {color(best["score"])}" title="Best powder score in the window">{best["score"]}</div></div>'
            f'<div class="next">Tomorrow <b>{nxt["ovn"]:.0f}"</b> · next 5 days ~{r["total5"]:.0f}" · {best_txt}{lvl_txt}</div>'
            f'<div class="bars" style="grid-template-columns:repeat({len(r["days"])},1fr)">{"".join(bars)}</div></section>')


def render(data, tzs=None):
    secs = []
    for reg in ("UT", "WY", "CO", "CA"):
        rs = [r for r in data["resorts"] if r["region"] == reg]
        if not rs:
            continue
        if reg != "UT":
            rs.sort(key=lambda r: -r["total5"])
        scale = max([12] + [d["ovn"] for r in rs for d in r["days"]])
        top = max(rs, key=lambda r: r["total5"])
        secs.append(f'<details class="reg" id="reg-{reg}" open><summary><span class="rt">{REGION_NAMES[reg]}</span>'
                    f'<span class="rs">{len(rs)} resorts · most snow next 5 days: {esc(top["name"])} ~{top["total5"]:.0f}"</span></summary>'
                    f'<div class="chartbox"><div class="ct">Projected snow piling up, inches by each morning (overnight + daytime)</div>{render_chart(rs)}</div>'
                    f'<div class="grid">{"".join(render_card(r, scale) for r in rs)}</div></details>')
    alerts = "".join(f'<div class="alert {a["level"]}">{esc(a["text"])}</div>' for a in data["alerts"])
    provo = esc(data["provo"]["text"]) if data.get("provo") else ""
    stamp = datetime.fromisoformat(data["generated"]).astimezone(ZoneInfo("America/Denver")).strftime("%a %b %-d, %-I:%M %p MT")
    real = [w for w in data["warnings"] if "Open-Meteo" not in w]
    warn = f'<p class="warn">Missing tonight: {esc("; ".join(real))}</p>' if real else ""
    tzs = tzs or data.get("tzs") or {}
    mdata = {"radar": data.get("radar"),
             "resorts": [{"id": r["id"], "name": r["name"], "lat": POS[r["id"]][0], "lon": POS[r["id"]][1],
                          "short": MAPLBL.get(r["id"], (r["name"], 5, 7))[0], "zs": MAPLBL.get(r["id"], (r["name"], 5, 7))[1],
                          "zf": MAPLBL.get(r["id"], (r["name"], 5, 7))[2], "t5": r["total5"],
                          "tl": point_timeline(r, tzs.get(r["id"], REGION_TZ[r["region"]]))} for r in data["resorts"]]}
    return (TEMPLATE.replace("{{stamp}}", stamp).replace("{{call}}", esc(data["call"]))
            .replace("{{alerts}}", alerts).replace("{{provo}}", provo)
            .replace("{{mdata}}", json.dumps(mdata, separators=(",", ":")).replace("</", "<\\/"))
            .replace("{{sections}}", "".join(secs)).replace("{{models}}", esc(", ".join(data["sources_ok"])))
            .replace("{{warn}}", warn).replace("{{leafletcss}}", LEAFLET_CSS))


def main():
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "out"
    os.makedirs(out, exist_ok=True)
    now = datetime.now(UTC)
    members, tzs, snotel, afd = gather()
    resorts = analyze(members, tzs, snotel, now)
    if not resorts:
        log("FATAL: no forecast data from any source")
        print(json.dumps({"fatal": True, "warnings": LOG}, indent=1))
        sys.exit(2)
    alerts, call, sunday = alerts_and_call(resorts, now.astimezone(ZoneInfo("America/Denver")))
    srcs = sorted({n for r in resorts for n in r["sources"]})
    radar_meta = None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import radar
        radar_meta = radar.build(out, now, log=log)
    except Exception as e:
        log(f"Model map: {e}")
    try:
        import shutil
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "base.json"), os.path.join(out, "base.json"))
    except Exception as e:
        log(f"Basemap: {e}")
    data = {"generated": now.isoformat(), "call": call, "alerts": alerts, "weekly_outlook": sunday,
            "provo": provo_outlook(members, now), "resorts": resorts, "afd_slc": afd,
            "sources_ok": srcs, "nws_ok": sum(1 for r in resorts if "NWS" in r["sources"]),
            "snotel_ok": len(snotel), "warnings": LOG, "radar": radar_meta, "tzs": tzs}
    json.dump(data, open(os.path.join(out, "data.json"), "w"), indent=1, default=str)
    open(os.path.join(out, "index.html"), "w").write(render(data))
    print(json.dumps({"call": call, "alerts": alerts, "weekly_outlook": sunday, "sources_ok": srcs,
                      "nws_ok": data["nws_ok"], "snotel_ok": data["snotel_ok"], "warnings": LOG}, indent=1))


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Snow Tracker</title>
<style>{{leafletcss}}</style>
<style>
:root{--bg:#f6f8fb;--card:#fff;--ink:#14202e;--mut:#5d6b7a;--line:#e3e8ef;--ocean:#d9e6f2;--land:#f3f5f7;--stb:#8c9bab;--cty:#5f6f80;--s0:#c9d1db;--s1:#9cc3ea;--s2:#3f88d6;--s3:#1d3fa8;--bt:#fff;--warn:#b25b00}
@media (prefers-color-scheme:dark){:root{--bg:#0e141b;--card:#161f2a;--ink:#e8eef5;--mut:#93a1b1;--line:#253242;--ocean:#0a131c;--land:#1a2531;--stb:#4d6176;--cty:#7d90a4;--s0:#3a4757;--s1:#3d6a96;--s2:#4f9be6;--s3:#9ec3ff;--bt:#0e141b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}
.wrap{max-width:1040px;margin:0 auto;padding:14px 12px 40px}
header{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:4px}
header h1{margin:0;font-size:21px}
.stamp{color:var(--mut);font-size:12.5px}
.call{margin:8px 0 6px;font-size:14.5px}
.alerts{display:flex;flex-direction:column;gap:4px}
.alert{padding:6px 10px;border-radius:7px;font-size:13px;background:var(--card);border:1px solid var(--line)}
.alert.powder{border-left:4px solid var(--s3);font-weight:600}.alert.storm{border-left:4px solid var(--s2)}.alert.away,.alert.early{border-left:4px solid var(--s1)}
.provo{color:var(--mut);font-size:13px;margin:6px 2px 0}
.mapcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px;margin-top:14px}
.tbar{text-align:center;padding:2px 0 8px}
#tmain{font-size:24px;font-weight:700;letter-spacing:-.01em}
#tsub{font-size:12.5px;color:var(--mut)}
.seg{display:flex;justify-content:center;gap:0;margin:0 auto 8px;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:3px;width:max-content}
.seg button{border:none;background:none;color:var(--mut);font:600 13.5px inherit;padding:7px 14px;border-radius:8px;cursor:pointer}
.seg button.on{background:var(--s2);color:#fff}
#map{height:58vh;min-height:340px;max-height:600px;border-radius:10px;background:var(--ocean)}
.leaflet-container{background:var(--ocean);font:inherit}
.cty{fill:var(--land);fill-opacity:1;stroke:var(--cty);stroke-width:1.2}
.stb{stroke:var(--stb);stroke-width:.9;fill:none}
.wximg{image-rendering:auto}
.rm{fill:#fff;stroke:#14202e;stroke-width:2}
.leaflet-tooltip.rlab{background:rgba(20,32,46,.82);color:#fff;border:none;box-shadow:none;font-size:12px;font-weight:600;padding:2px 6px;border-radius:6px}
.leaflet-tooltip.rlab:before{display:none}
.leaflet-popup-content{font-size:13.5px;line-height:1.4;margin:10px 12px}
.chips{display:flex;gap:6px;overflow-x:auto;padding:8px 0 2px;-webkit-overflow-scrolling:touch}
.chips button{flex:none;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:16px;padding:6px 12px;font:600 13px inherit;cursor:pointer}
.ctrl{display:flex;align-items:center;gap:8px;margin-top:8px}
.ctrl button{flex:none;border:none;border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center}
#play{width:48px;height:48px;background:var(--s2);color:#fff;font-size:18px}
.ctrl .stp{width:36px;height:36px;background:var(--bg);color:var(--ink);border:1px solid var(--line);font-size:13px}
#slider{flex:1;min-width:0;accent-color:var(--s2);height:30px}
.legend{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center;font-size:12px;color:var(--mut);margin-top:8px}
.legend .sw{display:inline-flex;gap:0;vertical-align:middle;margin:0 4px}
.legend .sw i{display:inline-block;width:14px;height:10px}
.links{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.links a{padding:7px 11px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);text-decoration:none;font-size:13.5px}
.links a.primary{background:var(--s2);border-color:var(--s2);color:#fff;font-weight:600}
.mnote{font-size:12px;color:var(--mut);margin-top:8px}
.key{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 12px;font-size:13px;color:var(--mut);margin-top:16px}
.key b{color:var(--ink)}
details.reg{margin-top:18px}
details.reg>summary{list-style:none;cursor:pointer;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;padding:8px 2px 10px}
details.reg>summary::-webkit-details-marker{display:none}
details.reg>summary::before{content:"▾";color:var(--mut);font-size:14px;width:12px}
details.reg:not([open])>summary::before{content:"▸"}
.rt{font-size:13.5px;text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.rs{font-size:12.5px;color:var(--mut)}
.chartbox{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 10px 4px;margin-bottom:12px}
.ct{font-size:12.5px;color:var(--mut);margin:0 2px 4px}
.chart{width:100%;height:auto;display:block}
.chart .gl{stroke:var(--line);stroke-width:1}
.chart .ax{fill:var(--mut);font-size:11px}
.chart .ln{fill:none;stroke-width:2.6;stroke-linejoin:round;stroke-linecap:round}
.chart .el{font-size:12px;font-weight:700}
.chart .farbg{fill:var(--line);opacity:.45}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px;scroll-margin-top:12px}
.card:target{outline:2px solid var(--s2)}
.ch{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.ch h3{margin:0;font-size:16.5px}
.sub{color:var(--mut);font-size:12.5px}
.badge{min-width:38px;text-align:center;padding:4px 8px;border-radius:8px;font-weight:700;color:#fff}
.badge.s0{background:var(--s0);color:var(--ink)}.badge.s1{background:var(--s1)}.badge.s2{background:var(--s2)}.badge.s3{background:var(--s3);color:var(--bt)}
.next{font-size:13px;margin:7px 0 9px}
.bars{display:grid;gap:3px;align-items:start}
.bar{text-align:center;font-size:10.5px}
.bar .v{font-weight:700;font-size:12px}
.track{height:66px;display:flex;align-items:flex-end;justify-content:center;background:linear-gradient(var(--line),var(--line)) bottom/100% 1px no-repeat}
.fill{width:72%;border-radius:4px 4px 0 0}
.fill.s0{background:var(--s0)}.fill.s1{background:var(--s1)}.fill.s2{background:var(--s2)}.fill.s3{background:var(--s3)}
.bar.far .fill{opacity:.5;background-image:repeating-linear-gradient(45deg,transparent 0 3px,rgba(255,255,255,.35) 3px 6px)}
.bar.far .v,.bar.far .dl{opacity:.65}
.rng{color:var(--mut);font-size:10px}
.dl{color:var(--mut);margin-top:1px;line-height:1.2}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:24px 2px 10px}
.read{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;font-size:14.5px}
.foot{color:var(--mut);font-size:12.5px;margin-top:22px}
.warn{color:var(--warn);font-size:12px}
@media (max-width:480px){#tmain{font-size:21px}.grid{grid-template-columns:1fr}.seg button{padding:7px 11px}}
</style></head><body><div class="wrap">
<header><h1>Snow Tracker</h1><div class="stamp">Updated {{stamp}}</div></header>
<div class="call">{{call}}</div>
<div class="alerts">{{alerts}}</div>
<div class="provo">{{provo}}</div>

<div class="mapcard">
  <div class="tbar"><div id="tmain">Loading…</div><div id="tsub">&nbsp;</div></div>
  <div class="seg" id="seg"><button type="button" data-l="radar" class="on">Future radar</button><button type="button" data-l="snow">Snow total</button></div>
  <div id="map"></div>
  <div class="chips" id="chips"></div>
  <div class="ctrl"><button type="button" class="stp" id="prev" aria-label="Back">◀</button><button type="button" id="play" aria-label="Play">▶</button><button type="button" class="stp" id="next" aria-label="Forward">▶</button><input type="range" id="slider" min="0" max="0" value="0" aria-label="Forecast time"></div>
  <div class="legend" id="legend"></div>
  <div class="mnote" id="mnote">Pinch or use + and − to zoom (on a computer, click the map once to zoom with the scroll wheel), drag to move, tap anywhere for that spot's forecast. White dots are your resorts; their numbers are projected snow from now until the time shown, the same blended forecast as the cards below.</div>
  <div class="links"><a class="primary" href="https://www.windy.com/-Weather-radar-radar?radar,41.0,-112.0,5">Live radar right now</a><a href="https://radar.weather.gov/station/kmtx/standard">Salt Lake radar</a></div>
</div>

<div class="key"><b>What the score means:</b> the number in each card's corner rates that resort's best powder morning in the window, 0 to 10. It is mostly overnight snow before first chair (about 3" = 3.5, 6" = 6, 10" = 7.6, 15" = 9), minus points when the forecasts disagree, gusts top 35 mph (lift holds), temps run above freezing (heavy snow), or it lands on a Utah weekend (crowds). 6+ is a go day, 3.5 to 6 is good, under 3.5 is a refresh. Tap a region name to hide or show it.</div>
{{sections}}

<h2>Forecaster's read (NWS Salt Lake City)</h2>
<div class="read"><!--FORECASTER_READ--></div>

<h2>Check it yourself</h2>
<div class="links">
<a href="https://forecast.weather.gov/product.php?site=SLC&amp;issuedby=SLC&amp;product=AFD">NWS forecast discussion</a>
<a href="https://utahavalanchecenter.org/forecast/salt-lake">Avalanche forecast</a>
<a href="https://udottraffic.utah.gov">UDOT roads and canyons</a>
<a href="https://opensnow.com/dailysnow/utah">OpenSnow Utah (other notes)</a>
</div>

<div class="foot">
<p><b>How to read it.</b> The map's colors come from NOAA's GFS weather model (about 25 km grid), stepping every 3 hours for 5 days and every 6 hours after that. "Future radar" shows what the model expects to be falling at each time; "Snow total" shows snow piled up from now to that time. The model's grid smooths mountain peaks, so its mountain totals usually run lower than the resort numbers. The resort numbers on the map, the charts, and the cards use the finer blended forecast: the official NWS snowfall forecast averaged with the median of the independent weather models. Faded, striped bars and shaded chart areas are past the NWS range (about 7 days) and come from models alone. Sources this run: {{models}}. Snow on the ground comes from the nearest SNOTEL station (measured, not resort-reported). The live radar can't run inside this page, so that button opens it in a new tab.</p>
{{warn}}
</div>
</div>
<script id="mdata" type="application/json">{{mdata}}</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
(function(){
  try{document.querySelectorAll("details.reg").forEach(function(d){
    try{if(localStorage.getItem("snow-hide-"+d.id)==="1")d.removeAttribute("open")}catch(e){}
    d.addEventListener("toggle",function(){try{localStorage.setItem("snow-hide-"+d.id,d.open?"0":"1")}catch(e){}});
  })}catch(e){}
  var M=JSON.parse(document.getElementById("mdata").textContent),R=M.radar;
  var $=function(id){return document.getElementById(id)};
  if(typeof L==="undefined"){$("tmain").textContent="Map could not load";return}
  var map=L.map("map",{attributionControl:false,zoomSnap:0.5,minZoom:3,maxZoom:10,scrollWheelZoom:false,maxBounds:[[10,-150],[60,-50]]});map.on("click",function(){map.scrollWheelZoom.enable()});map.on("mouseout",function(){map.scrollWheelZoom.disable()});
  map.createPane("land").style.zIndex=200;map.createPane("wx").style.zIndex=300;
  var lp=map.createPane("lines");lp.style.zIndex=350;lp.style.pointerEvents="none";
  var VIEWS={"West":[[35.6,-124.4],[45.6,-104.2]],"Utah":[[39.9,-112.4],[41.5,-110.9]],"Tahoe":[[38.4,-120.9],[39.7,-119.5]],"Mammoth":[[37.2,-119.5],[38.0,-118.5]],"Tetons":[[43.2,-111.5],[44.2,-110.3]],"Colorado":[[38.9,-107.4],[40.7,-105.2]],"USA":[[24.5,-125],[49.5,-66.5]]};
  map.fitBounds(VIEWS.West);
  Object.keys(VIEWS).forEach(function(k){var b=document.createElement("button");b.type="button";b.textContent=k;b.onclick=function(){map.flyToBounds(VIEWS[k],{duration:.6})};$("chips").appendChild(b)});
  fetch("base.json").then(function(r){return r.json()}).then(function(b){
    L.geoJSON({type:"FeatureCollection",features:b.countries},{pane:"land",interactive:false,style:function(){return{className:"cty"}}}).addTo(map);
    L.geoJSON({type:"FeatureCollection",features:b.states},{pane:"lines",interactive:false,style:function(){return{className:"stb",fill:false}}}).addTo(map);
  }).catch(function(){});
  // frame times
  var times=[];
  if(R&&R.times&&R.times.length){times=R.times.map(function(t){return new Date(t)})}
  else{var seen={};M.resorts.forEach(function(r){r.tl.forEach(function(x){seen[x[0]]=1})});times=Object.keys(seen).sort().map(function(t){return new Date(t)})}
  var sl=$("slider");sl.max=Math.max(0,times.length-1);
  var start=0,now=Date.now();for(var i=0;i<times.length;i++){if(times[i].getTime()>=now-90*60000){start=i;break}}
  sl.value=start;
  var layer="radar",frame=start;
  // resort markers and their blended running totals
  function cumAt(r,t){var tl=r.tl,prev=0,ps=new Date(tl[0][0]).getTime()-17*3600000,T=t.getTime(),base=times.length?times[start].getTime():T;
    function at(T2){var p=0,s=new Date(tl[0][0]).getTime()-17*3600000;for(var k=0;k<tl.length;k++){var e=new Date(tl[k][0]).getTime();if(T2>=e){p=tl[k][1];s=e;continue}var c=tl[k][1];return p+(c-p)*Math.max(0,(T2-s)/(e-s))}return p}
    return Math.max(0,at(T)-at(base))}
  var marks=M.resorts.map(function(r){var m=L.circleMarker([r.lat,r.lon],{radius:5,className:"rm"}).addTo(map);
    m.bindTooltip("",{permanent:true,direction:"right",className:"rlab",offset:[7,0]});
    m.on("click",function(ev){L.DomEvent.stopPropagation(ev);var v=cumAt(r,times[frame]);
      L.popup().setLatLng([r.lat,r.lon]).setContent("<b>"+r.name+"</b><br>"+Math.round(v)+'" projected by '+fmt(times[frame])+"<br>Next 5 days: ~"+Math.round(r.t5)+'"<br><a href="#'+r.id+'">See the day-by-day card</a>').openOn(map)});
    return{r:r,m:m}});
  function labels(){var z=map.getZoom(),t=times[frame];marks.forEach(function(o){var r=o.r,txt=null;
    if(z>=r.zf)txt=r.name;else if(z>=r.zs&&r.short)txt=r.short;
    if(txt===null){o.m.closeTooltip();return}
    o.m.setTooltipContent(txt+" "+Math.round(cumAt(r,t))+'"');if(!o.m.isTooltipOpen())o.m.openTooltip()})}
  map.on("zoomend",labels);
  // model grids
  var A=null,S=null,cache={},overlay=null;
  var RAIN=["#9be89b","#6fd66f","#3fbd3f","#1f9a1f","#e8e000","#f5a300","#f05a00","#cc0000"],SNOW=["#cfe8ff","#a8d4ff","#7ab8ff","#4d94ff","#2f66eb","#3b35d0","#7330d0","#a82bd0"],MIX=["#ffd6ea","#ffb8dc","#ff94c9","#ff6fb5","#f2479f","#d6267f","#b3125f","#8c0046"];
  var SB=[0.5,1,2,3,4,6,8,12,18,24,30,36,48],SC=["#bdd7e7","#6baed6","#3182bd","#08519c","#082694","#ffff96","#ffc400","#ff8700","#db1400","#9e0000","#690000","#ccccff","#9f8cd8"];
  function hex(h,a){return[parseInt(h.substr(1,2),16),parseInt(h.substr(3,2),16),parseInt(h.substr(5,2),16),a]}
  var PR=[],PS=[];for(var v=0;v<256;v++){var k=v>>4,l=v&15,c=[0,0,0,0];if(v&&l){var pal=k==2?SNOW:k==3?MIX:RAIN;c=hex(pal[Math.min(l,8)-1],l<=1?140:l<=2?185:225)}PR.push(c);
    var inch=v/2,cs=[0,0,0,0];for(var q=SB.length-1;q>=0;q--){if(inch>=SB[q]){cs=hex(SC[q],205);break}}PS.push(cs)}
  function sprite(src){return new Promise(function(res){var im=new Image();im.onload=function(){try{var c=document.createElement("canvas");c.width=im.naturalWidth;c.height=im.naturalHeight;var x=c.getContext("2d",{willReadFrequently:true});x.drawImage(im,0,0);res(x.getImageData(0,0,c.width,c.height).data)}catch(e){res(null)}};im.onerror=function(){res(null)};im.src=src})}
  var B=null,rows=null,rowsF=null,OW=0,OH=0;
  if(R){var dlat=(R.north-R.south)/(R.ny-1),dlon=(R.east-R.west)/(R.nx-1),N=R.north+dlat/2,Sx=R.south-dlat/2,W=R.west-dlon/2,E=R.east+dlon/2;
    B=[[Sx,W],[N,E]];var mer=function(a){return Math.log(Math.tan(Math.PI/4+a*Math.PI/360))},yN=mer(N),yS=mer(Sx);
    OW=R.nx;OH=Math.round(R.ny*1.5);rows=new Int32Array(OH);rowsF=new Float32Array(OH);
    for(var j=0;j<OH;j++){var y=yN-(j+.5)/OH*(yN-yS),la=(2*Math.atan(Math.exp(y))-Math.PI/2)*180/Math.PI;rows[j]=Math.min(R.ny-1,Math.max(0,Math.round((R.north-la)/dlat)));rowsF[j]=(R.north-la)/dlat}}
  function frameURL(f){var key=layer+f;if(cache[key])return cache[key];var D=layer=="radar"?A:S;if(!D)return null;
    var U=4,w=OW*U,h=OH*U,nx=R.nx,ny=R.ny,base=f*ny*nx,c=document.createElement("canvas");c.width=w;c.height=h;var x=c.getContext("2d"),img=x.createImageData(w,h),o=img.data;
    for(var j=0;j<h;j++){var sy=rowsF[Math.floor(j/U)]+((j%U)+.5)/U*(rowsF[Math.min(OH-1,Math.floor(j/U)+1)]-rowsF[Math.floor(j/U)]),y0=Math.max(0,Math.min(ny-1,Math.floor(sy))),y1=Math.min(ny-1,y0+1),fy=Math.max(0,Math.min(1,sy-y0));
      for(var i=0;i<w;i++){var sx=(i+.5)/U-.5,x0=Math.max(0,Math.min(nx-1,Math.floor(sx))),x1=Math.min(nx-1,x0+1),fx=Math.max(0,Math.min(1,sx-x0));
        var a00=D[(base+y0*nx+x0)*4],a01=D[(base+y0*nx+x1)*4],a10=D[(base+y1*nx+x0)*4],a11=D[(base+y1*nx+x1)*4],w00=(1-fx)*(1-fy),w01=fx*(1-fy),w10=(1-fx)*fy,w11=fx*fy,p,d=(j*w+i)*4;
        if(layer=="radar"){var l=(a00&15)*w00+(a01&15)*w01+(a10&15)*w10+(a11&15)*w11;if(l<0.6){o[d+3]=0;continue}
          var k=a00>>4,bw=a00?w00:-1;if(a01&&w01>bw){k=a01>>4;bw=w01}if(a10&&w10>bw){k=a10>>4;bw=w10}if(a11&&w11>bw){k=a11>>4}
          p=PR[(k||1)*16+Math.max(1,Math.min(8,Math.round(l)))]}
        else{p=PS[Math.round(a00*w00+a01*w01+a10*w10+a11*w11)]}
        o[d]=p[0];o[d+1]=p[1];o[d+2]=p[2];o[d+3]=p[3]}}
    x.putImageData(img,0,0);return cache[key]=c.toDataURL()}
  function fmt(t){return t.toLocaleString([], {weekday:"short",hour:"numeric"})}
  function rel(t){var h=Math.round((t.getTime()-Date.now())/3600000);if(Math.abs(h)<=1)return"right now";if(h<0)return-h+" hrs ago";if(h<48)return"in "+h+" hrs";return"in "+Math.round(h/24)+" days"}
  function legend(){var g=$("legend");if(layer=="radar"){function sw(p){return'<span class="sw">'+p.map(function(c){return'<i style="background:'+c+'"></i>'}).join("")+"</span>"}
      g.innerHTML="<span>Light → heavy:</span><span>Snow"+sw(SNOW)+"</span><span>Mix/ice"+sw(MIX)+"</span><span>Rain"+sw(RAIN)+"</span>"}
    else{g.innerHTML="<span>Snow piled up by the time shown:</span>"+SB.map(function(b,i){return'<span><i style="display:inline-block;width:12px;height:10px;background:'+SC[i]+';margin-right:3px"></i>'+b+'"</span>'}).join("")}}
  function show(f){frame=f;var t=times[f];if(!t)return;
    $("tmain").textContent=fmt(t);
    var run=R?new Date(R.run):null;$("tsub").textContent=rel(t)+(R?" · GFS model run "+run.toLocaleString([], {weekday:"short",hour:"numeric"}):" · blended resort forecast");
    if(R){var u=frameURL(f);if(u){if(!overlay)overlay=L.imageOverlay(u,B,{pane:"wx",opacity:.85,className:"wximg",interactive:false}).addTo(map);else overlay.setUrl(u)}}
    labels()}
  var timer=null;function stop(){if(timer){clearInterval(timer);timer=null;$("play").textContent="▶"}}
  $("play").onclick=function(){if(timer){stop();return}if(frame>=times.length-1){sl.value=start;show(start)}$("play").textContent="❚❚";
    timer=setInterval(function(){var n=frame+1;if(n>times.length-1){stop();return}sl.value=n;show(n)},650)};
  $("prev").onclick=function(){stop();var n=Math.max(0,frame-1);sl.value=n;show(n)};
  $("next").onclick=function(){stop();var n=Math.min(times.length-1,frame+1);sl.value=n;show(n)};
  sl.addEventListener("input",function(){stop();show(+sl.value)});
  document.querySelectorAll("#seg button").forEach(function(b){b.onclick=function(){layer=b.getAttribute("data-l");if(layer=="snow"&&frame===start){var tgt=Date.now()+5*86400000,bi=start;for(var q=start;q<times.length;q++){if(times[q].getTime()<=tgt)bi=q}frame=bi;sl.value=bi}document.querySelectorAll("#seg button").forEach(function(x){x.classList.toggle("on",x===b)});legend();show(frame)}});
  map.on("click",function(e){if(!R||!A)return;var i=Math.round((e.latlng.lng-R.west)/((R.east-R.west)/(R.nx-1))),j=Math.round((R.north-e.latlng.lat)/((R.north-R.south)/(R.ny-1)));
    if(i<0||j<0||i>=R.nx||j>=R.ny)return;var idx=((frame*R.ny+j)*R.nx+i)*4,vr=A[idx],vs=S?S[idx]/2:0,k=vr>>4,l=vr&15;
    var kinds={1:"Rain",2:"Snow",3:"Mixed or icy"},ints=["","very light","very light","light","light","moderate","moderate","heavy","very heavy"];
    var now=vr&&l?kinds[k]+", "+ints[Math.min(l,8)]:"Nothing falling";
    L.popup().setLatLng(e.latlng).setContent("<b>"+fmt(times[frame])+"</b><br>"+now+"<br>Snow piled up since now: "+(vs<0.5?"under 0.5":Math.round(vs*2)/2)+'"<br><span style="color:#888">GFS model, ~25 km grid</span>').openOn(map)});
  if(!R){$("seg").style.display="none";$("legend").textContent="Model map unavailable tonight; resort numbers still update."}
  legend();show(start);
  if(R){Promise.all([sprite("radar.png"),sprite("snowacc.png")]).then(function(a){A=a[0];S=a[1];if(!A){$("legend").textContent="Model map could not load."}cache={};show(frame)})}
})();
</script>
</body></html>
"""

LEAFLET_CSS = r"""/* required styles */

.leaflet-pane,
.leaflet-tile,
.leaflet-marker-icon,
.leaflet-marker-shadow,
.leaflet-tile-container,
.leaflet-pane > svg,
.leaflet-pane > canvas,
.leaflet-zoom-box,
.leaflet-image-layer,
.leaflet-layer {
	position: absolute;
	left: 0;
	top: 0;
	}
.leaflet-container {
	overflow: hidden;
	}
.leaflet-tile,
.leaflet-marker-icon,
.leaflet-marker-shadow {
	-webkit-user-select: none;
	   -moz-user-select: none;
	        user-select: none;
	  -webkit-user-drag: none;
	}
/* Prevents IE11 from highlighting tiles in blue */
.leaflet-tile::selection {
	background: transparent;
}
/* Safari renders non-retina tile on retina better with this, but Chrome is worse */
.leaflet-safari .leaflet-tile {
	image-rendering: -webkit-optimize-contrast;
	}
/* hack that prevents hw layers "stretching" when loading new tiles */
.leaflet-safari .leaflet-tile-container {
	width: 1600px;
	height: 1600px;
	-webkit-transform-origin: 0 0;
	}
.leaflet-marker-icon,
.leaflet-marker-shadow {
	display: block;
	}
/* .leaflet-container svg: reset svg max-width decleration shipped in Joomla! (joomla.org) 3.x */
/* .leaflet-container img: map is broken in FF if you have max-width: 100% on tiles */
.leaflet-container .leaflet-overlay-pane svg {
	max-width: none !important;
	max-height: none !important;
	}
.leaflet-container .leaflet-marker-pane img,
.leaflet-container .leaflet-shadow-pane img,
.leaflet-container .leaflet-tile-pane img,
.leaflet-container img.leaflet-image-layer,
.leaflet-container .leaflet-tile {
	max-width: none !important;
	max-height: none !important;
	width: auto;
	padding: 0;
	}

.leaflet-container img.leaflet-tile {
	/* See: https://bugs.chromium.org/p/chromium/issues/detail?id=600120 */
	mix-blend-mode: plus-lighter;
}

.leaflet-container.leaflet-touch-zoom {
	-ms-touch-action: pan-x pan-y;
	touch-action: pan-x pan-y;
	}
.leaflet-container.leaflet-touch-drag {
	-ms-touch-action: pinch-zoom;
	/* Fallback for FF which doesn't support pinch-zoom */
	touch-action: none;
	touch-action: pinch-zoom;
}
.leaflet-container.leaflet-touch-drag.leaflet-touch-zoom {
	-ms-touch-action: none;
	touch-action: none;
}
.leaflet-container {
	-webkit-tap-highlight-color: transparent;
}
.leaflet-container a {
	-webkit-tap-highlight-color: rgba(51, 181, 229, 0.4);
}
.leaflet-tile {
	filter: inherit;
	visibility: hidden;
	}
.leaflet-tile-loaded {
	visibility: inherit;
	}
.leaflet-zoom-box {
	width: 0;
	height: 0;
	-moz-box-sizing: border-box;
	     box-sizing: border-box;
	z-index: 800;
	}
/* workaround for https://bugzilla.mozilla.org/show_bug.cgi?id=888319 */
.leaflet-overlay-pane svg {
	-moz-user-select: none;
	}

.leaflet-pane         { z-index: 400; }

.leaflet-tile-pane    { z-index: 200; }
.leaflet-overlay-pane { z-index: 400; }
.leaflet-shadow-pane  { z-index: 500; }
.leaflet-marker-pane  { z-index: 600; }
.leaflet-tooltip-pane   { z-index: 650; }
.leaflet-popup-pane   { z-index: 700; }

.leaflet-map-pane canvas { z-index: 100; }
.leaflet-map-pane svg    { z-index: 200; }

.leaflet-vml-shape {
	width: 1px;
	height: 1px;
	}
.lvml {
	behavior: url(#default#VML);
	display: inline-block;
	position: absolute;
	}


/* control positioning */

.leaflet-control {
	position: relative;
	z-index: 800;
	pointer-events: visiblePainted; /* IE 9-10 doesn't have auto */
	pointer-events: auto;
	}
.leaflet-top,
.leaflet-bottom {
	position: absolute;
	z-index: 1000;
	pointer-events: none;
	}
.leaflet-top {
	top: 0;
	}
.leaflet-right {
	right: 0;
	}
.leaflet-bottom {
	bottom: 0;
	}
.leaflet-left {
	left: 0;
	}
.leaflet-control {
	float: left;
	clear: both;
	}
.leaflet-right .leaflet-control {
	float: right;
	}
.leaflet-top .leaflet-control {
	margin-top: 10px;
	}
.leaflet-bottom .leaflet-control {
	margin-bottom: 10px;
	}
.leaflet-left .leaflet-control {
	margin-left: 10px;
	}
.leaflet-right .leaflet-control {
	margin-right: 10px;
	}


/* zoom and fade animations */

.leaflet-fade-anim .leaflet-popup {
	opacity: 0;
	-webkit-transition: opacity 0.2s linear;
	   -moz-transition: opacity 0.2s linear;
	        transition: opacity 0.2s linear;
	}
.leaflet-fade-anim .leaflet-map-pane .leaflet-popup {
	opacity: 1;
	}
.leaflet-zoom-animated {
	-webkit-transform-origin: 0 0;
	    -ms-transform-origin: 0 0;
	        transform-origin: 0 0;
	}
svg.leaflet-zoom-animated {
	will-change: transform;
}

.leaflet-zoom-anim .leaflet-zoom-animated {
	-webkit-transition: -webkit-transform 0.25s cubic-bezier(0,0,0.25,1);
	   -moz-transition:    -moz-transform 0.25s cubic-bezier(0,0,0.25,1);
	        transition:         transform 0.25s cubic-bezier(0,0,0.25,1);
	}
.leaflet-zoom-anim .leaflet-tile,
.leaflet-pan-anim .leaflet-tile {
	-webkit-transition: none;
	   -moz-transition: none;
	        transition: none;
	}

.leaflet-zoom-anim .leaflet-zoom-hide {
	visibility: hidden;
	}


/* cursors */

.leaflet-interactive {
	cursor: pointer;
	}
.leaflet-grab {
	cursor: -webkit-grab;
	cursor:    -moz-grab;
	cursor:         grab;
	}
.leaflet-crosshair,
.leaflet-crosshair .leaflet-interactive {
	cursor: crosshair;
	}
.leaflet-popup-pane,
.leaflet-control {
	cursor: auto;
	}
.leaflet-dragging .leaflet-grab,
.leaflet-dragging .leaflet-grab .leaflet-interactive,
.leaflet-dragging .leaflet-marker-draggable {
	cursor: move;
	cursor: -webkit-grabbing;
	cursor:    -moz-grabbing;
	cursor:         grabbing;
	}

/* marker & overlays interactivity */
.leaflet-marker-icon,
.leaflet-marker-shadow,
.leaflet-image-layer,
.leaflet-pane > svg path,
.leaflet-tile-container {
	pointer-events: none;
	}

.leaflet-marker-icon.leaflet-interactive,
.leaflet-image-layer.leaflet-interactive,
.leaflet-pane > svg path.leaflet-interactive,
svg.leaflet-image-layer.leaflet-interactive path {
	pointer-events: visiblePainted; /* IE 9-10 doesn't have auto */
	pointer-events: auto;
	}

/* visual tweaks */

.leaflet-container {
	background: #ddd;
	outline-offset: 1px;
	}
.leaflet-container a {
	color: #0078A8;
	}
.leaflet-zoom-box {
	border: 2px dotted #38f;
	background: rgba(255,255,255,0.5);
	}


/* general typography */
.leaflet-container {
	font-family: "Helvetica Neue", Arial, Helvetica, sans-serif;
	font-size: 12px;
	font-size: 0.75rem;
	line-height: 1.5;
	}


/* general toolbar styles */

.leaflet-bar {
	box-shadow: 0 1px 5px rgba(0,0,0,0.65);
	border-radius: 4px;
	}
.leaflet-bar a {
	background-color: #fff;
	border-bottom: 1px solid #ccc;
	width: 26px;
	height: 26px;
	line-height: 26px;
	display: block;
	text-align: center;
	text-decoration: none;
	color: black;
	}
.leaflet-bar a,
.leaflet-control-layers-toggle {
	background-position: 50% 50%;
	background-repeat: no-repeat;
	display: block;
	}
.leaflet-bar a:hover,
.leaflet-bar a:focus {
	background-color: #f4f4f4;
	}
.leaflet-bar a:first-child {
	border-top-left-radius: 4px;
	border-top-right-radius: 4px;
	}
.leaflet-bar a:last-child {
	border-bottom-left-radius: 4px;
	border-bottom-right-radius: 4px;
	border-bottom: none;
	}
.leaflet-bar a.leaflet-disabled {
	cursor: default;
	background-color: #f4f4f4;
	color: #bbb;
	}

.leaflet-touch .leaflet-bar a {
	width: 30px;
	height: 30px;
	line-height: 30px;
	}
.leaflet-touch .leaflet-bar a:first-child {
	border-top-left-radius: 2px;
	border-top-right-radius: 2px;
	}
.leaflet-touch .leaflet-bar a:last-child {
	border-bottom-left-radius: 2px;
	border-bottom-right-radius: 2px;
	}

/* zoom control */

.leaflet-control-zoom-in,
.leaflet-control-zoom-out {
	font: bold 18px 'Lucida Console', Monaco, monospace;
	text-indent: 1px;
	}

.leaflet-touch .leaflet-control-zoom-in, .leaflet-touch .leaflet-control-zoom-out  {
	font-size: 22px;
	}


/* layers control */

.leaflet-control-layers {
	box-shadow: 0 1px 5px rgba(0,0,0,0.4);
	background: #fff;
	border-radius: 5px;
	}
.leaflet-control-layers-toggle {
	background-image: url(images/layers.png);
	width: 36px;
	height: 36px;
	}
.leaflet-retina .leaflet-control-layers-toggle {
	background-image: url(images/layers-2x.png);
	background-size: 26px 26px;
	}
.leaflet-touch .leaflet-control-layers-toggle {
	width: 44px;
	height: 44px;
	}
.leaflet-control-layers .leaflet-control-layers-list,
.leaflet-control-layers-expanded .leaflet-control-layers-toggle {
	display: none;
	}
.leaflet-control-layers-expanded .leaflet-control-layers-list {
	display: block;
	position: relative;
	}
.leaflet-control-layers-expanded {
	padding: 6px 10px 6px 6px;
	color: #333;
	background: #fff;
	}
.leaflet-control-layers-scrollbar {
	overflow-y: scroll;
	overflow-x: hidden;
	padding-right: 5px;
	}
.leaflet-control-layers-selector {
	margin-top: 2px;
	position: relative;
	top: 1px;
	}
.leaflet-control-layers label {
	display: block;
	font-size: 13px;
	font-size: 1.08333em;
	}
.leaflet-control-layers-separator {
	height: 0;
	border-top: 1px solid #ddd;
	margin: 5px -10px 5px -6px;
	}

/* Default icon URLs */
.leaflet-default-icon-path { /* used only in path-guessing heuristic, see L.Icon.Default */
	background-image: url(images/marker-icon.png);
	}


/* attribution and scale controls */

.leaflet-container .leaflet-control-attribution {
	background: #fff;
	background: rgba(255, 255, 255, 0.8);
	margin: 0;
	}
.leaflet-control-attribution,
.leaflet-control-scale-line {
	padding: 0 5px;
	color: #333;
	line-height: 1.4;
	}
.leaflet-control-attribution a {
	text-decoration: none;
	}
.leaflet-control-attribution a:hover,
.leaflet-control-attribution a:focus {
	text-decoration: underline;
	}
.leaflet-attribution-flag {
	display: inline !important;
	vertical-align: baseline !important;
	width: 1em;
	height: 0.6669em;
	}
.leaflet-left .leaflet-control-scale {
	margin-left: 5px;
	}
.leaflet-bottom .leaflet-control-scale {
	margin-bottom: 5px;
	}
.leaflet-control-scale-line {
	border: 2px solid #777;
	border-top: none;
	line-height: 1.1;
	padding: 2px 5px 1px;
	white-space: nowrap;
	-moz-box-sizing: border-box;
	     box-sizing: border-box;
	background: rgba(255, 255, 255, 0.8);
	text-shadow: 1px 1px #fff;
	}
.leaflet-control-scale-line:not(:first-child) {
	border-top: 2px solid #777;
	border-bottom: none;
	margin-top: -2px;
	}
.leaflet-control-scale-line:not(:first-child):not(:last-child) {
	border-bottom: 2px solid #777;
	}

.leaflet-touch .leaflet-control-attribution,
.leaflet-touch .leaflet-control-layers,
.leaflet-touch .leaflet-bar {
	box-shadow: none;
	}
.leaflet-touch .leaflet-control-layers,
.leaflet-touch .leaflet-bar {
	border: 2px solid rgba(0,0,0,0.2);
	background-clip: padding-box;
	}


/* popup */

.leaflet-popup {
	position: absolute;
	text-align: center;
	margin-bottom: 20px;
	}
.leaflet-popup-content-wrapper {
	padding: 1px;
	text-align: left;
	border-radius: 12px;
	}
.leaflet-popup-content {
	margin: 13px 24px 13px 20px;
	line-height: 1.3;
	font-size: 13px;
	font-size: 1.08333em;
	min-height: 1px;
	}
.leaflet-popup-content p {
	margin: 17px 0;
	margin: 1.3em 0;
	}
.leaflet-popup-tip-container {
	width: 40px;
	height: 20px;
	position: absolute;
	left: 50%;
	margin-top: -1px;
	margin-left: -20px;
	overflow: hidden;
	pointer-events: none;
	}
.leaflet-popup-tip {
	width: 17px;
	height: 17px;
	padding: 1px;

	margin: -10px auto 0;
	pointer-events: auto;

	-webkit-transform: rotate(45deg);
	   -moz-transform: rotate(45deg);
	    -ms-transform: rotate(45deg);
	        transform: rotate(45deg);
	}
.leaflet-popup-content-wrapper,
.leaflet-popup-tip {
	background: white;
	color: #333;
	box-shadow: 0 3px 14px rgba(0,0,0,0.4);
	}
.leaflet-container a.leaflet-popup-close-button {
	position: absolute;
	top: 0;
	right: 0;
	border: none;
	text-align: center;
	width: 24px;
	height: 24px;
	font: 16px/24px Tahoma, Verdana, sans-serif;
	color: #757575;
	text-decoration: none;
	background: transparent;
	}
.leaflet-container a.leaflet-popup-close-button:hover,
.leaflet-container a.leaflet-popup-close-button:focus {
	color: #585858;
	}
.leaflet-popup-scrolled {
	overflow: auto;
	}

.leaflet-oldie .leaflet-popup-content-wrapper {
	-ms-zoom: 1;
	}
.leaflet-oldie .leaflet-popup-tip {
	width: 24px;
	margin: 0 auto;

	-ms-filter: "progid:DXImageTransform.Microsoft.Matrix(M11=0.70710678, M12=0.70710678, M21=-0.70710678, M22=0.70710678)";
	filter: progid:DXImageTransform.Microsoft.Matrix(M11=0.70710678, M12=0.70710678, M21=-0.70710678, M22=0.70710678);
	}

.leaflet-oldie .leaflet-control-zoom,
.leaflet-oldie .leaflet-control-layers,
.leaflet-oldie .leaflet-popup-content-wrapper,
.leaflet-oldie .leaflet-popup-tip {
	border: 1px solid #999;
	}


/* div icon */

.leaflet-div-icon {
	background: #fff;
	border: 1px solid #666;
	}


/* Tooltip */
/* Base styles for the element that has a tooltip */
.leaflet-tooltip {
	position: absolute;
	padding: 6px;
	background-color: #fff;
	border: 1px solid #fff;
	border-radius: 3px;
	color: #222;
	white-space: nowrap;
	-webkit-user-select: none;
	-moz-user-select: none;
	-ms-user-select: none;
	user-select: none;
	pointer-events: none;
	box-shadow: 0 1px 3px rgba(0,0,0,0.4);
	}
.leaflet-tooltip.leaflet-interactive {
	cursor: pointer;
	pointer-events: auto;
	}
.leaflet-tooltip-top:before,
.leaflet-tooltip-bottom:before,
.leaflet-tooltip-left:before,
.leaflet-tooltip-right:before {
	position: absolute;
	pointer-events: none;
	border: 6px solid transparent;
	background: transparent;
	content: "";
	}

/* Directions */

.leaflet-tooltip-bottom {
	margin-top: 6px;
}
.leaflet-tooltip-top {
	margin-top: -6px;
}
.leaflet-tooltip-bottom:before,
.leaflet-tooltip-top:before {
	left: 50%;
	margin-left: -6px;
	}
.leaflet-tooltip-top:before {
	bottom: 0;
	margin-bottom: -12px;
	border-top-color: #fff;
	}
.leaflet-tooltip-bottom:before {
	top: 0;
	margin-top: -12px;
	margin-left: -6px;
	border-bottom-color: #fff;
	}
.leaflet-tooltip-left {
	margin-left: -6px;
}
.leaflet-tooltip-right {
	margin-left: 6px;
}
.leaflet-tooltip-left:before,
.leaflet-tooltip-right:before {
	top: 50%;
	margin-top: -6px;
	}
.leaflet-tooltip-left:before {
	right: 0;
	margin-right: -12px;
	border-left-color: #fff;
	}
.leaflet-tooltip-right:before {
	left: 0;
	margin-left: -12px;
	border-right-color: #fff;
	}

/* Printing */

@media print {
	/* Prevent printers from removing background-images of controls. */
	.leaflet-control {
		-webkit-print-color-adjust: exact;
		print-color-adjust: exact;
		}
	}
"""


if __name__ == "__main__":
    main()
