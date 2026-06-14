#!/usr/bin/env python3
"""
google_saved_places_pipeline.py

One-shot pipeline for Google Maps saved places. Two stages in a single run:

  STAGE 1 — Per-list geocoding (always runs)
    Reads the per-list "Saved" Takeout CSVs in the input folder, geocodes each place
    via Google Places API (New) Text Search (region-biased per LIST_HINTS), and writes
    <List>.geojson / .kml / .gpx for each list.  Places whose URL already contains
    coordinates skip the API.

  STAGE 2 — Starred / visited recovery (only if 'Saved Places.json' is present)
    Google won't export your Starred list (private, non-shareable). If the merged
    'Saved Places.json' ("Maps (your places)" export) is in the input folder, this
    stage reconstructs Starred by elimination: every placed entry in the merged file
    that is NOT already in a per-list result (from Stage 1) is treated as Starred/
    visited, then reverse-geocoded (Nearby Search, tight radius) for a name + address.
    Writes Starred_visited.geojson / .kml / .gpx / .csv.

INPUTS (single folder, first argument):
    - per-list *.csv  (Takeout "Saved" export — select "Saved", not "Maps (your places)")
    - optionally 'Saved Places.json' (space or underscore) to trigger Stage 2

OUTPUT (second argument):
    - all per-list and Starred_visited files
    - .cache/ subfolder holds the API caches (re-run safe, avoids re-charging)

API KEY (safe to push to GitHub — key never in the script):
    ./google_api_key.txt  (one line), or $GOOGLE_API_KEY.  Gitignore the key file.

Requires: Places API (New) enabled, billing-enabled project. Field-masked calls keep
cost low; a full first run of a few hundred places is a small fraction of the monthly
free credit, and the cache makes re-runs near-free.

USAGE:
    python3 -m pip install requests --break-system-packages
    echo 'AIza...' > google_api_key.txt
    python3 google_saved_places_pipeline.py . ./output_places

KNOWN LIMITATION (Stage 1, unhinted global lists): places are geocoded BY NAME, so an
ambiguous name in a hint-less list (e.g. "San José") can resolve to the wrong country.
Add a region to LIST_HINTS for such lists, or resolve by the URL's ftid (future work).
"""

import os, sys, json, csv, math, time, glob, re
import requests

# ---------------------------------------------------------------- shared config
SLEEP        = 0.12
PLACES_TEXT  = "https://places.googleapis.com/v1/places:searchText"
PLACES_NEAR  = "https://places.googleapis.com/v1/places:searchNearby"
NEAR_RADIUS  = 25.0   # metres — Stage 2 tight match
KNOWN_LISTS  = ("Beer_gardens", "Favourite_places", "got_hates_flags",
                "Liverpool_Pubs", "Vietnam", "Want_to_go")

# Region bias per list for Stage 1 (free-text appended to the query). Edit to taste.
LIST_HINTS = {
    "Liverpool_Pubs": "Liverpool, UK",
    "Beer_gardens":   "Liverpool, UK",
    "Vietnam":        "Vietnam",
    # Want_to_go / Favourite_places: global, no hint
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
    if os.path.exists("google_api_key.txt"):
        return open("google_api_key.txt").read().strip()
    if os.path.exists(here):
        return open(here).read().strip()
    if os.environ.get("GOOGLE_API_KEY"):
        return os.environ["GOOGLE_API_KEY"].strip()
    sys.exit("No API key. Create google_api_key.txt or set GOOGLE_API_KEY.")

def esc(s):
    s = s or ""
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def hav(a, b, c, d):
    R = 6371000; p1, p2 = math.radians(a), math.radians(c)
    dp = math.radians(c-a); dl = math.radians(d-b)
    x = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(x))

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
        if m:
            return [float(m.group(2)), float(m.group(1))]  # [lon, lat]
    return None

def slug_name(url):
    m = place_slug_re.search(url or "")
    return m.group(1).replace("+", " ").strip() if m else None

def display_name_and_extra(p):
    title  = (p.get("name") or "").strip()
    note   = (p.get("note") or "").strip()
    gmatch = (p.get("google_match") or "").strip()
    is_coord = bool(title) and bool(_COORDISH.match(title)) and any(c in title for c in "°NSEW")
    if is_coord:
        return (note, f"Coordinates: {title}") if note else (title, "")
    return (gmatch or title), ""

