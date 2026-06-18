#!/usr/bin/env python3
"""
google_saved_places_exporter.py

Three-stage pipeline for Google Maps saved places. One script, run top to bottom;
later stages are optional and prompted.

  STAGE 1 — Per-list geocoding (always runs)
    Reads the per-list "Saved" Takeout CSVs, geocodes each place via Google Places
    API (New) Text Search (region-biased per LIST_HINTS), writes <List>.geojson /
    .kml / .gpx. Places whose URL already contains coordinates skip the API and are
    EXACT. Places with only an ftid in the URL are geocoded BY NAME — accurate for
    distinctive names, but ambiguous names ("San José", "Hannover Bar") can resolve
    to the wrong namesake. These name-geocoded places are the "outliers" Stage 3 can
    fix.

  (after Stage 1) MISMATCH ECHO
    Lists name-geocoded places whose matched name differs sharply from what you saved
    — a quick heads-up of likely-suspect pins.

  STAGE 2 — Starred / visited recovery (prompted, only if 'Saved Places.json' present)
    Google won't export the Starred list. If the merged 'Saved Places.json' ("Maps
    (your places)" export) is in the input folder, reconstructs Starred by elimination
    against Stage 1's lists, then reverse-geocodes the remainder (Nearby Search).

  STAGE 3 — Resolve outliers via Maps URL (prompted; needs Playwright)
    For the name-geocoded places, opens each saved Maps URL in headless Chromium —
    which runs Maps' JS and resolves the ftid to Google's OWN coordinate — and compares
    to Stage 1's result. <=1km apart: confirmed, left alone. >1km: a name-geocode error;
    after showing the full list and one confirmation, corrects those pins IN PLACE
    (a timestamped backup is written first).

INPUTS  (folder, arg 1): per-list *.csv  (+ optionally 'Saved Places.json')
OUTPUT  (folder, arg 2): per-list + Starred_visited files; .cache/ holds caches.

API KEY: ./google_api_key.txt or $GOOGLE_API_KEY (gitignore the file).
Stage 3 needs no key (it browses, not API). Playwright imported lazily — Stages 1-2
run without it installed.

USAGE:
    python3 -m pip install requests --break-system-packages
    echo 'AIza...' > google_api_key.txt
    python3 google_saved_places_exporter.py . ./output_places
"""

import os, sys, json, csv, math, time, glob, re, shutil, tempfile, datetime
import requests

# ---------------------------------------------------------------- config
SLEEP        = 0.12
PLACES_TEXT  = "https://places.googleapis.com/v1/places:searchText"
PLACES_NEAR  = "https://places.googleapis.com/v1/places:searchNearby"
NEAR_RADIUS  = 25.0
OUTLIER_KM   = 1.0     # Stage 3: >this between name-geocode and Maps-resolved = correct
STAGE3_PACING= 2.0     # seconds between browser loads (be polite to Google)
KNOWN_LISTS  = ("Beer_gardens", "Favourite_places", "got_hates_flags",
                "Liverpool_Pubs", "Vietnam", "Want_to_go")

LIST_HINTS = {
    "Liverpool_Pubs": "Liverpool, UK",
    "Beer_gardens":   "Liverpool, UK",
    "Vietnam":        "Vietnam",
}

coord_patterns = [
    re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)"),
    re.compile(r"/maps/search/(-?\d+\.\d+),(-?\d+\.\d+)"),
    re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)"),
]
place_slug_re = re.compile(r"/maps/place/([^/@]+)")
_COORDISH     = re.compile(r'^[\d\s°\'".,NSEW+-]+$')

# ---------------------------------------------------------------- helpers
def load_key():
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "google_api_key.txt")
    if os.path.exists("google_api_key.txt"): return open("google_api_key.txt").read().strip()
    if os.path.exists(here): return open(here).read().strip()
    if os.environ.get("GOOGLE_API_KEY"): return os.environ["GOOGLE_API_KEY"].strip()
    sys.exit("No API key. Create google_api_key.txt or set GOOGLE_API_KEY.")

def esc(s):
    s = s or ""
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def hav(a, b, c, d):
    R=6371000; p1,p2=math.radians(a),math.radians(c)
    dp=math.radians(c-a); dl=math.radians(d-b)
    x=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(x))

