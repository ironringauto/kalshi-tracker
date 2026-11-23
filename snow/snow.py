#!/usr/bin/env python3
"""Snow Tracker: weather-model snow forecasts for Ryan's resorts.

Sources (no resort-reported numbers):
  - Open-Meteo: hourly forecasts from 5 models (ECMWF, GFS/HRRR, ICON, GEM, NBM)
  - NWS (api.weather.gov): official forecaster-edited snowfall grid + SLC forecast discussion
  - SNOTEL (USDA): measured snow depth at the nearest station

Writes OUT/data.json and OUT/index.html. Standard library only.
Usage: python3 snow.py [--out DIR]
"""
import json, math, os, re, statistics, sys, time, urllib.request, urllib.parse
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
    pop = expand(g.get("probabilityOfPrecipitation", {}).get("values", []))
    wind = expand(g.get("windSpeed", {}).get("values", []), 0.6214)
    rows = [row(t, snow.get(t, 0.0), pr.get(t, 0.0), tc.get(t), gust.get(t), lvl.get(t)) for t in sorted(snow)]
    for r_ in rows:
        r_["pop"], r_["wind"] = pop.get(r_["t"]), wind.get(r_["t"])
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


# ---------- trip panel: live cams, roads, avalanche (links; the page cannot embed live video) ----------
UDOT_CC = ("UDOT canyon road status + cams", "https://cottonwoodcanyons.udot.utah.gov/road-information/")
UDOT = ("UDOT Traffic map + cams", "https://udottraffic.utah.gov")
UDOT_X = ("@UDOTcottonwoods on X", "https://x.com/UDOTcottonwoods")
WYDOT = ("WYDOT road report + cams", "https://www.wyoroad.info/")
IDAHO = ("Idaho 511 (ID-33, Driggs)", "https://511.idaho.gov/")
COTRIP = ("COtrip roads + cams", "https://www.cotrip.org/")
QUICKMAP = ("Caltrans QuickMap + cams", "https://quickmap.dot.ca.gov/")
NV511 = ("Nevada 511 (Reno side)", "https://www.nvroads.com/")
def _xs(q):
    return (f"X live posts: {q}", "https://x.com/search?q=" + urllib.parse.quote(f'"{q}"') + "&f=live")
def _ct(n):
    return (f"Caltrans chain controls, Hwy {n}", f"https://roads.dot.ca.gov/roadscell.php?roadnumber={n}")
UAC_SL = ("Utah Avalanche Center (Salt Lake)", "https://utahavalanchecenter.org/forecast/salt-lake")
UAC_OG = ("Utah Avalanche Center (Ogden)", "https://utahavalanchecenter.org/forecast/ogden")
BTAC = ("Bridger-Teton Avalanche Center", "https://bridgertetonavalanchecenter.org/")
CAIC = ("Colorado Avalanche Info Center", "https://avalanche.state.co.us/")
SAC = ("Sierra Avalanche Center", "https://www.sierraavalanchecenter.org/")
ESAC = ("Eastern Sierra Avalanche Center", "https://www.esavalanche.org/")
OTS = "https://www.onthesnow.com/"
TRIP = {
    "snowbird": ("Little Cottonwood (SR-210)", "https://www.snowbird.com/the-mountain/webcams/",
                 [UDOT_CC, UDOT_X, _xs("Little Cottonwood"), UDOT], [UAC_SL]),
    "brighton": ("Big Cottonwood (SR-190)", "https://www.brightonresort.com/conditions",
                 [UDOT_CC, UDOT_X, _xs("Big Cottonwood"), UDOT], [UAC_SL]),
    "solitude": ("Big Cottonwood (SR-190)", "https://www.solitudemountain.com/mountain-and-village/webcams",
                 [UDOT_CC, UDOT_X, _xs("Big Cottonwood"), UDOT], [UAC_SL]),
    "snowbasin": ("I-84 + Trappers Loop (SR-167)", "https://www.snowbasin.com/the-mountain/web-cams",
                  [UDOT, _xs("Trappers Loop"), _xs("I-84 Ogden")], [UAC_OG]),
    "targhee": ("Teton Pass (WY-22) or ID-33 via Driggs", OTS + "wyoming/grand-targhee-resort/webcams",
                [WYDOT, IDAHO, _xs("Teton Pass"), UDOT], [BTAC]),
    "jackson": ("Teton Pass (WY-22) or US-89", OTS + "wyoming/jackson-hole/webcams",
                [WYDOT, IDAHO, _xs("Teton Pass"), UDOT], [BTAC]),
    "winterpark": ("I-70 + Berthoud Pass (US-40)", OTS + "colorado/winter-park-resort/webcams",
                   [COTRIP, _xs("Berthoud Pass"), _xs("I-70")], [CAIC]),
    "copper": ("I-70 (Eisenhower Tunnel, Vail Pass)", OTS + "colorado/copper-mountain-resort/webcams",
               [COTRIP, _xs("Vail Pass"), _xs("I-70")], [CAIC]),
    "abasin": ("I-70 + Loveland Pass (US-6)", OTS + "colorado/arapahoe-basin-ski-area/webcams",
               [COTRIP, _xs("Loveland Pass"), _xs("I-70")], [CAIC]),
    "steamboat": ("US-40 Rabbit Ears Pass", OTS + "colorado/steamboat/webcams",
                  [COTRIP, _xs("Rabbit Ears Pass")], [CAIC]),
    "eldora": ("Boulder Canyon (CO-119)", OTS + "denver/eldora-mountain-resort/webcams",
               [COTRIP, _xs("Boulder Canyon")], [CAIC]),
    "snowmass": ("I-70 Glenwood Canyon + CO-82", "https://www.aspensnowmass.com/our-mountains/mountain-cams",
                 [COTRIP, _xs("Glenwood Canyon"), _xs("Highway 82")], [CAIC]),
    "palisades": ("I-80 Donner Summit + SR-89", "https://www.palisadestahoe.com/mountain-information/webcams",
                  [_ct(80), QUICKMAP, _xs("Donner Pass"), NV511], [SAC]),
    "sugarbowl": ("I-80 Donner Summit", OTS + "california/sugar-bowl-resort/webcams",
                  [_ct(80), QUICKMAP, _xs("Donner Pass"), NV511], [SAC]),
    "sierra": ("US-50 Echo Summit", "https://sierraattahoe.com/live-cams/",
               [_ct(50), QUICKMAP, _xs("Echo Summit")], [SAC]),
    "mammoth": ("US-395 + SR-203", OTS + "california/mammoth-mountain-ski-area/webcams",
                [_ct(395), QUICKMAP, _xs("Mammoth 395")], [ESAC]),
}


def nws_hourly(rows, tzname, now, hours=24, step=3):
    """NWS grid -> next 24 h in 3-hour columns: snow, temp, wind, gusts, chance of precip."""
    if not rows:
        return None
    tz = ZoneInfo(tzname)
    start = now.replace(minute=0, second=0, microsecond=0)
    cols = []
    for k in range(0, hours, step):
        a, b = start + timedelta(hours=k), start + timedelta(hours=k + step)
        sel = [x for x in rows if a < x["t"] <= b]
        if not sel:
            continue
        tcs = [x["tc"] for x in sel if x["tc"] is not None]
        g = [x["gust"] for x in sel if x.get("gust") is not None]
        w = [x["wind"] for x in sel if x.get("wind") is not None]
        p = [x["pop"] for x in sel if x.get("pop") is not None]
        cols.append({"t": b.astimezone(tz).strftime("%-I%p").lower().replace("m", ""), "day": b.astimezone(tz).strftime("%a"),
                     "snow": round(sum(x["snow"] for x in sel), 1),
                     "temp": round(statistics.mean(tcs) * 9 / 5 + 32) if tcs else None,
                     "wind": round(max(w)) if w else None, "gust": round(max(g)) if g else None,
                     "pop": round(max(p)) if p else None})
    return cols


def fetch_nws_alerts(resorts):
    """Active NWS watches/warnings/advisories at each resort, as of this run."""
    def one(r):
        lat, lon = POS[r["id"]]
        d = get(f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}", tries=2)
        out = []
        for f in (d or {}).get("features", []):
            p = f.get("properties", {})
            if p.get("status") != "Actual" or p.get("messageType") == "Cancel":
                continue
            out.append({"event": p.get("event"), "ends": p.get("ends") or p.get("expires"),
                        "url": f"https://forecast.weather.gov/MapClick.php?lat={lat:.4f}&lon={lon:.4f}"})
        seen, uniq = set(), []
        for a in out:
            if a["event"] not in seen:
                seen.add(a["event"]); uniq.append(a)
        return uniq
    with ThreadPoolExecutor(6) as ex:
        futs = {r["id"]: ex.submit(one, r) for r in resorts}
        for r in resorts:
            try:
                r["nws_alerts"] = futs[r["id"]].result()
            except Exception as e:
                r["nws_alerts"] = []
                log(f"NWS alerts {r['id']}: {e}")


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
    return (f'<details class="card" id="{r["id"]}"><summary><div class="ch"><div><h3>{esc(r["name"])}</h3>'
            f'<div class="sub">{esc(" · ".join(sub))}</div></div>'
            f'<div class="chr"><div class="badge {color(best["score"])}" title="Best powder score in the window">{best["score"]}</div>'
            f'<span class="chev" aria-hidden="true"></span></div></div>'
            f'<div class="next">Tomorrow <b>{nxt["ovn"]:.0f}"</b> · next 5 days ~{r["total5"]:.0f}" · {best_txt}{lvl_txt}</div>'
            f'<div class="qs" data-q="{r["id"]}"></div></summary><div class="cb">'
            f'{render_report(r)}{render_wx_alerts(r)}'
            f'<div class="bars" style="grid-template-columns:repeat({len(r["days"])},1fr)">{"".join(bars)}</div>'
            f'{render_hourly(r)}{render_live(r)}{render_trip(r)}</div></details>')


def render_report(r):
    rp = (r.get("live") or {}).get("report") or {"link": "#", "flag": None}
    today = datetime.now(ZoneInfo(REGION_TZ[r["region"]])).date()
    opens = None
    try:
        opens = datetime.strptime(rp.get("opens") or "", "%Y-%m-%d").date()
    except ValueError:
        pass
    if rp.get("flag") == 1:
        pill, cls = "Open", "op"
    elif opens and opens > today:
        d = (opens - today).days
        pill, cls = f'Opens {opens.strftime("%a %b %-d")}' + (" (tomorrow)" if d == 1 else f" (in {d} days)" if d < 8 else ""), "soon"
    else:
        pill, cls = "Closed today", "cl"
    def bar(label, o, t):
        if not t:
            return ""
        o = o or 0
        pct = max(0, min(100, round(100 * o / t)))
        return (f'<div class="mrow"><span class="ml">{label}</span><span class="mbar"><i style="width:{pct}%"></i></span>'
                f'<span class="mv"><b>{o}</b>/{t}</span></div>')
    extra = []
    if rp.get("acres_total") and rp.get("acres_open"):
        extra.append(f'{rp["acres_open"]:,} of {rp["acres_total"]:,} acres open')
    lbl, iso = _ts(rp.get("updated", ""), ZoneInfo(REGION_TZ[r["region"]]))
    return (f'<div class="mstat" data-rid="{r["id"]}" data-opens="{esc(rp.get("opens") or "")}" data-today="{today.isoformat()}" '
            f'data-extra="{esc(" · ".join(extra))}"><div class="mhead"><span class="mpill {cls}">{esc(pill)}</span>'
            f'<span class="mut">Lifts and runs (resort-reported) · <span class="ago" data-t="{iso}">{lbl}</span></span></div>'
            f'{bar("Lifts", rp.get("lifts_open"), rp.get("lifts_total"))}{bar("Runs", rp.get("runs_open"), rp.get("runs_total"))}'
            f'<div class="mut mx">{" · ".join(extra)}{" · " if extra else ""}<a href="{esc(rp["link"])}" target="_blank" rel="noopener">Full lift and trail report</a></div></div>')


def render_wx_alerts(r):
    out = []
    tz = ZoneInfo(REGION_TZ[r["region"]])
    for a in r.get("nws_alerts") or []:
        until = ""
        try:
            until = " until " + datetime.fromisoformat(a["ends"]).astimezone(tz).strftime("%a %-I %p")
        except Exception:
            pass
        out.append(f'<a class="wxa" href="{esc(a["url"])}">NWS: {esc(a["event"])}{esc(until)}</a>')
    return f'<div class="wxas">{"".join(out)}</div>' if out else ""


def _ts(iso, tz):
    try:
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(tz).strftime("%a %-I:%M %p"), d.astimezone(UTC).isoformat()
    except Exception:
        return "", ""


def render_hourly(r):
    h = r.get("hourly")
    if not h:
        return ""
    def cell(v, fmt="{}"):
        return "" if v is None else fmt.format(v)
    head = "".join(f'<th>{c["t"]}<br><span>{c["day"]}</span></th>' for c in h)
    snow = "".join(f'<td class="{"sn" if c["snow"] >= 0.5 else ""}">{c["snow"]:g}"</td>' if c["snow"] >= 0.1 else "<td>–</td>" for c in h)
    temp = "".join(f"<td>{cell(c['temp'], '{}°')}</td>" for c in h)
    wind = "".join(f'<td class="{"gw" if (c["gust"] or 0) >= 35 else ""}">{cell(c["wind"])}{("/" + str(c["gust"])) if c["gust"] else ""}</td>' for c in h)
    pop = "".join(f"<td>{cell(c['pop'], '{}%')}</td>" for c in h)
    return (f'<div class="hrw"><div class="lvt">Next 24 hours (NWS hourly forecast, every 3 hours)</div><div class="hrs"><table class="hr">'
            f'<tr><th></th>{head}</tr><tr><th>Snow</th>{snow}</tr><tr><th>Temp</th>{temp}</tr>'
            f'<tr><th>Wind/gust</th>{wind}</tr><tr><th>Precip</th>{pop}</tr></table></div></div>')


