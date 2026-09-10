#!/usr/bin/env bash
# Daily DB backup. cron example (02:30):  30 2 * * * /opt/order-status-bot/backend/scripts/backup_db.sh
#
# MODE=auto (default) reads DATABASE_URL from backend/.env and picks mysqldump or a SQLite online
# backup. A plain file copy of a SQLite database is NOT safe while the bot runs (WAL keeps recent
# writes in a side file), so sqlite3's .backup is used instead.
set -euo pipefail
BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-/var/backups/order-status-bot}"
HOST="${MYSQL_HOST:-localhost}"; USER="${MYSQL_USER:-root}"; PASS="${MYSQL_PASSWORD:-root}"; DB="${MYSQL_DB:-order_bot}"
KEEP_DAYS="${KEEP_DAYS:-14}"
MODE="${MODE:-auto}"
mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"

url=""
[ -f "$BACKEND/.env" ] && url="$(grep -E '^[[:space:]]*DATABASE_URL[[:space:]]*=' "$BACKEND/.env" | tail -1 | sed -E 's/^[[:space:]]*DATABASE_URL[[:space:]]*=[[:space:]]*//')" || true
if [ "$MODE" = "auto" ]; then
  case "$url" in sqlite*) MODE=sqlite ;; *) MODE=mysql ;; esac
fi

if [ "$MODE" = "sqlite" ]; then
  path="$(printf '%s' "$url" | sed -E 's#^.*sqlite[^:]*:///##')"
  [ -n "$path" ] || path="order_bot.db"
  case "$path" in /*) ;; *) path="$BACKEND/$path" ;; esac
  [ -f "$path" ] || { echo "SQLite database not found at $path" >&2; exit 1; }
  FILE="$OUT_DIR/order_bot-$STAMP.db"
  python="$BACKEND/.venv/bin/python"; [ -x "$python" ] || python=python3
  "$python" -c 'import sqlite3,sys; src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()' "$path" "$FILE"
  gzip -f "$FILE"; FILE="$FILE.gz"
else
  FILE="$OUT_DIR/order_bot-$STAMP.sql.gz"
  mysqldump -h "$HOST" -u "$USER" -p"$PASS" --single-transaction --routines "$DB" | gzip > "$FILE"
fi

find "$OUT_DIR" \( -name 'order_bot-*.sql.gz' -o -name 'order_bot-*.db.gz' \) -mtime +"$KEEP_DAYS" -delete
echo "backup written: $FILE"
