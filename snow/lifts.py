"""Official lift + run status, straight from each resort's own feed or website.

fetch_all(log) -> {resort_id: {"lifts":[{n,s,t}], "runs":[{n,s,d}], "updated", "src", "link"}}
  status s: "open" | "hold" (on hold / delayed) | "sched" (scheduled to run, usually next day) | "closed"
build_status_file(out_dir, data, log) writes lifts.json for the page: counts, per-lift lists and
  per-feature status for the trail maps (matched by name to OpenSkiMap geometry in trailmaps/).
"""
import json, os, re, time, difflib, unicodedata, urllib.request, gzip
from datetime import datetime, timezone

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
HERE = os.path.dirname(os.path.abspath(__file__))


def http(u, timeout=30):
    req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or b[:2] == b"\x1f\x8b":
            b = gzip.decompress(b)
    return b.decode("utf-8", "replace")


def jget(u):
    return json.loads(http(u))


def norm_status(s):
    s = (s or "").strip().lower().replace("_", " ")
    if s.startswith("open") or s in ("o", "yes", "true", "running", "operating", "day open"):
        return "open"
    if any(k in s for k in ("schedul", "expected", "anticipat", "forecast")):
        return "sched"
    if any(k in s for k in ("hold", "delay", "standby", "wind", "patrol")):
        return "hold"
    return "closed"


def soup(u):
    from bs4 import BeautifulSoup
    return BeautifulSoup(http(u), "html.parser")


