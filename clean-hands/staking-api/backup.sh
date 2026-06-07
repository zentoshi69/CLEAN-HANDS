#!/usr/bin/env bash
#
# backup.sh — consistent, timestamped snapshot of the staking SQLite DB.
# Uses sqlite3's online .backup (safe while the API is running, WAL and all).
#
# Usage:
#   bash backup.sh                       # -> backups/staking-YYYYmmdd-HHMMSS.db
#   BACKUP_DIR=/mnt/vol bash backup.sh   # custom destination
#   KEEP=30 bash backup.sh               # keep the newest 30 (default 14)
#
# Cron it daily:
#   0 3 * * *  cd /home/youruser/bots/staking-api && bash backup.sh >> backup.log 2>&1
#
set -euo pipefail
cd "$(dirname "$0")"

DB="${STAKE_DB:-staking.db}"
DEST="${BACKUP_DIR:-backups}"
KEEP="${KEEP:-14}"

if [ ! -f "$DB" ]; then
  echo "No DB at $DB — nothing to back up." >&2
  exit 0
fi
command -v sqlite3 >/dev/null || { echo "sqlite3 not installed (apt install sqlite3)" >&2; exit 1; }

mkdir -p "$DEST"
OUT="$DEST/staking-$(date +%Y%m%d-%H%M%S).db"
sqlite3 "$DB" ".backup '$OUT'"
gzip -f "$OUT"
echo "Backed up -> $OUT.gz"

# Retention: keep the newest $KEEP, delete older.
ls -1t "$DEST"/staking-*.db.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "Retained newest $KEEP backups in $DEST/"
