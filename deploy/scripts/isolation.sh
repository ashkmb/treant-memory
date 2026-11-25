#!/usr/bin/env bash
# Nightly live isolation test. Runs `treant isolation-test` against the live database
# (exit 0 clean, 1 on any fault). Its output is counts and check/from/to triples, never
# data; it goes to the journal. On exit 0 only, writes the stamp file
# <ISOLATION_STATE_DIR>/isolation-ok (epoch seconds) for your monitoring to watch.
# Run by treant-isolation.service. Every workspace needs a canary first:
#   treant canary plant <workspace> --if-missing
set -euo pipefail
export LC_ALL=C
: "${COMPOSE_FILE:=/opt/treant/deploy/compose.prod.yml}"
export COMPOSE_FILE
STATE_DIR=${ISOLATION_STATE_DIR:-/var/lib/treant-isolation}
STAMP=$STATE_DIR/isolation-ok

install -d -m 0755 "$STATE_DIR"
docker compose --profile admin run --rm -T admin isolation-test </dev/null

tmp=$(mktemp "$STATE_DIR/.isolation-ok.XXXXXX")
date +%s >"$tmp"
chmod 0644 "$tmp"
mv -f -- "$tmp" "$STAMP"
