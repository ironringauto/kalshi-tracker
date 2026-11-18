"""Live conditions snapshot for every resort: resort cams, road cams, road status,
X posts from road agencies, avalanche danger. Everything is downloaded at run time
and saved into the output folder, because the published page cannot load other sites.
"""
import gzip, html, io, json, math, os, re, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
UTC = timezone.utc

# ---------------------------------------------------------------- per-resort sources
LCC = "LCC"
BCC = "BCC"
D3 = "https://cwwp2.dot.ca.gov/data/d3/cctv/image/{0}/{0}.jpg"
D9 = "https://cwwp2.dot.ca.gov/data/d9/cctv/image/{0}/{0}.jpg"
UT_X = [("UDOTcottonwoods", None), ("UtahDOT", r"Cottonwood|SR-?210|SR-?190|Wasatch Blvd|canyon")]
TAHOE_X = [("CHP_Truckee", None), ("CaltransDist3", r"I-?80|Hwy 80|Donner|Truckee|SR-?89|Hwy 89|chain|Tahoe")]
CO_X = [("ColoradoDOT", r"I-?70|US ?40|US ?6|Berthoud|Loveland|Vail Pass|Eisenhower|Rabbit Ears|CO ?119|CO ?82|"
                         r"Glenwood|Traction|Chain|mountain corridor|Boulder Canyon")]
