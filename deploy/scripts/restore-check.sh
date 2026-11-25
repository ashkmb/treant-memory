#!/usr/bin/env bash
# Restore drill (by hand, as root): restores every schema's latest restic snapshot into a
# scratch database in the same cluster (dropped on exit) and compares row counts per table.
# Example: sudo /opt/treant/deploy/scripts/restore-check.sh
#
# Pass/fail compares each table's restored count(*) with the number of rows the snapshot's
# dump itself holds (its COPY blocks). The live database has normally moved on since the
# backup, so live counts are printed for the operator only (delta = live - restored). It
# also fails when a table that exists live is missing from the backup, or when a schema
# listed by `treant schemas` has no snapshot or does not restore in one transaction.
# Output: schema.table names and counts only, never row content (pg_restore's stderr is
# withheld by default because it can quote rows; RESTORE_CHECK_SHOW_ERRORS=1 prints it).
#
# Reads /etc/treant/backup.env (or BACKUP_ENV_FILE) when RESTIC_PASSWORD is not already set.
set -euo pipefail
shopt -s inherit_errexit
umask 077
export LC_ALL=C

: "${COMPOSE_FILE:=/opt/treant/deploy/compose.prod.yml}"
export COMPOSE_FILE
ENV_FILE=${BACKUP_ENV_FILE:-/etc/treant/backup.env}

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root: sudo $0" >&2
  exit 1
fi
if [ -z "${RESTIC_PASSWORD:-}" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD is not set}"
: "${BACKUP_REPO_BASE:?BACKUP_REPO_BASE is not set}"
export RESTIC_PASSWORD

DB=${DB_NAME:-treant}
DB_USER=${DB_SUPERUSER:-postgres}
SCRATCH=treant_restore_check
DUMP_DIR=${BACKUP_STAGING_DIR:-/var/backups/treant}
STATE_DIR=${BACKUP_STATE_DIR:-/var/lib/treant-backup}
LOCK=$STATE_DIR/treant-backup.lock
RESTIC_HOST=treant
SCHEMA_RE='^(control|general|ws_[a-z][a-z0-9]{1,30})$'
TAB=$'\t'

treant() { docker compose --profile admin run --rm -T admin "$@" </dev/null; }

psql_q() { # db, then psql args; reads SQL on stdin
  local db=$1
  shift
  docker compose exec -T db psql -X -q -A -t -F "$TAB" -v ON_ERROR_STOP=1 \
    -U "$DB_USER" -d "$db" "$@"
}

# Not while a backup or purge runs.
install -d -m 0755 "$STATE_DIR"
exec 9>"$LOCK"
flock -w 3600 9

install -d -m 0700 "$DUMP_DIR"
# Work directories a killed run could not clean up; safe here, the lock is held.
rm -rf -- "$DUMP_DIR"/restore-check.*
WORK=$(mktemp -d "$DUMP_DIR/restore-check.XXXXXX")
cleanup() {
  printf 'SET client_min_messages = warning;\nDROP DATABASE IF EXISTS %s WITH (FORCE);\n' "$SCRATCH" |
    psql_q postgres >/dev/null || true
  rm -rf -- "$WORK"
}
trap cleanup EXIT

listing=$(treant schemas)
mapfile -t listed <<<"$listing"
for s in "${listed[@]}"; do
  if [[ ! $s =~ $SCHEMA_RE ]]; then
    echo "treant schemas returned an invalid schema name" >&2
    exit 1
  fi
done
# ws_* tables reference general.nodes: control and general go first.
schemas=(control general)
for s in "${listed[@]}"; do
  case $s in control | general) ;; *) schemas+=("$s") ;; esac
done

psql_q postgres <<SQL >/dev/null
SET client_min_messages = warning;
DROP DATABASE IF EXISTS $SCRATCH WITH (FORCE);
CREATE DATABASE $SCRATCH;
SQL
echo "CREATE EXTENSION vector;" | psql_q "$SCRATCH" >/dev/null

table_counts() { # db schema -> "schema.table<TAB>count", sorted
  psql_q "$1" -v s="$2" <<'SQL' | LC_ALL=C sort
SELECT schemaname || '.' || tablename,
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, tablename),
                           false, true, '')))[1]::text
FROM pg_catalog.pg_tables
WHERE schemaname = :'s';
SQL
}

dump_counts() { # dump file -> "schema.table<TAB>rows in its COPY block", sorted
  docker compose exec -T db pg_restore --data-only -f - <"$1" | awk -v OFS='\t' '
    copying && $0 == "\\." { print table, n; copying = 0; next }
    copying { n++; next }
    /^COPY / { table = $2; n = 0; copying = 1 }
  ' | LC_ALL=C sort
}

# One line per table; exits 1 on any mismatch or missing table.
compare() { # dump.tsv restored.tsv live.tsv
  awk -F'\t' -v OFS='\t' '
    FILENAME == ARGV[1] { d[$1] = $2; k[$1] = 1; next }
    FILENAME == ARGV[2] { r[$1] = $2; k[$1] = 1; next }
    { l[$1] = $2; k[$1] = 1 }
    END {
      bad = 0
      for (t in k) {
        st = "ok"
        if (!(t in d) || !(t in r)) { st = "MISSING_FROM_BACKUP"; bad = 1 }
        else if (d[t] != r[t]) { st = "MISMATCH"; bad = 1 }
        delta = (t in l && t in r) ? l[t] - r[t] : "n/a"
        print t, (t in d ? d[t] : "-"), (t in r ? r[t] : "-"), (t in l ? l[t] : "-"), delta, st
      }
      exit bad
    }
  ' "$1" "$2" "$3" | LC_ALL=C sort
}

check_one() {
  local s=$1
  local file=$WORK/$s.dump
  export RESTIC_REPOSITORY=$BACKUP_REPO_BASE/$s
  if ! restic cat config >/dev/null 2>&1; then
    echo "$s: no restic repository at $RESTIC_REPOSITORY" >&2
    return 1
  fi
  restic dump --quiet --host "$RESTIC_HOST" --tag "treant-$s" latest "$DUMP_DIR/$s.dump" >"$file"
  if [ ! -s "$file" ]; then
    echo "$s: latest snapshot holds an empty dump" >&2
    return 1
  fi
  if ! docker compose exec -T db pg_restore -U "$DB_USER" -d "$SCRATCH" \
    --no-owner --no-privileges --single-transaction <"$file" 2>"$WORK/$s.err"; then
    echo "$s: pg_restore failed ($(wc -l <"$WORK/$s.err") stderr lines withheld; rerun with RESTORE_CHECK_SHOW_ERRORS=1 to print them, they may quote data)" >&2
    if [ "${RESTORE_CHECK_SHOW_ERRORS:-0}" = 1 ]; then
      grep -v -e 'DETAIL:' -e 'CONTEXT:' -e '^COPY ' "$WORK/$s.err" >&2 || true
    fi
    return 1
  fi
  dump_counts "$file" >"$WORK/$s.dump.tsv"
  table_counts "$SCRATCH" "$s" >"$WORK/$s.restored.tsv"
  table_counts "$DB" "$s" >"$WORK/$s.live.tsv"
  rm -f -- "$file"
  compare "$WORK/$s.dump.tsv" "$WORK/$s.restored.tsv" "$WORK/$s.live.tsv"
}

printf 'table\tdump\trestored\tlive\tdelta\tstatus\n'
failed=0
for s in "${schemas[@]}"; do
  set +e
  (set -e; check_one "$s")
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "$s: restore check FAILED" >&2
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "restore check FAILED" >&2
  exit 1
fi
echo "restore check ok: ${schemas[*]}"
