#!/usr/bin/env bash
# Treant backup, in two phases:
#  1. one `pg_dump --schema=<s> -Fc` per schema listed by `treant schemas` (control,
#     general and every ws_*, archived ones included), all from ONE exported snapshot, so
#     references between schemas (ws_*.nodes -> general.nodes) are consistent; each dump
#     is checked with `pg_restore --list`;
#  2. each good dump is pushed to that schema's own encrypted restic repository
#     <BACKUP_REPO_BASE>/<schema>. One repository per schema lets a purge delete one
#     workspace's history without touching the others.
# A failing schema does not stop the others; any failure exits non-zero and leaves the
# stamp alone. Full success writes <BACKUP_STATE_DIR>/backup-ok (epoch seconds).
#
# Runs as root (docker). Settings come from the environment (treant-backup.service reads
# /etc/treant/backup.env): RESTIC_PASSWORD and BACKUP_REPO_BASE are required; BACKUP_KEEP,
# BACKUP_STAGING_DIR, BACKUP_STATE_DIR, DB_NAME, DB_SUPERUSER and COMPOSE_FILE are optional.
set -euo pipefail
shopt -s inherit_errexit
umask 077
# Byte-wise [a-z] in the name checks.
export LC_ALL=C

: "${COMPOSE_FILE:=/opt/treant/deploy/compose.prod.yml}"
export COMPOSE_FILE
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD is not set}"
: "${BACKUP_REPO_BASE:?BACKUP_REPO_BASE is not set}"
export RESTIC_PASSWORD

DB=${DB_NAME:-treant}
DB_USER=${DB_SUPERUSER:-postgres}
DUMP_DIR=${BACKUP_STAGING_DIR:-/var/backups/treant}
STATE_DIR=${BACKUP_STATE_DIR:-/var/lib/treant-backup}
KEEP=${BACKUP_KEEP:---keep-daily 7 --keep-weekly 4 --keep-monthly 12}
STAMP=$STATE_DIR/backup-ok
# Shared with restore-check.sh and purge.sh. Under a root-owned directory, not a
# world-writable one.
LOCK=$STATE_DIR/treant-backup.lock
# Fixed restic host name, so restores find the snapshots whatever the machine is called.
RESTIC_HOST=treant
SCHEMA_RE='^(control|general|ws_[a-z][a-z0-9]{1,30})$'

treant() { docker compose --profile admin run --rm -T admin "$@" </dev/null; }

install -d -m 0755 "$STATE_DIR"
exec 9>"$LOCK"
flock -w 3600 9

install -d -m 0700 "$DUMP_DIR"
trap 'rm -f -- "$DUMP_DIR"/*.dump' EXIT

listing=$(treant schemas)
mapfile -t schemas <<<"$listing"

# Validate every name before it is used in a path or an argv.
seen_control=0
seen_general=0
for s in "${schemas[@]}"; do
  if [[ ! $s =~ $SCHEMA_RE ]]; then
    echo "treant schemas returned an invalid schema name; nothing backed up" >&2
    exit 1
  fi
  [ "$s" = control ] && seen_control=1
  [ "$s" = general ] && seen_general=1
done
if [ "$seen_control" -ne 1 ] || [ "$seen_general" -ne 1 ]; then
  echo "treant schemas did not list control and general; nothing backed up" >&2
  exit 1
fi

# Phase 1. A REPEATABLE READ transaction held open in a psql session exports its snapshot;
# every pg_dump imports it (--snapshot), so all dumps see the database at one instant. The
# session must stay open until the last dump.
coproc SNAP { docker compose exec -T db psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB"; }
printf '%s\n' 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;' 'SELECT pg_export_snapshot();' >&"${SNAP[1]}"
snapshot=
read -r -t 60 snapshot <&"${SNAP[0]}" || true
if [[ ! $snapshot =~ ^[0-9A-F]+-[0-9A-F]+-[0-9]+$ ]]; then
  echo "could not export a database snapshot; nothing backed up" >&2
  exit 1
fi
echo "dumping ${schemas[*]} from one snapshot"

dump_one() {
  local s=$1
  local dump=$DUMP_DIR/$s.dump
  rm -f -- "$dump"
  # Custom format to stdout over the container's local socket; no TTY, so the stream
  # stays binary-clean. Nothing is written inside the container.
  docker compose exec -T db pg_dump -U "$DB_USER" -d "$DB" --snapshot="$snapshot" \
    --schema="$s" -Fc >"$dump"
  if [ ! -s "$dump" ]; then
    echo "$s: pg_dump produced an empty file" >&2
    return 1
  fi
  # Parses the archive's table of contents; writes nothing.
  docker compose exec -T db pg_restore --list <"$dump" >/dev/null
}

push_one() {
  local s=$1
  local dump=$DUMP_DIR/$s.dump
  export RESTIC_REPOSITORY=$BACKUP_REPO_BASE/$s
  # First run for this schema only: create its repository. `init` refuses an existing one.
  if ! restic cat config >/dev/null 2>&1; then
    restic init --quiet
  fi
  restic backup --quiet --host "$RESTIC_HOST" --tag "treant-$s" "$dump"
  # shellcheck disable=SC2086 # KEEP is a list of restic flags
  restic forget --quiet --host "$RESTIC_HOST" --tag "treant-$s" $KEEP --prune
  rm -f -- "$dump"
  echo "$s: backed up"
}

# errexit is ignored inside `if`/`||`, so each schema runs in a subshell with errexit on
# and its status is read afterwards.
failed=0
dumped=()
for s in "${schemas[@]}"; do
  set +e
  (set -e; dump_one "$s")
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    dumped+=("$s")
  else
    echo "$s: dump FAILED (exit $rc)" >&2
    failed=1
  fi
done

# End the snapshot's transaction: close psql's stdin and let it exit.
{ printf 'COMMIT;\n' >&"${SNAP[1]:-}"; } 2>/dev/null || true
snap_in=${SNAP[1]:-}
if [ -n "$snap_in" ]; then exec {snap_in}>&-; fi
wait "${SNAP_PID:-}" 2>/dev/null || true

# Phase 2: push each good dump to its own repository.
for s in ${dumped[@]+"${dumped[@]}"}; do
  set +e
  (set -e; push_one "$s")
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "$s: backup FAILED (exit $rc)" >&2
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "treant backup incomplete; stamp not written" >&2
  exit 1
fi

printf '%s\n' "$(date +%s)" >"$STAMP.tmp"
chmod 0644 "$STAMP.tmp"
mv -f "$STAMP.tmp" "$STAMP"
echo "treant backed up: ${schemas[*]}"
