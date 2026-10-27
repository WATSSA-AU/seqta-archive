#!/bin/bash
# SEQTA Archive - build the database from an export and put it in place.
#   bash linux/import.sh /path/to/au_wa_yourschool [--no-corr-fts]
# Copies the attachment files into /srv/seqta-archive/files and restarts the service.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then exec sudo bash "$0" "$@"; fi
EXPORT="${1:?usage: bash linux/import.sh /path/to/au_wa_yourschool [--no-corr-fts]}"
shift
DATA=/srv/seqta-archive
BIN=/opt/seqta-archive/venv/bin/seqta-archive
[ -x "$BIN" ] || { echo "Run linux/install.sh first" >&2; exit 1; }

NEED=$(du -sm "$EXPORT" | cut -f1)
FREE=$(df -Pm "$DATA" | awk 'NR==2 {print $4}')
echo "Export is ${NEED} MB; ${FREE} MB free in $DATA (needs roughly half the export size plus the files)."

echo "== building database (10-30 minutes for a large school)"
"$BIN" build --export "$EXPORT" --out "$DATA/seqta.db.new" "$@"

echo "== attachment files"
SRC_FILES="$(python3 -c 'import sqlite3,sys,os; c=sqlite3.connect(sys.argv[1]); print(os.path.join(dict(c.execute("select key,value from meta"))["source_export"],"files"))' "$DATA/seqta.db.new")"
if command -v rsync >/dev/null; then
  rsync -a --delete --exclude '.DS_Store' "$SRC_FILES/" "$DATA/files/"
else
  rm -rf "$DATA/files" && cp -a "$SRC_FILES" "$DATA/files"
fi

[ -f "$DATA/seqta.db" ] && mv -f "$DATA/seqta.db" "$DATA/seqta.db.prev"
mv "$DATA/seqta.db.new" "$DATA/seqta.db"
chown -R root:seqta-archive "$DATA"
chmod 0640 "$DATA/seqta.db"
find "$DATA/files" -type d -exec chmod 0750 {} +
find "$DATA/files" -type f -exec chmod 0640 {} +
systemctl restart seqta-archive
echo "Done. Previous database (if any) kept as $DATA/seqta.db.prev - delete it once you're happy."
