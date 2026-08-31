#!/bin/bash
# Sync LASSO Brain podcast transcripts (local, read-only corpus) to the echo worker volume.
# Run from the repo dir so the railway project link resolves. Idempotent: tar-overwrite.
set -euo pipefail
SRC="$HOME/LASSO/lasso-brain/podcast/transcripts"
DEST="/data/lasso-brain/podcast/transcripts"
cd /Users/blakeruff/lasso-echo-work
[ -d "$SRC" ] || { echo "source missing: $SRC"; exit 0; }
COUNT_LOCAL=$(ls "$SRC"/*.txt 2>/dev/null | wc -l | tr -d ' ')
[ "$COUNT_LOCAL" -gt 0 ] || { echo "no transcripts locally, skipping"; exit 0; }
tar -C "$SRC" -cz . | base64 | railway ssh --service echo -- bash -lc \
  "D=\$(cat); echo \"\$D\" | base64 -d > /tmp/brain_tr.tgz && mkdir -p $DEST && tar -xzf /tmp/brain_tr.tgz -C $DEST && rm -f /tmp/brain_tr.tgz && echo WORKER_COUNT=\$(ls $DEST/*.txt 2>/dev/null | wc -l)"
echo "LOCAL_COUNT=$COUNT_LOCAL"
