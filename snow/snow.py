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


def xy(lat, lon):
    lon0, lon1, lat0, lat1 = STATE_MAP["box"]
    return ((lon - lon0) / (lon1 - lon0) * STATE_MAP["W"], (lat1 - lat) / (lat1 - lat0) * STATE_MAP["H"])


# label placement on the map: ("rel", dx, dy) from the dot or ("abs", x, y), plus text anchor
LABELS = {
    "snowbasin": ("rel", -12, -8, "end"), "snowbird": ("rel", -14, 16, "end"),
    "solitude": ("rel", 16, -12, "start"), "brighton": ("rel", 16, 6, "start"),
    "targhee": ("rel", -12, -6, "end"), "jackson": ("rel", 12, 10, "start"),
    "steamboat": ("rel", -12, -6, "end"), "snowmass": ("rel", -12, 12, "end"),
    "eldora": ("abs", 776, 282, "start"), "winterpark": ("abs", 776, 300, "start"),
    "abasin": ("abs", 776, 318, "start"), "copper": ("abs", 776, 336, "start"),
    "sugarbowl": ("abs", 128, 318, "end"), "palisades": ("abs", 128, 338, "end"),
    "sierra": ("abs", 128, 366, "end"), "mammoth": ("rel", 12, 6, "start"),
}
STATE_LABELS = {"UT": (39.0, -111.2), "ID": (44.6, -115.2), "WY": (42.6, -107.8), "CO": (38.3, -104.2),
                "CA": (36.2, -119.6), "NV": (39.6, -116.8), "OR": (44.6, -120.6), "MT": (45.5, -111.5),
                "AZ": (35.5, -112.0), "NM": (35.5, -105.2)}
POS = {p[0]: (p[3], p[4]) for p in RESORTS}


def render_map(resorts):
    out = [f'<svg viewBox="0 0 {STATE_MAP["W"]} {STATE_MAP["H"]}" class="map" role="img" aria-label="Map of resorts with forecast snow">']
    for st, d in STATE_MAP["paths"].items():
        out.append(f'<path d="{d}" class="st"/>')
    for st, (la, lo) in STATE_LABELS.items():
        x, y = xy(la, lo)
        out.append(f'<text x="{x:.0f}" y="{y:.0f}" class="stl">{st}</text>')
    for r in resorts:
        x, y = xy(*POS[r["id"]])
        best = max(r["days"][:5], key=lambda d: d["score"])
        rad = 4 + min(9, math.sqrt(max(r["total5"], 0)) * 1.3)
        kind, a, b, anchor = LABELS.get(r["id"], ("rel", 10, 4, "start"))
        lx, ly = (x + a, y + b) if kind == "rel" else (a, b)
        tip = f'{r["name"]}: ~{r["total5"]:.0f}" next 5 days; best morning {best["label"]} ~{best["ovn"]:.0f}"'
        out.append(f'<a href="#{r["id"]}"><title>{esc(tip)}</title>'
                   f'<line x1="{x:.0f}" y1="{y:.0f}" x2="{lx:.0f}" y2="{ly - 4:.0f}" class="ld"/>'
                   f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{rad:.1f}" class="dot {color(best["score"])}"/>'
                   f'<text x="{lx:.0f}" y="{ly:.0f}" text-anchor="{anchor}" class="rl">{esc(r["name"])} '
                   f'<tspan class="rv">{r["total5"]:.0f}"</tspan></text></a>')
    out.append("</svg>")
    return "".join(out)


def render_card(r, scale):
    bars = []
    for d in r["days"]:
        h = max(2, round(d["ovn"] / scale * 100))
        far = d["nws_ovn"] is None
        nws_txt = f', NWS {d["nws_ovn"]}"' if not far else ", model only"
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


