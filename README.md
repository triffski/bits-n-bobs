# Bits 'n bobs

Somewhere to store random scripts, batch files and whatnot.

## Scripts & Batch Files

|Name|Description|
|----|-----------|
|google_saved_places_exporter.py|Convert stuff you've saved in your Google account and use it anywhere that can import GeoJSON, KML or CSV data. "Saved" list CSVs become one GeoJSON per list, with coordinates. <br/><br/> Stage 2 will also process a 'Saved Places.json' export ontaining *all* of your saved locations, then exclude any found in the .csv files, then reverse geocode the remainder to the nearest found location via the Maps API (within 25m).|

# Google Saved Places Exporter
  
### Getting your data from Google

The pipeline needs two inputs from **Google Takeout** (takeout.google.com), coming from two *different* Takeout products.  

The script also requires a Google API key, saved to `google_api_ket.txt`, whack this in a single line text file, in the same folder as the script.<br/>

Run: `python3 google_saved_places_pipeline.py {data location} {output folder}`</br>Example: `python3 google_saved_places_pipeline.py . ./output_places`

#### Per-list CSVs (required — Stage 1)
1. Go to Takeout, **Deselect all**, then select only **"Saved"**.
2. Export, download the zip, and unzip.
3. Inside the `Takeout/Saved/` folder you'll find **one CSV per list** (`Beer gardens.csv`,
   `Want to go.csv`, etc.) — these are the pipeline's main input.<br/>

⚠️ Select **"Saved"**, NOT "Maps (your places)" — the latter gives a single merged
JSON with no per-list split, which Stage 1 can't use.

#### Saved Places.json (optional — enables Stage 2, Starred/visited recovery)
Google won't export your **Starred** list directly (it's private/non-shareable), so
Stage 2 reconstructs it from the merged export by elimination.
1. In Takeout, **Deselect all**, then select **"Maps (your places)"**.
2. Export, download, unzip.
3. Inside `Maps (your places)/` is **`Saved Places.json`** — drop it in the input
   folder alongside the CSVs.

If `Saved Places.json` isn't present, Stage 2 is skipped.
| Input | If present | If missing |
|-------|-----------|------------|
| Per-list `*.csv` | Stage 1 geocodes each list → `.geojson/.kml/.gpx` | Stage 1 produces nothing (silent — check your input folder) |
| `Saved Places.json` (space or underscore) | Stage 2 reconstructs the Starred/visited list | Stage 2 skipped cleanly |
| `.cache/` | Lookups reused — run is near-free | Full API calls (writes cache for next time) |

#### Notes

- **Cache is cost-only.** Its absence never breaks a run; it just means that run pays full API price, then caches results for next time.
- **Stage dependency:** Stage 2 excludes places already matched in Stage 1's CSV output. If the merged file is present but the CSVs are **not**, Stage 2 has nothing to exclude against and will reverse-geocode the *entire* merged file as if all of it were Starred (no confirmed matches). Normal use (both present) avoids this.
- **Known limitation:** unhinted global lists are geocoded by name, so ambiguous names (e.g. "San José") can resolve to the wrong place. Add a region to `LIST_HINTS`, or spot-fix outliers after import.
