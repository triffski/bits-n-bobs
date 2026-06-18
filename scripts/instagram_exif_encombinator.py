#!/usr/bin/env python3
"""
instagram_exif_encombinator.py - Enrich an Instagram export with embedded metadata, ready for Immich.

Instagram strips EXIF on upload and keeps the real metadata (post dates, captions, GPS)
in sidecar JSON. This reads `content/posts_*.json` and writes that metadata INTO copies
of the photos with exiftool, so a downstream importer (e.g. immich-go `upload
from-folder`) and Immich get correct dates, captions and source tags instead of a pile
of dateless JPEGs all stamped with today's date.

What it does (deliberately basic):
  - caption (post title)     -> description
  - post creation_timestamp  -> DateTimeOriginal  (IG keeps no original capture time)
  - GPS                      -> if present in exif_data (often stripped by IG)
  - tags                     -> instagram + a dated batch tag (default ig_2026_06)
  - Carousels: a post's caption + timestamp are fanned out across all its media.
  - Fixes Instagram's mislabelled files: media whose extension lies about the content
    (e.g. a JPEG named .webp or .heic) is renamed in the OUTPUT to match the real bytes,
    sniffed from magic numbers. Genuine WebP/HEIC/PNG files are left as-is. The source
    is never touched.
  - No albums   (Instagram has none; use your importer's "into album" option).
  - No comments (Instagram's export does not include comment threads on your posts).

Input / output:
  --input   is treated as READ-ONLY. Nothing is ever written or renamed there.
  --output  receives enriched COPIES, mirroring the source's media subfolder structure.
            Use --clean to wipe it first for a guaranteed fresh rebuild.

Requirements: Python 3.9+, exiftool on PATH  (macOS: `brew install exiftool`).

Examples:
    # Look at the export structure first (input only):
    python3 instagram_exif_encombinator.py --input /data/ig_export/json --inspect

    # Dry run a few items:
    python3 instagram_exif_encombinator.py --input /data/ig_export/json --output /data/out --dry-run --limit 5

    # Real run, wiping output first:
    python3 instagram_exif_encombinator.py --input /data/ig_export/json --output /data/out --clean
"""

import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
SOURCE_TAG = "instagram"
DEFAULT_IMPORT_TAG = "ig_2026_06"   # override per-run with --import-tag
PROGRESS_EVERY = 100                # print a progress line every N media

# posts content files, searched recursively under --input
POSTS_GLOBS = ["**/content/posts_*.json", "**/posts_*.json"]

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
TZ_OFFSET_HOURS = 0                 # FB/IG epochs are UTC; DateTimeOriginal is tz-naive


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def fix_mojibake(s):
    """Meta exports UTF-8 as escaped Latin-1, garbling accents/emoji. Recover it."""
    if not isinstance(s, str):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  !! could not parse {path}: {e}", file=sys.stderr)
        return None


def dig(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d


def ts_to_exif(ts):
    dt = datetime.datetime.utcfromtimestamp(int(ts)) + datetime.timedelta(hours=TZ_OFFSET_HOURS)
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def posts_from(data):
    """posts_*.json is normally a bare list; tolerate a dict wrapper too."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def gps_from(media):
    exif = dig(media, "media_metadata", "photo_metadata", "exif_data", default=None) \
        or dig(media, "media_metadata", "video_metadata", "exif_data", default=None) or []
    if exif:
        lat, lon = exif[0].get("latitude"), exif[0].get("longitude")
        if lat is not None and lon is not None and not (lat == 0 and lon == 0):
            return lat, lon
    return None, None


def detect_export_root(input_dir):
    """uris in posts_*.json resolve relative to the export root (the dir holding
    'media/'). Find it; fall back to input_dir."""
    for d in input_dir.rglob("media"):
        if d.is_dir():
            return d.parent
    return input_dir


def real_image_ext(path):
    """Sniff the true image format from magic bytes and return the matching extension
    (e.g. '.jpg'), or None if unknown. Instagram frequently mislabels JPEGs as .webp or
    .heic; exiftool refuses to write when the extension contradicts the content, so we
    rename the output copy to match reality. Genuine formats return their own extension
    (so no rename happens)."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
        return ".heic"
    return None


def corrected_output(dst_file, src_file):
    """Return the output path with its extension corrected to the real format if the
    name lies; otherwise the original dst_file. Returns (path, did_rename)."""
    real = real_image_ext(src_file)
    if real and dst_file.suffix.lower() != real:
        return dst_file.with_suffix(real), True
    return dst_file, False


def safe_clean(output, input_dir):
    """Wipe and recreate output, with guards so it can never delete the source or a
    filesystem root."""
    out, inp = output.resolve(), input_dir.resolve()
    if out == inp:
        sys.exit("FATAL: --clean refused: output equals input.")
    try:
        inp.relative_to(out)
        sys.exit("FATAL: --clean refused: input lives inside output; would delete the source.")
    except ValueError:
        pass
    if len(out.parts) <= 2:
        sys.exit(f"FATAL: --clean refused: {out} is too close to the filesystem root.")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"Cleaned output: {out}")