SOURCES = {
    "snowbird": {"ots": "utah/snowbird", "road": [("cc", LCC)], "status": [("cc", LCC)], "x": UT_X},
    "brighton": {"ots": "utah/brighton-resort", "yt": "https://www.brightonresort.com/conditions", "road": [("cc", BCC)], "status": [("cc", BCC)], "x": UT_X},
    "solitude": {"ots": "utah/solitude-mountain-resort", "yt": "https://www.solitudemountain.com/mountain-and-village/webcams", "road": [("cc", BCC)], "status": [("cc", BCC)], "x": UT_X},
    "snowbasin": {"ots": "utah/snowbasin",
                  "road": [("udot", [(179250, "SR-226 Snowbasin Rd"), (178027, "Trappers Loop @ Snowbasin Rd"),
                                     (178920, "SR-39 @ Trappers Loop"), (179622, "I-84 @ Trappers Loop")])],
                  "status": [], "x": [("UtahDOT", r"Ogden|Weber|I-?84|Trappers|SR-?167|SR-?39|Snowbasin|Huntsville")]},
    "targhee": {"ots": "wyoming/grand-targhee-resort",
                "road": [("idaho", [(963, "SH-33 at WY/ID line"), (987, "SH-31 Pine Creek Pass")]),
                         ("wydot", ["WYO22TetonPass"])],
                "status": [("wydot", "Jackson", r"WY ?22|WYO ?22")],
                "x": [("IdahoITD", r"Teton|Driggs|Victor|SH-?33|SH-?31|Pine Creek|Swan Valley|US-?26|eastern Idaho")]},
    "jackson": {"ots": "wyoming/jackson-hole",
                "road": [("wydot", ["WYO22TetonPass", "WYO22Wilson", "WYO22WYO390Jct"])],
                "status": [("wydot", "Jackson", r"WY ?22|WYO ?22|WY ?390|US 26/89|US 191")],
                "x": [("IdahoITD", r"Teton Pass|Victor|SH-?33")]},
    "winterpark": {"ots": "colorado/winter-park-resort",
                   "road": [("cdot", [963, 964, 19488, 19546])],
                   "status": [("cdot", ["US 40", "I-70"], (39.55, -106.0, 40.0, -105.4))], "x": CO_X},
    "copper": {"ots": "colorado/copper-mountain-resort",
               "road": [("cdot", [20199, 19467, 20077, 19546])],
               "status": [("cdot", ["I-70", "CO 91"], (39.45, -106.3, 39.75, -105.85))], "x": CO_X},
    "abasin": {"ots": "colorado/arapahoe-basin-ski-area",
               "road": [("cdot", [19546, 20283, 19488]), ("cdotq", r"US ?6 .*(Loveland|Arapahoe)")],
               "status": [("cdot", ["I-70", "US 6"], (39.55, -106.0, 39.75, -105.7))], "x": CO_X},
    "steamboat": {"ots": "colorado/steamboat",
                  "road": [("cdot", [20386]), ("cdotq", r"US ?40 .*Rabbit Ears")],
                  "status": [("cdot", ["US 40"], (40.2, -107.0, 40.6, -106.3))], "x": CO_X},
    "eldora": {"ots": None,
               "road": [("cdotq", r"CO ?119 ")],
               "status": [("cdot", ["CO 119"], (39.9, -105.6, 40.05, -105.25))], "x": CO_X},
    "snowmass": {"ots": "colorado/aspen-snowmass",
                 "road": [("cdot", [1025, 1023]), ("cdotq", r"I-70 .*Glenwood Canyon|CO ?82 .*Aspen")],
                 "status": [("cdot", ["CO 82", "I-70"], (39.15, -107.4, 39.65, -106.8))], "x": CO_X},
    "palisades": {"ots": "california/palisades-tahoe",
                  "road": [("ct", [(D3.format("hwy89atolympicvalley"), "Hwy 89 at Olympic Valley"),
                                   (D3.format("hwy80atdonnersummit"), "I-80 at Donner Summit"),
                                   (D3.format("hwy89atalpinemeadows"), "Hwy 89 at Alpine Meadows"),
                                   (D3.format("hwy80athwy89"), "I-80 at Hwy 89, Truckee")])],
                  "status": [("chain", "d3", ["I-80", "SR-89"], 39.25, -120.25, 40)], "x": TAHOE_X},
    "sugarbowl": {"ots": "california/sugar-bowl-resort", "yt": "https://www.sugarbowl.com/webcams",
                  "imgpage": ("https://www.sugarbowl.com/webcams", r"//sugar3\.sugarbowl\.com/graphics/webcams/(?!royalgorge)[^\"']+\.jpg"),
                  "road": [("ct", [(D3.format("hwy80atdonnersummit"), "I-80 at Donner Summit"),
                                   (D3.format("hwy80atcastlepeak"), "I-80 at Castle Peak"),
                                   (D3.format("hwy80atkingvaleeb1"), "I-80 at Kingvale"),
                                   (D3.format("hwy80atdonnerlake"), "I-80 at Donner Lake")])],
                  "status": [("chain", "d3", ["I-80"], 39.3, -120.35, 40)], "x": TAHOE_X},
    "sierra": {"ots": "california/sierra-at-tahoe",
               "road": [("ct", [(D3.format("hwy50atechosummit"), "US-50 at Echo Summit"),
                                (D3.format("hwy50atsierraeb"), "US-50 at Sierra-at-Tahoe"),
                                (D3.format("hwy50attwinbridges"), "US-50 at Twin Bridges"),
                                (D3.format("hwy50atmeyers"), "US-50 at Meyers")])],
               "status": [("chain", "d3", ["US-50", "SR-89"], 38.8, -120.05, 30)],
               "x": [("CaltransDist3", r"US-?50|Hwy 50|Echo|Meyers|Twin Bridges|Strawberry|chain")]},
    "mammoth": {"ots": "california/mammoth-mountain-ski-area",
                "road": [("ct", [(D9.format("sr203mammothmountain"), "SR-203 at Mammoth Mountain"),
                                 (D9.format("us395obsidiandome"), "US-395 at Obsidian Dome"),
                                 (D9.format("us395crestview"), "US-395 at Crestview"),
                                 (D9.format("us395tomsplace"), "US-395 at Tom's Place")])],
                "status": [("chain", "d9", ["US-395", "SR-203"], 37.65, -118.95, 45)],
                "x": [("Caltrans9", r"395|203|Mammoth|chain|snow|closed|closure|traffic alert|road condition|storm")]},
}
CHAIN = {"R-0": "No chain controls", "R-1": "R1: chains required, snow tires OK",
         "R-2": "R2: chains required, except 4WD/AWD with snow tires",
         "R-3": "R3: chains required on all vehicles", "RC": "Road closed"}