def render_live(r):
    lv = r.get("live")
    if not lv:
        return ""
    tz = ZoneInfo(REGION_TZ[r["region"]])
    t = TRIP.get(r["id"])
    road_name = t[0] if t else "Road"
    out = []
    cams = [("Mountain", c) for c in lv.get("mtn", [])] + [("Road", c) for c in lv.get("road", [])]
    if cams:
        figs = []
        for kind, c in cams:
            lbl, iso = _ts(c.get("t", ""), tz)
            vid = c.get("video")
            vlink = (f'<a class="vid" href="{esc(vid)}" target="_blank" rel="noopener">▶ Live video</a>' if vid else "")
            figs.append(f'<figure class="cam"><img src="{esc(c["src"])}" alt="{esc(c["title"])}" loading="lazy" data-cap="{esc(kind + ": " + c["title"])}">'
                        f'<figcaption><span class="ck {kind.lower()}">{kind}</span> {esc(c["title"])}<br><span class="ago" data-t="{iso}">{lbl}</span>{vlink}</figcaption></figure>')
        out.append(f'<div class="lvt">Live cams <span class="mut">(photos grabbed each update · tap to enlarge · swipe for more)</span></div><div class="cams">{"".join(figs)}</div>')
    st = lv.get("status") or []
    signs = [x for x in (lv.get("signs") or []) if x and x != "No message"]
    if st or lv.get("signs"):
        items = []
        for x in st[:3]:
            lvl = x.get("level", 0) or 0
            hot = re.search(r"\bclosed\b|closure|chains? (are )?required|R[23]:|traction law (is )?(now )?(in effect|active|enacted)|code 15|code 16", x["text"], re.I)
            cls = " bad" if (lvl >= 2 or hot) and not re.search(r"lifted|no chain|reopen", x["text"], re.I) else ""
            when = f'<span class="mut">{esc(x["when"])} · {esc(x["src"])}</span><br>' if x.get("when") else f'<span class="mut">{esc(x["src"])}</span><br>'
            items.append(f'<li class="rsi{cls}">{when}<div class="xt c4">{esc(x["text"]).replace(chr(10), "<br>")}</div></li>')
        if lv.get("signs") is not None and r["id"] in ("snowbird", "brighton", "solitude"):
            items.append(f'<li class="rsi"><span class="mut">Parking signs at the canyon mouth</span><br>{esc(" · ".join(signs)) if signs else "No message posted (lots not reported full)"}</li>')
        out.append(f'<div class="lvt">Road: {esc(road_name)}</div><ul class="rsl">{"".join(items)}</ul>')
    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())[:50]
    seen_st = {norm(x["text"]) for x in st}
    xs = [x for x in (lv.get("x") or []) if norm(x["text"]) not in seen_st]
    if xs:
        items = []
        for x in xs:
            lbl, iso = _ts(x["t"], tz)
            im = f'<img src="{esc(x["img"])}" alt="" loading="lazy" data-cap="@{esc(x["handle"])}">' if x.get("img") else ""
            items.append(f'<div class="xpost">{im}<div><a href="{esc(x["url"])}" target="_blank" rel="noopener"><b>@{esc(x["handle"])}</b></a> '
                         f'<span class="ago mut" data-t="{iso}">{lbl}</span><div class="xt">{esc(x["text"]).replace(chr(10), "<br>")}</div></div></div>')
        out.append(f'<div class="lvt">Latest posts on X</div>{"".join(items)}')
    a = lv.get("avy")
    if a:
        body = a.get("bottom_line") or a.get("advice") or ""
        out.append(f'<div class="avy" style="border-left-color:{a["color"]}"><span class="dg" style="background:{a["color"]};color:{"#fff" if (a.get("level") or 0) >= 4 else "#111"}">'
                   f'{esc(a["danger"])}</span> <b>Avalanche danger</b> <span class="mut">{esc(a.get("zone") or "")} · {esc(a.get("center") or "")}</span>'
                   f'<div class="xt">{esc(body)}</div><a href="{esc(a.get("link") or "#")}" target="_blank" rel="noopener">Full forecast</a></div>')
    return f'<div class="live">{"".join(out)}</div>' if out else ""


def render_health(h):
    if not h:
        return ""
    probs = [p for p in h.get("problems", []) if p["level"] != "gap"]
    gaps = [p for p in h.get("problems", []) if p["level"] == "gap"]
    if not probs and not gaps:
        return '<p class="hc ok">Live source check: every cam, road and post source answered this update.</p>'
    parts = [f'{esc(p["feeds"])} ({esc(p["level"])})' for p in probs] + [esc(p["detail"]) for p in gaps]
    cls = "bad" if probs else "ok"
    return f'<p class="hc {cls}">Live source check: {"; ".join(parts)}. The update task repairs broken sources automatically.</p>'


def render_trip(r):
    t = TRIP.get(r["id"])
    if not t:
        return ""
    road, cams, roads, avy = t
    lat, lon = POS[r["id"]]
    nws = f"https://forecast.weather.gov/MapClick.php?lat={lat:.4f}&lon={lon:.4f}"
    def a(lbl, url, cls=""):
        return f'<a{" class=" + chr(34) + cls + chr(34) if cls else ""} href="{esc(url)}" target="_blank" rel="noopener">{esc(lbl)}</a>'
    return (f'<details class="trip"><summary>More: all cams, live video, road maps</summary>'
            f'<div class="tg"><div class="tl">Mountain</div>{a("Live resort cams", cams, "pri")}{a("NWS hourly forecast", nws)}</div>'
            f'<div class="tg"><div class="tl">Road: {esc(road)}</div>{"".join(a(l, u) for l, u in roads)}</div>'
            f'<div class="tg"><div class="tl">Backcountry</div>{"".join(a(l, u) for l, u in avy)}</div>'
            f'</details>')


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
    opts = "".join(f'<optgroup label="{REGION_NAMES[reg]}">' + "".join(
        f'<option value="{r["id"]}">{esc(r["name"])}</option>' for r in sorted([r for r in data["resorts"] if r["region"] == reg], key=lambda r: r["name"]))
        + '</optgroup>' for reg in ("UT", "WY", "CO", "CA") if any(r["region"] == reg for r in data["resorts"]))
    chips = "".join(f'<a href="#reg-{reg}">{REGION_NAMES[reg]}</a>' for reg in ("UT", "WY", "CO", "CA") if any(r["region"] == reg for r in data["resorts"]))
    nav = (f'<nav class="rnav"><select id="rjump" aria-label="Jump to a resort"><option value="">Jump to a resort…</option>{opts}</select>'
           f'<span class="rchips">{chips}</span><span class="rbtns"><button type="button" id="xall">Open all</button>'
           f'<button type="button" id="call">Close all</button></span></nav>')
    secs.insert(0, nav)
    alerts = "".join(f'<div class="alert {a["level"]}">{esc(a["text"])}</div>' for a in data["alerts"])
    provo = esc(data["provo"]["text"]) if data.get("provo") else ""
    stamp = datetime.fromisoformat(data["generated"]).astimezone(ZoneInfo("America/Denver")).strftime("%a %b %-d, %-I:%M %p MT")
    real = [w for w in data["warnings"] if "Open-Meteo" not in w and not w.startswith("Live")]
    warn = f'<p class="warn">Missing tonight: {esc("; ".join(real))}</p>' if real else ""
    tzs = tzs or data.get("tzs") or {}
    try:
        tiles = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiles_index.json")))
    except Exception:
        tiles = None
    mdata = {"radar": data.get("radar"), "tiles": tiles,
             "resorts": [{"id": r["id"], "name": r["name"], "lat": POS[r["id"]][0], "lon": POS[r["id"]][1],
                          "short": MAPLBL.get(r["id"], (r["name"], 5, 7))[0], "zs": MAPLBL.get(r["id"], (r["name"], 5, 7))[1],
                          "zf": MAPLBL.get(r["id"], (r["name"], 5, 7))[2], "t5": r["total5"],
                          "tl": point_timeline(r, tzs.get(r["id"], REGION_TZ[r["region"]]))} for r in data["resorts"]]}
    return (TEMPLATE.replace("{{stamp}}", stamp).replace("{{call}}", esc(data["call"]))
            .replace("{{alerts}}", alerts).replace("{{provo}}", provo)
            .replace("{{mdata}}", json.dumps(mdata, separators=(",", ":")).replace("</", "<\\/"))
            .replace("{{sections}}", "".join(secs)).replace("{{models}}", esc(", ".join(data["sources_ok"])))
            .replace("{{warn}}", warn).replace("{{health}}", render_health(data.get("live_health")))
            .replace("{{leafletcss}}", LEAFLET_CSS))


def build_lifts(out):
    """Official lift/run status -> out/lifts.json, plus trail-map geometry + imagery -> out/trailmaps/.
    Also runnable alone: python3 snow.py --lifts-only --out DIR (fast refresh, ~30 s)."""
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        sys.path.insert(0, here)
        import lifts as lifts_mod
        d, health = lifts_mod.fetch_all(log=log)
        lifts_mod.build_status_file(out, d, log=log)
    except Exception as e:
        log(f"Lift status: {e}")
        health = {"all": f"{type(e).__name__}: {e}"}
    try:
        td = os.path.join(out, "trailmaps"); os.makedirs(td, exist_ok=True)
        for f in os.listdir(os.path.join(here, "trailmaps")):
            shutil.copy(os.path.join(here, "trailmaps", f), os.path.join(td, f))
    except Exception as e:
        log(f"Trail maps: {e}")
    bad = {k: v for k, v in health.items() if v != "ok"}
    return {"status": "ok" if not bad else "degraded", "failed": bad}


