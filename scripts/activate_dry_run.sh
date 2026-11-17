#!/bin/bash
# Activate the prospective-validation DRY RUN on the Pro (macOS). Run from anywhere:   bash scripts/activate_dry_run.sh
# It never prints .env, never deletes, never touches Hardwood, never freezes. Idempotent: safe to run twice.
# Steps: preflight checks -> stop on an existing freeze -> LEDGER_ENABLED=true -> install/load the scheduler job only if absent ->
#        install/load the two helper jobs only if absent -> verify -> observe one scheduler tick -> health check.
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"; PY="$ROOT/.venv/bin/python"; UIDN="$(id -u)"; LA="$HOME/Library/LaunchAgents"
say() { printf '\n== %s\n' "$*"; }
die() { printf '\nSTOP: %s\n' "$*"; exit 1; }

say "1. Checkout, Python, timezone"
[ "$(git rev-parse --abbrev-ref HEAD)" = "checkpoint/prospective-validation-2026-11-15" ] || die "not on the checkpoint branch"
echo "branch ok, HEAD $(git rev-parse --short HEAD)"
git status --short -- app scripts tests docs/PROSPECTIVE_PROTOCOL.md docs/PROSPECTIVE_OPERATIONS.md requirements.txt | grep -q . && die "tracked source files have uncommitted changes (git status)"
[ -x "$PY" ] || die "$PY not found"
"$PY" -c "import fastapi, httpx, numpy, dotenv, anthropic" 2>/dev/null || die "a required package is missing from .venv"
echo "python: $("$PY" --version 2>&1)   mac timezone: $(date +%Z) ($(date +%z))   now: $(date '+%a %Y-%m-%d %H:%M')"

say "2. Running processes and existing jobs"
pgrep -fl "app.scheduler|ledger_daily|ledger_check|scripts/settle" | grep -v pgrep || echo "no scheduler/helper process running right now"
pgrep -f "app.scheduler.*--loop" >/dev/null && die "a foreground scheduler --loop is running; stop it first (Ctrl-C in its terminal)"
launchctl list | grep chalkthat || echo "no chalkthat launchd jobs loaded"

say "3. Existing ledger and freeze"
if [ -e "$ROOT/data/ledger/ledger.db" ]; then
  FR="$("$PY" - <<'PYEOF'
import sys; sys.path.insert(0, ".")
from app.config import load_settings
from app.ledger import Ledger
s = load_settings(); L = Ledger(s.ledger_path, s.ledger_mirror_dir)
print("FREEZE" if L.latest_freeze() else "none", len(L.records()))
L.close()
PYEOF
)"
  echo "existing ledger: freeze=${FR% *} records=${FR#* }"
  case "$FR" in FREEZE*) die "this ledger already has a freeze; not reusing it";; esac
else echo "no ledger yet (it is created by the first tick)"; fi

say "4. Enable the ledger (value only; other settings untouched)"
touch .env; [ -n "$(tail -c1 .env)" ] && printf '\n' >> .env
if grep -q '^LEDGER_ENABLED=' .env; then sed -i '' 's/^LEDGER_ENABLED=.*/LEDGER_ENABLED=true/' .env; else printf 'LEDGER_ENABLED=true\n' >> .env; fi
grep -c '^LEDGER_ENABLED=true$' .env | sed 's/^/LEDGER_ENABLED=true lines in .env: /'

install_job() {   # label, plist-generating command...
  local label="$1"; shift; local plist="$LA/$label.plist"
  if launchctl list | grep -q "[[:space:]]$label\$"; then echo "$label already loaded; left as is"; return; fi
  if [ -e "$plist" ]; then echo "$plist exists but is not loaded; loading it"; else mkdir -p "$LA"; "$@" > "$plist" || die "could not generate $label"; echo "wrote $plist"; fi
  launchctl bootstrap "gui/$UIDN" "$plist" 2>/dev/null || launchctl load "$plist" || die "could not load $label"
  echo "$label loaded"
}
say "5. Jobs"
mkdir -p logs
install_job com.chalkthat.props-scheduler "$PY" -m app.scheduler --print-launchd
install_job com.chalkthat.ledger-daily "$PY" scripts/ledger_daily.py --print-launchd
install_job com.chalkthat.ledger-check "$PY" scripts/ledger_check.py --print-launchd

say "6. What each job points to"
for l in props-scheduler ledger-daily ledger-check; do
  p="$LA/com.chalkthat.$l.plist"; echo "-- $l"
  grep -A4 "ProgramArguments" "$p" | grep -o "<string>[^<]*</string>" | sed 's/<[^>]*>//g' | tr '\n' ' '; echo
  grep -A1 "WorkingDirectory" "$p" | grep -o "<string>[^<]*</string>" | sed 's/<[^>]*>//g' | sed 's/^/workdir: /'
  grep -o "<key>Start[A-Za-z]*</key>.*" "$p" | head -1
  grep -q "$PY" "$p" && grep -q "$ROOT" "$p" && echo "points at this checkout and this .venv python: OK" || echo "WARNING: does not point at $ROOT / $PY"
done

say "7. Observe one scheduler tick"
LOG=logs/scheduler.log; B=$(wc -l < "$LOG" 2>/dev/null || echo 0)
launchctl kickstart "gui/$UIDN/com.chalkthat.props-scheduler" 2>&1 | head -2
for i in $(seq 1 12); do sleep 10; pgrep -f "app.scheduler" >/dev/null || break; done
echo "--- new scheduler log lines (no secrets are logged):"; tail -n +$((B+1)) "$LOG" 2>/dev/null | tail -20

say "8. Health (counts and status only, no outcomes)"
"$PY" scripts/ledger_check.py --scope all | tail -30
echo; echo "launchd status:"; launchctl list | grep chalkthat
echo; echo "Done. Dry run is ENABLED; nothing is frozen and no record can count."