def atomic_write_json(path, obj):
    """Write JSON atomically: temp file + rename, so a crash can't truncate it."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)            # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

def load_cache(path):
    """Load a cache; if it's corrupt, FAIL LOUD rather than silently wiping it."""
    if not os.path.exists(path): return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        sys.exit(f"ERROR: cache file '{path}' is corrupt. Inspect or delete it "
                 f"(deleting forces a clean rebuild), then re-run. Not overwriting it automatically.")

def atomic_write_text(path, text):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

def find_rows(path):
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        lines = list(csv.reader(f))
    for i, row in enumerate(lines):
        if row[:3] == ["Title", "Note", "URL"]:
            hdr = row
            return [dict(zip(hdr, r + [""]*(len(hdr)-len(r)))) for r in lines[i+1:]]
    return []

def coords_from_url(url):
    for rx in coord_patterns:
        m = rx.search(url or "")
        if m: return [float(m.group(2)), float(m.group(1))]
    return None

def slug_name(url):
    m = place_slug_re.search(url or "")
    return m.group(1).replace("+", " ").strip() if m else None

def display_name_and_extra(p):
    title=(p.get("name") or "").strip(); note=(p.get("note") or "").strip()
    gmatch=(p.get("google_match") or "").strip()
    is_coord = bool(title) and bool(_COORDISH.match(title)) and any(c in title for c in "°NSEW")
    if is_coord:
        return (note, f"Coordinates: {title}") if note else (title, "")
    return (gmatch or title), ""

# ---------------------------------------------------------------- writers
def write_list_files(listname, feats, out):
    json.dump({"type":"FeatureCollection","features":feats},
              open(os.path.join(out,f"{listname}.geojson"),"w"), indent=2, ensure_ascii=False)
    # KML
    parts=['<?xml version="1.0" encoding="UTF-8"?>','<kml xmlns="http://www.opengis.net/kml/2.2">',
           '<Document>',f'  <name>{esc(listname)}</name>']
    for f in feats:
        p=f["properties"]; lon,lat=f["geometry"]["coordinates"]
        dname,extra=display_name_and_extra(p); bits=[]
        if p.get("google_address"): bits.append(esc(p["google_address"]))
        if extra: bits.append(esc(extra))
        if p.get("note") and not extra: bits.append(esc(p["note"]))
        if p.get("google_maps_url"): bits.append(esc(p["google_maps_url"]))
        parts+=['  <Placemark>',f'    <name>{esc(dname)}</name>',
                f'    <description>{"&#10;".join(bits)}</description>',
                f'    <Point><coordinates>{lon},{lat},0</coordinates></Point>','  </Placemark>']
    parts+=['</Document>','</kml>']
    open(os.path.join(out,f"{listname}.kml"),"w",encoding="utf-8").write("\n".join(parts))
    # GPX
    parts=['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="google_saved_places_exporter" '
           'xmlns="http://www.topografix.com/GPX/1/1" xmlns:osmand="https://osmand.net">']
    for f in feats:
        p=f["properties"]; lon,lat=f["geometry"]["coordinates"]
        dname,extra=display_name_and_extra(p); address=esc(p.get("google_address") or ""); bits=[]
        if extra: bits.append(esc(extra))
        if p.get("note") and not extra: bits.append("Note: "+esc(p["note"]))
        if p.get("google_maps_url"): bits.append(esc(p["google_maps_url"]))
        wpt=[f'  <wpt lat="{lat}" lon="{lon}">',f'    <name>{esc(dname)}</name>',
             f'    <desc>{" | ".join(bits)}</desc>',f'    <type>{esc(listname)}</type>']
        if address:
            wpt+=['    <extensions>',f'      <osmand:address>{address}</osmand:address>','    </extensions>']
        wpt.append('  </wpt>'); parts+=wpt
    parts+=['</gpx>']
    open(os.path.join(out,f"{listname}.gpx"),"w",encoding="utf-8").write("\n".join(parts))

# ---------------------------------------------------------------- Stage 1
def places_text(query, key, cache, cache_path):
    if query in cache: return cache[query]
    try:
        r=requests.post(PLACES_TEXT,
            headers={"Content-Type":"application/json","X-Goog-Api-Key":key,
                     "X-Goog-FieldMask":"places.location,places.displayName,places.formattedAddress"},
            json={"textQuery":query,"maxResultCount":1},timeout=20)
        time.sleep(SLEEP)
        if r.status_code==200 and r.json().get("places"):
            pl=r.json()["places"][0]; loc=pl["location"]
            res={"lat":loc["latitude"],"lon":loc["longitude"],
                 "name":pl.get("displayName",{}).get("text"),"addr":pl.get("formattedAddress")}
        elif r.status_code==200: res=None
        else: res={"error":f"HTTP {r.status_code}: {r.text[:160]}"}
    except Exception as e:
        res={"error":str(e)}
    cache[query]=res; atomic_write_json(cache_path, cache)
    return res

