#!/bin/bash
# Sync LASSO Brain podcast transcripts (local, read-only corpus) to the echo worker volume.
# DELTA sync: uploads only files missing on the worker, one at a time via stdin —
# immune to ARG_MAX no matter how large the corpus grows (the full-tarball version
# died with "Argument list too long" at ~141 files).
set -euo pipefail
SRC="$HOME/LASSO/lasso-brain/podcast/transcripts"
DEST="/data/lasso-brain/podcast/transcripts"
cd /Users/blakeruff/lasso-echo-work
[ -d "$SRC" ] || { echo "source missing: $SRC"; exit 0; }

REMOTE=$(railway ssh --service echo -- bash -lc "mkdir -p $DEST && ls $DEST" 2>/dev/null | grep '\.txt$' || true)

UPLOADED=0
for f in "$SRC"/*.txt; do
  name=$(basename "$f")
  if ! grep -qxF "$name" <<<"$REMOTE"; then
    base64 -i "$f" | railway ssh --service echo -- bash -lc \
      "base64 -d > '$DEST/$name.part' && mv '$DEST/$name.part' '$DEST/$name'" 2>/dev/null
    UPLOADED=$((UPLOADED+1))
  fi
done

COUNT_LOCAL=$(ls "$SRC"/*.txt 2>/dev/null | wc -l | tr -d ' ')
COUNT_REMOTE=$(railway ssh --service echo -- bash -lc "ls $DEST/*.txt 2>/dev/null | wc -l" 2>/dev/null | tr -d ' \r' | tail -1)
echo "uploaded=$UPLOADED local=$COUNT_LOCAL worker=$COUNT_REMOTE"