# ----------------------------------------------------------------------------
# exiftool command
# ----------------------------------------------------------------------------
def build_exiftool_cmd(path, ts, lat, lon, description, import_tag):
    # -m : ignore minor errors/warnings (belt-and-braces alongside the extension fix).
    cmd = ["exiftool", "-m", "-overwrite_original", "-charset", "UTF8", "-codedcharacterset=utf8"]
    is_video = path.suffix.lower() in VIDEO_EXTS

    if ts:
        d = ts_to_exif(ts)
        if is_video:
            cmd += ["-api", "QuickTimeUTC=1",
                    f"-QuickTime:CreateDate={d}", f"-QuickTime:ModifyDate={d}",
                    f"-FileModifyDate={d}"]
        else:
            cmd += [f"-DateTimeOriginal={d}", f"-CreateDate={d}",
                    f"-ModifyDate={d}", f"-FileModifyDate={d}"]

    if description:
        if is_video:
            cmd += [f"-QuickTime:Description={description}", f"-XMP-dc:Description={description}"]
        else:
            cmd += [f"-EXIF:ImageDescription={description}",
                    f"-XMP-dc:Description={description}",
                    f"-IPTC:Caption-Abstract={description}"]

    for kw in (SOURCE_TAG, import_tag):
        cmd += [f"-IPTC:Keywords+={kw}", f"-XMP-dc:Subject+={kw}"]

    if lat is not None and lon is not None:
        cmd += [f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}"]

    cmd.append(str(path))
    return cmd


# ----------------------------------------------------------------------------
# Load + count
# ----------------------------------------------------------------------------
def collect_posts(posts_files):
    """Pass 1: load every posts file once, return (all_posts, parse_failures)."""
    all_posts, failures = [], 0
    for pf in posts_files:
        data = load_json(pf)
        if data is None:
            failures += 1
            continue
        all_posts.extend(posts_from(data))
    return all_posts, failures


# ----------------------------------------------------------------------------
# Inspect
# ----------------------------------------------------------------------------
def inspect(input_dir, export_root, posts_files):
    print(f"\nInput:                {input_dir}")
    print(f"Detected export root: {export_root}")
    print(f"posts_*.json found:   {len(posts_files)}")
    for p in posts_files[:10]:
        print(f"  - {p.relative_to(input_dir)}")
    if not posts_files:
        print("\n  No posts files matched POSTS_GLOBS - check the patterns at the top.")
        return

    data = load_json(posts_files[0])
    posts = posts_from(data)
    print(f"\nPosts in first file: {len(posts)}")
    if posts:
        p0 = posts[0]
        print(f"Post-level keys: {sorted(p0.keys()) if isinstance(p0, dict) else type(p0)}")
        media = p0.get("media") or []
        print(f"media[] count (carousel size): {len(media)}")
        if media:
            print(f"media[0] keys: {sorted(media[0].keys())}")
            uri = media[0].get("uri")
            resolved = export_root / uri if uri else None
            cap = fix_mojibake(p0.get("title") or media[0].get("title") or "")
            ts = p0.get("creation_timestamp") or media[0].get("creation_timestamp")
            lat, lon = gps_from(media[0])
            print("\nWhat would be extracted from post #1, media #0:")
            print(f"  uri:        {uri}")
            print(f"  resolves:   {resolved}  exists={resolved.exists() if resolved else False}")
            print(f"  timestamp:  {ts} -> {ts_to_exif(ts) if ts else '(none)'}")
            print(f"  caption:    {cap!r}")
            print(f"  gps:        {lat}, {lon}")
    print("\nIf that looks right, drop --inspect and run --dry-run --limit 5.\n")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Embed Instagram JSON metadata into photo copies, ready for Immich.")
    ap.add_argument("-i", "--input", required=True,
                    help="Source export dir (READ-ONLY). The folder containing content/posts_*.json")
    ap.add_argument("-o", "--output",
                    help="Destination dir for enriched copies (required unless --inspect)")
    ap.add_argument("--clean", action="store_true",
                    help="Wipe the output dir before running, for a fresh rebuild")
    ap.add_argument("--inspect", action="store_true", help="Print detected schema and exit")
    ap.add_argument("--dry-run", action="store_true", help="Print actions, copy/write nothing")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N media (0 = all)")
    ap.add_argument("--import-tag", default=DEFAULT_IMPORT_TAG,
                    help=f"Dated batch tag (default {DEFAULT_IMPORT_TAG})")
    ap.add_argument("--verbose", action="store_true", help="Print each command as it runs")
    args = ap.parse_args()

    input_dir = Path(args.input).expanduser().resolve()
    if not input_dir.is_dir():
        sys.exit(f"FATAL: input not found: {input_dir}")

    if not args.inspect and not args.output:
        sys.exit("FATAL: --output is required (except with --inspect).")
    output = Path(args.output).expanduser().resolve() if args.output else None

    if not args.dry_run and not args.inspect and not shutil.which("exiftool"):
        sys.exit("FATAL: exiftool not found on PATH. Install with: brew install exiftool")

    export_root = detect_export_root(input_dir)

    seen, posts_files = set(), []
    for g in POSTS_GLOBS:
        for p in input_dir.glob(g):
            if p.is_file() and p not in seen:
                seen.add(p)
                posts_files.append(p)

    if args.inspect:
        inspect(input_dir, export_root, posts_files)
        return

    if not posts_files:
        sys.exit("FATAL: no posts_*.json found. Run --inspect and check POSTS_GLOBS.")

    # Pass 1: load posts and count total media for the progress readout.
    all_posts, failed = collect_posts(posts_files)
    total_media = sum(len(p.get("media") or []) for p in all_posts)
    display_total = min(args.limit, total_media) if args.limit else total_media
    print(f"Found {total_media} media across {len(all_posts)} posts"
          + (f" (processing first {display_total})" if args.limit else "") + "\n")

    if args.clean and not args.dry_run:
        safe_clean(output, input_dir)

    # Pass 2: copy + enrich.
    ok = skipped = renamed = processed = 0
    for post in all_posts:
        caption = fix_mojibake(post.get("title") or "")
        post_ts = post.get("creation_timestamp")

        for m in (post.get("media") or []):
            if args.limit and processed >= args.limit:
                break
            processed += 1

            uri = m.get("uri")
            if not uri:
                skipped += 1
            else:
                src_file = export_root / uri
                if not src_file.exists():
                    print(f"  !! missing source: {uri}", file=sys.stderr)
                    skipped += 1
                else:
                    ts = post_ts or m.get("creation_timestamp")
                    cap = caption or fix_mojibake(m.get("title") or "")
                    lat, lon = gps_from(m)

                    # Correct the output extension if IG mislabelled the file (e.g. a
                    # JPEG named .webp/.heic). Sniffed from SOURCE bytes so dry-run works.
                    dst_file = output / uri
                    final_dst, did_rename = corrected_output(dst_file, src_file)
                    cmd = build_exiftool_cmd(final_dst, ts, lat, lon, cap, args.import_tag)

                    if args.dry_run or args.verbose:
                        tag = f"  (rename -> {final_dst.suffix})" if did_rename else ""
                        print(f"  copy {uri} -> {final_dst.name}{tag}")
                        print("    " + " ".join(repr(a) if (" " in a or "\n" in a) else a for a in cmd))

                    if args.dry_run:
                        ok += 1
                        if did_rename:
                            renamed += 1
                    else:
                        final_dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, final_dst)
                        if did_rename:
                            renamed += 1
                        res = subprocess.run(cmd, capture_output=True, text=True)
                        if res.returncode == 0:
                            ok += 1
                        else:
                            failed += 1
                            print(f"  !! exiftool failed on {uri}: {res.stderr.strip()}",
                                  file=sys.stderr)

            if processed % PROGRESS_EVERY == 0:
                print(f"Processed: {processed} of {display_total} files", flush=True)

        if args.limit and processed >= args.limit:
            print("\n(reached --limit)")
            break

    _summary(output, ok, skipped, failed, renamed)
    if failed:
        sys.exit(1)


def _summary(output, ok, skipped, failed, renamed):
    print(f"\n{'='*48}")
    print(f"Done.  written/ok: {ok}   skipped: {skipped}   failed: {failed}   "
          f"renamed to real ext: {renamed}")
    print(f"{'='*48}")
    print("Next:  immich-go upload from-folder --server=... --api-key=... "
          f'--into-album "Instagram"  {output}')


if __name__ == "__main__":
    main()