def stage1(csv_folder, out, key, cache_dir):
    cache_path=os.path.join(cache_dir,"places_cache.json")
    cache=load_cache(cache_path); review=[]; all_lists={}
    files=glob.glob(os.path.join(csv_folder,"*.csv"))
    files+=[f for f in glob.glob(os.path.join(csv_folder,"*")) if "homes_and" in os.path.basename(f)]
    for path in sorted(set(files)):
        listname=re.sub(r"[^A-Za-z0-9]+","_",os.path.splitext(os.path.basename(path))[0]).strip("_")
        hint=LIST_HINTS.get(listname,""); feats=[]
        for row in find_rows(path):
            title=(row.get("Title") or "").strip(); url=(row.get("URL") or "").strip(); note=(row.get("Note") or "").strip()
            if not title and not url: continue
            if "/shopping/" in url or "/product/" in url: continue
            props={"name":title,"note":note,"google_maps_url":url,"list":listname}
            c=coords_from_url(url)
            if c:
                props["coord_source"]="url"
                feats.append({"type":"Feature","geometry":{"type":"Point","coordinates":c},"properties":props}); continue
            q=f"{title}, {hint}" if hint else (title or slug_name(url) or "")
            if not q: review.append([listname,title,"EMPTY QUERY","",url]); continue
            g=places_text(q,key,cache,cache_path)
            if g and "lat" in g:
                props.update(coord_source="google_places",google_match=g.get("name"),
                             google_address=g.get("addr"),query=q)
                feats.append({"type":"Feature","geometry":{"type":"Point","coordinates":[g["lon"],g["lat"]]},"properties":props})
            else:
                review.append([listname,title,f"NO MATCH ({g.get('error') if g else 'no result'})","",url])
        write_list_files(listname,feats,out); all_lists[listname]=feats
        print(f"  {listname}: {len(feats)} features")
    if review:
        atomic_write_text(os.path.join(out,"_geocoding_review.csv"),
            "list,title,matched_to,importance,url\n"+
            "\n".join(",".join('"'+str(c).replace('"','""')+'"' for c in r) for r in review))
        print(f"  {len(review)} flagged for review -> _geocoding_review.csv")
    atomic_write_json(cache_path, cache)
    return all_lists

# ---------------------------------------------------------------- mismatch echo
def echo_mismatches(all_lists):
    def words(s): return set(re.findall(r'[a-z0-9]{3,}',(s or '').lower()))
    rows=[]
    for ln,feats in all_lists.items():
        for f in feats:
            p=f["properties"]
            if p.get("coord_source")!="google_places": continue
            saved=p.get("name",""); got=p.get("google_match","")
            sw,gw=words(saved),words(got)
            if sw and gw and not (sw&gw):
                rows.append((ln,saved,got,p.get("google_address","")))
    if rows:
        print(f"\n  Heads-up: {len(rows)} name-geocoded place(s) matched a differently-named")
        print("  venue — possible wrong match (translations are usually fine):")
        for ln,saved,got,addr in rows[:25]:
            print(f'    [{ln}] "{saved[:22]}" -> "{got[:22]}" | {addr[:34]}')
        if len(rows)>25: print(f"    ...and {len(rows)-25} more")
        print("  (Stage 3 can resolve these precisely via their Maps URLs.)")

# ---------------------------------------------------------------- Stage 2
def nearby(lat, lon, key, cache, cache_path):
    ck=f"{lat:.6f},{lon:.6f}"
    if ck in cache: return cache[ck]
    try:
        r=requests.post(PLACES_NEAR,
            headers={"Content-Type":"application/json","X-Goog-Api-Key":key,
                     "X-Goog-FieldMask":"places.displayName,places.location,places.formattedAddress"},
            json={"maxResultCount":1,"locationRestriction":{"circle":{
                "center":{"latitude":lat,"longitude":lon},"radius":NEAR_RADIUS}}},timeout=20)
        time.sleep(SLEEP)
        if r.status_code==200 and r.json().get("places"):
            pl=r.json()["places"][0]
            res={"name":pl.get("displayName",{}).get("text"),"address":pl.get("formattedAddress")}
        else: res=None
    except Exception as e: res={"error":str(e)}
    cache[ck]=res; atomic_write_json(cache_path, cache)
    return res

