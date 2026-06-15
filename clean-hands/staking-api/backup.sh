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
umask 077  # the snapshot is the full ledger — never world-readable
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
# Verify the snapshot is sound BEFORE gzipping / pruning older good copies — a
# corrupt snapshot must never silently replace a valid one.
if ! sqlite3 "$OUT" "PRAGMA integrity_check" | grep -qx "ok"; then
  echo "Snapshot FAILED integrity_check — discarding it, keeping prior backups." >&2
  rm -f "$OUT"
  exit 1
fi
gzip -f "$OUT"
chmod 600 "$OUT.gz"
echo "Backed up -> $OUT.gz"

# Off-box copy (STRONGLY recommended — a box loss must not lose the ledger).
# Configure once:
#   apt install rclone && rclone config           # create a remote, e.g. 'b2'
#   For cleartext protection off-box, make it an rclone CRYPT remote (or
#   pre-encrypt with age/gpg) — each snapshot is the full money ledger.
#   echo 'BACKUP_RCLONE_REMOTE=b2:your-bucket/clean' >> ../.env
# Then the daily degen-backup.timer pushes each snapshot off-box automatically.
if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
  if command -v rclone >/dev/null; then
    rclone copy "$OUT.gz" "$BACKUP_RCLONE_REMOTE" --no-traverse
    echo "Pushed off-box -> $BACKUP_RCLONE_REMOTE"
  else
    echo "BACKUP_RCLONE_REMOTE set but rclone not installed (apt install rclone)" >&2
    exit 1
  fi
else
  echo "NOTE: BACKUP_RCLONE_REMOTE unset — snapshot is LOCAL only (no off-box copy)." >&2
fi

# Retention: keep the newest $KEEP, delete older.
ls -1t "$DEST"/staking-*.db.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "Retained newest $KEEP backups in $DEST/"