def _t(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


# ---------- per-platform fetchers ----------

def powdr(host):  # Snowbird, Copper, Eldora
    L = jget(f"https://api.{host}/api/v1/dor/drupal/lifts")
    T = jget(f"https://api.{host}/api/v1/dor/drupal/trails")
    lifts = [{"n": l["name"], "s": norm_status(l.get("status")), "t": l.get("type") or ""}
             for l in L if l.get("season") in ("winter", "all", None) and l.get("include", True) is not False]
    runs = [{"n": t["name"], "s": norm_status(t.get("status")), "d": t.get("difficulty") or ""}
            for t in T if t.get("type") in ("alpine_trail", "terrain_park_trail") and t.get("include", True)]
    up = max([l.get("updated") or 0 for l in L] + [t.get("updated") or 0 for t in T])
    return lifts, runs, datetime.fromtimestamp(up, timezone.utc).isoformat() if up else None


def mtnpowder(rid):  # Alterra: Solitude, Winter Park, A-Basin, Steamboat, Palisades, Mammoth
    d = jget(f"https://mtnpowder.com/feed?resortId={rid}")
    lifts, runs = [], []
    for a in d.get("MountainAreas") or []:
        for l in a.get("Lifts") or []:
            lifts.append({"n": l["Name"], "s": norm_status(l.get("StatusEnglish") or l.get("Status")), "t": l.get("LiftType") or ""})
        for t in a.get("Trails") or []:
            if t.get("Nordic") == "Yes":
                continue
            runs.append({"n": t["Name"], "s": norm_status(t.get("StatusEnglish") or t.get("Status")), "d": t.get("Difficulty") or ""})
    up = None
    try:
        up = datetime.strptime(d["LastUpdate"], "%Y-%m-%dT%H:%M:%S%z").isoformat()
    except Exception:
        pass
    return lifts, runs, up


def boyne(code):  # Brighton
    base = "https://apim-marketing-001.azure-api.net/FeedService/v1/Feed/Facilities/Areas/{}/All?resortName=" + code
    L = jget(base.format("Lifts")); T = jget(base.format("Trails"))
    lifts = [{"n": l["name"], "s": norm_status(l.get("status")), "t": l.get("type") or ""} for l in L]
    runs = [{"n": t["name"], "s": norm_status(t.get("status")), "d": t.get("difficulty") or ""} for t in T if not t.get("uphill")]
    return lifts, runs, None


def jackson():
    d = jget("https://www.jacksonhole.com/api/all.json")
    m = {"OPEN": "open", "DELAYED": "hold", "FORECAST": "sched", "CLOSED": "closed", "OUT_OF_PERIOD": "closed"}
    lifts = [{"n": v["name"], "s": m.get(v.get("openingStatus"), norm_status(v.get("openingStatus"))), "t": v.get("liftType") or ""}
             for v in (d.get("lifts") or {}).values()]
    runs = [{"n": v["name"], "s": m.get(v.get("openingStatus"), norm_status(v.get("openingStatus"))), "d": v.get("trailLevel") or ""}
            for v in (d.get("trails") or {}).values() if v.get("trailType") in (None, "DOWNHILL_SKIING", "SNOWPARK")]
    up = None
    try:
        up = datetime.fromisoformat(d["lastModified"]).isoformat()
    except Exception:
        pass
    return lifts, runs, up


def aspen(mtn):
    L = jget(f"https://www.aspensnowmass.com/aspensnowmass/liftstatus/feed?mountain={mtn}")
    G = jget(f"https://www.aspensnowmass.com/AspenSnowmass/GroomingReport/Feed?mountain={mtn}")
    lifts = [{"n": l["liftName"], "s": norm_status(l.get("status")), "t": l.get("type") or ""} for l in L.get("liftStatuses") or []]
    runs = [{"n": t["name"], "s": "open" if (t.get("isOpen") or t.get("isDayOpen")) else "closed", "d": t.get("difficulty") or ""}
            for a in G.get("areas") or [] for t in a.get("trails") or []]
    return lifts, runs, L.get("lastUpdated")


def snowbasin():
    s = soup("https://www.snowbasin.com/the-mountain/conditions")
    def st(tr):
        sp = tr.select_one(".mc__icon-open, .mc__icon-closed, .mc__icon-hold, .mc__icon-scheduled, .mc__icon-delayed")
        if sp:
            return norm_status(re.sub(r"^mc__icon-", "", [c for c in sp["class"] if c.startswith("mc__icon-")][0]))
        return norm_status(_t(tr.select_one(".sr-only:last-of-type")))
    lifts = []
    for tr in s.select(".mc-lifts__table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) >= 3:
            srs = tds[2].select_one(".sr-only")
            lifts.append({"n": _t(tds[0]), "s": norm_status(_t(srs).replace("Lift ", "")) if srs else st(tds[2]), "t": _t(tds[1].select_one(".sr-only"))})
    runs = []
    for tr in s.select(".mc-trails__table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            ic = [c for c in (tds[0].select_one(".mc__icon") or {}).get("class", []) if c.startswith("mc__icon-")]
            srs = tds[1].select_one(".sr-only")
            runs.append({"n": _t(tds[0]), "s": norm_status(_t(srs).replace("Trail ", "")) if srs else "closed",
                         "d": ic[0].replace("mc__icon-", "") if ic else ""})
    return lifts, runs, None


def targhee():
    s = soup("https://www.grandtarghee.com/the-mountain/cams-conditions/lifts-trails-report")
    lifts = [{"n": _t(a.select_one(".title")), "s": norm_status((a.select_one(".status") or {}).get("title")), "t": ""}
             for a in s.select(".node--type-lift.season-winter, .node--type-lift:not([class*=season-])")]
    runs = []
    for a in s.select(".node--type-trail"):
        lv = a.select_one(".level")
        name = _t(a.select_one(".title"))
        if lv:
            name = name.replace(_t(lv), "", 1).strip()
        runs.append({"n": name, "s": norm_status((a.select_one(".status") or {}).get("title")), "d": (lv or {}).get("title", "") if lv else ""})
    return lifts, runs, None


def sugarbowl():
    s = soup("https://www.sugarbowl.com/conditions")
    lifts, runs = [], []
    for dd in s.select(".lift_status_dropdown"):
        top = dd.select_one(".lift_status_top")
        if not top:
            continue
        name = _t(top.select_one(".fw_semi_bold"))
        stt = top.select_one('[data-update-id$="_status_text"]')
        if name:
            lifts.append({"n": name, "s": norm_status(_t(stt)), "t": ""})
        for row in dd.select(".run_info_row"):
            dif = (row.select_one(".run_difficulty_icon") or {}).get("data-difficulty", "")
            runs.append({"n": _t(row.select_one(".run_name")), "s": norm_status(_t(row.select_one(".run_status span"))),
                         "d": {"1": "easy", "2": "intermediate", "3": "advanced", "4": "expert"}.get(dif, dif)})
    seen, uniq = set(), []
    for r in runs:  # runs can be listed under more than one lift
        if r["n"] not in seen:
            seen.add(r["n"]); uniq.append(r)
    return lifts, uniq, None


def sierra():
    s = soup("https://www.sierraattahoe.com/lifts-trails-grooming/")
    lifts = []
    for h in s.select('[data-panel="lift-status"] h4'):
        nx = h.parent.find_next_sibling()
        lifts.append({"n": _t(h), "s": norm_status(_t(nx)), "t": ""})
    runs = []
    panel = s.select_one("#trail-status")
    if panel:
        toks = [x for x in panel.get_text("\n", strip=True).split("\n") if x.strip()]
        i = 0
        while i < len(toks) - 1:
            if toks[i + 1].lower() in ("open", "closed", "on hold", "hold", "delayed", "scheduled") and toks[i] not in ("Status",):
                runs.append({"n": toks[i], "s": norm_status(toks[i + 1]), "d": ""})
                i += 2
            else:
                i += 1
    return lifts, runs, None


SRC = {
    "snowbird": (lambda: powdr("snowbird.com"), "https://www.snowbird.com/the-mountain/mountain-report/lift-trail-report/"),
    "brighton": (lambda: boyne("br"), "https://brightonresort.com/conditions"),
    "solitude": (lambda: mtnpowder(65), "https://www.solitudemountain.com/mountain-and-village/conditions-and-maps"),
    "snowbasin": (snowbasin, "https://www.snowbasin.com/the-mountain/conditions"),
    "jackson": (jackson, "https://www.jacksonhole.com/lift-trail-status"),
    "targhee": (targhee, "https://www.grandtarghee.com/the-mountain/cams-conditions/lifts-trails-report"),
    "winterpark": (lambda: mtnpowder(5), "https://www.winterparkresort.com/the-mountain/mountain-report"),
    "copper": (lambda: powdr("coppercolorado.com"), "https://www.coppercolorado.com/the-mountain/trail-lift-info/winter-trail-report"),
    "abasin": (lambda: mtnpowder(93), "https://www.arapahoebasin.com/snow-report/"),
    "steamboat": (lambda: mtnpowder(6), "https://www.steamboat.com/the-mountain/mountain-report"),
    "eldora": (lambda: powdr("eldora.com"), "https://www.eldora.com/the-mountain/trail-lift-info/winter-trail-report"),
    "snowmass": (lambda: aspen("Snowmass"), "https://www.aspensnowmass.com/four-mountains/snowmass/lift-status"),
    "palisades": (lambda: mtnpowder(61), "https://www.palisadestahoe.com/mountain-information/lift-and-grooming-status"),
    "sugarbowl": (sugarbowl, "https://www.sugarbowl.com/conditions"),
    "sierra": (sierra, "https://www.sierraattahoe.com/lifts-trails-grooming/"),
    "mammoth": (lambda: mtnpowder(60), "https://www.mammothmountain.com/on-the-mountain/mountain-report"),
}
MAPLINK = {
    "snowbird": "https://www.snowbird.com/the-mountain/maps/",
    "brighton": "https://www.brightonresort.com/trail-maps",
    "solitude": "https://www.solitudemountain.com/mountain-and-village/conditions-and-maps",
    "snowbasin": "https://www.snowbasin.com/the-mountain/trail-maps/",
    "jackson": "https://www.jacksonhole.com/maps",
    "targhee": "https://www.grandtarghee.com/the-mountain/mountain-information/trail-maps",
    "winterpark": "https://www.winterparkresort.com/the-mountain/mountain-report",
    "copper": "https://www.coppercolorado.com/the-mountain/trail-area-maps/winter-trail-map",
    "abasin": "https://www.arapahoebasin.com/trail-maps/",
    "steamboat": "https://www.steamboat.com/the-mountain/trail-maps",
    "eldora": "https://www.eldora.com/the-mountain/maps/alpine-trail-map/",
    "snowmass": "https://www.aspensnowmass.com/four-mountains/snowmass/trail-maps",
    "palisades": "https://www.palisadestahoe.com/mountain-information/trail-maps",
    "sugarbowl": "https://www.sugarbowl.com/maps",
    "sierra": "https://www.sierraattahoe.com/trail-map/",
    "mammoth": "https://www.mammothmountain.com/on-the-mountain/winter-trail-map",
}
PLATFORM = {"snowbird": "resort API", "copper": "resort API", "eldora": "resort API", "brighton": "resort API",
            "jackson": "resort API", "snowmass": "resort API", "snowbasin": "resort site", "targhee": "resort site",
            "sugarbowl": "resort site", "sierra": "resort site"}


def fetch_one(rid):
    fn, link = SRC[rid]
    lifts, runs, up = fn()
    carpet = re.compile(r"carpet|conveyor|conveyer|magic|wonder ?carpet|sun ?kid", re.I)
    lifts = [l for l in lifts if l["n"] and not carpet.search(l["n"] + " " + str(l.get("t") or ""))]
    runs = [r for r in runs if r["n"]]
    if not lifts and not runs:
        raise ValueError("no lifts or runs parsed")
    return {"lifts": lifts, "runs": runs, "updated": up, "fetched": datetime.now(timezone.utc).isoformat(),
            "src": PLATFORM.get(rid, "resort feed"), "link": link}


def fetch_all(log=print, only=None):
    out, health = {}, {}
    for rid in SRC:
        if only and rid not in only:
            continue
        for attempt in (1, 2):
            try:
                out[rid] = fetch_one(rid); health[rid] = "ok"
                break
            except Exception as e:
                health[rid] = f"{type(e).__name__}: {str(e)[:120]}"
                if attempt == 1:
                    time.sleep(2)
        if health[rid] != "ok":
            log(f"  lifts: {rid} official source failed: {health[rid]}")
    return out, health


# ---------- name matching to map geometry ----------

STOP = {"run", "trail", "the", "lift", "chair", "chairlift", "express", "quad", "triple", "double", "six", "pack",
        "high", "speed", "detachable", "gondola", "tram", "aerial", "lower", "upper", "middle", "mid"}


def key(s, strict=True):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("'", "").replace("’", "")
    w = re.findall(r"[a-z0-9]+", s)
    if not strict:
        w = [x for x in w if x not in STOP]
    return " ".join(w)


def match(names, feats):
    """names: [{n,s}] official; feats: [{n}] map features. Returns list of status per feature (None = unknown)."""
    exact = {}
    loose = {}
    for o in names:
        exact.setdefault(key(o["n"]), []).append(o["s"])
        loose.setdefault(key(o["n"], False), []).append(o["s"])
    lk = [k for k in loose if k]
    res = []
    for f in feats:
        n = f.get("n") or ""
        if not n:
            res.append(None); continue
        k = key(n)
        ss = exact.get(k)
        if not ss:
            kl = key(n, False)
            ss = loose.get(kl) if kl else None
            if not ss and kl:
                c = difflib.get_close_matches(kl, lk, n=1, cutoff=0.86)
                ss = loose.get(c[0]) if c else None
        if not ss:
            res.append(None)
        else:  # if a name covers several segments (upper/lower), open wins, then hold
            res.append("open" if "open" in ss else "hold" if "hold" in ss else "sched" if "sched" in ss else "closed")
    return res


def summarize(d):
    c = lambda xs, s: sum(1 for x in xs if x["s"] == s)
    return {"lifts_open": c(d["lifts"], "open"), "lifts_hold": c(d["lifts"], "hold"), "lifts_sched": c(d["lifts"], "sched"), "lifts_total": len(d["lifts"]),
            "runs_open": c(d["runs"], "open"), "runs_total": len(d["runs"])}


RESORT_X = {"snowbird": "Snowbird", "brighton": "BrightonResort", "solitude": "SolitudeMtn", "snowbasin": "SnowbasinResort",
            "sugarbowl": "SugarBowlResort", "palisades": "PalisadesTahoe", "snowmass": "AspenSnowmass"}
# Checked Nov 2026: JacksonHole, GrandTarghee, CopperMtn, Arapahoe_Basin, SteamboatResort, MammothMountain, SierraAtTahoe
# have not posted in 1+ years; WinterParkResort / Eldora handles do not exist. Re-check occasionally.


def resort_x(log=print, max_age_h=72):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    try:
        import live
    except Exception as e:
        log(f"  resort X: {e}"); return {}, {}
    cut = datetime.now(timezone.utc) - timedelta(hours=max_age_h)
    def one(item):
        rid, h = item
        try:
            ps = [p for p in live.x_posts(h) if p["t"] >= cut][:3]
            return rid, [{"h": h, "t": p["t"].isoformat(), "text": p["text"][:400]} for p in ps], "ok"
        except Exception as e:
            return rid, [], f"{type(e).__name__}: {str(e)[:80]}"
    out, health = {}, {}
    with ThreadPoolExecutor(4) as ex:
        for rid, ps, st in ex.map(one, RESORT_X.items()):
            out[rid] = ps; health[rid] = st
    return out, health


def build_status_file(out_dir, data, log=print):
    """lifts.json: per resort -> summary, lift list, runs list, map status arrays (index-aligned to trailmaps/<id>.json)."""
    res = {"as_of": datetime.now(timezone.utc).isoformat(), "resorts": {}}
    xp, xh = resort_x(log)
    res["x_health"] = xh
    for rid, d in data.items():
        e = dict(summarize(d)); e.update({k: d[k] for k in ("updated", "fetched", "src", "link")})
        e["lifts"] = d["lifts"]; e["runs"] = d["runs"]
        tm = os.path.join(HERE, "trailmaps", rid + ".json")
        if os.path.exists(tm):
            g = json.load(open(tm))
            ms = {"runs": match(d["runs"], g["runs"]), "lifts": match(d["lifts"], g["lifts"])}
            code = {"open": "o", "hold": "h", "sched": "s", "closed": "c", None: "u"}
            e["map"] = {k: "".join(code[x] for x in v) for k, v in ms.items()}
            named = [i for i, f in enumerate(g["runs"]) if f.get("n")]
            hit = sum(1 for i in named if ms["runs"][i])
            e["map_match"] = f"{hit}/{len(named)} named runs, {sum(1 for x in ms['lifts'] if x)}/{len(g['lifts'])} lifts"
        e["x"] = xp.get(rid) or []
        e["x_handle"] = RESORT_X.get(rid)
        e["maplink"] = MAPLINK.get(rid)
        res["resorts"][rid] = e
    os.makedirs(out_dir, exist_ok=True)
    json.dump(res, open(os.path.join(out_dir, "lifts.json"), "w"), separators=(",", ":"))
    return res


if __name__ == "__main__":
    import sys
    data, health = fetch_all()
    r = build_status_file(sys.argv[1] if len(sys.argv) > 1 else "/tmp/liftsout", data)
    for rid, e in r["resorts"].items():
        print(f'{rid:11} lifts {e["lifts_open"]:>2}/{e["lifts_total"]:<3} runs {e["runs_open"]:>3}/{e["runs_total"]:<4} {e.get("map_match","")}  upd {e["updated"]}')
    print({k: v for k, v in health.items() if v != "ok"})