def stage2(merged_path, all_lists, out, key, cache_dir):
    cache_path=os.path.join(cache_dir,"nearby_cache.json"); cache=load_cache(cache_path)
    known=[]
    for feats in all_lists.values():
        for f in feats:
            lon,lat=f["geometry"]["coordinates"]
            if [lon,lat]!=[0,0]:
                known.append((lat,lon,f["properties"].get("google_match") or f["properties"].get("name")))
    def confirmed(lat,lon):
        for klat,klon,knm in known:
            if abs(klat-lat)<0.05 and abs(klon-lon)<0.05 and hav(lat,lon,klat,klon)<=50: return knm
        return None
    d=json.load(open(merged_path))
    placed=[f for f in d["features"] if f["geometry"]["coordinates"] not in ([0,0],[0.0,0.0])]
    recs=[]; nc=na=nn=0
    for f in placed:
        lon,lat=f["geometry"]["coordinates"]; date=f["properties"].get("date","")[:10]; url=f["properties"].get("google_maps_url","")
        cn=confirmed(lat,lon)
        if cn: recs.append({"lat":lat,"lon":lon,"title":cn,"date":date,"url":url,"address":"","approx":False}); nc+=1
        else:
            g=nearby(lat,lon,key,cache,cache_path)
            if g and g.get("name"): recs.append({"lat":lat,"lon":lon,"title":g["name"],"date":date,"url":url,"address":g.get("address") or "","approx":True}); na+=1
            else: recs.append({"lat":lat,"lon":lon,"title":"Visited","date":date,"url":url,"address":"","approx":False}); nn+=1
    def desc(r):
        bits=[]
        if r["date"]: bits.append(f"Visited {r['date']}")
        if r["address"]: bits.append(r["address"])
        if r["approx"]: bits.append("Approximate location match")
        if r["url"]: bits.append(r["url"])
        return " | ".join(bits)
    gj=[{"type":"Feature","geometry":{"type":"Point","coordinates":[r["lon"],r["lat"]]},
         "properties":{"name":r["title"],"date":r["date"],"address":r["address"],
                       "approximate":r["approx"],"google_maps_url":r["url"],"list":"Starred_visited"}} for r in recs]
    json.dump({"type":"FeatureCollection","features":gj},open(os.path.join(out,"Starred_visited.geojson"),"w"),indent=2,ensure_ascii=False)
    parts=['<?xml version="1.0" encoding="UTF-8"?>','<gpx version="1.1" creator="google_saved_places_exporter" xmlns="http://www.topografix.com/GPX/1/1" xmlns:osmand="https://osmand.net">']
    for r in recs:
        parts+=[f'  <wpt lat="{r["lat"]}" lon="{r["lon"]}">',f'    <name>{esc(r["title"])}</name>',f'    <desc>{esc(desc(r))}</desc>','    <type>Starred_visited</type>']
        if r["address"]: parts+=['    <extensions>',f'      <osmand:address>{esc(r["address"])}</osmand:address>','    </extensions>']
        parts.append('  </wpt>')
    parts.append('</gpx>'); open(os.path.join(out,"Starred_visited.gpx"),"w").write("\n".join(parts))
    parts=['<?xml version="1.0" encoding="UTF-8"?>','<kml xmlns="http://www.opengis.net/kml/2.2">','<Document>','  <name>Starred_visited</name>']
    for r in recs:
        parts+=['  <Placemark>',f'    <name>{esc(r["title"])}</name>',f'    <description>{esc(desc(r))}</description>',f'    <Point><coordinates>{r["lon"]},{r["lat"]},0</coordinates></Point>','  </Placemark>']
    parts+=['</Document>','</kml>']; open(os.path.join(out,"Starred_visited.kml"),"w").write("\n".join(parts))
    with open(os.path.join(out,"Starred_visited.csv"),"w",newline="",encoding="utf-8") as fh:
        w=csv.writer(fh); w.writerow(["Title","Note","URL","Tags","Comment"]); w.writerow(["","","","",""])
        for r in recs:
            nb=[]
            if r["date"]: nb.append(f"Visited {r['date']}")
            if r["address"]: nb.append(r["address"])
            if r["approx"]: nb.append("Approximate location match")
            w.writerow([r["title"]," | ".join(nb),r["url"],"",""])
    atomic_write_json(cache_path, cache)
    print(f"  total placed: {len(recs)} | confirmed: {nc} | auto-named: {na} | unnamed 'Visited': {nn}")