def main():
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "out"
    os.makedirs(out, exist_ok=True)
    if "--lifts-only" in sys.argv:
        h = build_lifts(out)
        print(json.dumps({"lifts_health": h, "warnings": LOG}, indent=1))
        return
    now = datetime.now(UTC)
    members, tzs, snotel, afd = gather()
    resorts = analyze(members, tzs, snotel, now)
    if not resorts:
        log("FATAL: no forecast data from any source")
        print(json.dumps({"fatal": True, "warnings": LOG}, indent=1))
        sys.exit(2)
    alerts, call, sunday = alerts_and_call(resorts, now.astimezone(ZoneInfo("America/Denver")))
    fetch_nws_alerts(resorts)
    for r in resorts:
        r["hourly"] = nws_hourly(members.get(r["id"], {}).get("NWS"), tzs.get(r["id"], REGION_TZ[r["region"]]), now)
    try:
        import live as live_mod
        lv = live_mod.build(out, {r["id"]: POS[r["id"]] for r in resorts}, log=log)
        for r in resorts:
            r["live"] = lv["resorts"].get(r["id"])
        live_asof = lv["as_of"]
        live_health = lv.get("health")
    except Exception as e:
        log(f"Live cams and roads: {e}")
        live_asof = None
        live_health = {"status": "broken", "problems": [{"source": "live.py", "feeds": "all cams and roads", "level": "broken", "detail": str(e)[:200]}]}
    lifts_health = build_lifts(out)
    srcs = sorted({n for r in resorts for n in r["sources"]})
    radar_meta = None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import radar
        radar_meta = radar.build(out, now, log=log)
    except Exception as e:
        log(f"Model map: {e}")
    if radar_meta:
        try:
            radar_meta["hrrr"] = radar.build_hrrr(out, now, log=log)
        except Exception as e:
            log(f"High-res radar: {e}")
        try:
            radar_meta["nbm"] = radar.build_nbm(out, now, log=log)
        except Exception as e:
            log(f"National Blend map: {e}")
    try:
        import shutil
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "base.json"), os.path.join(out, "base.json"))
    except Exception as e:
        log(f"Basemap: {e}")
    data = {"generated": now.isoformat(), "call": call, "alerts": alerts, "weekly_outlook": sunday,
            "provo": provo_outlook(members, now), "resorts": resorts, "afd_slc": afd,
            "sources_ok": srcs, "nws_ok": sum(1 for r in resorts if "NWS" in r["sources"]),
            "snotel_ok": len(snotel), "warnings": LOG, "radar": radar_meta, "tzs": tzs, "live_asof": live_asof, "live_health": live_health, "lifts_health": lifts_health}
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
:root{--bg:#f6f8fb;--card:#fff;--ink:#14202e;--mut:#5d6b7a;--line:#e3e8ef;--ocean:#163462;--land:#f3f5f7;--stb:#8c9bab;--cty:#5f6f80;--s0:#c9d1db;--s1:#9cc3ea;--s2:#3f88d6;--s3:#1d3fa8;--bt:#fff;--warn:#b25b00}
@media (prefers-color-scheme:dark){:root{--bg:#0e141b;--card:#161f2a;--ink:#e8eef5;--mut:#93a1b1;--line:#253242;--ocean:#163462;--land:#1a2531;--stb:#4d6176;--cty:#7d90a4;--s0:#3a4757;--s1:#3d6a96;--s2:#4f9be6;--s3:#9ec3ff;--bt:#0e141b}}
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
#tmain{font-size:21px;font-weight:700;letter-spacing:-.01em;line-height:1.15}
#tsub{font-size:12.5px;color:var(--mut)}
.seg{display:flex;justify-content:center;gap:0;margin:0 auto 8px;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:3px;width:max-content}
.seg button{border:none;background:none;color:var(--mut);font:600 13.5px inherit;padding:7px 14px;border-radius:8px;cursor:pointer}
.seg button.on{background:var(--s2);color:#fff}
#map{height:58vh;min-height:340px;max-height:600px;border-radius:10px;background:var(--ocean)}
.leaflet-container{background:var(--ocean);font:inherit}
.cty{fill:none;stroke:rgba(255,255,255,.8);stroke-width:1.4}
.stb{stroke:rgba(255,255,255,.55);stroke-width:1;fill:none}
.city{color:#fff;font-size:11.5px;font-weight:600;text-shadow:0 0 3px #000,0 0 6px #000;white-space:nowrap;pointer-events:none}
.city.t5{font-size:13px;font-weight:700}.city.t6{font-size:12px}.city.t8{font-size:11px;font-weight:500}
.city:before{content:"";display:inline-block;width:5px;height:5px;border-radius:50%;background:#fff;margin-right:4px;vertical-align:1px;box-shadow:0 0 2px #000}
.attr{font-size:10.5px;color:var(--mut);text-align:right;margin-top:4px}
.wxcv{opacity:.8}
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
#slider{accent-color:var(--s2);height:30px}
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
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px;scroll-margin-top:64px}
.grid{align-items:start}.grid>.card{min-width:0;max-width:100%}details.card>.cb{min-width:0;overflow:hidden}
details.card>summary{list-style:none;cursor:pointer;-webkit-tap-highlight-color:transparent}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary .next{margin-bottom:0}
.chr{display:flex;align-items:center;gap:8px}
.chev{width:26px;height:26px;border-radius:50%;background:var(--line);display:inline-flex;align-items:center;justify-content:center;flex:none;transition:transform .15s}
.chev::before{content:"";width:7px;height:7px;border-right:2px solid var(--ink);border-bottom:2px solid var(--ink);transform:translateY(-2px) rotate(45deg)}
details.card[open] .chev{transform:rotate(180deg)}
details.card:not([open]):hover{border-color:var(--s2)}
.qs{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:7px;font-size:12px;color:var(--mut)}
.qs:empty{display:none}
.qs .mpill{margin:0}
.qs b{color:var(--ink)}
.qs .qx{color:#1d9bf0;font-weight:600}
details.card[open]>summary{border-bottom:1px solid var(--line);padding-bottom:9px;margin-bottom:9px}
details.card[open] .qs{display:none}
.rnav{position:sticky;top:0;z-index:1100;background:var(--bg,#f6f8fa);display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 0;margin-top:14px;border-bottom:1px solid var(--line)}
.rnav select{font:inherit;font-size:14px;font-weight:600;padding:7px 10px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);flex:1 1 200px;max-width:340px}
.rchips{display:flex;gap:6px;flex-wrap:wrap}
.rchips a{font-size:12.5px;font-weight:600;color:var(--s2);text-decoration:none;padding:5px 9px;border:1px solid var(--line);border-radius:14px;background:var(--card)}
.rbtns{display:flex;gap:6px;margin-left:auto}
.rbtns button{white-space:nowrap;font:inherit;font-size:12.5px;font-weight:600;padding:5px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
details.reg{scroll-margin-top:60px}
@media (max-width:560px){.rchips{display:none}.rnav{flex-wrap:nowrap}.rnav select{flex:1 1 auto;min-width:0;font-size:13.5px}.rbtns button{padding:6px 8px;font-size:12px}}
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
.live{margin-top:10px;border-top:1px solid var(--line);padding-top:4px}
.lvt{font-size:11.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;margin:10px 0 5px;font-weight:700}
.lvt .mut{text-transform:none;letter-spacing:0;font-weight:400}
.mut{color:var(--mut)}
.cams{display:flex;gap:8px;overflow-x:auto;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;padding-bottom:4px}
.cam{flex:0 0 78%;max-width:300px;margin:0;scroll-snap-align:start}
.cam img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:8px;display:block;background:var(--line);cursor:zoom-in}
.cam figcaption{font-size:11.5px;line-height:1.3;margin-top:3px}
.ck{display:inline-block;font-size:10px;font-weight:700;padding:0 5px;border-radius:4px;background:var(--s2);color:#fff}
.ck.road{background:#6b7785}
.vid{margin-left:6px;font-weight:700;color:var(--s2);text-decoration:none}
.rsl{list-style:none;margin:0;padding:0}
.rsi{font-size:12.5px;line-height:1.35;padding:6px 8px;border-radius:7px;background:var(--bg);border:1px solid var(--line);margin-bottom:5px}
.rsi.bad{border-left:4px solid #e0602a}
.xpost{display:flex;gap:8px;font-size:12.5px;line-height:1.35;padding:6px 0;border-bottom:1px solid var(--line)}
.xpost:last-child{border-bottom:none}
.xpost img{flex:0 0 74px;width:74px;height:74px;object-fit:cover;border-radius:6px;cursor:zoom-in}
.xpost a{color:var(--ink);text-decoration:none}
.xt.c4{-webkit-line-clamp:4}
.xt{display:-webkit-box;-webkit-line-clamp:6;-webkit-box-orient:vertical;overflow:hidden;margin-top:2px}
.avy{margin-top:10px;font-size:12.5px;line-height:1.35;padding:7px 9px;border-left:5px solid #999;border-radius:7px;background:var(--bg)}
.avy a{color:var(--s2);font-weight:600;text-decoration:none}
.dg{font-weight:700;padding:1px 6px;border-radius:5px;font-size:11.5px}
.hrw{margin-top:8px}
.hrs{overflow-x:auto;-webkit-overflow-scrolling:touch}
table.hr{border-collapse:collapse;font-size:11.5px;width:100%;min-width:420px}
table.hr th,table.hr td{text-align:center;padding:3px 2px;border-bottom:1px solid var(--line);white-space:nowrap}
table.hr th:first-child{text-align:left;color:var(--mut);font-weight:600;padding-right:6px}
table.hr th span{color:var(--mut);font-weight:400}
table.hr td.sn{font-weight:700;color:var(--s2)}
table.hr td.gw{color:#e0602a;font-weight:700}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.88);display:none;align-items:center;justify-content:center;flex-direction:column;z-index:9999;padding:12px}
#lb.on{display:flex}
#lb img{max-width:100%;max-height:82vh;border-radius:8px}
#lb div{color:#fff;font-size:14px;margin-top:8px;text-align:center}
.hc{font-size:12px;color:var(--mut)}.hc.bad{color:var(--warn)}
.mstat{margin:4px 0 8px;padding:8px 10px;border:1px solid var(--line);border-radius:9px;background:var(--bg)}
.mhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:11.5px;margin-bottom:5px}
.mpill{font-size:12px;font-weight:700;padding:2px 8px;border-radius:20px}
.mpill.op{background:#1f9d55;color:#fff}.mpill.soon{background:#e8b730;color:#1b1b1b}.mpill.cl{background:#6b7785;color:#fff}
.mrow{display:flex;align-items:center;gap:8px;font-size:12.5px;margin:3px 0}
.ml{width:38px;color:var(--mut)}.mv{min-width:56px;text-align:right}
.mbar{flex:1;height:7px;border-radius:4px;background:var(--line);overflow:hidden}.mbar i{display:block;height:100%;background:#1f9d55;border-radius:4px}
.mx{font-size:11.5px;margin-top:4px}.mx a{color:var(--s2);text-decoration:none;font-weight:600}
.mhold{color:#b7791f;font-weight:600;font-size:11.5px}
.mbtns{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px}
.mbtn{font:inherit;font-size:12px;font-weight:600;padding:4px 10px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
.mbtn.on{background:var(--s2);color:#fff;border-color:var(--s2)}
.mbtns a{font-size:12px;color:var(--s2);text-decoration:none;font-weight:600}
.llist{margin-top:7px;font-size:12px}.llist h5{margin:6px 0 4px;font-size:11.5px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.03em}
.lch{display:inline-flex;align-items:center;gap:4px;margin:0 4px 4px 0;padding:2px 7px;border-radius:12px;background:var(--line);font-size:11.5px}
.lch i{width:7px;height:7px;border-radius:50%;background:#8a96a3;display:inline-block}.lch.open i{background:#22c55e}.lch.hold i{background:#f5b301}
.lch.open{background:#e3f6ea;color:#14532d}.lch.hold{background:#fff4d6;color:#7a5200}.lch.sched i{background:#60a5fa}.lch.sched{background:#e6f0fd;color:#1e3a8a}
.oruns{line-height:1.55;color:var(--ink)}
.rx{margin-top:7px;border-left:3px solid #1d9bf0;padding:2px 0 2px 9px}
.rx .rxh{font-size:11.5px;color:var(--mut)}.rx .rxh a{color:#1d9bf0;font-weight:700;text-decoration:none}
.rx .rxt{font-size:12.5px;line-height:1.4;white-space:pre-line;margin:1px 0 5px}
.tmwrap{margin-top:7px}
.tmbar{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px;align-items:center}
.tmseg{display:inline-flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
.tmseg button,.tmfs{font:inherit;font-size:12px;font-weight:600;padding:4px 9px;border:0;background:var(--card);color:var(--ink);cursor:pointer}
.tmseg button+button{border-left:1px solid var(--line)}.tmseg button.on{background:var(--s2);color:#fff}
.tmfs{border:1px solid var(--line);border-radius:7px;margin-left:auto}
.tmap{height:440px;border-radius:9px;overflow:hidden;background:#f1f4f8;border:1px solid var(--line)}
.tmleg{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:11.5px;color:var(--mut);margin-top:6px;align-items:center}
.tmleg b{display:inline-block;width:18px;height:4px;border-radius:2px;vertical-align:middle;margin-right:4px}
.tmleg .fade{opacity:.4}
.tml{white-space:nowrap;font:600 11px/1.1 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1d2733;text-shadow:0 0 2px #fff,0 0 2px #fff,0 0 3px #fff,0 0 4px #fff;pointer-events:none;transform:translate(-50%,-50%);display:inline-block}
.tml.lift{font-weight:700;font-size:11px;color:#6b7785}.tml.lift.op{font-weight:800;font-size:11.5px;color:#111}
.tml.c{color:#6b7785;font-weight:500}.tml i{font-style:normal;margin-right:2px}
.tmwrap.full{position:fixed;inset:0;z-index:9999;background:var(--card);padding:10px;margin:0;display:flex;flex-direction:column}
.tmwrap.full .tmap{flex:1;height:auto!important}
.leaflet-tooltip.tmt{font-size:12px}
.wxas{display:flex;flex-wrap:wrap;gap:5px;margin:0 0 8px}
.wxa{font-size:12px;font-weight:600;padding:3px 8px;border-radius:6px;background:#fff1e0;color:#8a4b00;text-decoration:none;border:1px solid #f1c48a}
details.trip{margin-top:10px;border-top:1px solid var(--line);padding-top:8px}
details.trip>summary{cursor:pointer;font-size:13px;font-weight:600;color:var(--s2);list-style:none}
details.trip>summary::-webkit-details-marker{display:none}
details.trip>summary::before{content:"▸ "}
details.trip[open]>summary::before{content:"▾ "}
.tg{margin-top:8px}
.tl{font-size:11.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}
.tg a{display:inline-block;margin:0 5px 5px 0;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);text-decoration:none;font-size:13px}
.tg a.pri{background:var(--s2);border-color:var(--s2);color:#fff;font-weight:600}
.dock{margin-top:8px}
.dock .ctrl{margin-top:0}
.dock .tbar{flex:1;min-width:0;text-align:left;padding:0 0 0 4px}
#slider{display:block;width:100%;margin-top:6px}
.ticks{position:relative;height:16px;margin:0 9px;font-size:11px;color:var(--mut)}
.ticks span{position:absolute;transform:translateX(-50%);white-space:nowrap}
.ticks span:before{content:"";position:absolute;left:50%;top:-5px;width:1px;height:4px;background:var(--mut)}
@media (max-width:480px){#tmain{font-size:19px}.grid{grid-template-columns:1fr}.seg button{padding:7px 11px}}
</style></head><body><div class="wrap">
<header><h1>Snow Tracker</h1><div class="stamp">Updated {{stamp}}</div></header>
<div class="call">{{call}}</div>
<div class="alerts">{{alerts}}</div>
<div class="provo">{{provo}}</div>

<div class="mapcard">
  <div class="seg" id="seg"><button type="button" data-l="radar" class="on">Future radar</button><button type="button" data-l="snow">Snow total</button><button type="button" data-l="wind">Wind</button></div>
  <div id="map"></div>
  <div class="dock">
    <div class="ctrl"><button type="button" class="stp" id="prev" aria-label="Back">◀</button><button type="button" id="play" aria-label="Play">▶</button><button type="button" class="stp" id="next" aria-label="Forward">▶</button><div class="tbar"><div id="tmain">Loading…</div><div id="tsub">&nbsp;</div></div></div>
    <input type="range" id="slider" min="0" max="0" value="0" aria-label="Forecast time">
    <div class="ticks" id="ticks"></div>
  </div>
  <div class="chips" id="chips"></div>
  <div class="legend" id="legend"></div>
  <div class="mnote" id="mnote">Pinch or use + and − to zoom (on a computer, click the map once to zoom with the scroll wheel), drag to move, tap anywhere for that spot's forecast. White dots are your resorts; their numbers are projected snow from now until the time shown, the same blended forecast as the cards below.</div>
  <div class="links"><a class="primary" href="https://www.windy.com/-Weather-radar-radar?radar,41.0,-112.0,5">Live radar right now</a><a href="https://radar.weather.gov/station/kmtx/standard">Salt Lake radar</a></div>
  <div class="attr">Imagery: NASA EOSDIS GIBS (Blue Marble), USGS The National Map. Weather: NOAA HRRR (0-2 days), National Blend of Models (2-10 days), GFS outside the U.S.</div>
</div>

<div class="key"><b>What the score means:</b> the number in each card's corner rates that resort's best powder morning in the window, 0 to 10. It is mostly overnight snow before first chair (about 3" = 3.5, 6" = 6, 10" = 7.6, 15" = 9), minus points when the forecasts disagree, gusts top 35 mph (lift holds), temps run above freezing (heavy snow), or it lands on a Utah weekend (crowds). 6+ is a go day, 3.5 to 6 is good, under 3.5 is a refresh. Tap a resort to open its full report (lifts, trail map, cams, roads, hourly); tap again to fold it. Tap a region name to hide or show it.</div>
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
<p><b>How to read it.</b> The map's weather colors come from NOAA models: the HRRR model (3 km grid) for the next 48 hours, the same model behind most weather apps' future radar; then the National Blend of Models (2.5 km grid, the starting point for NWS forecasts) out to 10 days in 6-hour steps; and the GFS model (about 25 km, smoothed) only over the ocean, Canada and Mexico. "Future radar" shows what is expected to be falling at each time; "Snow total" shows snow piled up from now to that time. The resort numbers on the map, the charts and the cards use the official NWS snowfall forecast averaged with the median of independent weather models. Faded, striped bars and shaded chart areas are past the NWS range (about 7 days) and come from models alone. Sources this run: {{models}}. Snow on the ground comes from the nearest SNOTEL station (measured, not resort-reported). Cam photos, road status, X posts and avalanche ratings are grabbed from the resorts, state transportation departments, X and avalanche.org at each update (5:39 AM, 11:39 AM and 5:39 PM Mountain), because this page cannot play other sites live; tap "Live video" for a stream.</p>
{{health}}
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
  var map=L.map("map",{attributionControl:false,zoomSnap:0.5,maxZoom:12,scrollWheelZoom:false,minZoom:3,maxBounds:[[0,-180],[75,-25]],maxBoundsViscosity:1});map.on("click",function(){map.scrollWheelZoom.enable()});map.on("mouseout",function(){map.scrollWheelZoom.disable()});
  map.createPane("land").style.zIndex=200;map.createPane("wx").style.zIndex=300;
  var lp=map.createPane("lines");lp.style.zIndex=350;lp.style.pointerEvents="none";
  var VIEWS={"Pacific":[[18,-170],[60,-100]],"West":[[35.6,-124.4],[45.6,-104.2]],"Utah":[[36.9,-114.2],[42.1,-108.9]],"Wasatch":[[40.25,-112.05],[41.3,-111.35]],"Tahoe":[[38.4,-120.9],[39.7,-119.5]],"Mammoth":[[37.2,-119.5],[38.0,-118.5]],"Tetons":[[43.2,-111.5],[44.2,-110.3]],"Colorado":[[38.9,-107.4],[40.7,-105.2]],"USA":[[24.5,-125],[49.5,-66.5]]};
  map.fitBounds(VIEWS.West);
  var TI=M.tiles||{},blank="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
  function avail(list){var o={};(list||[]).forEach(function(k){o[k]=1});return o}
  var AB=avail(TI.bm),AU=avail(TI.usgs);
  var imgs={};function getImg(src){if(!imgs[src])imgs[src]=new Promise(function(res){var im=new Image();im.onload=function(){res(im)};im.onerror=function(){res(null)};im.src=src});return imgs[src]}
  var Atlas=L.GridLayer.extend({createTile:function(c,done){var t=document.createElement("canvas");t.width=t.height=256;var k=c.z+"/"+c.x+"/"+c.y,o=this.options;
    var z=c.z,x=c.x,y=c.y,dz=0;
    // no tile at this zoom (open ocean, Canada, Mexico): stretch the nearest lower-zoom tile so there is never a dark gap
    while(!o.av[(z-dz)+"/"+(x>>dz)+"/"+(y>>dz)]&&dz<4&&z-dz>o.minZoom)dz++;
    var pz=z-dz,px=x>>dz,py=y>>dz,pk=pz+"/"+px+"/"+py;
    if(!o.av[pk]){setTimeout(function(){done(null,t)},0);return t}
    var sz=256>>dz,sx=(x-(px<<dz))*sz,sy=(y-(py<<dz))*sz;
    getImg(o.dir+pz+"_"+Math.floor(px/8)+"_"+Math.floor(py/8)+".jpg").then(function(im){if(im){var g=t.getContext("2d");g.imageSmoothingQuality="high";g.drawImage(im,(px%8)*256+sx,(py%8)*256+sy,sz,sz,0,0,256,256)}done(null,t)});return t}});
  if(TI.bm){new Atlas({dir:"tiles/bm_",av:AB,minZoom:3,maxNativeZoom:7,maxZoom:12,keepBuffer:2}).addTo(map)}
  if(TI.usgs){new Atlas({dir:"tiles/usgs_",av:AU,minZoom:8,maxNativeZoom:11,maxZoom:12,keepBuffer:2}).addTo(map)}
  var CITIES=[["Salt Lake City",40.760,-111.891,5],["Provo",40.234,-111.659,6],["Ogden",41.223,-111.974,6],["Orem",40.297,-111.695,8],["Lehi",40.392,-111.851,8],["American Fork",40.377,-111.796,9],["Pleasant Grove",40.364,-111.738,10],["Lindon",40.343,-111.721,10],["Alpine",40.453,-111.778,10],["Saratoga Springs",40.349,-111.904,10],["Eagle Mountain",40.314,-112.007,10],["Springville",40.165,-111.611,9],["Spanish Fork",40.115,-111.655,8],["Payson",40.044,-111.732,9],["Sandy",40.565,-111.839,8],["Draper",40.524,-111.864,9],["South Jordan",40.562,-111.930,10],["West Jordan",40.610,-111.939,9],["West Valley City",40.692,-112.001,9],["Murray",40.667,-111.888,10],["Midvale",40.611,-111.900,10],["Cottonwood Heights",40.620,-111.810,9],["Holladay",40.669,-111.825,10],["Millcreek",40.687,-111.875,10],["Bountiful",40.889,-111.881,8],["Centerville",40.918,-111.872,10],["Farmington",40.980,-111.887,9],["Kaysville",41.035,-111.938,10],["Layton",41.060,-111.971,8],["Clearfield",41.110,-112.026,9],["Roy",41.162,-112.026,10],["Brigham City",41.510,-112.016,8],["Logan",41.737,-111.834,7],["Huntsville",41.260,-111.768,9],["Eden",41.302,-111.822,10],["Morgan",41.036,-111.677,9],["Park City",40.646,-111.498,7],["Kimball Junction",40.721,-111.545,10],["Heber City",40.507,-111.413,8],["Midway",40.512,-111.474,9],["Kamas",40.643,-111.280,9],["Coalville",40.918,-111.399,9],["Wanship",40.811,-111.405,10],["Alta",40.588,-111.638,9],["Tooele",40.531,-112.298,8],["Evanston",41.268,-110.963,8],["Nephi",39.710,-111.836,9],["Price",39.599,-110.811,8],["Vernal",40.456,-109.528,8],["Moab",38.573,-109.550,7],["Richfield",38.773,-112.084,8],["Cedar City",37.677,-113.062,7],["St. George",37.096,-113.568,6],["Jackson",43.480,-110.762,6],["Wilson",43.500,-110.875,9],["Teton Village",43.588,-110.828,9],["Victor",43.603,-111.112,9],["Driggs",43.723,-111.111,7],["Alta WY",43.755,-111.036,10],["Swan Valley",43.455,-111.336,10],["Rexburg",43.826,-111.789,8],["Idaho Falls",43.492,-112.034,6],["Pocatello",42.871,-112.446,6],["Twin Falls",42.563,-114.461,6],["Pinedale",42.867,-109.861,8],["Rock Springs",41.587,-109.203,7],["Cheyenne",41.140,-104.820,6],["Casper",42.867,-106.313,6],["Denver",39.739,-104.990,5],["Boulder",40.015,-105.271,7],["Nederland",39.961,-105.511,9],["Golden",39.756,-105.221,8],["Idaho Springs",39.743,-105.514,9],["Georgetown",39.706,-105.697,9],["Silverthorne",39.630,-106.071,9],["Dillon",39.630,-106.044,10],["Frisco",39.574,-106.097,9],["Breckenridge",39.482,-106.038,8],["Keystone",39.605,-105.954,10],["Winter Park",39.892,-105.763,9],["Fraser",39.945,-105.817,10],["Granby",40.086,-105.939,9],["Kremmling",40.059,-106.389,10],["Steamboat Springs",40.485,-106.832,8],["Vail",39.640,-106.374,7],["Avon",39.631,-106.522,9],["Eagle",39.655,-106.828,9],["Glenwood Springs",39.551,-107.325,8],["Carbondale",39.402,-107.211,10],["Aspen",39.191,-106.818,8],["Snowmass Village",39.213,-106.938,10],["Leadville",39.251,-106.292,8],["Fort Collins",40.585,-105.084,7],["Colorado Springs",38.834,-104.821,6],["Grand Junction",39.064,-108.551,6],["Montrose",38.478,-107.876,8],["Gunnison",38.546,-106.925,8],["Telluride",37.938,-107.812,8],["Durango",37.275,-107.880,7],["Sacramento",38.582,-121.494,5],["Roseville",38.752,-121.288,7],["Rocklin",38.791,-121.236,9],["Folsom",38.678,-121.176,8],["Auburn",38.897,-121.077,8],["Grass Valley",39.219,-121.061,9],["Colfax",39.101,-120.953,9],["Davis",38.545,-121.741,8],["Placerville",38.730,-120.798,8],["Pollock Pines",38.761,-120.586,10],["Kyburz",38.774,-120.296,10],["Twin Bridges",38.810,-120.123,10],["Meyers",38.856,-120.014,9],["South Lake Tahoe",38.933,-119.984,7],["Stateline",38.962,-119.940,10],["Tahoe City",39.172,-120.140,8],["Olympic Valley",39.197,-120.235,9],["Kings Beach",39.238,-120.026,9],["Incline Village",39.251,-119.973,9],["Truckee",39.328,-120.183,7],["Soda Springs",39.324,-120.381,10],["Kingvale",39.320,-120.437,10],["Reno",39.530,-119.814,5],["Sparks",39.535,-119.753,9],["Carson City",39.164,-119.767,7],["Minden",38.954,-119.766,9],["Stockton",37.958,-121.291,7],["Modesto",37.639,-120.997,7],["Fresno",36.738,-119.787,6],["Yosemite Valley",37.746,-119.594,8],["Lee Vining",37.957,-119.122,9],["June Lake",37.780,-119.074,9],["Mammoth Lakes",37.649,-118.972,7],["Bishop",37.364,-118.395,7],["Bakersfield",35.373,-119.019,6],["San Francisco",37.775,-122.419,5],["Oakland",37.804,-122.271,7],["San Jose",37.338,-121.886,6],["Redding",40.587,-122.392,7],["Chico",39.729,-121.837,8],["Los Angeles",34.052,-118.244,5],["San Diego",32.716,-117.161,5],["Las Vegas",36.170,-115.140,5],["Elko",40.832,-115.763,7],["Ely",39.247,-114.889,8],["Winnemucca",40.973,-117.736,8],["Phoenix",33.448,-112.074,5],["Tucson",32.222,-110.975,6],["Flagstaff",35.198,-111.651,6],["Albuquerque",35.084,-106.650,5],["Santa Fe",35.687,-105.938,6],["El Paso",31.762,-106.485,6],["Boise",43.615,-116.202,5],["Seattle",47.606,-122.332,5],["Spokane",47.659,-117.426,6],["Portland",45.515,-122.679,5],["Bend",44.058,-121.315,7],["Eugene",44.052,-123.087,7],["Medford",42.327,-122.873,7],["Missoula",46.872,-113.994,6],["Bozeman",45.677,-111.043,6],["Big Sky",45.284,-111.368,8],["Billings",45.783,-108.501,6],["Helena",46.589,-112.039,7],["Rapid City",44.081,-103.231,6],["Omaha",41.257,-95.934,5],["Kansas City",39.100,-94.578,5],["Oklahoma City",35.468,-97.516,5],["Dallas",32.777,-96.797,5],["Houston",29.760,-95.370,5],["Minneapolis",44.978,-93.265,5],["Chicago",41.878,-87.630,5]];
  var cityL=CITIES.map(function(c){return{z:c[3],m:L.marker([c[1],c[2]],{interactive:false,keyboard:false,icon:L.divIcon({className:"city t"+Math.min(c[3],8),html:c[0],iconSize:null,iconAnchor:[3,6]})})}});
  function cities(){var z=map.getZoom();cityL.forEach(function(c){var on=z>=c.z-0.25;if(on&&!map.hasLayer(c.m))c.m.addTo(map);else if(!on&&map.hasLayer(c.m))map.removeLayer(c.m)})}
  map.on("zoomend",cities);cities();
  Object.keys(VIEWS).forEach(function(k){var b=document.createElement("button");b.type="button";b.textContent=k;b.onclick=function(){map.flyToBounds(VIEWS[k],{duration:.6})};$("chips").appendChild(b)});
  fetch("base.json").then(function(r){return r.json()}).then(function(b){
    L.geoJSON({type:"FeatureCollection",features:b.countries},{pane:"land",interactive:false,style:function(){return{className:"cty"}}}).addTo(map);
    L.geoJSON({type:"FeatureCollection",features:b.states},{pane:"lines",interactive:false,style:function(){return{className:"stb",fill:false}}}).addTo(map);
  }).catch(function(){});
  // ---- timeline: HRRR hourly for ~48 h (West), then GFS out to ~10 days ----
  var H=R&&R.hrrr?R.hrrr:null,NB=R&&R.nbm&&R.nbm.parts&&R.nbm.parts.length?R.nbm:null,now=Date.now();
  var gT=R&&R.times?R.times.map(function(t){return new Date(t)}):[],hT=H?H.times.map(function(t){return new Date(t)}):[],nT=NB?NB.times.map(function(t){return new Date(t)}):[];
  function nearest(arr,t){var bi=-1,bd=1e15;for(var i=0;i<arr.length;i++){var d=Math.abs(arr[i]-t);if(d<bd){bd=d;bi=i}}return bi}
  // NBM: radar = the 6-hr period containing t; snow = cumulative, interpolated between 6-hr steps
  function fr0(t,h,g){var o={t:t,h:h,g:g,nr:-1,n0:-1,n1:-1,w:0};if(!nT.length)return o;var T=t.getTime(),P=6*3600000;
    for(var i=0;i<nT.length;i++){if(nT[i].getTime()>=T&&nT[i].getTime()-P<T-1){o.nr=i;break}}
    if(T<nT[0].getTime()){o.n1=0;o.w=Math.max(0,1-(nT[0].getTime()-T)/P)}
    else{for(var j=nT.length-1;j>=0;j--){if(nT[j].getTime()<=T){o.n0=j;break}}if(o.n0+1<nT.length){o.n1=o.n0+1;o.w=(T-nT[o.n0].getTime())/(nT[o.n1].getTime()-nT[o.n0].getTime())}}
    return o}
  var F=[],tail=NB?nT:gT;
  if(H&&hT.length){hT.forEach(function(t,i){F.push(fr0(t,i,nearest(gT,t)))});var last=hT[hT.length-1];tail.forEach(function(t){if(t>last)F.push(fr0(t,-1,nearest(gT,t)))})}
  else if(tail.length){tail.forEach(function(t){F.push(fr0(t,-1,nearest(gT,t)))})}
  else{var seen={};M.resorts.forEach(function(r){r.tl.forEach(function(x){seen[x[0]]=1})});Object.keys(seen).sort().forEach(function(t){F.push({t:new Date(t),h:-1,g:-1})})}
  var times=F.map(function(f){return f.t});
  var sl=$("slider");sl.max=Math.max(0,F.length-1);
  (function(){var tk=$("ticks"),lk=null,lastPos=-99;F.forEach(function(f,i){var k=f.t.toDateString();if(k===lk)return;lk=k;if(i===0)return;var pos=i/Math.max(1,F.length-1)*100;if(pos-lastPos<7)return;lastPos=pos;var sp=document.createElement("span");sp.style.left=pos+"%";sp.textContent=f.t.toLocaleDateString([],{weekday:"short"});tk.appendChild(sp)})})();
  var start=0;for(var i=0;i<F.length;i++){if(F[i].t.getTime()>=now-90*60000){start=i;break}}
  sl.value=start;var layer="radar",frame=start;
  // ---- resort markers: blended running totals (same numbers as the cards) ----
  function cumAt(r,t){var tl=r.tl;function at(T2){var p=0,s=new Date(tl[0][0]).getTime()-17*3600000;for(var k=0;k<tl.length;k++){var e=new Date(tl[k][0]).getTime();if(T2>=e){p=tl[k][1];s=e;continue}var c=tl[k][1];return p+(c-p)*Math.max(0,(T2-s)/(e-s))}return p}
    return Math.max(0,at(t.getTime())-at(times[start].getTime()))}
  var marks=M.resorts.map(function(r){var m=L.circleMarker([r.lat,r.lon],{radius:5,className:"rm"}).addTo(map);
    var LD={snowbird:["left",0],brighton:["right",9],solitude:["right",-9],targhee:["left",0],jackson:["right",0],sugarbowl:["left",0],palisades:["right",0],sierra:["right",0],abasin:["right",9],winterpark:["right",-9],eldora:["right",0],copper:["left",0]},ld=LD[r.id]||["right",0];
    m.bindTooltip("",{permanent:true,direction:ld[0],className:"rlab",offset:[ld[0]=="left"?-7:7,ld[1]]});
    m.on("click",function(ev){L.DomEvent.stopPropagation(ev);var v=cumAt(r,times[frame]);
      L.popup().setLatLng([r.lat,r.lon]).setContent("<b>"+r.name+"</b><br>"+Math.round(v)+'" projected by '+fmt(times[frame])+"<br>Next 5 days: ~"+Math.round(r.t5)+'"<br><a href="#'+r.id+'">Day-by-day card, cams and roads</a>').openOn(map)});
    return{r:r,m:m}});
  function labels(){var z=map.getZoom(),t=times[frame];marks.forEach(function(o){var r=o.r,txt=null;
    if(z>=r.zf)txt=r.name;else if(z>=r.zs&&r.short)txt=r.short;
    if(txt===null){o.m.closeTooltip();return}
    o.m.setTooltipContent(txt+" "+Math.round(cumAt(r,t))+'"');if(!o.m.isTooltipOpen())o.m.openTooltip()})}
  map.on("zoomend",labels);
  // ---- palettes ----
  var RAIN=["#9be89b","#6fd66f","#3fbd3f","#1f9a1f","#e8e000","#f5a300","#f05a00","#cc0000"],SNOW=["#cfe8ff","#a8d4ff","#7ab8ff","#4d94ff","#2f66eb","#3b35d0","#7330d0","#a82bd0"],MIX=["#ffd6ea","#ffb8dc","#ff94c9","#ff6fb5","#f2479f","#d6267f","#b3125f","#8c0046"];
  var SB=[0.5,1,2,3,4,6,8,12,18,24,30,36,48],SC=["#bdd7e7","#6baed6","#3182bd","#08519c","#082694","#ffff96","#ffc400","#ff8700","#db1400","#9e0000","#690000","#ccccff","#9f8cd8"];
  function hex(h,a){return[parseInt(h.substr(1,2),16),parseInt(h.substr(3,2),16),parseInt(h.substr(5,2),16),a]}
  var PAL={1:RAIN.map(function(h){return hex(h,0)}),2:SNOW.map(function(h){return hex(h,0)}),3:MIX.map(function(h){return hex(h,0)})};
  var PR=[],PS=[];for(var v=0;v<256;v++){var k=v>>4,l=v&15,c=[0,0,0,0];if(v&&l){var pal=k==2?SNOW:k==3?MIX:RAIN;c=hex(pal[Math.min(l,8)-1],l<=1?150:l<=2?195:235)}PR.push(c);
    var inch=v/2,cs=[0,0,0,0];for(var q=SB.length-1;q>=0;q--){if(inch>=SB[q]){cs=hex(SC[q],215);break}}PS.push(cs)}
  // ---- model grids ----
  function sprite(src){return new Promise(function(res){var im=new Image();im.onload=function(){try{var c=document.createElement("canvas");c.width=im.naturalWidth;c.height=im.naturalHeight;var x=c.getContext("2d",{willReadFrequently:true});x.drawImage(im,0,0);var d=x.getImageData(0,0,c.width,c.height).data,o=new Uint8Array(d.length/4);for(var i=0;i<o.length;i++)o[i]=d[i*4];res(o)}catch(e){res(null)}};im.onerror=function(){res(null)};im.src=src})}
  function meta(m){return{m:m,dlat:(m.north-m.south)/(m.ny-1),dlon:(m.east-m.west)/(m.nx-1),chunks:[],per:m.chunks?m.chunks[0].n:1e9}}
  var G=R?meta(R):null,HDs=H&&H.parts?H.parts.map(function(p){var d=meta(p);d.isH=true;return d}):[],HD=HDs.length?HDs[0]:null;
  var NDs=NB?NB.parts.map(function(p){var d=meta(p);d.isH=true;d.L={};d.perL={};Object.keys(p.layers).forEach(function(k){d.L[k]=[];d.perL[k]=p.layers[k].length?p.layers[k][0].n:1e9});return d}):[],NOK=false;
  function getv(ds,f,layer){if(f<0)return null;if(ds.L){var A=ds.L[layer],per=ds.perL[layer];if(!A)return null;var c=Math.floor(f/per),a=A[c];return a?{a:a,base:(f-c*per)*ds.m.ny*ds.m.nx}:null}
    if(ds.isH){var c=Math.floor(f/ds.per),a=ds.chunks[c];return a?{a:a,base:(f-c*ds.per)*ds.m.ny*ds.m.nx}:null}
    var a=layer=="snow"?ds.snow:ds.radar;return a?{a:a,base:f*ds.m.ny*ds.m.nx}:null}
  var OUT=[0,0,0,0];
  // cubic B-spline sampling for the coarse GFS grid: smooth shapes instead of 25 km squares
  function bw(t){var t2=t*t,t3=t2*t;return[(1-3*t+3*t2-t3)/6,(4-6*t2+3*t3)/6,(1+3*t+3*t2-3*t3)/6,t3/6]}
  function sampC(ds,gv,layer,lat,lon){var m=ds.m,sx=(lon-m.west)/ds.dlon,sy=(m.north-lat)/ds.dlat;if(sx<-.5||sy<-.5||sx>m.nx-.5||sy>m.ny-.5)return null;
    var nx=m.nx,ny=m.ny,ix=Math.floor(sx),iy=Math.floor(sy),wx=bw(sx-ix),wy=bw(sy-iy),D=gv.a,b=gv.base,acc=0,kw={1:0,2:0,3:0};
    for(var j=0;j<4;j++){var yy=Math.max(0,Math.min(ny-1,iy-1+j));for(var i=0;i<4;i++){var xx=Math.max(0,Math.min(nx-1,ix-1+i)),v=D[b+yy*nx+xx],w=wx[i]*wy[j];
      if(layer=="radar"){var l=v&15;acc+=w*l;if(l)kw[(v>>4)||1]+=w*l}else acc+=w*v}}
    if(layer!="radar")return Math.round(acc);
    if(acc<0.6)return 0;var k=kw[2]>=kw[1]&&kw[2]>=kw[3]?2:kw[3]>kw[1]?3:1,pk=PAL[k],li=Math.max(0,Math.min(7,acc-1)),i0=Math.floor(li),i1=Math.min(7,i0+1),f=li-i0;
    OUT[0]=pk[i0][0]*(1-f)+pk[i1][0]*f;OUT[1]=pk[i0][1]*(1-f)+pk[i1][1]*f;OUT[2]=pk[i0][2]*(1-f)+pk[i1][2]*f;OUT[3]=Math.min(235,Math.max(0,(acc-0.6)*240));return -1}
  function samp(ds,gv,layer,lat,lon){if(!ds.isH)return sampC(ds,gv,layer,lat,lon);var m=ds.m,sx=(lon-m.west)/ds.dlon,sy=(m.north-lat)/ds.dlat;if(sx<-.5||sy<-.5||sx>m.nx-.5||sy>m.ny-.5)return null;
    var nx=m.nx,ny=m.ny,x0=Math.max(0,Math.min(nx-1,Math.floor(sx))),x1=Math.min(nx-1,x0+1),y0=Math.max(0,Math.min(ny-1,Math.floor(sy))),y1=Math.min(ny-1,y0+1),fx=Math.max(0,Math.min(1,sx-x0)),fy=Math.max(0,Math.min(1,sy-y0)),D=gv.a,b=gv.base;
    var a00=D[b+y0*nx+x0],a01=D[b+y0*nx+x1],a10=D[b+y1*nx+x0],a11=D[b+y1*nx+x1],w00=(1-fx)*(1-fy),w01=fx*(1-fy),w10=(1-fx)*fy,w11=fx*fy;
    if(ds.isH&&(a00==255||a01==255||a10==255||a11==255))return null;
    if(layer=="radar"){var l=(a00&15)*w00+(a01&15)*w01+(a10&15)*w10+(a11&15)*w11;if(l<0.5)return 0;
      var r=0,g=0,bb=0,ws=0,cs=[a00,a01,a10,a11],ww=[w00,w01,w10,w11];
      for(var q=0;q<4;q++){var v=cs[q];if(!v||!(v&15))continue;var pk=PAL[(v>>4)||1]||PAL[1],li=Math.max(0,Math.min(7,l-1)),i0=Math.floor(li),i1=Math.min(7,i0+1),f=li-i0,wq=ww[q]*(v&15);
        r+=wq*(pk[i0][0]*(1-f)+pk[i1][0]*f);g+=wq*(pk[i0][1]*(1-f)+pk[i1][1]*f);bb+=wq*(pk[i0][2]*(1-f)+pk[i1][2]*f);ws+=wq}
      if(!ws)return 0;OUT[0]=r/ws;OUT[1]=g/ws;OUT[2]=bb/ws;OUT[3]=Math.min(235,Math.max(0,(l-0.5)*260));return -1}
    return Math.round(a00*w00+a01*w01+a10*w10+a11*w11)}
  // weather canvas redrawn at screen resolution for the current view: crisp at any zoom
  var cv=document.createElement("canvas");cv.className="wxcv";cv.style.position="absolute";cv.style.pointerEvents="none";map.getPane("wx").appendChild(cv);
  var raf=0;function redraw(){if(raf)return;raf=requestAnimationFrame(function(){raf=0;draw()})}
  // which sharp model drives this frame: HRRR (radar, first 48 h) → National Blend (to 10 days) → none (GFS only)
  function hiSrc(fr){if(layer=="radar"){if(fr.h>=0&&HD&&HD.chunks.length)return{ds:HDs,f0:fr.h,f1:-1,w:0,name:"HRRR 3 km model"};if(fr.nr>=0&&NOK)return{ds:NDs,f0:fr.nr,f1:-1,w:0,name:"National Blend 2.5 km (6-hr avg)"};return null}
    if(NOK&&(fr.n0>=0||fr.n1>=0))return{ds:NDs,f0:fr.n0,f1:fr.n1,w:fr.w,name:"National Blend 2.5 km"};return null}
  function draw(){var fr=F[frame];if(!G||!G.radar||!fr){cv.style.display="none";return}cv.style.display="";
    var sz=map.getSize(),S2=sz.x<700?1.5:1.25,W=Math.ceil(sz.x/S2),Hh=Math.ceil(sz.y/S2);cv.width=W;cv.height=Hh;cv.style.width=sz.x+"px";cv.style.height=sz.y+"px";L.DomUtil.setPosition(cv,map.containerPointToLayerPoint([0,0]));
    var x=cv.getContext("2d"),img=x.createImageData(W,Hh),o=img.data,lonC=new Float64Array(W),latR=new Float64Array(Hh);
    for(var i=0;i<W;i++)lonC[i]=map.containerPointToLatLng([(i+.5)*S2,0]).lng;for(var j=0;j<Hh;j++)latR[j]=map.containerPointToLatLng([0,(j+.5)*S2]).lat;
    var hs=hiSrc(fr),gA=hs?hs.ds.map(function(d){return getv(d,hs.f0,layer)}):null,gB=hs&&layer=="snow"?hs.ds.map(function(d){return getv(d,hs.f1,"snow")}):null,hw2=hs?hs.w:0;
    if(layer=="wind"){drawWind(fr,x,img,o,W,Hh,lonC,latR);return}
    // zoomed out you see the whole Pacific, where only the global model exists; zoomed in, only sharp data is drawn
    var zf=Math.max(0,Math.min(1,5.75-map.getZoom()));
    var gvG=fr.g>=0?getv(G,fr.g,layer):null,P=layer=="snow"?PS:PR;
    function col(v,P2){if(!v)return null;var c=v<0?OUT:P2[v];return[c[0],c[1],c[2],c[3]]}
    for(var j=0;j<Hh;j++){var lat=latR[j];
      for(var i=0;i<W;i++){var lon=lonC[i],d=(j*W+i)*4,hc=null,hit=false,hw=1;
        if(hs){for(var q=0;q<hs.ds.length;q++){var hd=hs.ds[q],hm=hd.m;if((gA[q]||(gB&&gB[q]))&&lat<=hm.north&&lat>=hm.south&&lon>=hm.west&&lon<=hm.east){var hv;
          if(layer=="radar")hv=samp(hd,gA[q],"radar",lat,lon);else{var v0=gA[q]?samp(hd,gA[q],"snow",lat,lon):0,v1=gB[q]?samp(hd,gB[q],"snow",lat,lon):0;hv=(v0===null||v1===null)?null:Math.round(v0*(1-hw2)+v1*hw2)}
          if(hv!==null){hit=true;hc=col(hv,P);if(hd.edge){var ei=Math.round((hm.north-lat)/hd.dlat)*hm.nx+Math.round((lon-hm.west)/hd.dlon);hw=(hd.edge[ei]||0)/255}}break}}}
        var c=hc;
        if(!hit||hw<0.999){var gc=(gvG&&(!hs||zf>0))?col(samp(G,gvG,layer,lat,lon),P):null;if(gc&&hs)gc[3]*=zf;
          if(!hit)c=gc;else{var a1=hc?hc[3]*hw:0,a2=gc?gc[3]*(1-hw):0,at=a1+a2;c=at>0?[((hc?hc[0]*a1:0)+(gc?gc[0]*a2:0))/at,((hc?hc[1]*a1:0)+(gc?gc[1]*a2:0))/at,((hc?hc[2]*a1:0)+(gc?gc[2]*a2:0))/at,at]:null}}
        if(!c){o[d+3]=0;continue}o[d]=c[0];o[d+1]=c[1];o[d+2]=c[2];o[d+3]=c[3]}}
    x.putImageData(img,0,0)}
  map.on("move resize",redraw);map.on("zoomstart",function(){cv.style.display="none"});map.on("zoomend",redraw);
  function fmt(t){return t.toLocaleString([], {weekday:"short",hour:"numeric"})}
  function rel(t){var hh=Math.round((t.getTime()-Date.now())/3600000);if(Math.abs(hh)<=1)return"right now";if(hh<0)return-hh+" hrs ago";if(hh<48)return"in "+hh+" hrs";return"in "+Math.round(hh/24)+" days"}
  // ---- wind: GFS 10 m wind, color = speed, moving streaks = direction (like a weather app wind map) ----
  var WM=R&&R.wind?R.wind:null,WD=null;
  if(WM){WM.dlat=(WM.north-WM.south)/(WM.ny-1);WM.dlon=(WM.east-WM.west)/(WM.nx-1)}
  function windAt(f,lat,lon){if(!WD||f<0)return null;var sx=(lon-WM.west)/WM.dlon,sy=(WM.north-lat)/WM.dlat;if(sx<0||sy<0||sx>WM.nx-1||sy>WM.ny-1)return null;
    var i=Math.floor(sx),j=Math.floor(sy),fx=sx-i,fy=sy-j,i1=Math.min(i+1,WM.nx-1),j1=Math.min(j+1,WM.ny-1),b=f*WM.nx*WM.ny,n=WM.nx;
    function g(a,ii,jj){return(a[b+jj*n+ii]-128)/2}
    var u=(g(WD.u,i,j)*(1-fx)+g(WD.u,i1,j)*fx)*(1-fy)+(g(WD.u,i,j1)*(1-fx)+g(WD.u,i1,j1)*fx)*fy,
        v=(g(WD.v,i,j)*(1-fx)+g(WD.v,i1,j)*fx)*(1-fy)+(g(WD.v,i,j1)*(1-fx)+g(WD.v,i1,j1)*fx)*fy;return[u,v]}
  var WSTOP=[[0,[60,90,170]],[5,[60,140,200]],[10,[70,180,170]],[15,[90,200,110]],[20,[200,210,70]],[28,[240,160,50]],[36,[230,80,60]],[45,[200,50,140]],[60,[150,60,200]]];
  function wcol(mph){for(var k=1;k<WSTOP.length;k++){if(mph<=WSTOP[k][0]){var a=WSTOP[k-1],b=WSTOP[k],t=(mph-a[0])/(b[0]-a[0]);return[a[1][0]+(b[1][0]-a[1][0])*t,a[1][1]+(b[1][1]-a[1][1])*t,a[1][2]+(b[1][2]-a[1][2])*t]}}return WSTOP[WSTOP.length-1][1]}
  function drawWind(fr,x,img,o,W,Hh,lonC,latR){
    for(var j=0;j<Hh;j++){for(var i=0;i<W;i++){var d=(j*W+i)*4,w=windAt(fr.g,latR[j],lonC[i]);if(!w){o[d+3]=0;continue}
      var mph=Math.sqrt(w[0]*w[0]+w[1]*w[1])*2.237,c=wcol(mph);o[d]=c[0];o[d+1]=c[1];o[d+2]=c[2];o[d+3]=150}}
    x.putImageData(img,0,0);startParticles()}
  var pc=document.createElement("canvas");pc.style.position="absolute";pc.style.pointerEvents="none";map.getPane("wx").appendChild(pc);
  var P_=[],panim=0,moving=false;
  function resetParticles(){var sz=map.getSize();pc.width=sz.x;pc.height=sz.y;pc.style.width=sz.x+"px";pc.style.height=sz.y+"px";L.DomUtil.setPosition(pc,map.containerPointToLayerPoint([0,0]));
    var n=Math.min(3500,Math.round(sz.x*sz.y/260));P_=[];for(var k=0;k<n;k++)P_.push({x:Math.random()*sz.x,y:Math.random()*sz.y,a:Math.random()*80})}
  function startParticles(){if(layer!=="wind"||!WD){stopParticles();return}pc.style.display="";if(!P_.length)resetParticles();if(!panim)panim=requestAnimationFrame(stepParticles)}
  function stopParticles(){if(panim)cancelAnimationFrame(panim);panim=0;var c=pc.getContext("2d");c.clearRect(0,0,pc.width,pc.height);pc.style.display="none"}
  function stepParticles(){panim=0;if(layer!=="wind"||moving||document.hidden){if(layer==="wind"&&!moving)panim=requestAnimationFrame(stepParticles);return}
    var c=pc.getContext("2d"),sz=map.getSize(),fr=F[frame];c.globalCompositeOperation="destination-in";c.fillStyle="rgba(0,0,0,0.92)";c.fillRect(0,0,sz.x,sz.y);c.globalCompositeOperation="source-over";
    c.strokeStyle="rgba(255,255,255,0.85)";c.lineWidth=1.1;c.beginPath();
    for(var k=0;k<P_.length;k++){var p=P_[k];var ll=map.containerPointToLatLng([p.x,p.y]),w=windAt(fr.g,ll.lat,ll.lng);
      if(!w||p.a>90){p.x=Math.random()*sz.x;p.y=Math.random()*sz.y;p.a=0;continue}
      var nx=p.x+w[0]*0.22,ny=p.y-w[1]*0.22/Math.max(0.2,Math.cos(ll.lat*Math.PI/180))*Math.cos(ll.lat*Math.PI/180);
      c.moveTo(p.x,p.y);c.lineTo(nx,ny);p.x=nx;p.y=ny;p.a++;if(p.x<0||p.y<0||p.x>sz.x||p.y>sz.y)p.a=999}
    c.stroke();panim=requestAnimationFrame(stepParticles)}
  map.on("movestart zoomstart",function(){moving=true;if(layer==="wind"){var c=pc.getContext("2d");c.clearRect(0,0,pc.width,pc.height)}});
  map.on("moveend zoomend resize",function(){moving=false;if(layer==="wind"){resetParticles();startParticles()}});
  function legend(){var g=$("legend");if(layer=="wind"){g.innerHTML="<span>Wind at ground level (mph):</span>"+[[5,"5"],[10,"10"],[20,"20"],[28,"30"],[36,"40"],[45,"50"],[60,"60+"]].map(function(q){var c=wcol(q[0]);return'<span><i style="display:inline-block;width:12px;height:10px;background:rgb('+c.map(Math.round).join(",")+');margin-right:3px"></i>'+q[1]+"</span>"}).join("")+'<span class="mut">The moving streaks show which way the wind blows. Zoom out (or tap Pacific) to watch storms come in off the ocean.</span>';return}
    if(layer=="radar"){function sw(p){return'<span class="sw">'+p.map(function(c){return'<i style="background:'+c+'"></i>'}).join("")+"</span>"}
      g.innerHTML="<span>Light → heavy:</span><span>Snow"+sw(SNOW)+"</span><span>Mix/ice"+sw(MIX)+"</span><span>Rain"+sw(RAIN)+"</span><span class=\"mut\">Zoomed out, the Pacific and Canada use a coarser global model so you can see storms coming; zoomed in, only the sharp U.S. models are shown.</span>"}
    else{g.innerHTML="<span>Snow piled up by the time shown:</span>"+SB.map(function(b,i){return'<span><i style="display:inline-block;width:12px;height:10px;background:'+SC[i]+';margin-right:3px"></i>'+b+'"</span>'}).join("")}}
  function show(f){frame=f;var fr=F[f];if(!fr)return;var t=fr.t;
    $("tmain").textContent=fmt(t);
    var hs=layer=="wind"?null:hiSrc(fr),src=layer=="wind"?"GFS wind, 10 m above ground":hs?hs.name:G?(NB&&!NOK?"GFS 25 km (sharp layer loading…)":"GFS 25 km model"):"blended resort forecast";
    $("tsub").textContent=rel(t)+" · "+src;
    draw();
    labels()}
  var timer=null;function stop(){if(timer){clearInterval(timer);timer=null;$("play").textContent="▶"}}
  $("play").onclick=function(){if(timer){stop();return}if(frame>=F.length-1){sl.value=start;show(start)}$("play").textContent="❚❚";
    var tick=function(){var n=frame+1;if(n>F.length-1){stop();return}sl.value=n;show(n);clearInterval(timer);timer=setInterval(tick,F[n]&&F[n].h>=0?420:700)};timer=setInterval(tick,450)};
  $("prev").onclick=function(){stop();var n=Math.max(0,frame-1);sl.value=n;show(n)};
  $("next").onclick=function(){stop();var n=Math.min(F.length-1,frame+1);sl.value=n;show(n)};
  sl.addEventListener("input",function(){stop();show(+sl.value)});
  document.querySelectorAll("#seg button").forEach(function(b){b.onclick=function(){layer=b.getAttribute("data-l");
    if(layer=="snow"&&frame===start){var tgt=Date.now()+5*86400000,bi=start;for(var q=start;q<F.length;q++){if(F[q].t.getTime()<=tgt)bi=q}frame=bi;sl.value=bi}
    document.querySelectorAll("#seg button").forEach(function(x){x.classList.toggle("on",x===b)});if(layer!=="wind")stopParticles();else resetParticles();legend();show(frame)}});
  map.on("click",function(e){if(layer=="wind"){var fw=F[frame],ww=windAt(fw.g,e.latlng.lat,e.latlng.lng);if(ww){var mph=Math.round(Math.sqrt(ww[0]*ww[0]+ww[1]*ww[1])*2.237),dir=(Math.atan2(-ww[0],-ww[1])*180/Math.PI+360)%360,names=["N","NE","E","SE","S","SW","W","NW"];
      L.popup().setLatLng(e.latlng).setContent("<b>"+fmt(fw.t)+"</b><br>Wind "+mph+" mph from the "+names[Math.round(dir/45)%8]+'<br><span style="color:#888">GFS model, 10 m above ground</span>').openOn(map)}return}
    if(!G||!G.radar)return;var fr=F[frame],la=e.latlng.lat,lo=e.latlng.lng,kinds={1:"Rain",2:"Snow",3:"Mixed or icy"},ints=["","very light","very light","light","light","moderate","moderate","heavy","very heavy"],vr=0,srcn="GFS model, ~25 km grid";
    function pick(ds,f,layer){var i=Math.round((lo-ds.m.west)/ds.dlon),j=Math.round((ds.m.north-la)/ds.dlat);if(i<0||j<0||i>=ds.m.nx||j>=ds.m.ny)return null;var gv=getv(ds,f,layer);return gv?gv.a[gv.base+j*ds.m.nx+i]:null}
    vr=null;if(fr.h>=0&&HD&&HD.chunks.length){for(var q=0;q<HDs.length;q++){var hv=pick(HDs[q],fr.h,"radar");if(hv!==null&&hv!==255){vr=hv;srcn="HRRR model, ~3 km";break}}}
    if(vr===null&&fr.nr>=0&&NOK){for(var q=0;q<NDs.length;q++){var nv=pick(NDs[q],fr.nr,"radar");if(nv!==null&&nv!==255){vr=nv;srcn="National Blend, ~2.5 km, 6-hr average";break}}}
    if(vr===null)vr=pick(G,fr.g,"radar")||0;
    var vs=null,ssrc="GFS";if(NOK&&(fr.n0>=0||fr.n1>=0)){for(var q=0;q<NDs.length;q++){var a0=fr.n0>=0?pick(NDs[q],fr.n0,"snow"):0,a1=fr.n1>=0?pick(NDs[q],fr.n1,"snow"):0;if(a0!==null&&a1!==null&&a0!==255&&a1!==255){vs=(a0*(1-fr.w)+a1*fr.w)/2;ssrc="National Blend";break}}}
    if(vs===null)vs=fr.g>=0?(pick(G,fr.g,"snow")||0)/2:0;
    var k=vr>>4,l=vr&15,nowtxt=vr&&l?kinds[k]+", "+ints[Math.min(l,8)]:"Nothing falling";
    L.popup().setLatLng(e.latlng).setContent("<b>"+fmt(fr.t)+"</b><br>"+nowtxt+'<br><span style="color:#888">'+srcn+"</span><br>Snow piled up from now ("+ssrc+"): "+(vs<0.5?"under 0.5":Math.round(vs*2)/2)+'"').openOn(map)});
  if(!G){$("seg").style.display="none";$("legend").textContent="Model map unavailable tonight; resort numbers still update."}
  pc.style.display="none";legend();show(start);
  if(WM){var wim=new Image();wim.onload=function(){try{var c=document.createElement("canvas");c.width=wim.naturalWidth;c.height=wim.naturalHeight;var cx=c.getContext("2d",{willReadFrequently:true});cx.drawImage(wim,0,0);var d=cx.getImageData(0,0,c.width,c.height).data,n=d.length/4,u=new Uint8Array(n),v=new Uint8Array(n);for(var i=0;i<n;i++){u[i]=d[i*4];v[i]=d[i*4+1]}WD={u:u,v:v};if(layer=="wind")show(frame)}catch(e){}};wim.src=WM.file}
  else{var wb=document.querySelector('#seg button[data-l="wind"]');if(wb)wb.style.display="none"}
  if(G){var jobs=[sprite("radar.png"),sprite("snowacc.png")];HDs.forEach(function(d){d.m.chunks.forEach(function(c){jobs.push(sprite(c.file))})});HDs.forEach(function(d){if(d.m.edge)sprite(d.m.edge).then(function(e){d.edge=e;redraw()})});
    Promise.all(jobs).then(function(a){G.radar=a[0];G.snow=a[1];var pos=2;HDs.forEach(function(d){d.chunks=a.slice(pos,pos+d.m.chunks.length);pos+=d.m.chunks.length;if(d.chunks.some(function(x){return!x}))d.chunks=[]});if(!G.radar){$("legend").textContent="Model map could not load."}show(frame);
      if(NDs.length){var nj=[];NDs.forEach(function(d){Object.keys(d.m.layers).forEach(function(k){d.m.layers[k].forEach(function(c,ci){nj.push(sprite(c.file).then(function(a){d.L[k][ci]=a}))})});if(d.m.edge)nj.push(sprite(d.m.edge).then(function(e){d.edge=e}))});
        Promise.all(nj).then(function(){NOK=NDs.every(function(d){return Object.keys(d.m.layers).every(function(k){var A=d.L[k];return A.length==d.m.layers[k].length&&A.every(Boolean)})});show(frame)})}})}
})();
</script>
<div id="lb" role="dialog" aria-label="Photo"><img alt=""><div></div></div>
<script>
(function(){
var COL={o:"#22c55e",h:"#f5b301",s:"#60a5fa",c:"#ef4444",u:"#d5dde6"},WORD={o:"Open",h:"On hold",s:"Scheduled to run",c:"Closed",u:"Status not matched"};
var DIFF=function(d){d=(d||"").toLowerCase();return /novice|easy|begin|green/.test(d)?"Beginner (green)":/double.?black|expert|extreme|freeride|dbl/.test(d)?"Expert (double black)":/black|advanced|most_difficult|difficult/.test(d)?"Advanced (black)":/intermed|blue|more_difficult/.test(d)?"Intermediate (blue)":/park/.test(d)?"Terrain park":""};
var LJ=null;
function esc(t){return String(t==null?"":t).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
function fmt(iso){var t=Date.parse(iso);return t?new Date(t).toLocaleString([],{weekday:"short",hour:"numeric",minute:"2-digit"}):""}
function bar(l,o,t,x){if(!t)return"";var p=Math.max(0,Math.min(100,Math.round(100*o/t)));return '<div class="mrow"><span class="ml">'+l+'</span><span class="mbar"><i style="width:'+p+'%"></i></span><span class="mv"><b>'+o+'</b>/'+t+'</span></div>'+(x||"")}
function card(el,e){
  var rid=el.getAttribute("data-rid"),opens=el.getAttribute("data-opens"),today=el.getAttribute("data-today"),extra=el.getAttribute("data-extra");
  var pill,cls;
  if(e.lifts_open>0){pill="Open";cls="op"}
  else if(e.lifts_hold>0){pill="Lifts on hold";cls="soon"}
  else if(e.lifts_sched>0&&!(opens&&opens>today)){pill="Closed for the day";cls="cl"}
  else if(opens&&opens>today){var d=Math.round((Date.parse(opens)-Date.parse(today))/864e5);pill="Opens "+new Date(opens+"T12:00").toLocaleDateString([],{weekday:"short",month:"short",day:"numeric"})+(d===1?" (tomorrow)":d<8?" (in "+d+" days)":"");cls="soon"}
  else{pill="Closed today";cls="cl"}
  var when=e.updated&&Date.parse(e.updated)>Date.now()-36*36e5?'resort updated <span class="ago" data-t="'+esc(e.updated)+'">'+fmt(e.updated)+'</span>':'checked <span class="ago" data-t="'+esc(e.fetched)+'">'+fmt(e.fetched)+'</span>';
  var hold=(e.lifts_hold?'<div class="mhold">'+e.lifts_hold+' lift'+(e.lifts_hold>1?"s":"")+' on hold</div>':"")+(e.lifts_sched&&!e.lifts_open?'<div class="mut mx">'+e.lifts_sched+' lift'+(e.lifts_sched>1?"s":"")+' scheduled to run next</div>':"");
  var h='<div class="mhead"><span class="mpill '+cls+'">'+esc(pill)+'</span><span class="mut">Lifts and runs from the resort\'s own report · '+when+'</span></div>'
   +bar("Lifts",e.lifts_open,e.lifts_total,hold)+bar("Runs",e.runs_open,e.runs_total)
   +(extra?'<div class="mut mx">'+esc(extra)+'</div>':"")
   +(e.x&&e.x.length?'<div class="rx">'+e.x.slice(0,2).map(function(p){return '<div class="rxh"><a href="https://x.com/'+esc(p.h)+'" target="_blank" rel="noopener">@'+esc(p.h)+'</a> on X · <span class="ago" data-t="'+esc(p.t)+'">'+fmt(p.t)+'</span></div><div class="rxt">'+esc(p.text)+'</div>'}).join("")+'</div>':"")
   +'<div class="mbtns">'+(e.map?'<button class="mbtn tmbtn">Trail map</button>':"")+'<button class="mbtn llbtn">Lift by lift</button><a href="'+esc(e.link)+'" target="_blank" rel="noopener">Lift report ↗</a>'+(e.maplink?'<a href="'+esc(e.maplink)+'" target="_blank" rel="noopener">Official trail map ↗</a>':"")+'</div>'
   +'<div class="llist" hidden></div><div class="tmwrap" hidden></div>';
  el.innerHTML=h;el._e=e;
  var q=document.querySelector('.qs[data-q="'+rid+'"]');
  if(q){var xs=e.x&&e.x.length?' · <span class="qx">@'+esc(e.x[0].h)+' posted <span class="ago" data-t="'+esc(e.x[0].t)+'"></span></span>':"";
    q.innerHTML='<span class="mpill '+cls+'">'+esc(pill)+'</span><span><b>'+e.lifts_open+'</b>/'+e.lifts_total+' lifts · <b>'+e.runs_open+'</b>/'+e.runs_total+' runs</span>'+(e.lifts_sched&&!e.lifts_open?'<span>· '+e.lifts_sched+' scheduled next</span>':"")+xs}
  var o=document.querySelector('#rjump option[value="'+rid+'"]');
  if(o){if(!o._n)o._n=o.textContent;o.textContent=o._n+(e.lifts_open>0?" · open "+e.lifts_open+"/"+e.lifts_total+" lifts":" · "+pill.toLowerCase())}
}
function list(el){
  var e=el._e,o=e.runs.filter(function(r){return r.s==="open"}),h='<h5>Lifts</h5>';
  var ord={open:0,hold:1,sched:2,closed:3};
  e.lifts.slice().sort(function(a,b){return ord[a.s]-ord[b.s]}).forEach(function(l){h+='<span class="lch '+l.s+'" title="'+esc(l.s)+'"><i></i>'+esc(l.n)+'</span>'});
  h+='<h5>Open runs ('+o.length+')</h5><div class="oruns">'+(o.length?o.map(function(r){return esc(r.n)}).join(" · "):'<span class="mut">None open right now</span>')+'</div>';
  return h;
}
function dcat(d){d=(d||"").toLowerCase();if(/park/.test(d))return"p";if(/expert|extreme|freeride|double|dbl/.test(d))return"kk";if(/black|advanced|difficult/.test(d)&&!/more/.test(d))return"k";if(/intermed|blue|more/.test(d))return"b";if(/novice|easy|begin|green/.test(d))return"g";return"u"}
var DC={g:"#1f9d45",b:"#1d63d6",k:"#111111",kk:"#111111",p:"#ee7a00",u:"#6b7785"},DN={g:"Beginner",b:"Intermediate",k:"Advanced",kk:"Expert",p:"Terrain park",u:"Run"},DS={g:"●",b:"■",k:"◆",kk:"◆◆",p:"▲",u:""};
function tmap(wrap,rid,e){
  wrap.innerHTML='<div class="tmbar"><span class="tmseg" data-k="base"><button class="on" data-v="hill">Terrain</button><button data-v="sat">Satellite</button></span>'
   +'<span class="tmseg" data-k="col"><button class="on" data-v="diff">Trail colors</button><button data-v="stat">Open / closed</button></span>'
   +'<span class="tmseg" data-k="show"><button class="on" data-v="all">All runs</button><button data-v="open">Open only</button></span>'
   +'<button class="tmfs">⤢ Full screen</button></div><div class="tmap"></div><div class="tmleg"></div>';
  var div=wrap.querySelector(".tmap"),leg=wrap.querySelector(".tmleg"),st={base:"hill",col:"diff",show:"all"},m=L.map(div,{attributionControl:false,zoomSnap:0.25,scrollWheelZoom:false,renderer:L.canvas({tolerance:9})});
  m.on("click",function(){m.scrollWheelZoom.enable()});m.on("mouseout",function(){m.scrollWheelZoom.disable()});
  var G,bnd,bases={},lines=L.layerGroup().addTo(m),labs=L.layerGroup().addTo(m),items=[];
  function legend(){var h=st.col==="diff"
      ?'<span><b style="background:'+DC.g+'"></b>● Beginner</span><span><b style="background:'+DC.b+'"></b>■ Intermediate</span><span><b style="background:#111"></b>◆ Advanced · ◆◆ Expert</span><span><b style="background:'+DC.p+'"></b>Park</span><span><b class="fade" style="background:#555"></b>Faded = closed</span><span><b style="background:#111"></b><span style="color:#16a34a">●</span> Open lift</span><span><b class="fade" style="background:#555"></b>Closed lift (grey, dashed)</span>'
      :'<span><b style="background:'+COL.o+'"></b>Open</span><span><b style="background:'+COL.h+'"></b>On hold</span><span><b style="background:'+COL.s+'"></b>Scheduled</span><span><b style="background:'+COL.c+'"></b>Closed</span><span><b style="background:#aab4bf"></b>No status</span>';
    leg.innerHTML=h+'<span>Tap a line for details · runs © OpenStreetMap · imagery USGS</span>'}
  function draw(){
    lines.clearLayers();items=[];var only=st.show==="open";
    function add(kind,feats,codes){feats.forEach(function(f,i){
      var c=codes.charAt(i)||"u",open=c==="o"||c==="h",lift=kind==="lift",cat=lift?"":dcat(f.d);
      if(only&&!open&&!lift)return;
      var col,w,op,dash=null,casing=false;
      if(st.col==="diff"){
        if(lift){col=open?"#111":"#7d8996";w=open?2.8:2.2;op=1;casing=open;dash=open?null:"6 4"}
        else{col=DC[cat];w=open?3.4:2.2;op=open?1:(c==="u"?0.6:0.38);casing=open;dash=open?(cat==="kk"?"8 3":null):"5 5"}
      }else{
        col=c==="u"?"#aab4bf":COL[c];w=lift?2.8:(open?3.4:2.2);op=open?1:0.75;casing=open||lift;dash=lift?"7 4":null}
      var tip='<b>'+esc(f.n||(lift?"Unnamed lift":"Unnamed run"))+'</b><br>'+(lift?"Lift":(DS[cat]?DS[cat]+" ":"")+DN[cat])+' · <span style="color:'+(c==="u"?"#667":COL[c])+';font-weight:700">'+WORD[c]+'</span>';
      f.g.forEach(function(seg){var ll=seg.map(function(p){return[p[1],p[0]]});
        if(casing)L.polyline(ll,{color:"#fff",weight:w+3,opacity:st.base==="sat"?0.75:0.95,interactive:false,lineCap:"round"}).addTo(lines);
        L.polyline(ll,{color:col,weight:w,opacity:op,dashArray:dash,lineCap:"round"}).bindPopup(tip).addTo(lines);
        if(lift){[ll[0],ll[ll.length-1]].forEach(function(p){L.circleMarker(p,{radius:3,color:"#fff",weight:1.5,fillColor:col,fillOpacity:1,interactive:false}).addTo(lines)})}
      });
      if(f.n)items.push({n:f.n,lift:lift,c:c,open:open,cat:cat,g:f.g});
    })}
    add("run",G.runs,e.map.runs);add("lift",G.lifts,e.map.lifts);
    // one label per name, at the middle of its longest piece
    var by={};items.forEach(function(it){var k=(it.lift?"L|":"R|")+it.n;(by[k]=by[k]||{it:it,segs:[]}).segs=by[k].segs.concat(it.g)});
    items=Object.keys(by).map(function(k){var o=by[k],best=null,bl=-1,tot=0;o.segs.forEach(function(sg){var l=0;for(var j=1;j<sg.length;j++)l+=Math.hypot(sg[j][0]-sg[j-1][0],sg[j][1]-sg[j-1][1]);tot+=l;if(l>bl){bl=l;best=sg}});
      var half=bl/2,acc=0,pt=best[0];for(var j=1;j<best.length;j++){var d=Math.hypot(best[j][0]-best[j-1][0],best[j][1]-best[j-1][1]);if(acc+d>=half){var t=(half-acc)/(d||1);pt=[best[j-1][0]+t*(best[j][0]-best[j-1][0]),best[j-1][1]+t*(best[j][1]-best[j-1][1])];break}acc+=d}
      var it=o.it;return{n:it.n,lift:it.lift,c:it.c,open:it.open,cat:it.cat,ll:[pt[1],pt[0]],len:tot,pri:(it.lift?(it.open?4e3:3e3):(it.open?2e3:1e3))+Math.min(999,tot*1e4)}});
    items.sort(function(a,b){return b.pri-a.pri});
    labels();
  }
  function labels(){
    labs.clearLayers();var placed=[],z=m.getZoom(),fz=m._fitZ||z,only=st.show==="open",max=z<fz+0.6?40:z<fz+1.6?90:400;
    var size=m.getSize();
    for(var i=0;i<items.length&&placed.length<max;i++){var it=items[i];if(only&&!it.open&&!it.lift)continue;
      var p=m.latLngToContainerPoint(it.ll);if(p.x<0||p.y<0||p.x>size.x||p.y>size.y)continue;
      var txt=(it.lift?"":(DS[it.cat]?DS[it.cat]+" ":""))+it.n,w=txt.length*(it.lift?7:6.2)+8,h=14,r={x0:p.x-w/2,x1:p.x+w/2,y0:p.y-h/2,y1:p.y+h/2},hit=false;
      for(var j=0;j<placed.length;j++){var q=placed[j];if(r.x0<q.x1&&r.x1>q.x0&&r.y0<q.y1&&r.y1>q.y0){hit=true;break}}
      if(hit)continue;placed.push(r);
      var cls="tml"+(it.lift?" lift"+(it.open?" op":""):(!it.open&&st.col==="diff"?" c":"")),color=(!it.lift&&st.col==="diff"&&it.open)?' style="color:'+(it.cat==="g"?"#137a33":it.cat==="b"?"#1550b3":it.cat==="p"?"#b85a00":"#111")+'"':"";
      L.marker(it.ll,{interactive:false,keyboard:false,icon:L.divIcon({className:"",iconSize:null,html:'<span class="'+cls+'"'+color+'>'+(it.lift&&it.open?'<i style="color:#16a34a">●</i>':"")+esc(txt)+'</span>'})}).addTo(labs);
    }
  }
  function setBase(){Object.keys(bases).forEach(function(k){if(k===st.base)bases[k].addTo(m);else m.removeLayer(bases[k])});div.style.background=st.base==="sat"?"#24303c":"#f1f4f8"}
  function fit(){var fb=G.bbox?L.latLngBounds([[G.bbox[1],G.bbox[0]],[G.bbox[3],G.bbox[2]]]):bnd,pr=function(ll){return L.CRS.EPSG3857.project(ll)},p0=pr(fb.getSouthWest()),p1=pr(fb.getNorthEast()),asp=Math.abs((p1.y-p0.y)/(p1.x-p0.x));
    if(!wrap.classList.contains("full"))div.style.height=Math.round(Math.max(300,Math.min(600,div.clientWidth*asp)))+"px";
    m.invalidateSize();var bb=G.bbox?L.latLngBounds([[G.bbox[1],G.bbox[0]],[G.bbox[3],G.bbox[2]]]):bnd;m.fitBounds(bb,{padding:[8,8]});m._fitZ=m.getZoom();m.setMinZoom(m._fitZ-1);m.setMaxZoom(m._fitZ+3.5);labels()}
  fetch("trailmaps/"+rid+".json").then(function(r){return r.json()}).then(function(g){
    G=g;bnd=L.latLngBounds(g.img.bounds);bases.sat=L.imageOverlay(g.img.file,bnd);bases.hill=L.imageOverlay((g.hill||g.img).file,bnd);
    m.setMaxBounds(bnd.pad(0.3));setBase();bases[st.base].bringToBack();fit();legend();draw();
    m.on("zoomend moveend",labels);
  }).catch(function(){div.innerHTML='<div style="padding:14px">Trail map unavailable</div>'});
  wrap.querySelector(".tmbar").addEventListener("click",function(ev){
    if(ev.target.closest(".tmfs")){wrap.classList.toggle("full");ev.target.closest(".tmfs").textContent=wrap.classList.contains("full")?"✕ Close":"⤢ Full screen";setTimeout(fit,30);return}
    var bt=ev.target.closest(".tmseg button");if(!bt)return;var sg=bt.parentNode,k=sg.getAttribute("data-k");
    sg.querySelectorAll("button").forEach(function(x){x.classList.toggle("on",x===bt)});st[k]=bt.getAttribute("data-v");
    if(k==="base"){setBase();draw()}else{if(k==="col")legend();draw()}
  });
  document.addEventListener("keydown",function(ev){if(ev.key==="Escape"&&wrap.classList.contains("full")){wrap.querySelector(".tmfs").click()}});
  setTimeout(function(){m.invalidateSize()},50);
}
document.addEventListener("click",function(ev){
  var b=ev.target.closest(".tmbtn,.llbtn");if(!b)return;var el=b.closest(".mstat");if(!el||!el._e)return;
  var isMap=b.classList.contains("tmbtn"),box=el.querySelector(isMap?".tmwrap":".llist"),open=box.hasAttribute("hidden");
  if(open){box.removeAttribute("hidden");b.classList.add("on");if(!box._built){box._built=1;if(isMap)tmap(box,el.getAttribute("data-rid"),el._e);else box.innerHTML=list(el)}}
  else{box.setAttribute("hidden","");b.classList.remove("on")}
});
fetch("lifts.json",{cache:"no-store"}).then(function(r){return r.json()}).then(function(j){LJ=j;
  document.querySelectorAll(".mstat[data-rid]").forEach(function(el){var e=j.resorts[el.getAttribute("data-rid")];if(e)card(el,e)});
  var ev=document.createEvent("Event");ev.initEvent("liftsready",true,true);document.dispatchEvent(ev);
}).catch(function(){});
})();
</script>
<script>
(function(){
var KEY="snowOpenCards";
function saved(){try{return JSON.parse(localStorage.getItem(KEY)||"[]")}catch(e){return[]}}
function save(){try{localStorage.setItem(KEY,JSON.stringify([].map.call(document.querySelectorAll("details.card[open]"),function(d){return d.id})))}catch(e){}}
saved().forEach(function(id){var d=document.getElementById(id);if(d&&d.tagName==="DETAILS")d.open=true});
document.querySelectorAll("details.card").forEach(function(d){d.addEventListener("toggle",function(){save();if(d.open){var m=d.querySelectorAll(".leaflet-container");for(var i=0;i<m.length;i++){window.dispatchEvent(new Event("resize"))}}})});
function go(id,smooth){var d=document.getElementById(id);if(!d)return;var reg=d.closest("details.reg");if(reg)reg.open=true;if(d.tagName==="DETAILS"&&d.classList.contains("card"))d.open=true;
  setTimeout(function(){d.scrollIntoView({behavior:smooth===false?"auto":"smooth",block:"start"})},40)}
var sel=document.getElementById("rjump");
if(sel)sel.addEventListener("change",function(){if(sel.value){history.replaceState(null,"","#"+sel.value);go(sel.value);sel.value=""}});
var xa=document.getElementById("xall"),ca=document.getElementById("call");
if(xa)xa.addEventListener("click",function(){document.querySelectorAll("details.reg").forEach(function(r){r.open=true});document.querySelectorAll("details.card").forEach(function(d){d.open=true});save()});
if(ca)ca.addEventListener("click",function(){document.querySelectorAll("details.card").forEach(function(d){d.open=false});save()});
window.addEventListener("hashchange",function(){var h=location.hash.slice(1);if(h&&!/^reg-/.test(h))go(h)});
if(location.hash&&!/^#reg-/.test(location.hash))go(location.hash.slice(1),false);
})();
</script>
<script>
(function(){var lb=document.getElementById("lb"),li=lb.querySelector("img"),lc=lb.querySelector("div");
document.addEventListener("click",function(e){var t=e.target;if(t.tagName==="IMG"&&t.hasAttribute("data-cap")){li.src=t.src;lc.textContent=t.getAttribute("data-cap");lb.classList.add("on")}else if(lb.classList.contains("on")&&lb.contains(t)){lb.classList.remove("on")}});
document.addEventListener("keydown",function(e){if(e.key==="Escape")lb.classList.remove("on")});
function ago(){var n=Date.now();document.querySelectorAll(".ago[data-t]").forEach(function(el){var t=Date.parse(el.getAttribute("data-t"));if(!t)return;var m=Math.round((n-t)/60000),s=m<1?"just now":m<60?m+" min ago":m<1440?Math.round(m/60)+" hr ago":Math.round(m/1440)+" days ago";
  el.textContent=new Date(t).toLocaleString([],{weekday:"short",hour:"numeric",minute:"2-digit"})+" · "+s})}ago();setInterval(ago,60000);document.addEventListener("liftsready",ago)})();
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
