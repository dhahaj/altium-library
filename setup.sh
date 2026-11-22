#!/usr/bin/env bash
# Altium DbLib server helper.
#   ./setup.sh up        start SQL Server, wait until healthy
#   ./setup.sh init      create database, logins, template table, helper proc (re-runnable)
#   ./setup.sh mirror    copy Celestial tables in (extra args go to mirror.py: --list, --only X)
#   ./setup.sh backup    full backup to ./backup/
#   ./setup.sh sql       interactive sqlcmd as sa
#   ./setup.sh all       up + init + mirror
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
  "${SQLCMD[@]}" -i /sql/01_init.sql \
    -v DB_NAME="$DB_NAME" \
       READER_USER="$READER_USER" READER_PASSWORD="$READER_PASSWORD" \
       LIBRARIAN_USER="$LIBRARIAN_USER" LIBRARIAN_PASSWORD="$LIBRARIAN_PASSWORD" \
       CUSTOM_PREFIX="$CUSTOM_PREFIX"
}

cmd_mirror() {
  docker compose --profile tools build mirror
  docker compose --profile tools run --rm mirror "$@"
}

cmd_backup() {
  local f="${DB_NAME}_$(date +%Y%m%d_%H%M%S).bak"
  "${SQLCMD[@]}" -Q "BACKUP DATABASE [$DB_NAME] TO DISK = N'/var/opt/mssql/backup/$f' WITH COMPRESSION, INIT, CHECKSUM;" \
    || "${SQLCMD[@]}" -Q "BACKUP DATABASE [$DB_NAME] TO DISK = N'/var/opt/mssql/backup/$f' WITH INIT, CHECKSUM;"  # Express lacks COMPRESSION
  echo "Backup written: ./backup/$f"
  ls -1t backup/*.bak 2>/dev/null | tail -n +11 | xargs -r rm -f   # keep newest 10
}

cmd_sql() {
  docker compose exec mssql /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -d "$DB_NAME"
}

case "${1:-}" in
  up)     cmd_up ;;
  init)   cmd_init ;;
  mirror) shift; cmd_mirror "$@" ;;
  backup) cmd_backup ;;
  sql)    cmd_sql ;;
  all)    cmd_up; cmd_init; cmd_mirror ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