# ---------------------------------------------------------------- Stage 1 writers
def write_kml(listname, feats, out):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<kml xmlns="http://www.opengis.net/kml/2.2">','<Document>',
             f'  <name>{esc(listname)}</name>']
    for f in feats:
        p = f["properties"]; lon, lat = f["geometry"]["coordinates"]
        dname, extra = display_name_and_extra(p)
        bits = []
        if p.get("google_address"): bits.append(esc(p["google_address"]))
        if extra:                   bits.append(esc(extra))
        if p.get("note") and not extra: bits.append(esc(p["note"]))
        if p.get("google_maps_url"):bits.append(esc(p["google_maps_url"]))
        parts += ['  <Placemark>', f'    <name>{esc(dname)}</name>',
                  f'    <description>{"&#10;".join(bits)}</description>',
                  f'    <Point><coordinates>{lon},{lat},0</coordinates></Point>', '  </Placemark>']
    parts += ['</Document>','</kml>']
    open(os.path.join(out, f"{listname}.kml"), "w", encoding="utf-8").write("\n".join(parts))

def write_gpx(listname, feats, out):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="google_saved_places_pipeline" '
             'xmlns="http://www.topografix.com/GPX/1/1" xmlns:osmand="https://osmand.net">']
    for f in feats:
        p = f["properties"]; lon, lat = f["geometry"]["coordinates"]
        dname, extra = display_name_and_extra(p)
        address = esc(p.get("google_address") or "")
        bits = []
        if extra:                   bits.append(esc(extra))
        if p.get("note") and not extra: bits.append("Note: " + esc(p["note"]))
        if p.get("google_maps_url"):bits.append(esc(p["google_maps_url"]))
        wpt = [f'  <wpt lat="{lat}" lon="{lon}">', f'    <name>{esc(dname)}</name>',
               f'    <desc>{" | ".join(bits)}</desc>', f'    <type>{esc(listname)}</type>']
        # No icon styling — set per-group "Default appearance" in OsmAnd.
        # Only address goes in an extension (populates OsmAnd's Address field).
        if address:
            wpt += ['    <extensions>', f'      <osmand:address>{address}</osmand:address>', '    </extensions>']
        wpt.append('  </wpt>'); parts += wpt
    parts += ['</gpx>']
    open(os.path.join(out, f"{listname}.gpx"), "w", encoding="utf-8").write("\n".join(parts))

# ---------------------------------------------------------------- Stage 1
def places_text(query, key, cache):
    if query in cache: return cache[query]
    try:
        r = requests.post(PLACES_TEXT,
            headers={"Content-Type":"application/json","X-Goog-Api-Key":key,
                     "X-Goog-FieldMask":"places.location,places.displayName,places.formattedAddress"},
            json={"textQuery": query, "maxResultCount": 1}, timeout=20)
        time.sleep(SLEEP)
        if r.status_code == 200 and r.json().get("places"):
            pl = r.json()["places"][0]; loc = pl["location"]
            res = {"lat":loc["latitude"],"lon":loc["longitude"],
                   "name":pl.get("displayName",{}).get("text"),"addr":pl.get("formattedAddress")}
        elif r.status_code == 200:
            res = None
        else:
            res = {"error": f"HTTP {r.status_code}: {r.text[:160]}"}
    except Exception as e:
        res = {"error": str(e)}
    cache[query] = res
    return res

