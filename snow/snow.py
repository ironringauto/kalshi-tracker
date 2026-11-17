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
DAYS = 7
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
            for k in range(1, 6):
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
    call = "No standout powder morning in Utah this week."
    if home:
        best = max(((r, d) for r in home for d in r["days"]), key=lambda x: x[1]["score"])
        r, d = best
        if d["ovn"] >= 3:
            call = (f"Best Utah morning: {d['label']} at {r['name']}, ~{d['ovn']:.0f}\" overnight "
                    f"(range {d['ovn_lo']:.0f}-{d['ovn_hi']:.0f}\", {d['conf'] or '?'} confidence, score {d['score']}).")
            if d["date"] != home[0]["days"][0]["date"] and max(x["days"][0]["score"] for x in home) < d["score"] - 3:
                call += f" Tomorrow is weak by comparison. Save the day for {d['dow']}."
    return alerts, call, now_denver.weekday() == 6


# ---------- render ----------
def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def color(score):
    return "s3" if score >= 6 else "s2" if score >= 3.5 else "s1" if score >= 1 else "s0"


def render(data):
    ut = [r for r in data["resorts"] if r["region"] == "UT"]
    away = [r for r in data["resorts"] if r["region"] != "UT"]
    scale = max([12] + [d["ovn"] for r in ut for d in r["days"]])
    cards = []
    for r in ut:
        bars = []
        for d in r["days"]:
            h = max(2, round(d["ovn"] / scale * 100))
            nws_txt = f', NWS {d["nws_ovn"]}"' if d["nws_ovn"] is not None else ""
            tip = (f'{d["label"]}: {d["ovn"]}" overnight (models {d["ovn_lo"]}-{d["ovn_hi"]}"{nws_txt}), '
                   f'+{d["day"]}" during the day, score {d["score"]}')
            rng = f'{d["ovn_lo"]:.0f}-{d["ovn_hi"]:.0f}' if d["ovn_hi"] >= 1 else "&nbsp;"
            bars.append(f'<div class="bar" title="{esc(tip)}"><div class="v">{d["ovn"]:.0f}"</div>'
                        f'<div class="track"><div class="fill {color(d["score"])}" style="height:{h}%"></div></div>'
                        f'<div class="rng">{rng}</div><div class="dl">{d["dow"]}<br>{d["dnum"]}</div></div>')
        s = r["snotel"]
        depth = "base —"
        if s:
            depth = f'{s["depth_in"]:.0f}" depth at {s["station"].title()} SNOTEL'
            if s.get("chg24") is not None:
                depth += f' · {s["chg24"]:+.0f}" in 24h'
        d0 = r["days"][0]
        lvl = f' · snow level {d0["snow_level_ft"]:,} ft' if d0["snow_level_ft"] else ""
        cards.append(f'''<section class="card" id="{r["id"]}"><div class="ch"><div><h3>{esc(r["name"])}</h3>
<div class="sub">{esc(r["drive"] or "")} · {depth}</div></div><div class="badge {color(d0["score"])}">{d0["score"]}</div></div>
<div class="next">Next morning ({d0["label"]}): <b>{d0["ovn"]:.0f}"</b> overnight{lvl} · 5-day total ~{r["total5"]:.0f}"</div>
<div class="bars">{"".join(bars)}</div></section>''')
    rows = []
    for reg in ("WY", "CO", "CA"):
        rs = [r for r in away if r["region"] == reg]
        if not rs:
            continue
        rows.append(f'<tr class="rg"><td colspan="5">{REGION_NAMES[reg]}</td></tr>')
        for r in sorted(rs, key=lambda x: -x["total5"]):
            b = max(r["days"], key=lambda d: d["score"])
            s = r["snotel"]
            base = f'{s["depth_in"]:.0f}"' if s else "—"
            rows.append(f'<tr><td>{esc(r["name"])}</td><td class="n">{r["total5"]:.0f}"</td>'
                        f'<td>{b["label"]} · {b["ovn"]:.0f}"</td><td>{b["conf"] or "—"}</td>'
                        f'<td class="n">{base}</td></tr>')
    alerts = "".join(f'<div class="alert {a["level"]}">{esc(a["text"])}</div>' for a in data["alerts"]) \
        or '<div class="alert none">No alerts tonight.</div>'
    provo = esc(data["provo"]["text"]) if data.get("provo") else ""
    stamp = datetime.fromisoformat(data["generated"]).astimezone(ZoneInfo("America/Denver")).strftime("%a %b %-d, %-I:%M %p MT")
    models = ", ".join(data["sources_ok"])
    return TEMPLATE.format(stamp=stamp, call=esc(data["call"]), alerts=alerts, provo=provo,
                           cards="".join(cards), rows="".join(rows), models=esc(models),
                           warn=('<p class="warn">' + esc("; ".join(data["warnings"])) + "</p>") if data["warnings"] else "")


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
:root{{--bg:#f6f8fb;--card:#fff;--ink:#14202e;--mut:#5d6b7a;--line:#e3e8ef;--s0:#d5dbe3;--s1:#9cc3ea;--s2:#3f88d6;--s3:#1d3fa8;--warn:#b25b00}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0e141b;--card:#161f2a;--ink:#e8eef5;--mut:#93a1b1;--line:#253242;--s0:#2c3846;--s1:#3d6a96;--s2:#4f9be6;--s3:#9ec3ff;--bt:#0e141b}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:980px;margin:0 auto;padding:18px 14px 40px}}
header h1{{margin:0;font-size:22px;letter-spacing:-.01em}}
.stamp{{color:var(--mut);font-size:13px}}
.call{{margin:14px 0 8px;padding:14px 16px;background:var(--card);border:1px solid var(--line);border-left:4px solid var(--s2);border-radius:10px;font-size:16px}}
.alert{{padding:9px 12px;border-radius:8px;margin:6px 0;font-size:14px;background:var(--card);border:1px solid var(--line)}}
.alert.powder{{border-left:4px solid var(--s3);font-weight:600}}
.alert.storm{{border-left:4px solid var(--s2)}}
.alert.away{{border-left:4px solid var(--s1)}}
.alert.none{{color:var(--mut)}}
.provo{{color:var(--mut);font-size:14px;margin:10px 2px 0}}
h2{{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:26px 2px 10px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}}
.ch{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}}
.ch h3{{margin:0;font-size:17px}}
.sub{{color:var(--mut);font-size:13px}}
.badge{{min-width:38px;text-align:center;padding:4px 8px;border-radius:8px;font-weight:700;color:#fff}}
.badge.s0{{background:var(--s0);color:var(--ink)}}.badge.s1{{background:var(--s1)}}.badge.s2{{background:var(--s2)}}.badge.s3{{background:var(--s3);color:var(--bt,#fff)}}
.next{{font-size:13.5px;margin:8px 0 10px}}
.bars{{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;align-items:start}}
.bar{{text-align:center;font-size:11px}}
.bar .v{{font-weight:700;font-size:12.5px}}
.track{{height:74px;display:flex;align-items:flex-end;justify-content:center;background:linear-gradient(var(--line),var(--line)) bottom/100% 1px no-repeat}}
.fill{{width:70%;border-radius:4px 4px 0 0}}
.fill.s0{{background:var(--s0)}}.fill.s1{{background:var(--s1)}}.fill.s2{{background:var(--s2)}}.fill.s3{{background:var(--s3)}}
.rng{{color:var(--mut);font-size:10.5px}}
.dl{{color:var(--mut);margin-top:2px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:14px}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line)}}
th{{font-size:12px;color:var(--mut);font-weight:600}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
tr.rg td{{background:var(--bg);font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600}}
.read{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;font-size:14.5px}}
.links{{display:flex;flex-wrap:wrap;gap:8px}}
.links a{{padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);text-decoration:none;font-size:14px}}
.foot{{color:var(--mut);font-size:12.5px;margin-top:22px}}
.warn{{color:var(--warn);font-size:12px}}
.legend{{display:flex;gap:12px;flex-wrap:wrap;color:var(--mut);font-size:12px;margin:8px 2px 0}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}}
</style></head><body><div class="wrap">
<header><h1>Snow Tracker</h1><div class="stamp">Updated {stamp} · weather models + NWS only, no resort reports</div></header>
<div class="call">{call}</div>
{alerts}
<div class="provo">{provo}</div>

<h2>Utah: snow by morning (inches overnight, 4 pm to 9 am)</h2>
<div class="grid">{cards}</div>
<div class="legend"><span><i style="background:var(--s3)"></i>Score 6+ go</span><span><i style="background:var(--s2)"></i>3.5 to 6 good</span><span><i style="background:var(--s1)"></i>1 to 3.5 refresh</span><span><i style="background:var(--s0)"></i>under 1</span><span>Small numbers = low-high across forecast sources</span></div>

<h2>Trip resorts: next 5 days</h2>
<table><thead><tr><th>Resort</th><th style="text-align:right">5-day</th><th>Best morning</th><th>Confidence</th><th style="text-align:right">Depth (SNOTEL)</th></tr></thead><tbody>{rows}</tbody></table>

<h2>Forecaster's read (NWS Salt Lake City)</h2>
<div class="read"><!--FORECASTER_READ--></div>

<h2>Check it yourself</h2>
<div class="links">
<a href="https://radar.weather.gov/station/kmtx/standard">Live radar (Salt Lake)</a>
<a href="https://www.windy.com/-Snow-accumulation-snowAccu?snowAccu,40.60,-111.60,8">Snow map (Windy)</a>
<a href="https://forecast.weather.gov/product.php?site=SLC&amp;issuedby=SLC&amp;product=AFD">NWS forecast discussion</a>
<a href="https://utahavalanchecenter.org/forecast/salt-lake">Avalanche forecast</a>
<a href="https://udottraffic.utah.gov">UDOT roads and canyons</a>
<a href="https://opensnow.com/dailysnow/utah">OpenSnow Utah (other notes)</a>
</div>

<div class="foot">
<p><b>How to read it.</b> Bars are the best estimate of snow falling between 4 pm and 9 am before each morning: the official NWS snowfall forecast (set by forecasters) averaged with the median of the independent weather models. Sources this run: {models}. Model snow uses a snow ratio based on temperature at mid-mountain, so cold Utah storms come out fluffier than a flat 10:1. The score (0 to 10) starts from overnight snow and loses points for low model agreement, gusts over 35 mph (possible lift holds), above-freezing temps (heavy snow), and Utah weekends (crowds). Base depth comes from the nearest SNOTEL station, which is measured, not resort-reported. Resort snow reports are left out on purpose. Hover over or tap a bar for the model range and the NWS number.</p>
{warn}
</div>
</div></body></html>
"""


if __name__ == "__main__":
    main()