def render(data):
    secs = []
    for reg in ("UT", "WY", "CO", "CA"):
        rs = [r for r in data["resorts"] if r["region"] == reg]
        if not rs:
            continue
        if reg != "UT":
            rs.sort(key=lambda r: -r["total5"])
        scale = max([12] + [d["ovn"] for r in rs for d in r["days"]])
        secs.append(f'<h2>{REGION_NAMES[reg]}</h2><div class="grid">{"".join(render_card(r, scale) for r in rs)}</div>')
    alerts = "".join(f'<div class="alert {a["level"]}">{esc(a["text"])}</div>' for a in data["alerts"])
    provo = esc(data["provo"]["text"]) if data.get("provo") else ""
    stamp = datetime.fromisoformat(data["generated"]).astimezone(ZoneInfo("America/Denver")).strftime("%a %b %-d, %-I:%M %p MT")
    models = ", ".join(data["sources_ok"])
    real = [w for w in data["warnings"] if "Open-Meteo" not in w]
    warn = f'<p class="warn">Missing tonight: {esc("; ".join(real))}</p>' if real else ""
    return (TEMPLATE.replace("{{stamp}}", stamp).replace("{{call}}", esc(data["call"]))
            .replace("{{alerts}}", alerts).replace("{{provo}}", provo).replace("{{map}}", render_map(data["resorts"]))
            .replace("{{sections}}", "".join(secs)).replace("{{models}}", esc(models)).replace("{{warn}}", warn))


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
    data = {"generated": now.isoformat(), "call": call, "alerts": alerts, "weekly_outlook": sunday,
            "provo": provo_outlook(members, now), "resorts": resorts, "afd_slc": afd,
            "sources_ok": srcs, "nws_ok": sum(1 for r in resorts if "NWS" in r["sources"]),
            "snotel_ok": len(snotel), "warnings": LOG}
    json.dump(data, open(os.path.join(out, "data.json"), "w"), indent=1, default=str)
    open(os.path.join(out, "index.html"), "w").write(render(data))
    print(json.dumps({"call": call, "alerts": alerts, "weekly_outlook": sunday, "sources_ok": srcs,
                      "nws_ok": data["nws_ok"], "snotel_ok": data["snotel_ok"], "warnings": LOG}, indent=1))


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Snow Tracker</title>
<style>
:root{--bg:#f6f8fb;--card:#fff;--ink:#14202e;--mut:#5d6b7a;--line:#e3e8ef;--land:#eef2f7;--border:#c9d2dd;--s0:#c9d1db;--s1:#9cc3ea;--s2:#3f88d6;--s3:#1d3fa8;--bt:#fff;--warn:#b25b00}
@media (prefers-color-scheme:dark){:root{--bg:#0e141b;--card:#161f2a;--ink:#e8eef5;--mut:#93a1b1;--line:#253242;--land:#18222e;--border:#33475c;--s0:#3a4757;--s1:#3d6a96;--s2:#4f9be6;--s3:#9ec3ff;--bt:#0e141b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:16px 14px 40px}
header{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px}
header h1{margin:0;font-size:21px}
.stamp{color:var(--mut);font-size:12.5px}
.call{margin:10px 0 6px;font-size:15px}
.alerts{display:flex;flex-direction:column;gap:4px}
.alert{padding:6px 10px;border-radius:7px;font-size:13px;background:var(--card);border:1px solid var(--line)}
.alert.powder{border-left:4px solid var(--s3);font-weight:600}
.alert.storm{border-left:4px solid var(--s2)}
.alert.away,.alert.early{border-left:4px solid var(--s1)}
.provo{color:var(--mut);font-size:13px;margin:6px 2px 0}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:24px 2px 10px}
.mapbox{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px}
.map{width:100%;height:auto;display:block}
.map .st{fill:var(--land);stroke:var(--border);stroke-width:1}
.map .stl{fill:var(--mut);font-size:15px;font-weight:600;opacity:.55;text-anchor:middle;letter-spacing:.08em}
.map .ld{stroke:var(--mut);stroke-width:.8;opacity:.6}
.map .dot{stroke:var(--card);stroke-width:1.5}
.map .dot.s0{fill:var(--s0)}.map .dot.s1{fill:var(--s1)}.map .dot.s2{fill:var(--s2)}.map .dot.s3{fill:var(--s3)}
.map .rl{fill:var(--ink);font-size:12.5px;font-weight:600;paint-order:stroke;stroke:var(--card);stroke-width:3px}
.map .rv{fill:var(--s2);font-weight:700}
.map a:hover .rl{text-decoration:underline}
.maprow{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-top:8px}
.legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--mut);font-size:12px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;vertical-align:-1px}
.links{display:flex;flex-wrap:wrap;gap:8px}
.links a{padding:7px 11px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);text-decoration:none;font-size:13.5px}
.links a.primary{background:var(--s2);border-color:var(--s2);color:#fff;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px}
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
.track{height:70px;display:flex;align-items:flex-end;justify-content:center;background:linear-gradient(var(--line),var(--line)) bottom/100% 1px no-repeat}
.fill{width:72%;border-radius:4px 4px 0 0}
.fill.s0{background:var(--s0)}.fill.s1{background:var(--s1)}.fill.s2{background:var(--s2)}.fill.s3{background:var(--s3)}
.bar.far .fill{opacity:.5;background-image:repeating-linear-gradient(45deg,transparent 0 3px,rgba(255,255,255,.35) 3px 6px)}
.bar.far .v,.bar.far .dl{opacity:.65}
.rng{color:var(--mut);font-size:10px}
.dl{color:var(--mut);margin-top:1px;line-height:1.2}
.read{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;font-size:14.5px}
.foot{color:var(--mut);font-size:12.5px;margin-top:22px}
.warn{color:var(--warn);font-size:12px}
</style></head><body><div class="wrap">
<header><h1>Snow Tracker</h1><div class="stamp">Updated {{stamp}} · forecasts only, no resort reports</div></header>
<div class="call">{{call}}</div>
<div class="alerts">{{alerts}}</div>
<div class="provo">{{provo}}</div>

<h2>Where the snow is headed (next 5 days)</h2>
<div class="mapbox">{{map}}
<div class="maprow"><div class="legend"><span>Dot size = snow in the next 5 days · color = best powder morning:</span><span><i style="background:var(--s3)"></i>go</span><span><i style="background:var(--s2)"></i>good</span><span><i style="background:var(--s1)"></i>refresh</span><span><i style="background:var(--s0)"></i>little or none</span></div>
<div class="links"><a class="primary" href="https://www.windy.com/-Weather-radar-radar?radar,41.0,-112.0,5">Live radar (whole West)</a><a href="https://radar.weather.gov/station/kmtx/standard">Salt Lake radar</a><a href="https://www.windy.com/-Snow-accumulation-snowAccu?snowAccu,40.6,-111.6,6">Snow forecast map</a></div></div>
</div>

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
<p><b>How to read it.</b> Each bar is snow expected between 4 pm and 9 am before that morning: the official NWS snowfall forecast averaged with the median of the independent weather models. Small numbers under a bar are the low-high range across sources. Faded, striped bars are past the NWS forecast range (about 7 days) and come from the weather models alone, with no NWS forecaster input, so treat them as an early look. Sources this run: {{models}}. The score (0 to 10) starts from overnight snow and loses points for low agreement, gusts over 35 mph, above-freezing temps, and Utah weekends. Snow on the ground comes from the nearest SNOTEL station (measured, not resort-reported). Tap a map dot to jump to that resort, and tap a bar for its details. The live radar can't run inside this page, so the radar buttons open it in a new tab.</p>
{{warn}}
</div>
</div></body></html>
"""

STATE_MAP = {"W":900,"H":546,"box":[-124.8,-101.6,35.2,45.9],"paths":{"AZ":"M391,673L392,671L395,672L399,671L398,669L401,666L401,663L399,657L397,656L394,656L391,654L392,651L393,644L391,643L392,640L391,638L393,637L397,631L399,630L398,628L399,623L399,619L398,617L399,614L399,612L402,606L402,603L403,602L406,600L408,599L410,598L414,594L414,592L412,589L410,589L406,584L404,584L404,581L401,571L397,568L396,565L394,563L394,556L395,552L394,551L397,549L397,546L396,538L395,535L393,533L393,531L393,529L393,525L392,523L393,521L392,520L392,518L391,515L392,513L393,512L392,510L390,506L391,504L390,501L393,499L394,498L397,497L399,497L403,498L405,498L407,501L410,504L413,504L416,496L417,485L417,473L417,454L445,454L476,454L519,454L533,454L555,454L589,454L611,454L611,528L611,539L611,581L611,607L611,614L611,619L611,743L602,743L574,743L556,743L532,743L482,722L453,711L439,705L387,684L388,681L388,678L391,673Z","CO":"M611,250L645,250L666,250L712,250L721,250L740,250L774,250L805,250L844,250L863,250L882,250L882,301L883,318L883,337L883,345L883,401L883,407L883,455L857,454L854,454L846,454L817,454L794,454L768,454L763,454L721,454L696,455L674,454L642,454L611,454L611,429L611,427L611,395L611,389L611,366L611,357L611,276L611,274L611,250Z","KS":"M882,301L918,301L933,301L969,301L1007,301L1030,301L1069,301L1105,301L1126,301L1144,301L1146,304L1148,304L1148,306L1150,306L1151,307L1154,308L1155,306L1159,307L1159,309L1161,310L1160,312L1159,313L1161,313L1161,315L1158,314L1157,317L1155,318L1154,320L1154,322L1152,323L1152,325L1154,327L1156,329L1158,332L1160,332L1160,334L1160,337L1163,340L1165,342L1165,343L1167,343L1169,344L1172,344L1171,346L1171,365L1171,371L1171,401L1171,403L1171,438L1171,442L1171,454L1136,454L1109,454L1085,454L1055,454L1026,454L990,454L976,454L954,454L934,454L904,454L883,455L883,407L883,401L883,345L883,337L883,318L882,301Z","SD":"M805,46L805,8L805,-2L820,-2L844,-2L885,-2L902,-2L937,-2L962,-2L989,-2L1024,-2L1040,-2L1066,-2L1095,-2L1095,4L1093,6L1091,9L1088,10L1085,13L1084,15L1088,19L1089,23L1091,25L1093,25L1098,28L1100,31L1100,52L1100,65L1100,69L1100,122L1094,122L1094,125L1097,128L1097,133L1095,133L1096,135L1099,137L1099,140L1100,142L1099,145L1097,145L1098,148L1097,149L1096,152L1096,156L1095,156L1094,159L1093,160L1093,163L1095,164L1097,167L1099,171L1098,173L1100,174L1098,174L1094,173L1093,171L1090,168L1091,166L1089,165L1086,165L1086,163L1082,162L1080,162L1079,160L1073,160L1070,158L1070,156L1067,155L1064,155L1062,155L1059,155L1057,156L1054,155L1052,156L1048,156L1046,155L1043,158L1042,160L1039,160L1035,157L1030,154L1022,151L1020,148L1005,148L979,148L957,148L928,148L899,148L890,148L866,148L846,148L805,148L805,133L805,52L805,46Z","WY":"M805,46L805,52L805,132L805,148L805,250L775,250L740,250L721,250L712,250L666,250L645,250L611,250L592,250L572,250L565,250L546,250L533,250L533,231L534,221L533,199L533,169L534,162L534,114L533,91L533,79L533,73L533,65L533,53L533,46L544,46L547,46L559,46L560,46L566,46L570,46L582,46L609,46L610,46L641,46L685,46L687,46L707,46L732,46L735,46L754,46L789,46L805,46Z","NM":"M611,454L642,454L674,454L695,454L721,454L763,454L768,454L794,454L817,454L846,454L846,474L846,480L844,480L844,489L844,524L844,543L844,610L844,620L843,638L843,645L843,656L843,709L818,709L786,709L751,709L705,709L705,714L705,716L706,717L708,719L679,720L644,720L644,743L611,743L611,619L611,614L611,607L611,581L611,540L611,528L611,454Z","CA":"M240,634L243,634L247,636L249,637L252,641L251,643L249,642L246,642L245,640L245,637L242,636L240,634ZM240,657L245,662L250,667L247,668L245,666L243,663L241,658ZM208,606L211,606L209,607L207,606ZM203,644L204,644L207,645L208,647L207,647L204,646L203,644ZM189,603L192,604L194,605L196,604L198,606L201,606L202,605L205,606L203,607L199,608L197,609L191,608L191,606L189,603ZM177,607L180,607L181,606L183,606L185,605L185,608L187,608L187,610L181,613L179,611L177,607ZM169,605L171,605L173,605L175,606L172,606L169,605ZM23,199L44,199L56,199L64,199L68,199L84,199L89,199L102,199L109,199L130,199L146,199L173,199L186,199L186,263L186,297L186,315L186,335L186,341L186,352L190,355L206,370L222,385L231,393L244,406L262,422L270,430L288,448L332,491L364,524L374,535L394,556L394,563L396,565L397,568L401,571L404,581L404,584L406,584L410,589L412,589L414,592L414,594L410,597L408,599L406,600L403,602L402,603L402,606L398,611L399,614L398,617L399,619L400,623L398,628L399,630L397,631L393,637L391,638L392,640L391,643L393,644L392,651L391,654L393,657L396,657L399,657L401,663L401,666L398,669L399,671L395,672L392,671L391,673L333,678L298,682L297,678L296,675L293,675L293,672L293,669L292,667L293,665L292,658L290,652L289,650L283,641L280,639L278,636L276,635L272,631L269,628L267,627L264,625L260,621L257,619L257,621L254,622L251,622L250,621L248,620L247,619L248,617L249,615L247,610L245,607L243,605L238,605L235,606L233,607L231,605L227,605L222,602L220,602L217,600L214,594L210,591L207,588L204,587L199,586L197,587L194,586L191,586L186,584L183,584L181,583L175,583L168,584L166,580L164,579L162,579L161,578L163,571L161,569L163,563L160,561L161,556L162,550L159,547L157,547L153,546L151,544L153,538L153,536L151,533L147,533L143,528L141,524L138,523L135,520L135,516L132,514L131,513L129,511L129,507L128,505L125,504L123,499L120,495L115,492L113,490L112,487L112,485L111,481L111,477L110,476L111,473L114,474L115,473L116,470L117,464L114,458L112,456L110,456L104,456L101,453L98,449L96,448L93,444L92,441L93,436L91,432L91,430L89,429L89,428L88,425L89,420L89,414L91,413L93,413L95,418L95,423L98,425L102,428L105,431L107,431L106,429L104,428L103,424L102,420L99,417L99,415L96,414L97,411L96,408L94,408L92,405L94,404L94,402L97,403L98,401L97,398L93,396L92,397L90,397L90,399L89,401L91,404L90,405L89,407L92,409L89,409L90,412L88,412L87,411L83,408L81,409L78,406L77,403L74,402L72,401L69,403L71,397L72,395L70,391L69,387L67,388L67,385L65,380L62,378L60,377L57,374L52,367L50,365L49,363L44,360L41,354L43,351L43,349L41,344L39,338L38,334L38,329L40,324L39,321L39,318L37,314L37,310L35,308L34,306L29,300L28,298L27,296L24,294L17,288L17,285L17,282L15,279L16,274L18,270L24,258L26,254L27,249L25,247L25,243L27,238L29,228L28,222L26,216L25,212L21,210L22,207L23,202L23,199Z","MT":"M340,-158L372,-158L404,-158L431,-158L457,-158L506,-158L535,-158L567,-158L613,-158L633,-158L673,-158L705,-158L725,-158L766,-158L782,-158L805,-158L805,-127L805,-2L805,8L805,46L789,46L754,46L735,46L732,46L707,46L687,46L685,46L641,46L610,46L609,46L582,46L570,46L566,46L560,46L559,46L547,46L544,46L533,46L533,53L533,65L533,73L531,72L530,70L526,67L527,65L523,60L520,58L519,61L517,61L517,62L515,67L517,69L513,68L508,68L503,71L502,68L499,69L497,70L495,69L492,70L489,70L488,68L485,68L484,69L483,72L482,74L478,72L476,72L471,72L469,71L466,72L464,75L465,78L464,79L462,77L460,76L458,74L457,72L457,70L455,67L456,64L455,62L453,57L448,55L445,57L444,55L440,53L439,49L440,48L441,46L440,43L438,41L435,39L434,36L431,33L431,31L429,29L429,26L427,23L428,21L428,19L425,19L426,15L424,14L423,14L422,11L419,10L418,12L416,14L414,17L411,19L409,18L409,21L406,22L405,21L403,20L401,17L398,17L398,15L397,13L400,12L400,10L397,6L400,3L403,2L404,1L402,-2L403,-4L400,-5L400,-8L401,-10L399,-12L399,-14L402,-14L401,-17L401,-19L402,-20L403,-25L404,-28L403,-31L405,-31L407,-36L406,-39L401,-38L398,-38L395,-38L394,-39L395,-41L394,-42L392,-43L389,-41L389,-44L385,-46L382,-49L383,-52L380,-55L378,-55L377,-57L375,-61L372,-63L371,-64L370,-65L368,-66L369,-67L368,-69L364,-70L360,-72L359,-74L352,-78L351,-78L352,-79L355,-80L354,-81L353,-83L351,-84L352,-85L353,-87L352,-89L352,-92L350,-93L349,-95L347,-98L345,-99L340,-106L339,-118L340,-123L340,-158Z","OR":"M49,-18L51,-19L53,-17L53,-14L55,-13L59,-13L63,-15L65,-15L74,-9L75,-7L77,-3L77,-1L78,2L78,5L79,8L79,10L79,12L84,15L90,16L92,17L94,17L96,18L98,18L101,16L105,16L112,13L114,11L116,10L119,11L123,10L127,9L129,10L132,11L134,10L139,12L140,15L142,15L145,13L150,12L151,13L153,12L159,9L162,8L165,8L167,11L170,10L175,9L178,9L180,7L183,6L187,4L194,3L199,2L201,-0L203,-1L206,-0L215,-2L217,-1L220,-2L224,-4L225,-5L248,-5L283,-5L306,-5L308,-0L310,1L312,4L314,4L316,6L318,6L321,8L321,11L322,13L323,15L320,20L320,22L319,23L315,30L315,31L314,34L313,39L311,41L310,43L308,45L308,47L309,50L308,53L305,56L301,59L299,63L298,67L296,70L294,72L294,75L293,77L295,80L294,82L296,84L299,83L301,85L304,85L304,87L306,88L305,92L303,93L304,94L305,96L305,98L303,101L303,104L302,104L302,107L302,199L278,199L240,199L237,199L217,199L186,199L173,199L146,199L130,199L109,199L102,199L89,199L84,199L68,199L64,199L56,199L46,199L23,199L20,197L18,194L17,191L15,186L14,182L14,177L16,170L15,165L13,162L11,162L10,156L12,152L13,148L14,144L14,142L15,139L16,134L15,132L18,130L19,128L21,122L23,114L25,102L26,92L27,83L28,74L28,71L29,63L29,59L28,58L29,54L31,44L32,39L33,32L32,29L33,24L32,21L33,12L33,9L32,7L33,3L32,-0L31,-2L33,-4L34,-9L31,-17L35,-14L37,-13L36,-15L39,-15L42,-15L44,-16L46,-16L49,-18Z","UT":"M417,199L426,200L462,199L489,199L517,199L533,199L534,221L533,231L533,250L546,250L565,250L572,250L592,250L611,250L611,274L611,276L611,357L611,366L611,389L611,395L611,427L611,429L611,454L589,454L556,454L533,454L519,454L476,454L445,454L417,454L417,416L417,403L417,396L417,382L417,369L417,296L417,289L417,262L417,250L417,199Z","WA":"M88,-74L89,-79L90,-82L92,-80L92,-77L94,-76L92,-75L88,-73ZM83,-135L83,-137L86,-135L84,-134ZM81,-145L85,-140L82,-142L81,-145ZM80,-137L81,-138L83,-136L81,-135L80,-137ZM79,-119L81,-123L82,-125L83,-128L85,-128L86,-125L89,-124L87,-122L85,-122L83,-119L81,-119L84,-119L86,-117L87,-113L88,-108L89,-109L88,-112L92,-109L94,-109L95,-105L94,-102L92,-103L90,-107L87,-105L85,-109L85,-113L85,-115L82,-115L79,-119ZM71,-148L72,-147L77,-145L73,-146L71,-148ZM62,-138L63,-139L66,-139L68,-136L70,-136L68,-139L73,-144L75,-143L80,-141L77,-139L79,-136L78,-133L77,-132L77,-129L75,-129L73,-131L68,-131L64,-133L63,-137L62,-138ZM61,-142L63,-142L66,-140L61,-141ZM301,-158L301,-145L301,-126L301,-114L301,-83L301,-81L301,-26L300,-24L303,-20L304,-18L304,-15L306,-14L303,-9L306,-5L283,-5L248,-5L225,-5L224,-4L220,-2L217,-1L215,-2L206,-0L203,-1L201,-0L199,2L194,3L187,4L183,6L180,7L178,9L175,9L170,10L167,11L165,8L162,8L159,9L153,12L151,13L150,12L145,13L142,15L140,15L139,12L134,10L132,11L129,10L127,9L123,10L119,11L116,10L114,11L112,13L105,16L101,16L98,18L96,18L94,17L92,17L90,16L84,15L79,12L79,10L79,8L78,5L78,2L77,-1L77,-3L75,-7L74,-9L65,-15L63,-15L59,-13L55,-13L53,-14L53,-17L51,-19L49,-18L44,-19L43,-20L42,-20L40,-19L38,-20L36,-17L32,-21L29,-19L29,-25L29,-30L28,-37L30,-35L30,-30L30,-24L33,-24L33,-26L32,-28L32,-30L33,-29L35,-32L33,-38L34,-39L38,-42L35,-43L32,-42L30,-41L27,-43L27,-46L26,-51L28,-49L29,-51L36,-53L35,-55L30,-56L30,-58L26,-58L25,-57L26,-53L24,-52L24,-54L24,-63L22,-71L19,-74L17,-83L17,-87L15,-94L13,-95L12,-98L10,-99L9,-100L7,-101L5,-106L4,-111L4,-113L3,-115L4,-118L5,-124L3,-126L6,-127L11,-125L16,-122L21,-120L27,-118L32,-116L40,-115L43,-116L47,-114L48,-115L49,-114L53,-114L55,-113L60,-113L65,-116L67,-113L68,-111L71,-112L73,-112L75,-110L75,-107L77,-110L75,-111L75,-113L76,-114L79,-114L77,-112L79,-109L80,-112L82,-112L83,-108L81,-108L82,-106L83,-102L85,-101L84,-100L82,-100L82,-99L80,-97L78,-92L76,-92L78,-97L74,-93L74,-91L71,-88L66,-79L64,-74L66,-75L69,-74L70,-75L73,-76L74,-78L68,-75L65,-76L71,-86L75,-89L79,-90L80,-94L82,-97L86,-100L85,-104L88,-102L90,-94L87,-94L88,-92L89,-92L88,-89L89,-86L88,-85L87,-83L89,-82L88,-80L87,-76L86,-73L87,-71L85,-67L82,-71L83,-75L80,-73L80,-70L84,-67L82,-66L82,-64L81,-63L79,-65L76,-69L78,-71L78,-74L75,-70L76,-67L75,-65L77,-65L79,-62L82,-61L84,-63L86,-65L87,-69L88,-71L91,-70L93,-71L92,-72L96,-74L96,-76L95,-79L95,-81L93,-82L92,-86L95,-87L92,-90L94,-93L93,-97L95,-99L97,-105L100,-106L100,-109L98,-110L94,-113L95,-117L93,-119L91,-119L90,-117L91,-114L94,-112L94,-110L90,-114L89,-114L88,-117L88,-120L91,-121L93,-120L94,-122L93,-124L89,-126L87,-128L87,-130L83,-128L82,-130L83,-132L81,-131L81,-133L85,-134L87,-133L88,-131L90,-131L89,-136L91,-136L92,-138L89,-141L88,-144L90,-145L88,-147L85,-147L84,-145L85,-143L82,-145L84,-147L82,-148L81,-150L78,-153L79,-154L79,-155L77,-156L79,-158L105,-158L118,-158L138,-158L158,-158L185,-158L208,-158L231,-158L256,-158L301,-158Z","NE":"M805,148L846,148L866,148L890,148L899,148L928,148L957,148L979,148L1005,148L1020,148L1022,151L1030,154L1035,157L1039,160L1042,160L1043,158L1045,155L1048,156L1052,156L1054,155L1057,156L1059,155L1061,156L1064,155L1067,155L1070,156L1070,158L1073,158L1079,160L1080,162L1082,162L1086,163L1086,165L1089,165L1091,166L1090,168L1093,171L1094,173L1097,173L1100,174L1102,174L1102,176L1101,178L1101,182L1103,183L1105,187L1103,188L1104,190L1107,193L1107,197L1109,198L1110,199L1112,200L1112,203L1113,206L1113,208L1115,209L1114,211L1114,214L1112,215L1114,217L1113,219L1114,220L1114,223L1116,224L1116,222L1117,226L1120,226L1120,231L1119,233L1122,234L1120,236L1121,238L1120,240L1123,241L1122,242L1122,246L1123,248L1123,250L1124,251L1124,254L1125,255L1123,257L1124,261L1122,263L1122,264L1124,267L1126,268L1127,270L1126,274L1129,274L1131,273L1129,275L1129,277L1131,279L1130,280L1132,285L1135,286L1137,288L1138,292L1141,295L1140,299L1144,301L1126,301L1105,301L1069,301L1030,301L1007,301L969,301L933,301L918,301L882,301L882,250L863,250L844,250L805,250L805,148Z","ID":"M306,-5L304,-9L306,-14L304,-15L304,-18L303,-20L300,-24L301,-26L301,-81L301,-83L301,-114L301,-126L301,-145L301,-158L325,-158L340,-158L340,-123L339,-118L340,-106L345,-99L347,-98L348,-95L350,-93L352,-92L352,-89L353,-87L352,-85L351,-84L353,-83L354,-81L356,-81L352,-79L351,-78L352,-78L359,-74L360,-72L364,-70L368,-69L369,-67L368,-66L370,-65L371,-64L372,-63L375,-61L377,-57L378,-55L380,-55L383,-52L382,-49L385,-46L389,-44L388,-41L391,-42L394,-42L395,-41L394,-39L395,-38L398,-38L401,-37L405,-39L407,-36L405,-31L403,-31L404,-28L403,-25L402,-20L401,-19L401,-17L402,-14L399,-14L399,-12L401,-10L400,-8L400,-5L403,-4L402,-2L404,1L403,2L400,3L397,6L400,10L400,12L397,13L398,15L398,17L401,17L403,20L405,21L406,22L409,21L409,18L411,19L414,17L416,14L418,12L418,11L422,11L423,13L424,14L426,15L425,19L428,19L428,21L427,23L428,25L429,29L431,31L431,33L434,36L435,39L438,40L440,43L441,46L440,48L439,49L440,53L444,55L445,57L448,55L453,57L455,62L456,64L455,67L457,70L457,72L458,74L460,76L462,77L464,79L465,78L464,75L466,72L469,71L471,72L476,72L477,73L482,74L483,72L483,69L485,68L488,68L489,70L491,69L495,69L497,70L499,69L502,68L503,71L508,69L511,69L517,69L515,67L517,62L517,61L519,61L520,58L523,60L527,65L526,67L530,70L531,72L533,73L533,79L533,91L533,113L534,162L533,169L533,199L517,199L490,199L462,199L426,200L417,199L401,199L396,199L388,199L379,199L368,199L327,199L302,199L302,107L302,104L303,104L303,101L305,98L305,96L304,94L303,93L305,92L307,89L304,87L304,85L301,85L299,83L296,84L294,82L295,80L293,77L294,75L294,72L296,70L298,67L299,63L301,59L305,57L308,53L309,50L308,47L308,45L310,43L311,41L313,39L314,34L315,31L315,30L319,23L320,22L320,20L323,15L322,13L321,11L321,8L318,6L316,6L314,4L312,4L310,1L308,-0L306,-5Z","NV":"M186,199L217,199L237,199L240,199L278,199L302,199L327,199L368,199L379,199L388,199L396,199L401,199L417,199L417,250L417,262L417,289L417,296L417,369L417,382L417,396L417,403L417,415L417,454L417,473L417,485L417,495L413,504L410,504L407,502L405,498L403,498L399,499L397,497L395,499L393,499L390,501L391,504L390,506L392,510L393,512L392,513L391,515L392,518L392,520L393,521L392,523L393,525L393,529L393,531L393,533L395,535L396,538L397,546L397,549L394,551L395,552L394,556L374,535L364,524L332,491L288,448L270,430L262,422L244,406L231,393L222,385L206,370L190,355L186,352L186,341L186,335L186,315L186,297L186,264L186,199Z"}}


if __name__ == "__main__":
    main()
