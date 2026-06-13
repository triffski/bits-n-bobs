#!/usr/bin/env python3
"""
Convert Google Maps "Saved" list CSVs into one GeoJSON per list, with coordinates.

Strategy (free, no paid service, no API key):
  1. Parse coordinates directly from any URL that contains them (free, exact).
  2. Geocode the rest by place name via OpenStreetMap Nominatim (free, ~1 req/sec).
  3. Skip blank rows and non-place links (shopping/product URLs).
  4. Emit one <ListName>.geojson per input CSV, plus _geocoding_review.csv listing
     low-confidence / failed lookups to spot-check (per the export guide's advice).

Usage:
    pip install requests
    python3 saved_places_to_geojson.py /path/to/csv_folder /path/to/output_folder

Notes:
  - Nominatim usage policy: max 1 request/second, identifying User-Agent. Respected below.
  - Geocoding by NAME ALONE is weaker than by full address; generic pub names may
    resolve to the wrong city. Review the *_geocoding_review.csv output.
  - Re-run friendly: a geocode cache (geocode_cache.json) avoids re-querying on reruns.
"""

import csv, glob, json, os, re, sys, time, urllib.parse
import requests

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "triff-saved-places-converter/1.0 (personal use)"
SLEEP = 1.1  # seconds between Nominatim calls (policy: <=1/sec)

coord_search_re = re.compile(r"/maps/search/(-?\d+\.\d+),(-?\d+\.\d+)")
coord_at_re     = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")

def find_header_and_rows(path):
    """Some lists prepend a description line before the real header. Find it."""
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        lines = list(csv.reader(f))
    for i, row in enumerate(lines):
        if row[:3] == ["Title", "Note", "URL"]:
            header = row
            data = [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in lines[i+1:]]
            return data
    return []

def coords_from_url(url):
    for rx in (coord_search_re, coord_at_re):
        m = rx.search(url or "")
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            return lon, lat  # GeoJSON order: [lon, lat]
    return None

def load_cache(path):
    if os.path.exists(path):
        return json.load(open(path))
    return {}

def geocode(query, cache):
    if query in cache:
        return cache[query]
    params = {"q": query, "format": "jsonv2", "limit": 1}
    try:
        r = requests.get(NOMINATIM, params=params,
                         headers={"User-Agent": USER_AGENT}, timeout=20)
        time.sleep(SLEEP)
        if r.status_code == 200 and r.json():
            top = r.json()[0]
            result = {
                "lon": float(top["lon"]), "lat": float(top["lat"]),
                "importance": top.get("importance"),
                "display_name": top.get("display_name"),
                "type": top.get("type"),
            }
        else:
            result = None
    except Exception as e:
        result = {"error": str(e)}
    cache[query] = result
    return result

def main():
    if len(sys.argv) != 3:
        print("Usage: python3 saved_places_to_geojson.py <csv_folder> <output_folder>")
        sys.exit(1)
    csv_folder, out_folder = sys.argv[1], sys.argv[2]
    os.makedirs(out_folder, exist_ok=True)
    cache_path = os.path.join(out_folder, "geocode_cache.json")
    cache = load_cache(cache_path)
    review = []  # rows to spot-check

    # include the odd no-extension file too
    files = glob.glob(os.path.join(csv_folder, "*.csv"))
    files += [f for f in glob.glob(os.path.join(csv_folder, "*")) if "homes_and" in os.path.basename(f)]
    files = sorted(set(files))

    for path in files:
        listname = re.sub(r"[^A-Za-z0-9]+", "_", os.path.splitext(os.path.basename(path))[0]).strip("_")
        rows = find_header_and_rows(path)
        features = []
        for row in rows:
            title = (row.get("Title") or "").strip()
            url   = (row.get("URL") or "").strip()
            note  = (row.get("Note") or "").strip()
            if not title and not url:
                continue
            if "/shopping/" in url or "/product/" in url:
                continue  # non-place

            props = {"name": title, "note": note, "google_maps_url": url, "list": listname}

            # 1) free: coords embedded in URL
            c = coords_from_url(url)
            if c:
                props["coord_source"] = "url"
                features.append({"type": "Feature",
                                 "geometry": {"type": "Point", "coordinates": [c[0], c[1]]},
                                 "properties": props})
                continue

            # 2) geocode by name
            g = geocode(title, cache)
            if g and "lon" in g:
                imp = g.get("importance")
                props["coord_source"] = "nominatim"
                props["geocode_match"] = g.get("display_name")
                props["geocode_importance"] = imp
                features.append({"type": "Feature",
                                 "geometry": {"type": "Point", "coordinates": [g["lon"], g["lat"]]},
                                 "properties": props})
                # flag low-confidence for review
                if imp is None or imp < 0.4:
                    review.append([listname, title, g.get("display_name"), imp, url])
            else:
                review.append([listname, title, "NO MATCH", "", url])
            json.dump(cache, open(cache_path, "w"))  # persist as we go

        fc = {"type": "FeatureCollection", "features": features}
        out_path = os.path.join(out_folder, f"{listname}.geojson")
        json.dump(fc, open(out_path, "w"), indent=2, ensure_ascii=False)
        print(f"{listname}: {len(features)} features -> {out_path}")

    # write review file
    if review:
        rp = os.path.join(out_folder, "_geocoding_review.csv")
        with open(rp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["list", "title", "matched_to", "importance", "url"])
            w.writerows(review)
        print(f"\n{len(review)} entries flagged for review -> {rp}")
    json.dump(cache, open(cache_path, "w"))
    print("\nDone. Spot-check the review CSV before trusting low-confidence pins.")

if __name__ == "__main__":
    main()