def stage1(csv_folder, out, key, cache_dir):
    cache_path = os.path.join(cache_dir, "places_cache.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    review = []
    all_lists = {}
    files = glob.glob(os.path.join(csv_folder, "*.csv"))
    files += [f for f in glob.glob(os.path.join(csv_folder, "*")) if "homes_and" in os.path.basename(f)]
    for path in sorted(set(files)):
        listname = re.sub(r"[^A-Za-z0-9]+", "_", os.path.splitext(os.path.basename(path))[0]).strip("_")
        hint = LIST_HINTS.get(listname, "")
        feats = []
        for row in find_rows(path):
            title=(row.get("Title") or "").strip(); url=(row.get("URL") or "").strip(); note=(row.get("Note") or "").strip()
            if not title and not url: continue
            if "/shopping/" in url or "/product/" in url: continue
            props = {"name":title,"note":note,"google_maps_url":url,"list":listname}
            c = coords_from_url(url)
            if c:
                props["coord_source"]="url"
                feats.append({"type":"Feature","geometry":{"type":"Point","coordinates":c},"properties":props}); continue
            q = f"{title}, {hint}" if hint else (title or slug_name(url) or "")
            if not q:
                review.append([listname,title,"EMPTY QUERY","",url]); continue
            g = places_text(q, key, cache); json.dump(cache, open(cache_path,"w"))
            if g and "lat" in g:
                props.update(coord_source="google_places",google_match=g.get("name"),
                             google_address=g.get("addr"),query=q)
                feats.append({"type":"Feature","geometry":{"type":"Point","coordinates":[g["lon"],g["lat"]]},"properties":props})
            else:
                review.append([listname,title,f"NO MATCH ({g.get('error') if g else 'no result'})","",url])
        json.dump({"type":"FeatureCollection","features":feats},
                  open(os.path.join(out,f"{listname}.geojson"),"w"),indent=2,ensure_ascii=False)
        write_kml(listname, feats, out); write_gpx(listname, feats, out)
        all_lists[listname]=feats
        print(f"  {listname}: {len(feats)} features (.geojson + .kml + .gpx)")
    if review:
        with open(os.path.join(out,"_geocoding_review.csv"),"w",newline="",encoding="utf-8") as f:
            w=csv.writer(f); w.writerow(["list","title","matched_to","importance","url"]); w.writerows(review)
        print(f"  {len(review)} flagged for review -> {out}/_geocoding_review.csv")
    json.dump(cache, open(cache_path,"w"))
    return all_lists

# ---------------------------------------------------------------- Stage 2
def nearby(lat, lon, key, cache, cache_path):
    ck = f"{lat:.6f},{lon:.6f}"
    if ck in cache: return cache[ck]
    try:
        r = requests.post(PLACES_NEAR,
            headers={"Content-Type":"application/json","X-Goog-Api-Key":key,
                     "X-Goog-FieldMask":"places.displayName,places.location,places.formattedAddress"},
            json={"maxResultCount":1,"locationRestriction":{"circle":{
                "center":{"latitude":lat,"longitude":lon},"radius":NEAR_RADIUS}}}, timeout=20)
        time.sleep(SLEEP)
        if r.status_code==200 and r.json().get("places"):
            pl=r.json()["places"][0]
            res={"name":pl.get("displayName",{}).get("text"),"address":pl.get("formattedAddress")}
        else:
            res=None
    except Exception as e:
        res={"error":str(e)}
    cache[ck]=res; json.dump(cache,open(cache_path,"w"))
    return res

def stage2(merged_path, all_lists, out, key, cache_dir):
    cache_path = os.path.join(cache_dir, "nearby_cache.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    # confirmed names from the just-produced per-list features
    known=[]
    for feats in all_lists.values():
        for f in feats:
            lon,lat=f["geometry"]["coordinates"]
            if [lon,lat]!=[0,0]:
                known.append((lat,lon,f["properties"].get("google_match") or f["properties"].get("name")))
    def confirmed(lat,lon):
        for klat,klon,knm in known:
            if abs(klat-lat)<0.05 and abs(klon-lon)<0.05 and hav(lat,lon,klat,klon)<=50:
                return knm
        return None
    d=json.load(open(merged_path))
    placed=[f for f in d["features"] if f["geometry"]["coordinates"] not in ([0,0],[0.0,0.0])]
    recs=[]; nc=na=nn=0
    for f in placed:
        lon,lat=f["geometry"]["coordinates"]
        date=f["properties"].get("date","")[:10]; url=f["properties"].get("google_maps_url","")
        cn=confirmed(lat,lon)
        if cn:
            recs.append({"lat":lat,"lon":lon,"title":cn,"date":date,"url":url,"address":"","approx":False}); nc+=1
        else:
            g=nearby(lat,lon,key,cache,cache_path)
            if g and g.get("name"):
                recs.append({"lat":lat,"lon":lon,"title":g["name"],"date":date,"url":url,"address":g.get("address") or "","approx":True}); na+=1
            else:
                recs.append({"lat":lat,"lon":lon,"title":"Visited","date":date,"url":url,"address":"","approx":False}); nn+=1
    def desc(r):
        bits=[]
        if r["date"]:    bits.append(f"Visited {r['date']}")
        if r["address"]: bits.append(r["address"])
        if r["approx"]:  bits.append("Approximate location match")
        if r["url"]:     bits.append(r["url"])
        return " | ".join(bits)
    gj=[{"type":"Feature","geometry":{"type":"Point","coordinates":[r["lon"],r["lat"]]},
         "properties":{"name":r["title"],"date":r["date"],"address":r["address"],
                       "approximate":r["approx"],"google_maps_url":r["url"],"list":"Starred_visited"}} for r in recs]
    json.dump({"type":"FeatureCollection","features":gj},open(os.path.join(out,"Starred_visited.geojson"),"w"),indent=2,ensure_ascii=False)
    # GPX
    parts=['<?xml version="1.0" encoding="UTF-8"?>','<gpx version="1.1" creator="google_saved_places_pipeline" xmlns="http://www.topografix.com/GPX/1/1" xmlns:osmand="https://osmand.net">']
    for r in recs:
        parts+=[f'  <wpt lat="{r["lat"]}" lon="{r["lon"]}">',f'    <name>{esc(r["title"])}</name>',f'    <desc>{esc(desc(r))}</desc>','    <type>Starred_visited</type>']
        if r["address"]:
            parts+=['    <extensions>',f'      <osmand:address>{esc(r["address"])}</osmand:address>','    </extensions>']
        parts.append('  </wpt>')
    parts.append('</gpx>'); open(os.path.join(out,"Starred_visited.gpx"),"w").write("\n".join(parts))
    # KML
    parts=['<?xml version="1.0" encoding="UTF-8"?>','<kml xmlns="http://www.opengis.net/kml/2.2">','<Document>','  <name>Starred_visited</name>']
    for r in recs:
        parts+=['  <Placemark>',f'    <name>{esc(r["title"])}</name>',f'    <description>{esc(desc(r))}</description>',f'    <Point><coordinates>{r["lon"]},{r["lat"]},0</coordinates></Point>','  </Placemark>']
    parts+=['</Document>','</kml>']; open(os.path.join(out,"Starred_visited.kml"),"w").write("\n".join(parts))
    # CSV
    with open(os.path.join(out,"Starred_visited.csv"),"w",newline="",encoding="utf-8") as fh:
        w=csv.writer(fh); w.writerow(["Title","Note","URL","Tags","Comment"]); w.writerow(["","","","",""])
        for r in recs:
            nb=[]
            if r["date"]: nb.append(f"Visited {r['date']}")
            if r["address"]: nb.append(r["address"])
            if r["approx"]: nb.append("Approximate location match")
            w.writerow([r["title"]," | ".join(nb),r["url"],"",""])
    json.dump(cache,open(cache_path,"w"))
    print(f"  total placed: {len(recs)} | confirmed: {nc} | auto-named: {na} | unnamed 'Visited': {nn}")

# ---------------------------------------------------------------- main
def main():
    if len(sys.argv)!=3:
        sys.exit("Usage: python3 google_saved_places_pipeline.py <input_folder> <output_folder>")
    inp, out = sys.argv[1], sys.argv[2]
    key = load_key()
    os.makedirs(out, exist_ok=True)
    cache_dir = os.path.join(out, ".cache"); os.makedirs(cache_dir, exist_ok=True)

    print("STAGE 1 — per-list geocoding")
    all_lists = stage1(inp, out, key, cache_dir)

    merged = glob.glob(os.path.join(inp, "Saved[ _]Places.json"))
    if merged:
        print(f"STAGE 2 — Starred/visited recovery (found {os.path.basename(merged[0])})")
        stage2(merged[0], all_lists, out, key, cache_dir)
    else:
        print("STAGE 2 — skipped (no 'Saved Places.json' in input folder)")
    print("Done.")

if __name__ == "__main__":
    main()
