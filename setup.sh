#!/usr/bin/env bash
# Altium DbLib server helper.
#   ./setup.sh up        start SQL Server, wait until healthy
#   ./setup.sh init      create database, logins, template table, helper proc (re-runnable)
#   ./setup.sh mirror    copy Celestial tables in (extra args go to mirror.py: --list, --only X)
#   ./setup.sh drop-raw  drop the raw z_ tables an older mirror copied (asks first; --yes skips that)
#   ./setup.sh backup    full backup to ./backup/
#   ./setup.sh sql       interactive sqlcmd as sa
#   ./setup.sh ui        build + start the Parts Manager web UI (needs PARTSUI_PASSWORD)
#   ./setup.sh all       up + init + mirror + ui
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "No .env. Run: cp .env.example .env && chmod 600 .env, then edit it."; exit 1; }
set -a; source .env; set +a

DB_NAME="${DB_NAME:-altium_library}"
CUSTOM_PREFIX="${CUSTOM_PREFIX:-Custom - }"
SQLCMD=(docker compose exec -T mssql /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -b)

cmd_up() {
  mkdir -p data backup
  # SQL Server runs as uid 10001 inside the container.
  if [[ "$(stat -c %u data)" != "10001" ]]; then
    chown -R 10001:0 data backup 2>/dev/null || sudo chown -R 10001:0 data backup
  fi
  docker compose up -d mssql
  echo -n "Waiting for SQL Server"
  for _ in $(seq 1 60); do
    status="$(docker inspect -f '{{.State.Health.Status}}' altium-mssql 2>/dev/null || true)"
    [[ "$status" == "healthy" ]] && { echo " ready."; return; }
    echo -n "."; sleep 3
  done
  echo; echo "SQL Server did not become healthy. Check: docker compose logs mssql"; exit 1
}

cmd_init() {
  local f
  for f in 01_init.sql 02_partsui.sql; do
    "${SQLCMD[@]}" -i "/sql/$f" \
      -v DB_NAME="$DB_NAME" \
         READER_USER="$READER_USER" READER_PASSWORD="$READER_PASSWORD" \
         LIBRARIAN_USER="$LIBRARIAN_USER" LIBRARIAN_PASSWORD="$LIBRARIAN_PASSWORD" \
         CUSTOM_PREFIX="$CUSTOM_PREFIX"
  done
}

cmd_mirror() {
  docker compose --profile tools build mirror
  docker compose --profile tools run --rm mirror "$@"
}

cmd_drop_raw() {
  local n answer
  n="$("${SQLCMD[@]}" -d "$DB_NAME" -h -1 -W -Q \
    "SET NOCOUNT ON; SELECT COUNT(*) FROM sys.tables WHERE schema_id = SCHEMA_ID(N'dbo') AND name LIKE N'z[_]%';" \
    </dev/null | tr -d '[:space:]')"   # </dev/null: keep docker from eating the answer below
  if [[ "$n" == "0" ]]; then
    echo "No z_ tables in $DB_NAME. Nothing to do."
    return
  fi
  echo "This drops $n raw Celestial z_ tables from $DB_NAME."
  echo "Altium has them disabled, the Parts Manager ignores them, and the mirror no longer copies them."
  echo "To get them back later: ./setup.sh mirror --include-raw --only z_"
  if [[ "${1:-}" != "--yes" ]]; then
    read -r -p "Type 'drop' to continue: " answer || answer=""
    [[ "$answer" == "drop" ]] || { echo "Cancelled. Nothing dropped."; exit 1; }
  fi
  "${SQLCMD[@]}" -W -i /sql/03_drop_raw_tables.sql -v DB_NAME="$DB_NAME"
}

cmd_backup() {
  local f="${DB_NAME}_$(date +%Y%m%d_%H%M%S).bak"
  "${SQLCMD[@]}" -Q "BACKUP DATABASE [$DB_NAME] TO DISK = N'/var/opt/mssql/backup/$f' WITH COMPRESSION, INIT, CHECKSUM;" \
    || "${SQLCMD[@]}" -Q "BACKUP DATABASE [$DB_NAME] TO DISK = N'/var/opt/mssql/backup/$f' WITH INIT, CHECKSUM;"  # Express lacks COMPRESSION
  echo "Backup written: ./backup/$f"
  ls -1t backup/*.bak 2>/dev/null | tail -n +11 | xargs -r rm -f   # keep newest 10
}

cmd_ui() {
  if [[ -z "${PARTSUI_PASSWORD:-}" ]]; then
    echo "Set PARTSUI_PASSWORD in .env first (the web UI's login password), then run: ./setup.sh ui"
    [[ "${1:-}" == "optional" ]] && return 0 || exit 1
  fi
  mkdir -p libs
  docker compose build partsui
  docker compose up -d partsui
  echo -n "Waiting for the Parts Manager"
  for _ in $(seq 1 40); do
    status="$(docker inspect -f '{{.State.Health.Status}}' altium-partsui 2>/dev/null || true)"
    if [[ "$status" == "healthy" ]]; then
      local addr="${PARTSUI_BIND_ADDR:-${BIND_ADDR:-0.0.0.0}}"
      [[ "$addr" == "0.0.0.0" ]] && addr="$(hostname -I 2>/dev/null | awk '{print $1}')"
      echo " ready: http://${addr:-localhost}:${PARTSUI_PORT:-8080}/  (user: admin)"
      return
    fi
    echo -n "."; sleep 3
  done
  echo; echo "The Parts Manager did not become healthy. Check: docker compose logs partsui"; exit 1
}

cmd_sql() {
  docker compose exec mssql /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -d "$DB_NAME"
}

case "${1:-}" in
  up)     cmd_up ;;
  init)   cmd_init ;;
  mirror) shift; cmd_mirror "$@" ;;
  drop-raw) shift; cmd_drop_raw "$@" ;;
  backup) cmd_backup ;;
  sql)    cmd_sql ;;
  ui)     cmd_ui ;;
  all)    cmd_up; cmd_init; cmd_mirror; cmd_ui optional ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac
