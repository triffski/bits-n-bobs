#!/usr/bin/env python3
"""
tr_facebook_exif_encombinator.py - Enrich a Facebook photo export with embedded metadata,
ready for Immich, preserving album structure.

Facebook strips EXIF on upload and keeps the real metadata (capture dates, captions, GPS)
in per-album sidecar JSON (your_facebook_activity/posts/album/N.json). This reads those
album files and writes the metadata INTO copies of the photos with exiftool, grouping the
output one folder per album so a downstream importer (immich-go `upload from-folder
--folder-as-album FOLDER`) recreates your albums by name, and Immich gets correct dates,
captions and source tags instead of dateless files stamped with today.

What it does:
  - album name (from N.json)      -> output subfolder, prefixed with the album's earliest
                                     photo date as "YYYY_MM_DD - Name", so albums sort
                                     chronologically by name in Immich. -> Immich album
                                     via --folder-as-album.
  - caption (photo title)         -> description
  - taken_timestamp (else upload) -> DateTimeOriginal / QuickTime CreateDate for video
  - album "From" date fallback    -> a photo with NO date of its own inherits the album's
                                     earliest photo date. Photos with their own date are
                                     never overwritten.
  - GPS                           -> if present in exif_data
  - tags                          -> facebook + a dated batch tag (default fb_2026_06),
                                     embedded so the whole batch stays cullable by tag
  - Optional album exclusion via --exclude-album (none excluded by default).
  - Fixes mislabelled files: media whose extension lies about the content (e.g. a JPEG
    named .webp/.heic) is renamed in the OUTPUT to the real format (magic-number sniff).
  - No comments: this export vintage doesn't include comment threads on album photos.

Note: album last_modified_timestamp is deliberately NOT used - Facebook bulk-restamps it
during backend migrations, so it's unreliable. The "From" date (earliest photo) is used
instead, derived from the trustworthy per-photo timestamps.

Input / output:
  --input   READ-ONLY. Point at the extracted export root (dir containing
            'your_facebook_activity'). Never written or renamed.
  --output  receives enriched COPIES, one subfolder per album. Use --clean to wipe first.

Requirements: Python 3.9+, exiftool on PATH  (macOS: `brew install exiftool`).

Examples:
    python3 tr_facebook_exif_encombinator.py --input /data/fb/_extracted --inspect
    python3 tr_facebook_exif_encombinator.py --input /data/fb/_extracted --output /data/out --dry-run --limit 5
    python3 tr_facebook_exif_encombinator.py --input /data/fb/_extracted --output /data/out --clean
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
SOURCE_TAG = "facebook"
DEFAULT_IMPORT_TAG = "fb_2026_06"   # override per-run with --import-tag
PROGRESS_EVERY = 100

ALBUM_GLOBS = ["**/posts/album/*.json", "**/album/*.json"]

# No albums excluded by default. Add names (case-insensitive) here or via --exclude-album.
EXCLUDE_ALBUMS = set()

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
TZ_OFFSET_HOURS = 0
ALBUM_NAME_MAXLEN = 120


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


def ts_to_date_prefix(ts):
    dt = datetime.datetime.utcfromtimestamp(int(ts)) + datetime.timedelta(hours=TZ_OFFSET_HOURS)
    return dt.strftime("%Y_%m_%d")


def sanitise_album(name, fallback):
    name = fix_mojibake(name or "").strip()
    if not name:
        return fallback
    for ch in '/\\:*?"<>|':
        name = name.replace(ch, "_")
    name = name.strip(". ")
    return (name[:ALBUM_NAME_MAXLEN] or fallback)


def album_folder_name(raw_name, from_ts, fallback):
    """Date-prefixed, filesystem-safe album folder name, e.g.
    '2010_03_17 - Hipstamatic Prints #1'. Undated albums get just the name."""
    base = sanitise_album(raw_name, fallback)
    if from_ts:
        return f"{ts_to_date_prefix(from_ts)} - {base}"
    return base


def gps_from(media):
    exif = dig(media, "media_metadata", "photo_metadata", "exif_data", default=None) \
        or dig(media, "media_metadata", "video_metadata", "exif_data", default=None) or []
    if exif:
        lat, lon = exif[0].get("latitude"), exif[0].get("longitude")
        if lat is not None and lon is not None and not (lat == 0 and lon == 0):
            return lat, lon
    return None, None


def photo_timestamp(photo):
    """Prefer the camera's original capture time; fall back to FB's post time. May be
    None (then the album From-date fallback applies)."""
    exif = dig(photo, "media_metadata", "photo_metadata", "exif_data", default=None) or []
    taken = exif[0].get("taken_timestamp") if exif else None
    return taken or photo.get("creation_timestamp")


def real_image_ext(path):
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


def corrected_ext(dst_file, src_file):
    real = real_image_ext(src_file)
    if real and dst_file.suffix.lower() != real:
        return dst_file.with_suffix(real), True
    return dst_file, False


def unique_path(p):
    if not p.exists():
        return p
    i = 1
    while True:
        cand = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def safe_clean(output, input_dir):
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


class Resolver:
    """Resolve a photo 'uri' to a real file. Direct path first; on miss, build a one-off
    basename index and match by longest trailing-path overlap."""
    def __init__(self, input_dir):
        self.input_dir = input_dir
        self.index = None

    def _build(self):
        self.index = {}
        for p in self.input_dir.rglob("*"):
            if p.is_file():
                self.index.setdefault(p.name, []).append(p)

    @staticmethod
    def _score(path, uri):
        a, b, n = path.parts, Path(uri).parts, 0
        while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
            n += 1
        return n

    def resolve(self, uri):
        direct = self.input_dir / uri
        if direct.exists():
            return direct
        if self.index is None:
            self._build()
        matches = self.index.get(Path(uri).name, [])
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        return max(matches, key=lambda m: self._score(m, uri))


# ----------------------------------------------------------------------------
# exiftool command
# ----------------------------------------------------------------------------
def build_exiftool_cmd(path, ts, lat, lon, description, import_tag):
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
# Discovery + album loading
# ----------------------------------------------------------------------------
def find_albums(input_dir):
    seen, files = set(), []
    for g in ALBUM_GLOBS:
        for p in input_dir.glob(g):
            if p.is_file() and p not in seen:
                seen.add(p)
                files.append(p)
    return sorted(files)


def load_albums(album_files, exclude):
    """Return (albums, excluded_names, parse_failures). Each album is
    (folder_name, photos, from_ts); from_ts is the earliest per-photo timestamp."""
    albums, excluded, failures = [], [], 0
    for af in album_files:
        data = load_json(af)
        if data is None:
            failures += 1
            continue
        raw = (data.get("name") if isinstance(data, dict) else "") or ""
        if exclude and fix_mojibake(raw).strip().lower() in exclude:
            excluded.append(fix_mojibake(raw).strip())
            continue
        photos = data.get("photos") if isinstance(data, dict) else (data or [])
        tss = [t for t in (photo_timestamp(p) for p in photos) if t]
        from_ts = min(tss) if tss else None
        folder = album_folder_name(raw, from_ts, af.stem)
        albums.append((folder, photos, from_ts))
    return albums, excluded, failures


# ----------------------------------------------------------------------------
# Inspect
# ----------------------------------------------------------------------------
def inspect(input_dir, album_files, exclude):
    print(f"\nInput:              {input_dir}")
    print(f"Album JSONs found:  {len(album_files)}")
    albums, excluded, _ = load_albums(album_files, exclude)
    if excluded:
        print(f"Excluded albums:    {', '.join(excluded)}")
    for folder, photos, _ in albums[:8]:
        print(f"  - {folder}  ({len(photos)} photos)")
    if not albums:
        print("\n  No albums to process. Check ALBUM_GLOBS / --exclude-album.")
        return

    res = Resolver(input_dir)
    folder, photos, from_ts = albums[0]
    print(f"\nFirst album folder: {folder!r}")
    print(f"album From date:    {ts_to_exif(from_ts) if from_ts else '(none)'}")
    if photos:
        p0 = photos[0]
        uri = p0.get("uri")
        resolved = res.resolve(uri) if uri else None
        ts = photo_timestamp(p0)
        cap = fix_mojibake(p0.get("title") or "")
        lat, lon = gps_from(p0)
        print("\nWhat would be extracted from album #1, photo #0:")
        print(f"  uri:        {uri}")
        print(f"  resolves:   {resolved}  exists={bool(resolved)}")
        print(f"  timestamp:  {ts} -> {ts_to_exif(ts) if ts else '(none, would use album From)'}")
        print(f"  caption:    {cap!r}")
        print(f"  gps:        {lat}, {lon}")
        print(f"  -> output:  <output>/{folder}/{Path(uri).name if uri else '?'}")
    print("\nIf that looks right, drop --inspect and run --dry-run --limit 5.\n")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Embed Facebook album JSON metadata into photo copies, ready for Immich.")
    ap.add_argument("-i", "--input", required=True,
                    help="Extracted export root (READ-ONLY), the dir containing your_facebook_activity")
    ap.add_argument("-o", "--output",
                    help="Destination dir for enriched copies (required unless --inspect)")
    ap.add_argument("--clean", action="store_true", help="Wipe the output dir before running")
    ap.add_argument("--inspect", action="store_true", help="Print detected schema and exit")
    ap.add_argument("--dry-run", action="store_true", help="Print actions, copy/write nothing")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N photos (0 = all)")
    ap.add_argument("--import-tag", default=DEFAULT_IMPORT_TAG,
                    help=f"Dated batch tag (default {DEFAULT_IMPORT_TAG})")
    ap.add_argument("--exclude-album", action="append", default=[],
                    help="Album name to skip (repeatable; case-insensitive). None excluded by default.")
    ap.add_argument("--verbose", action="store_true", help="Print each command as it runs")
    args = ap.parse_args()

    exclude = set(EXCLUDE_ALBUMS) | {a.strip().lower() for a in args.exclude_album}

    input_dir = Path(args.input).expanduser().resolve()
    if not input_dir.is_dir():
        sys.exit(f"FATAL: input not found: {input_dir}")

    if not args.inspect and not args.output:
        sys.exit("FATAL: --output is required (except with --inspect).")
    output = Path(args.output).expanduser().resolve() if args.output else None

    if not args.dry_run and not args.inspect and not shutil.which("exiftool"):
        sys.exit("FATAL: exiftool not found on PATH. Install with: brew install exiftool")

    album_files = find_albums(input_dir)

    if args.inspect:
        inspect(input_dir, album_files, exclude)
        return

    if not album_files:
        sys.exit("FATAL: no album JSONs found. Run --inspect and check ALBUM_GLOBS.")

    albums, excluded, failed = load_albums(album_files, exclude)
    total = sum(len(p) for _, p, _ in albums)
    display_total = min(args.limit, total) if args.limit else total
    msg = f"Found {total} photos across {len(albums)} albums"
    if excluded:
        msg += f" (excluded {len(excluded)}: {', '.join(excluded)})"
    if args.limit:
        msg += f" (processing first {display_total})"
    print(msg + "\n")

    if args.clean and not args.dry_run:
        safe_clean(output, input_dir)

    res = Resolver(input_dir)
    ok = skipped = renamed = fellback = processed = 0
    stop = False
    for folder, photos, album_from in albums:
        if stop:
            break
        for photo in photos:
            if args.limit and processed >= args.limit:
                stop = True
                break
            processed += 1

            uri = photo.get("uri")
            src_file = res.resolve(uri) if uri else None
            if not src_file:
                print(f"  !! missing source: {uri}", file=sys.stderr)
                skipped += 1
            else:
                own_ts = photo_timestamp(photo)
                ts = own_ts or album_from
                if not own_ts and album_from:
                    fellback += 1
                cap = fix_mojibake(photo.get("title") or "")
                lat, lon = gps_from(photo)

                dst = output / folder / Path(uri).name
                dst, did_rename = corrected_ext(dst, src_file)
                if not args.dry_run:
                    dst = unique_path(dst)
                cmd = build_exiftool_cmd(dst, ts, lat, lon, cap, args.import_tag)

                if args.dry_run or args.verbose:
                    flags = []
                    if did_rename:
                        flags.append(f"rename -> {dst.suffix}")
                    if not own_ts and album_from:
                        flags.append("date<-albumFrom")
                    tag = f"  ({', '.join(flags)})" if flags else ""
                    print(f"  [{folder}] {Path(uri).name} -> {dst.name}{tag}")
                    print("    " + " ".join(repr(a) if (" " in a or "\n" in a) else a for a in cmd))

                if args.dry_run:
                    ok += 1
                    if did_rename:
                        renamed += 1
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst)
                    if did_rename:
                        renamed += 1
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    if r.returncode == 0:
                        ok += 1
                    else:
                        failed += 1
                        print(f"  !! exiftool failed on {uri}: {r.stderr.strip()}", file=sys.stderr)

            if processed % PROGRESS_EVERY == 0:
                print(f"Processed: {processed} of {display_total} photos", flush=True)

    _summary(output, ok, skipped, failed, renamed, fellback, len(albums))
    if failed:
        sys.exit(1)


def _summary(output, ok, skipped, failed, renamed, fellback, n_albums):
    print(f"\n{'='*56}")
    print(f"Done.  written/ok: {ok}   skipped: {skipped}   failed: {failed}")
    print(f"       renamed to real ext: {renamed}   date<-album From: {fellback}   albums: {n_albums}")
    print(f"{'='*56}")
    print("Next:  immich-go upload from-folder --server=... --api-key=... "
          f"--folder-as-album FOLDER  {output}")


if __name__ == "__main__":
    main()