CHAIN_RANK = {"R-0": 0, "R-1": 1, "R-2": 2, "R-3": 3, "RC": 4}
DANGER = {1: ("Low", "#50b848"), 2: ("Moderate", "#fff200"), 3: ("Considerable", "#f7941e"),
          4: ("High", "#ed1c24"), 5: ("Extreme", "#231f20")}


# ---------------------------------------------------------------- helpers
def http(url, timeout=25, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return b if raw else b.decode("utf-8", "ignore")


def clean(s):
    s = re.sub(r"<br\s*/?>|</p>", "\n", s or "")
    s = html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def km(a, b, c, d):
    return math.hypot((a - c) * 111.0, (b - d) * 111.0 * math.cos(math.radians(a)))


def save_jpg(url, path, width=720):
    """Download an image and save a web-sized JPEG. Returns True on success."""
    from PIL import Image
    b = http(url, timeout=25, raw=True)
    im = Image.open(io.BytesIO(b))
    im = im.convert("RGB")
    if im.width < 120 or im.height < 80:
        raise ValueError("image too small")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    im.save(path, "JPEG", quality=76, optimize=True, progressive=True)
    return True


# ---------------------------------------------------------------- fetchers
def ots_cams(slug, n=4, max_age_h=30):
    h = http(f"https://www.onthesnow.com/{slug}/webcams")
    cams = {}
    for m in re.finditer(r'https://ots-webcams\.s3\.amazonaws\.com/(\d+)/(\d+)/(\d{4}-\d\d-\d\d_\d{4})/la\.jpg"\s+alt="([^"]*)"', h):
        _, cid, ts, alt = m.groups()
        t = datetime.strptime(ts, "%Y-%m-%d_%H%M").replace(tzinfo=UTC)
        if cid not in cams or t > cams[cid]["t"]:
            cams[cid] = {"t": t, "url": m.group(0).split('"')[0].replace("/la.jpg", "/xl.jpg"),
                         "title": html.unescape(alt).replace("&#x27;", "'")}
    now = datetime.now(UTC)
    fresh = [c for c in cams.values() if now - c["t"] < timedelta(hours=max_age_h)]
    fresh.sort(key=lambda c: c["t"], reverse=True)
    # prefer cams that look like snow stakes / summits first, then the rest
    fresh.sort(key=lambda c: 0 if re.search(r"stake|summit|peak|top|bowl|mineral|tram|plot", c["title"], re.I) else 1)
    return fresh[:n]


def yt_cams(page):
    h = http(page)
    ids = list(dict.fromkeys(re.findall(r'youtube(?:-nocookie)?\.com/embed/([\w-]{11})', h)))
    out = []
    for vid in ids:
        title = ""
        try:
            title = json.loads(http(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json", timeout=15)).get("title", "")
        except Exception:
            continue  # private or removed stream
        out.append({"url": f"https://i.ytimg.com/vi/{vid}/maxresdefault_live.jpg", "alt": f"https://i.ytimg.com/vi/{vid}/hqdefault_live.jpg",
                    "title": title, "video": f"https://www.youtube.com/watch?v={vid}"})
    return out


def page_imgs(page, pat):
    h = http(page)
    return ["https:" + u if u.startswith("//") else u for u in dict.fromkeys(re.findall(pat, h))]


_ctv = {}
def caltrans_streams():
    if "m" not in _ctv:
        m = {}
        for dist in ("d3", "d9"):
            try:
                d = json.loads(http(f"https://cwwp2.dot.ca.gov/data/{dist}/cctv/cctvStatusD{int(dist[1:]):02d}.json", timeout=40))
                for x in d["data"]:
                    c = x["cctv"]; im = c.get("imageData", {})
                    if im.get("streamingVideoURL"):
                        m[im.get("static", {}).get("currentImageURL")] = im["streamingVideoURL"]
            except Exception:
                pass
        _ctv["m"] = m
    return _ctv["m"]


_cc_cache = {}
def cottonwood():
    if "v" in _cc_cache:
        return _cc_cache["v"]
    h = http("https://cottonwoodcanyons.udot.utah.gov/road-information/")
    cams = {LCC: [], BCC: []}
    for key, label in ((LCC, "Little Cottonwood Canyon (SR-210)"), (BCC, "Big Cottonwood Canyon (SR-190)")):
        i = h.find(f'x-acc-header-text">{label}')
        j = h.find("x-acc-header-text", i + 40)
        seg = h[i:j if j > 0 else i + 20000]
        for m in re.finditer(r'src="(https://www\.udottraffic\.utah\.gov/map/Cctv/\d+)"[^>]*>\s*<div class="camera-location">\s*([^<]+)', seg):
            cams[key].append((m.group(1), m.group(2).strip()))
    posts = []
    for sl in re.split(r'class="x-slide ', h)[1:]:
        sl = sl[: sl.find('class="x-slide ', 10) if 'class="x-slide ' in sl[10:] else len(sl)]
        tm = re.search(r"<strong>\s*([0-9]{1,2}:[0-9]{2}\s*[ap]m\s+[0-9]{1,2}/[0-9]{1,2})\s*</strong>", sl)
        if not tm:
            continue
        body = sl[tm.end():]
        body = body[: body.find("</div>")] if "</div>" in body else body
        txt = clean(body)
        img = re.search(r'<img[^>]+src="(https://cottonwoodcanyons\.udot\.utah\.gov/wp-content/uploads/[^"]+\.(?:jpe?g|png))"', sl)
        if txt:
            posts.append({"when": tm.group(1), "text": txt[:600], "img": img.group(1) if img else None})
    signs = []
    k = h.find("UDOT Signs at the Mouth")
    if k > 0:
        seg = h[k:k + 15000]
        for m in re.finditer(r'udot-message-signs">(.*?)</div>\s*</div>', seg, re.S):
            t = clean(m.group(1))
            if t:
                signs.append(t.replace("NO_MESSAGE", "No message"))
    _cc_cache["v"] = {"cams": cams, "posts": posts[:4], "signs": signs[:4]}
    return _cc_cache["v"]


_tip = {}
def udot_title(base, cid):
    if (base, cid) not in _tip:
        try:
            t = http(f"{base}/tooltip/Cameras/{cid}?lang=en", timeout=15)
            m = re.search(r'CameraTooltipDescriptionColumn[^>]*>\s*<strong>\s*([^<]+)', t)
            _tip[(base, cid)] = m.group(1).strip() if m else ""
        except Exception:
            _tip[(base, cid)] = ""
    return _tip[(base, cid)]


_wy = {}
def wydot_site(site):
    if site not in _wy:
        h = http(f"https://www.wyoroad.info/highway/webcameras/view?site={site}")
        _wy[site] = [("https://www.wyoroad.info" + m.group(1), html.unescape(m.group(2)))
                     for m in re.finditer(r'<img src="(/web-cam/cache\?ref=[^"]+)" class="photolarge"[^>]*alt="([^"]*)"', h)]
    return _wy[site]


_wyt = {}
def wydot_rows(town):
    if town not in _wyt:
        h = http(f"https://www.wyoroad.info/pls/Browse/WRR.TownResults?SelectedTown={urllib.parse.quote(town)}")
        rows, route = [], ""
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S):
            cells = [clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            if len(cells) < 4 or cells[0] == "Route":
                continue
            if len(cells) >= 6:
                route = cells[0] or route
                loc, cond, adv = cells[1], cells[2], cells[3]
                when = next((c for c in cells if re.search(r"\d{4}, \d", c)), "")
            else:
                loc, cond, adv = cells[0], cells[1], cells[2]
                when = next((c for c in cells if re.search(r"\d{4}, \d", c)), "")
            rows.append({"route": route, "loc": loc, "cond": cond, "adv": adv, "when": when})
        _wyt[town] = rows
    return _wyt[town]


_co = {}
def cdot_cams():
    if "c" not in _co:
        _co["c"] = json.loads(http("https://cotg.carsprogram.org/cameras_v1/api/cameras", timeout=40))
    return _co["c"]


def cdot_snapshot(view_url):
    m = re.search(r"/rtplive/([^/]+)/playlist\.m3u8", view_url)
    return f"https://cocam.carsprogram.org/Snapshots/{m.group(1)}.flv.png" if m else view_url


def cdot_events():
    if "e" not in _co:
        _co["e"] = json.loads(http("https://cotg.carsprogram.org/events_v1/api/eventReports", timeout=60))
    return _co["e"]


_cc3 = {}
def chain_status(dist):
    if dist not in _cc3:
        d = json.loads(http(f"https://cwwp2.dot.ca.gov/data/{dist}/cc/ccStatusD{int(dist[1:]):02d}.json", timeout=40))
        _cc3[dist] = [x["cc"] for x in d["data"]]
    return _cc3[dist]


_avy = {}
def avalanche_layer():
    if "m" not in _avy:
        _avy["m"] = json.loads(http("https://api.avalanche.org/v2/public/products/map-layer", timeout=40))
    return _avy["m"]


def in_poly(lat, lon, rings):
    inside = False
    for ring in rings:
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-12) + x1:
                inside = not inside
    return inside


def avalanche(lat, lon):
    best = None
    for f in avalanche_layer().get("features", []):
        g, p = f.get("geometry") or {}, f.get("properties") or {}
        polys = g.get("coordinates") or []
        if g.get("type") == "Polygon":
            polys = [polys]
        hit = any(in_poly(lat, lon, poly) for poly in polys)
        if hit:
            best = (f, p)
            break
        try:
            pts = [pt for poly in polys for ring in poly for pt in ring]
            d = min(km(lat, lon, pt[1], pt[0]) for pt in pts[::10])
            if d < 25 and (best is None or d < best[2]):
                best = (f, p, d)
        except ValueError:
            pass
    if not best:
        return None
    f, p = best[0], best[1]
    lvl = p.get("danger_level")
    name, color = DANGER.get(lvl, ("No rating yet", "#9aa5b1"))
    out = {"center": p.get("center"), "zone": p.get("name"), "level": lvl, "danger": name, "color": color,
           "advice": p.get("travel_advice"), "link": p.get("link"), "until": p.get("end_date"),
           "off": p.get("off_season")}
    try:
        prod = json.loads(http(f"https://api.avalanche.org/v2/public/product?type=forecast&center_id={p.get('center_id')}&zone_id={f.get('id')}", timeout=20))
        bl = clean(prod.get("bottom_line") or "")
        if bl:
            out["bottom_line"] = bl[:700]
        if prod.get("published_time"):
            out["published"] = prod["published_time"]
    except Exception:
        pass
    return out


_x = {}
def x_posts(handle):
    if handle in _x:
        return _x[handle]
    x = http(f"https://x.com/{handle}", timeout=30)
    x2 = x.replace("\\\\", "\x00").replace('\\"', '"').replace("\x00", "\\")
    out, seen = [], set()
    fts = list(re.finditer(r'"full_text":"((?:[^"\\]|\\.)*)"', x2))
    for k, ft in enumerate(fts):
        back = x2[max(0, ft.start() - 6000): ft.start()]
        ca = re.findall(r'"created_at_ms":(\d{12,})', back)
        if not ca:
            continue
        t = datetime.fromtimestamp(int(ca[-1]) / 1000, UTC)
        end = fts[k + 1].start() if k + 1 < len(fts) else ft.end() + 8000
        try:
            txt = json.loads('"' + ft.group(1) + '"')
        except Exception:
            txt = ft.group(1)
        txt = html.unescape(re.sub(r"\s*https://t\.co/\w+\s*$", "", txt)).strip()
        if txt.startswith("RT @") or txt[:60] in seen:
            continue
        seen.add(txt[:60])
        media = re.findall(r'"media_url_https":"(https://pbs\.twimg\.com/(?:media|amplify_video_thumb|ext_tw_video_thumb)/[^"]+)"', x2[ft.end():end])
        out.append({"handle": handle, "t": t, "text": txt, "media": media[0] if media else None})
    out.sort(key=lambda p: p["t"], reverse=True)
    _x[handle] = out
    return out


# ---------------------------------------------------------------- build
def build(out_dir, resorts_pos, log=print):
    """resorts_pos: {rid: (lat, lon)}. Writes live/*.jpg, returns {rid: {...}}."""
    ldir = os.path.join(out_dir, "live")
    os.makedirs(ldir, exist_ok=True)
    now = datetime.now(UTC)
    res = {}
    jobs = []  # (url, filename)

    def img(url, fname, alt=None):
        jobs.append((url, fname, alt))
        return "live/" + fname

    for rid, src in SOURCES.items():
        if rid not in resorts_pos:
            continue
        lat, lon = resorts_pos[rid]
        r = {"mtn": [], "road": [], "status": [], "signs": [], "x": [], "avy": None, "errors": []}
        # mountain cams
        if src.get("ots"):
            try:
                for i, c in enumerate(ots_cams(src["ots"])):
                    r["mtn"].append({"src": img(c["url"], f"{rid}_m{i}.jpg"), "title": c["title"], "t": c["t"].isoformat(),
                                     "link": f"https://www.onthesnow.com/{src['ots']}/webcams"})
            except Exception as e:
                r["errors"].append(f"resort cams: {e}")
        if src.get("yt") and len(r["mtn"]) < 4:
            try:
                for c in yt_cams(src["yt"]):
                    if len(r["mtn"]) >= 4:
                        break
                    i = len(r["mtn"])
                    r["mtn"].append({"src": img(c["url"], f"{rid}_m{i}.jpg", c["alt"]), "title": c["title"], "t": now.isoformat(),
                                     "video": c["video"], "live": True})
            except Exception as e:
                r["errors"].append(f"resort live streams: {e}")
        if src.get("imgpage") and len(r["mtn"]) < 4:
            try:
                for u in page_imgs(*src["imgpage"]):
                    if len(r["mtn"]) >= 4:
                        break
                    i = len(r["mtn"])
                    nm = re.sub(r"[-_\d.]+jpg$", "", u.rsplit("/", 1)[-1]) or "Resort cam"
                    r["mtn"].append({"src": img(u, f"{rid}_m{i}.jpg"), "title": re.sub(r"(?<=[a-z])(?=[A-Z])", " ", nm), "t": now.isoformat(),
                                     "link": src["imgpage"][0]})
            except Exception as e:
                r["errors"].append(f"resort page cams: {e}")
        # road cams
        n = 0
        for spec in src.get("road", []):
            try:
                kind = spec[0]
                if kind == "cc":
                    cams = cottonwood()["cams"][spec[1]]
                    pick = cams[1:] if len(cams) > 4 else cams
                    pick = [pick[i] for i in sorted({0, len(pick) // 3, 2 * len(pick) // 3, len(pick) - 1}) if i < len(pick)][::-1]
                    lst = [(u, t) for u, t in pick]
                elif kind == "udot":
                    lst = [(f"https://www.udottraffic.utah.gov/map/Cctv/{cid}", name) for cid, name in spec[1]]
                elif kind == "idaho":
                    lst = [(f"https://511.idaho.gov/map/Cctv/{cid}", name) for cid, name in spec[1]]
                elif kind == "wydot":
                    lst = []
                    for site in spec[1]:
                        lst += wydot_site(site)
                elif kind == "cdot":
                    by = {c["id"]: c for c in cdot_cams()}
                    lst = [(cdot_snapshot(by[i]["views"][0]["url"]), by[i]["name"], by[i]["views"][0]["url"]) for i in spec[1] if i in by and by[i].get("views")]
                elif kind == "cdotq":
                    lst = [(cdot_snapshot(c["views"][0]["url"]), c["name"], c["views"][0]["url"]) for c in cdot_cams()
                           if c.get("views") and c.get("active", True) and re.search(spec[1], c["name"], re.I)][:3]
                elif kind == "ct":
                    sm = caltrans_streams()
                    lst = [(u, t, sm.get(u)) for u, t in spec[1]]
                else:
                    lst = []
                for item in lst:
                    u, title = item[0], item[1]
                    vid = item[2] if len(item) > 2 and item[2] and ".m3u8" in item[2] else None
                    if n >= 4:
                        break
                    c = {"src": img(u, f"{rid}_r{n}.jpg"), "title": title, "t": now.isoformat(), "link": u}
                    if vid:
                        c["video"] = vid
                    r["road"].append(c)
                    n += 1
            except Exception as e:
                r["errors"].append(f"road cams {spec[0]}: {e}")
        # road status
        for spec in src.get("status", []):
            try:
                kind = spec[0]
                if kind == "cc":
                    v = cottonwood()
                    for p in v["posts"]:
                        r["status"].append({"when": p["when"], "text": p["text"], "src": "UDOT Cottonwood Canyons"})
                    r["signs"] = v["signs"]
                elif kind == "wydot":
                    for row in wydot_rows(spec[1]):
                        if re.search(spec[2], row["route"], re.I):
                            adv = "" if row["adv"] in ("None", "") else f" · {row['adv']}"
                            r["status"].append({"when": row["when"], "text": f"{row['route']} {row['loc']}: {row['cond']}{adv}", "src": "WYDOT"})
                elif kind == "cdot":
                    routes, (s, w, nn, e) = spec[1], spec[2]
                    seen = set()
                    wx = re.compile(r"Traction|Chain|Icy|Snow|Slush|Closed|Closure|Safety|Wet|Dry|Avalanche|Wind|Visibility", re.I)
                    evs = sorted(cdot_events(), key=lambda x: (0 if wx.search(json.dumps(x.get("eventDescription", {}))) else 1, x.get("priority", 9)))
                    for ev in evs:
                        loc, b = ev.get("location") or {}, ev.get("bounds") or {}
                        if loc.get("routeDesignator") not in routes:
                            continue
                        if b and (b.get("maxLat", 0) < s or b.get("minLat", 99) > nn or b.get("maxLon", 0) < w or b.get("minLon", 0) > e):
                            continue
                        d = ev.get("eventDescription") or {}
                        txt = clean(d.get("tooltip") or d.get("descriptionHeader") or "")
                        brief = clean(d.get("descriptionBrief") or "")
                        line = f"{txt} ({brief})" if brief and brief not in txt else txt
                        if line and line not in seen:
                            seen.add(line)
                            r["status"].append({"when": "", "text": line, "src": "CDOT COtrip"})
                        if len(seen) >= 5:
                            break
                elif kind == "chain":
                    _, dist, routes, clat, clon, rad = spec
                    worst = {}
                    for c in chain_status(dist):
                        l = c.get("location", {})
                        try:
                            d = km(clat, clon, float(l["latitude"]), float(l["longitude"]))
                        except (KeyError, ValueError):
                            continue
                        if l.get("route") not in routes or d > rad or c.get("inService") != "true":
                            continue
                        sd = c.get("statusData", {})
                        code = sd.get("status", "")
                        rank = CHAIN_RANK.get(code, -1)
                        key = l["route"]
                        if key not in worst or rank > worst[key][0]:
                            ts = sd.get("statusTimestamp", {})
                            worst[key] = (rank, code, l.get("locationName", ""), f'{ts.get("statusDate", "")} {ts.get("statusTime", "")}'.strip())
                    for route, (rank, code, where, ts) in sorted(worst.items()):
                        txt = CHAIN.get(code) if code in CHAIN else (code or "No report")
                        place = f" (worst at {where})" if rank > 0 else ""
                        r["status"].append({"when": ts, "text": f"{route}: {txt}{place}", "src": "Caltrans chain controls",
                                            "level": rank})
            except Exception as e:
                r["errors"].append(f"road status {spec[0]}: {e}")
        # X posts
        posts = []
        for handle, pat in src.get("x", []):
            try:
                for p in x_posts(handle):
                    if now - p["t"] > timedelta(days=4):
                        continue
                    if pat and not re.search(pat, p["text"], re.I):
                        continue
                    posts.append(p)
            except Exception as e:
                r["errors"].append(f"X @{handle}: {e}")
        posts.sort(key=lambda p: p["t"], reverse=True)
        for i, p in enumerate(posts[:3]):
            item = {"handle": p["handle"], "t": p["t"].isoformat(), "text": p["text"][:500],
                    "url": f"https://x.com/{p['handle']}"}
            if p.get("media") and i < 2:
                item["img"] = img(p["media"] + ("?name=small" if "pbs.twimg.com/media" in p["media"] else ""), f"{rid}_x{i}.jpg")
            r["x"].append(item)
        # avalanche
        try:
            r["avy"] = avalanche(lat, lon)
        except Exception as e:
            r["errors"].append(f"avalanche: {e}")
        res[rid] = r

    # download every image in parallel; drop the ones that fail
    ok = {}
    def dl(job):
        u, f, alt = job
        for url in [u] + ([alt] if alt else []):
            try:
                save_jpg(url, os.path.join(ldir, f))
                return f, True
            except Exception as e:
                err = str(e)[:80]
        return f, err
    with ThreadPoolExecutor(10) as ex:
        for f, st in ex.map(dl, jobs):
            ok[f] = st
    bad = 0
    for rid, r in res.items():
        for k in ("mtn", "road"):
            keep = []
            for c in r[k]:
                if ok.get(c["src"][5:]) is True:
                    keep.append(c)
                else:
                    bad += 1
            r[k] = keep
        for p in r["x"]:
            if "img" in p and ok.get(p["img"][5:]) is not True:
                p.pop("img")
        if r["errors"]:
            log(f"Live {rid}: " + "; ".join(r["errors"]))
    log(f"Live: {sum(1 for v in ok.values() if v is True)} photos saved, {bad} cams unavailable")
    # remove photos left over from earlier runs that this run did not refresh
    for f in os.listdir(ldir):
        if ok.get(f) is not True:
            try:
                os.remove(os.path.join(ldir, f))
            except OSError:
                pass
    return {"as_of": now.isoformat(), "resorts": res}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from snow import RESORTS
    out = sys.argv[1] if len(sys.argv) > 1 else "liveout"
    t0 = time.time()
    d = build(out, {p[0]: (p[3], p[4]) for p in RESORTS})
    json.dump(d, open(os.path.join(out, "live.json"), "w"), indent=1, default=str)
    for rid, r in d["resorts"].items():
        print(f"{rid:11s} mtn={len(r['mtn'])} road={len(r['road'])} status={len(r['status'])} x={len(r['x'])} avy={(r['avy'] or {}).get('danger')}")
    print(f"{time.time() - t0:.0f}s")
