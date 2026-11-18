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
    return (f'<section class="card" id="{r["id"]}"><div class="ch"><div><h3>{esc(r["name"])}</h3>'
            f'<div class="sub">{esc(" · ".join(sub))}</div></div>'
            f'<div class="badge {color(best["score"])}" title="Best powder score in the window">{best["score"]}</div></div>'
            f'<div class="next">Tomorrow <b>{nxt["ovn"]:.0f}"</b> · next 5 days ~{r["total5"]:.0f}" · {best_txt}{lvl_txt}</div>'
            f'{render_wx_alerts(r)}'
            f'<div class="bars" style="grid-template-columns:repeat({len(r["days"])},1fr)">{"".join(bars)}</div>'
            f'{render_hourly(r)}{render_live(r)}{render_trip(r)}</section>')


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
    alerts = "".join(f'<div class="alert {a["level"]}">{esc(a["text"])}</div>' for a in data["alerts"])
    provo = esc(data["provo"]["text"]) if data.get("provo") else ""
    stamp = datetime.fromisoformat(data["generated"]).astimezone(ZoneInfo("America/Denver")).strftime("%a %b %-d, %-I:%M %p MT")
    real = [w for w in data["warnings"] if "Open-Meteo" not in w]
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
    fetch_nws_alerts(resorts)
    for r in resorts:
        r["hourly"] = nws_hourly(members.get(r["id"], {}).get("NWS"), tzs.get(r["id"], REGION_TZ[r["region"]]), now)
    try:
        import live as live_mod
        lv = live_mod.build(out, {r["id"]: POS[r["id"]] for r in resorts}, log=log)
        for r in resorts:
            r["live"] = lv["resorts"].get(r["id"])
        live_asof = lv["as_of"]
    except Exception as e:
        log(f"Live cams and roads: {e}")
        live_asof = None
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
            "snotel_ok": len(snotel), "warnings": LOG, "radar": radar_meta, "tzs": tzs, "live_asof": live_asof}
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
  <div class="seg" id="seg"><button type="button" data-l="radar" class="on">Future radar</button><button type="button" data-l="snow">Snow total</button></div>
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
<p><b>How to read it.</b> The map's weather colors come from NOAA models: for the next 48 hours across the US, the HRRR model (3 km grid; hourly for a day, then every 2 hours), the same model behind most weather apps' future radar; beyond that, and over the ocean, Canada and Mexico, the GFS model (about 25 km grid), every 3 hours for 5 days and every 6 hours after that. Snow totals on the map use GFS throughout so they stay consistent over 10 days. "Future radar" shows what the model expects to be falling at each time; "Snow total" shows snow piled up from now to that time. The model's grid smooths mountain peaks, so its mountain totals usually run lower than the resort numbers. The resort numbers on the map, the charts, and the cards use the finer blended forecast: the official NWS snowfall forecast averaged with the median of the independent weather models. Faded, striped bars and shaded chart areas are past the NWS range (about 7 days) and come from models alone. Sources this run: {{models}}. Snow on the ground comes from the nearest SNOTEL station (measured, not resort-reported). The live radar can't run inside this page, so that button opens it in a new tab.</p>
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
  var map=L.map("map",{attributionControl:false,zoomSnap:0.5,maxZoom:12,scrollWheelZoom:false,minZoom:4,maxBounds:[[17,-141],[56,-57]],maxBoundsViscosity:1});map.on("click",function(){map.scrollWheelZoom.enable()});map.on("mouseout",function(){map.scrollWheelZoom.disable()});
  map.createPane("land").style.zIndex=200;map.createPane("wx").style.zIndex=300;
  var lp=map.createPane("lines");lp.style.zIndex=350;lp.style.pointerEvents="none";
  var VIEWS={"West":[[35.6,-124.4],[45.6,-104.2]],"Utah":[[36.9,-114.2],[42.1,-108.9]],"Wasatch":[[40.25,-112.05],[41.3,-111.35]],"Tahoe":[[38.4,-120.9],[39.7,-119.5]],"Mammoth":[[37.2,-119.5],[38.0,-118.5]],"Tetons":[[43.2,-111.5],[44.2,-110.3]],"Colorado":[[38.9,-107.4],[40.7,-105.2]],"USA":[[24.5,-125],[49.5,-66.5]]};
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
  var CITIES=[["Salt Lake City",40.76,-111.89,5],["Provo",40.23,-111.66,6],["Ogden",41.22,-111.97,7],["Park City",40.65,-111.50,7],["Jackson",43.48,-110.76,6],["Driggs",43.72,-111.11,7],["Idaho Falls",43.49,-112.03,6],["Boise",43.62,-116.20,5],["Denver",39.74,-104.99,5],["Vail",39.64,-106.37,7],["Reno",39.53,-119.81,5],["Truckee",39.33,-120.18,7],["Sacramento",38.58,-121.49,5],["Mammoth Lakes",37.65,-118.97,7],["Las Vegas",36.17,-115.14,5],["San Francisco",37.77,-122.42,5],["Los Angeles",34.05,-118.24,5],["Phoenix",33.45,-112.07,5],["Albuquerque",35.08,-106.65,5],["Portland",45.52,-122.68,5],["Seattle",47.61,-122.33,5],["Bozeman",45.68,-111.04,6],["St. George",37.10,-113.58,6],["Grand Junction",39.06,-108.55,6]];
  var cityL=CITIES.map(function(c){return{z:c[3],m:L.marker([c[1],c[2]],{interactive:false,keyboard:false,icon:L.divIcon({className:"city",html:c[0],iconSize:null,iconAnchor:[3,6]})})}});
  function cities(){var z=map.getZoom();cityL.forEach(function(c){var on=z>=c.z&&z<8.5;if(on&&!map.hasLayer(c.m))c.m.addTo(map);else if(!on&&map.hasLayer(c.m))map.removeLayer(c.m)})}
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
    var gvG=fr.g>=0?getv(G,fr.g,layer):null,P=layer=="snow"?PS:PR;
    function col(v,P2){if(!v)return null;var c=v<0?OUT:P2[v];return[c[0],c[1],c[2],c[3]]}
    for(var j=0;j<Hh;j++){var lat=latR[j];
      for(var i=0;i<W;i++){var lon=lonC[i],d=(j*W+i)*4,hc=null,hit=false,hw=1;
        if(hs){for(var q=0;q<hs.ds.length;q++){var hd=hs.ds[q],hm=hd.m;if((gA[q]||(gB&&gB[q]))&&lat<=hm.north&&lat>=hm.south&&lon>=hm.west&&lon<=hm.east){var hv;
          if(layer=="radar")hv=samp(hd,gA[q],"radar",lat,lon);else{var v0=gA[q]?samp(hd,gA[q],"snow",lat,lon):0,v1=gB[q]?samp(hd,gB[q],"snow",lat,lon):0;hv=(v0===null||v1===null)?null:Math.round(v0*(1-hw2)+v1*hw2)}
          if(hv!==null){hit=true;hc=col(hv,P);if(hd.edge){var ei=Math.round((hm.north-lat)/hd.dlat)*hm.nx+Math.round((lon-hm.west)/hd.dlon);hw=(hd.edge[ei]||0)/255}}break}}}
        var c=hc;
        if(!hit||hw<0.999){var gc=gvG?col(samp(G,gvG,layer,lat,lon),P):null;
          if(!hit)c=gc;else{var a1=hc?hc[3]*hw:0,a2=gc?gc[3]*(1-hw):0,at=a1+a2;c=at>0?[((hc?hc[0]*a1:0)+(gc?gc[0]*a2:0))/at,((hc?hc[1]*a1:0)+(gc?gc[1]*a2:0))/at,((hc?hc[2]*a1:0)+(gc?gc[2]*a2:0))/at,at]:null}}
        if(!c){o[d+3]=0;continue}o[d]=c[0];o[d+1]=c[1];o[d+2]=c[2];o[d+3]=c[3]}}
    x.putImageData(img,0,0)}
  map.on("move resize",redraw);map.on("zoomstart",function(){cv.style.display="none"});map.on("zoomend",redraw);
  function fmt(t){return t.toLocaleString([], {weekday:"short",hour:"numeric"})}
  function rel(t){var hh=Math.round((t.getTime()-Date.now())/3600000);if(Math.abs(hh)<=1)return"right now";if(hh<0)return-hh+" hrs ago";if(hh<48)return"in "+hh+" hrs";return"in "+Math.round(hh/24)+" days"}
  function legend(){var g=$("legend");if(layer=="radar"){function sw(p){return'<span class="sw">'+p.map(function(c){return'<i style="background:'+c+'"></i>'}).join("")+"</span>"}
      g.innerHTML="<span>Light → heavy:</span><span>Snow"+sw(SNOW)+"</span><span>Mix/ice"+sw(MIX)+"</span><span>Rain"+sw(RAIN)+"</span>"}
    else{g.innerHTML="<span>Snow piled up by the time shown:</span>"+SB.map(function(b,i){return'<span><i style="display:inline-block;width:12px;height:10px;background:'+SC[i]+';margin-right:3px"></i>'+b+'"</span>'}).join("")}}
  function show(f){frame=f;var fr=F[f];if(!fr)return;var t=fr.t;
    $("tmain").textContent=fmt(t);
    var hs=hiSrc(fr),src=hs?hs.name:G?(NB&&!NOK?"GFS 25 km (sharp layer loading…)":"GFS 25 km model"):"blended resort forecast";
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
    document.querySelectorAll("#seg button").forEach(function(x){x.classList.toggle("on",x===b)});legend();show(frame)}});
  map.on("click",function(e){if(!G||!G.radar)return;var fr=F[frame],la=e.latlng.lat,lo=e.latlng.lng,kinds={1:"Rain",2:"Snow",3:"Mixed or icy"},ints=["","very light","very light","light","light","moderate","moderate","heavy","very heavy"],vr=0,srcn="GFS model, ~25 km grid";
    function pick(ds,f,layer){var i=Math.round((lo-ds.m.west)/ds.dlon),j=Math.round((ds.m.north-la)/ds.dlat);if(i<0||j<0||i>=ds.m.nx||j>=ds.m.ny)return null;var gv=getv(ds,f,layer);return gv?gv.a[gv.base+j*ds.m.nx+i]:null}
    vr=null;if(fr.h>=0&&HD&&HD.chunks.length){for(var q=0;q<HDs.length;q++){var hv=pick(HDs[q],fr.h,"radar");if(hv!==null&&hv!==255){vr=hv;srcn="HRRR model, ~3 km";break}}}
    if(vr===null&&fr.nr>=0&&NOK){for(var q=0;q<NDs.length;q++){var nv=pick(NDs[q],fr.nr,"radar");if(nv!==null&&nv!==255){vr=nv;srcn="National Blend, ~2.5 km, 6-hr average";break}}}
    if(vr===null)vr=pick(G,fr.g,"radar")||0;
    var vs=null,ssrc="GFS";if(NOK&&(fr.n0>=0||fr.n1>=0)){for(var q=0;q<NDs.length;q++){var a0=fr.n0>=0?pick(NDs[q],fr.n0,"snow"):0,a1=fr.n1>=0?pick(NDs[q],fr.n1,"snow"):0;if(a0!==null&&a1!==null&&a0!==255&&a1!==255){vs=(a0*(1-fr.w)+a1*fr.w)/2;ssrc="National Blend";break}}}
    if(vs===null)vs=fr.g>=0?(pick(G,fr.g,"snow")||0)/2:0;
    var k=vr>>4,l=vr&15,nowtxt=vr&&l?kinds[k]+", "+ints[Math.min(l,8)]:"Nothing falling";
    L.popup().setLatLng(e.latlng).setContent("<b>"+fmt(fr.t)+"</b><br>"+nowtxt+'<br><span style="color:#888">'+srcn+"</span><br>Snow piled up from now ("+ssrc+"): "+(vs<0.5?"under 0.5":Math.round(vs*2)/2)+'"').openOn(map)});
  if(!G){$("seg").style.display="none";$("legend").textContent="Model map unavailable tonight; resort numbers still update."}
  legend();show(start);
  if(G){var jobs=[sprite("radar.png"),sprite("snowacc.png")];HDs.forEach(function(d){d.m.chunks.forEach(function(c){jobs.push(sprite(c.file))})});HDs.forEach(function(d){if(d.m.edge)sprite(d.m.edge).then(function(e){d.edge=e;redraw()})});
    Promise.all(jobs).then(function(a){G.radar=a[0];G.snow=a[1];var pos=2;HDs.forEach(function(d){d.chunks=a.slice(pos,pos+d.m.chunks.length);pos+=d.m.chunks.length;if(d.chunks.some(function(x){return!x}))d.chunks=[]});if(!G.radar){$("legend").textContent="Model map could not load."}show(frame);
      if(NDs.length){var nj=[];NDs.forEach(function(d){Object.keys(d.m.layers).forEach(function(k){d.m.layers[k].forEach(function(c,ci){nj.push(sprite(c.file).then(function(a){d.L[k][ci]=a}))})});if(d.m.edge)nj.push(sprite(d.m.edge).then(function(e){d.edge=e}))});
        Promise.all(nj).then(function(){NOK=NDs.every(function(d){return Object.keys(d.m.layers).every(function(k){var A=d.L[k];return A.length==d.m.layers[k].length&&A.every(Boolean)})});show(frame)})}})}
})();
</script>
<div id="lb" role="dialog" aria-label="Photo"><img alt=""><div></div></div>
<script>
(function(){var lb=document.getElementById("lb"),li=lb.querySelector("img"),lc=lb.querySelector("div");
document.addEventListener("click",function(e){var t=e.target;if(t.tagName==="IMG"&&t.hasAttribute("data-cap")){li.src=t.src;lc.textContent=t.getAttribute("data-cap");lb.classList.add("on")}else if(lb.classList.contains("on")&&lb.contains(t)){lb.classList.remove("on")}});
document.addEventListener("keydown",function(e){if(e.key==="Escape")lb.classList.remove("on")});
function ago(){var n=Date.now();document.querySelectorAll(".ago[data-t]").forEach(function(el){var t=Date.parse(el.getAttribute("data-t"));if(!t)return;var m=Math.round((n-t)/60000),s=m<1?"just now":m<60?m+" min ago":m<1440?Math.round(m/60)+" hr ago":Math.round(m/1440)+" days ago";
  el.textContent=new Date(t).toLocaleString([],{weekday:"short",hour:"numeric",minute:"2-digit"})+" · "+s})}ago();setInterval(ago,60000)})();
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