# ---------------------------------------------------------------- Stage 3
def stage3(all_lists, out, cache_dir):
    # lazy import — Stages 1-2 work without Playwright installed
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\nPlaywright isn't installed. Run these:\n")
        print("  python3 -m pip install playwright --break-system-packages")
        print("  python3 -m playwright install chromium\n")
        print("Then re-run:")
        print(f"  python3 {os.path.basename(__file__)} {sys.argv[1]} {sys.argv[2]}\n")
        return

    cache_path=os.path.join(cache_dir,"url_resolve_cache.json")   # ISOLATED from Stage 1/2 caches
    cache=load_cache(cache_path)

    # gather name-geocoded outliers (coord_source == google_places) with a usable URL
    targets=[]
    for ln,feats in all_lists.items():
        for f in feats:
            p=f["properties"]
            if p.get("coord_source")=="google_places" and p.get("google_maps_url"):
                targets.append((ln,f))
    if not targets:
        print("  No name-geocoded places to resolve."); return
    est_min = max(1, len(targets) * 9 // 60)   # ~9s/place real-world (load + JS resolve + pacing)
    print(f"  Resolving {len(targets)} place(s) via Maps URLs.")
    print(f"  This is SLOW: each opens a real browser page and waits for Maps to resolve")
    print(f"  (~8-10s per place, depending on machine/connection). Rough estimate: ~{est_min} min.")
    print(f"  A quiet terminal is NORMAL — progress prints every 10. Do not kill it; if you do,")
    print(f"  it resumes from cache on re-run (nothing is lost or re-charged).")

    # Liverpool default map centre that Maps shows BEFORE a place resolves (and when a
    # place fails to resolve). Any result at/near this point is a non-resolution and
    # must be rejected, never treated as a real coordinate.
    DEFAULT_CENTRE = (53.35613, -2.88358)
    def _is_default(lat, lon):
        return abs(lat-DEFAULT_CENTRE[0]) < 0.01 and abs(lon-DEFAULT_CENTRE[1]) < 0.01

    # Bleed guard: if a resolve returns coordinates BYTE-IDENTICAL to the immediately
    # preceding resolve, the page didn't actually navigate (SPA state carried over) —
    # reject it. Real same-town clusters differ in the lower decimals, so this only
    # catches true bleed, never legitimate nearby places.
    prev = {"coord": None}

    def resolve_url(url, page):
        if url in cache:
            c=cache[url]
            # Don't trust poisoned/failed cache entries — re-resolve them.
            if c and "lat" in c and not _is_default(c["lat"], c["lon"]):
                return c
        res={"error":"no coords"}
        try:
            # Blank the tab FIRST so a previous place's @lat,lng can't bleed into this read.
            try: page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
            except Exception: pass
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            for sel in ('button:has-text("Accept all")','button:has-text("Reject all")','form[action*="consent"] button'):
                try:
                    b=page.locator(sel).first
                    if b.count(): b.click(timeout=3000); break
                except Exception: pass

            # The place is resolved when the URL contains the canonical !3d<lat>!4d<lon>
            # (the PLACE's coordinate), NOT the @lat,lng (which is just the viewport centre
            # and appears immediately as the Liverpool default). Poll for !3d!4d, and require
            # it to be STABLE for two consecutive reads before trusting it.
            got=None; last=None; stable=0
            for _ in range(25):
                page.wait_for_timeout(1000)
                m=re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", page.url) or \
                  re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", page.content())
                if m:
                    cur=(float(m.group(1)), float(m.group(2)))
                    if cur==last: stable+=1
                    else: stable=0; last=cur
                    if stable>=1:            # same value twice running = settled
                        got=cur; break
            # Reject the default centre (= didn't really resolve)
            if got and _is_default(got[0], got[1]):
                res={"error":"unresolved (default centre)"}
            elif got and prev["coord"] is not None and got == prev["coord"]:
                # byte-identical to previous resolve = page state bled, not a real result
                res={"error":"unresolved (identical to previous — suspected bleed)"}
            elif got:
                res={"lat":got[0],"lon":got[1]}
                prev["coord"]=got
            else:
                res={"error":"unresolved (no place coord)"}
        except Exception as e:
            res={"error":str(e)}
        cache[url]=res; atomic_write_json(cache_path, cache)   # incremental + atomic = resume-safe
        return res

    mismatches=[]   # (listname, feature, oldlat, oldlon, newlat, newlon, dist_km)
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(locale="en-US")
        for i,(ln,f) in enumerate(targets,1):
            url=f["properties"]["google_maps_url"]
            cached = url in cache
            r=resolve_url(url, page)
            if not cached: time.sleep(STAGE3_PACING)   # pace only real loads, not cache hits
            if r and "lat" in r:
                olon,olat=f["geometry"]["coordinates"]
                dist=hav(olat,olon,r["lat"],r["lon"])/1000.0
                if dist>OUTLIER_KM:
                    mismatches.append((ln,f,olat,olon,r["lat"],r["lon"],dist))
            if i%10==0: print(f"    ...{i}/{len(targets)}  ({datetime.datetime.now():%H:%M:%S})")
        browser.close()

    if not mismatches:
        print("  No mismatches >1km. Stage 1 geocoding matches Maps for every place. Nothing to correct.")
        return

    print(f"\n  {len(mismatches)} place(s) differ from Maps by >1km (likely name-geocode errors):\n")
    for ln,f,olat,olon,nlat,nlon,dist in mismatches:
        nm=f["properties"].get("name","")
        print(f'    [{ln}] "{nm[:24]:24s}"  {dist:7.0f}km off')
        print(f'           was {olat:.5f},{olon:.5f} -> Maps {nlat:.5f},{nlon:.5f}')
    print()
    ans=input(f"  Apply all {len(mismatches)} corrections IN PLACE? A backup is written first. [y/N] ").strip().lower()
    if ans!="y":
        print("  No changes made."); return

    # backup affected lists first
    stamp=datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir=os.path.join(out,f".backup-{stamp}"); os.makedirs(backup_dir,exist_ok=True)
    affected={ln for ln,*_ in mismatches}
    for ln in affected:
        for ext in ("geojson","gpx","kml","csv"):
            src=os.path.join(out,f"{ln}.{ext}")
            if os.path.exists(src): shutil.copy2(src, os.path.join(backup_dir,f"{ln}.{ext}"))
    print(f"  Backup -> {backup_dir}")

    # apply corrections to the in-memory features, then rewrite those lists
    for ln,f,olat,olon,nlat,nlon,dist in mismatches:
        f["geometry"]["coordinates"]=[nlon,nlat]
        f["properties"]["coord_source"]="maps_url_resolved"
        f["properties"]["resolved_from_name_geocode"]=True
    for ln in affected:
        write_list_files(ln, all_lists[ln], out)
    print(f"  Corrected {len(mismatches)} pin(s) in place across {len(affected)} list(s).")

