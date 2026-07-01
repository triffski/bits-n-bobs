#!/bin/bash
# Copies contents of this script's own folder into Ableton Live 12's App-Resources folder.
#
# Usage:
#   Keep this script in the same folder as your theme files, then run it.
#   - Preferred: right-click -> Open (not double-click) to run it in Terminal.
#   - Or from Terminal directly: bash ableton-themes-installer.command
#
# macOS 26 Gatekeeper:
#   Downloaded copies may refuse to double-click-run with "do not have
#   appropriate access privileges", even after chmod +x and xattr -c.
#   This is macOS's com.apple.provenance flag, not a permissions fault.
#   Cloning via git avoids the flag entirely; a plain browser/ZIP download
#   will not. Running via right-click -> Open or `bash` bypasses it either way.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Themes"

if [ ! -d "$SRC" ]; then
  echo "Source folder not found: $SRC" >&2
  exit 1
fi

if pgrep -x "Live" > /dev/null; then
  echo "Ableton Live is running — quit it before copying." >&2
  exit 1
fi

if [ ! -d "$DEST" ]; then
  echo "Destination not found: $DEST" >&2
  echo "Check your Ableton Live 12 version/install path." >&2
  exit 1
fi

echo "Copying '$SRC' -> '$DEST'"

if rsync -av --exclude="$(basename "$0")" --exclude="copy-ableton-theme.err" "$SRC"/ "$DEST"/ 2>/tmp/copy-ableton-theme.err; then
  echo "Done."
else
  echo "Permission denied — retrying with sudo..."
  sudo rsync -av --exclude="$(basename "$0")" --exclude="copy-ableton-theme.err" "$SRC"/ "$DEST"/
  echo "Done."
fi
