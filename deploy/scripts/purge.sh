#!/usr/bin/env bash
# Irreversibly removes an ARCHIVED Treant workspace: its schema, role and control rows
# (`treant workspace purge`), then its restic repository <BACKUP_REPO_BASE>/ws_<name>,
# then records whether all of them are gone (`treant workspace purge-verify`, audited).
# Usage (root, in a terminal): sudo /opt/treant/deploy/scripts/purge.sh <workspace>
#
# Archive first (`treant workspace archive <workspace>`, which also revokes its tokens).
# The repository is deleted here only when BACKUP_REPO_BASE is a local path. For a remote
# restic backend (sftp:, s3:, ...) delete <BACKUP_REPO_BASE>/ws_<name> with that backend's
# own tools, then record it: treant workspace purge-verify <name> --repo-gone yes
#
# Running it again after an interruption finishes the rest: when the database purge has
# already happened it goes on with the repository and the verification.
set -euo pipefail
umask 077
export LC_ALL=C

: "${COMPOSE_FILE:=/opt/treant/deploy/compose.prod.yml}"
export COMPOSE_FILE
ENV_FILE=${BACKUP_ENV_FILE:-/etc/treant/backup.env}
STATE_DIR=${BACKUP_STATE_DIR:-/var/lib/treant-backup}
LOCK=$STATE_DIR/treant-backup.lock
NAME_RE='^[a-z][a-z0-9]{1,30}$'

treant() { docker compose --profile admin run --rm -T admin "$@" </dev/null; }

usage() {
  echo "usage: purge.sh <workspace>  (a-z, then 1-30 of a-z0-9; not general or control)" >&2
  exit 2
}

[ "$#" -eq 1 ] || usage
WS=$1
[[ $WS =~ $NAME_RE ]] || usage
case $WS in general | control) usage ;; esac

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root: sudo $0 $WS" >&2
  exit 1
fi
if [ -z "${BACKUP_REPO_BASE:-}" ] && [ -r "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
REPO_BASE=${BACKUP_REPO_BASE:-}

# Not while a backup runs: it could re-create the repository after the delete below.
install -d -m 0755 "$STATE_DIR"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "a treant backup or restore check is running; try again later" >&2
  exit 1
fi

cat <<EOF
About to purge workspace '$WS' for good: schema ws_$WS, its role, its tokens, jobs and
canary, and every backup of it in ${REPO_BASE:-<no BACKUP_REPO_BASE set>}/ws_$WS.
Items this workspace published to general stay there; review general first.
EOF
if ! (: </dev/tty) 2>/dev/null; then
  echo "no terminal to confirm on; nothing purged" >&2
  exit 1
fi
printf 'Type the workspace name to purge it: ' >/dev/tty
IFS= read -r typed </dev/tty || typed=
if [ "$typed" != "$WS" ]; then
  echo "confirmation did not match; nothing purged" >&2
  exit 1
fi

if ! treant workspace purge "$WS" --confirm "$WS"; then
  # Refused: not archived (stop), or already purged by an earlier run (finish it).
  if listing=$(treant schemas) && ! printf '%s\n' "$listing" | grep -qx "ws_$WS"; then
    echo "workspace '$WS' was already purged in the database; finishing"
  else
    echo "workspace '$WS' is not archived or the database is unreachable; nothing purged" >&2
    exit 1
  fi
fi

# From here on the database purge has happened: always end with purge-verify.
repo_gone=no
REPO=$REPO_BASE/ws_$WS
if [ -z "$REPO_BASE" ]; then
  echo "BACKUP_REPO_BASE is not set; delete the workspace's backups by hand" >&2
elif [[ $REPO_BASE != /* ]]; then
  echo "remote repository: delete $REPO with your backend's tools, then run:" >&2
  echo "  treant workspace purge-verify $WS --repo-gone yes" >&2
elif [ ! -d "$REPO_BASE" ]; then
  # An unmounted backup disk must never read as "repository gone".
  echo "$REPO_BASE is not there (backup disk not mounted?); remove $REPO by hand, then: treant workspace purge-verify $WS --repo-gone yes" >&2
elif [ ! -e "$REPO" ]; then
  repo_gone=yes
elif rm -rf --one-file-system -- "$REPO" && [ ! -e "$REPO" ]; then
  repo_gone=yes
else
  echo "could not remove $REPO; remove it by hand, then: treant workspace purge-verify $WS --repo-gone yes" >&2
fi

set +e
treant workspace purge-verify "$WS" --repo-gone "$repo_gone"
rc=$?
set -e
exit "$rc"