# ---------------------------------------------------------------- main
def main():
    if len(sys.argv)!=3:
        sys.exit("Usage: python3 google_saved_places_exporter.py <input_folder> <output_folder>")
    inp,out=sys.argv[1],sys.argv[2]; key=load_key()
    os.makedirs(out,exist_ok=True); cache_dir=os.path.join(out,".cache"); os.makedirs(cache_dir,exist_ok=True)

    print("STAGE 1 — per-list geocoding")
    all_lists=stage1(inp,out,key,cache_dir)
    echo_mismatches(all_lists)

    # Stage 2 gate — default filename only (ignore the underscore rename), per user
    merged=os.path.join(inp,"Saved Places.json")
    if not os.path.exists(merged):
        print("\nSTAGE 2 — 'Saved Places.json' not found; skipping Starred/visited recovery.")
    else:
        ans=input("\nSTAGE 2 — 'Saved Places.json' found. Run Starred/visited recovery? [y/N] ").strip().lower()
        if ans=="y":
            print("STAGE 2 — Starred/visited recovery")
            stage2(merged, all_lists, out, key, cache_dir)
        else:
            print("  Skipped.")

    # Stage 3 gate
    ans=input("\nSTAGE 3 — resolve name-geocoded places precisely via their Maps URLs? [y/N] ").strip().lower()
    if ans=="y":
        print("STAGE 3 — Maps-URL resolution")
        stage3(all_lists, out, cache_dir)
    else:
        print("  Skipped.")
    print("\nDone.")

if __name__=="__main__":
    main()