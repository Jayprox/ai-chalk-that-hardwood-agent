#!/bin/bash
# Move the dry-run OPERATING checkout out of ~/Documents (macOS blocks launchd jobs from reading it) to ~/Projects. Run on the Pro, from the
# CURRENT checkout:   bash scripts/migrate_operating_checkout.sh
# Safe by design: it never deletes anything, never touches Hardwood or the nba.* jobs, never changes macOS privacy/sleep settings, never freezes.
# It stops at the first problem and tells you why. The original checkout and its .env stay intact for rollback.
set -u
OLD="$(cd "$(dirname "$0")/.." && pwd)"; NEW="$HOME/Projects/ai-chalk-that-hardwood-agent"; UIDN="$(id -u)"; LA="$HOME/Library/LaunchAgents"
BR="checkpoint/prospective-validation-2026-11-15"; STAMP="$(date +%Y%m%d-%H%M%S)"; TMP="$(mktemp -d)"
LABELS="com.chalkthat.props-scheduler com.chalkthat.ledger-daily com.chalkthat.ledger-check"
say() { printf '\n== %s\n' "$*"; }
die() { printf '\nSTOP: %s\n' "$*"; echo "Nothing was deleted. Original checkout: $OLD"; exit 1; }

say "1. Inspect and preserve (read-only)"
cd "$OLD" || die "cannot enter $OLD"
[ "$(git rev-parse --abbrev-ref HEAD)" = "$BR" ] || die "current checkout is not on $BR"
HEAD_OLD="$(git rev-parse HEAD)"; REMOTE="$(git ls-remote origin "refs/heads/$BR" | cut -f1)"
echo "local HEAD $HEAD_OLD"; echo "remote $BR ${REMOTE:-<missing>}"
[ "$HEAD_OLD" = "$REMOTE" ] || die "the remote branch is not at your local HEAD. Push first: git push origin $BR"
git status --short | grep -v '^??' | grep -q . && die "tracked files have uncommitted changes; commit or review them first (git status)"
echo "untracked (kept in the old checkout, NOT copied or published):"; git status --short | grep '^??' | sed 's/^/   /'
[ -e "$NEW" ] && { echo "destination exists:"; ls -la "$NEW" | head -20; die "$NEW already exists; inspect it. I will not overwrite it"; }
[ -f .env ] || die "no .env in the current checkout"
echo "runtime inventory (names and sizes only):"; ls -la data data/ledger logs 2>/dev/null | sed 's/^/   /'
OLDPY="$OLD/.venv/bin/python"; [ -x "$OLDPY" ] || die "old venv python missing"
BASEHOME="$(sed -n 's/^home = //p' .venv/pyvenv.cfg | head -1)"; BASEPY="$BASEHOME/python3"; [ -x "$BASEPY" ] || BASEPY="$(command -v python3)"
echo "base interpreter for the new venv: $BASEPY ($("$BASEPY" --version 2>&1))"
M1="$(stat -f %m logs/scheduler.log 2>/dev/null || echo none)"; M2="$(stat -f %m data/ledger/ledger.db 2>/dev/null || echo none)"

say "2. Stop only the three agent jobs"
mkdir -p "$LA/chalkthat-old-plists-$STAMP"
for l in $LABELS; do [ -e "$LA/$l.plist" ] && cp -p "$LA/$l.plist" "$LA/chalkthat-old-plists-$STAMP/"; launchctl bootout "gui/$UIDN/$l" 2>/dev/null; done
for i in 1 2 3 4 5 6; do launchctl list | grep -qE 'com\.chalkthat\.(props-scheduler|ledger-daily|ledger-check)' || break; sleep 2; done
launchctl list | grep -E 'com\.chalkthat\.(props-scheduler|ledger-daily|ledger-check)' && die "an agent job is still loaded"
for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "$OLD/.venv/bin/python" >/dev/null || break; sleep 3; done
pgrep -fl "$OLD/.venv/bin/python" && die "an agent process is still running from the old checkout (wait for it to finish, then re-run)"
echo "jobs stopped; no agent process runs from the old checkout. old plists saved in $LA/chalkthat-old-plists-$STAMP"
echo "(com.chalkthat.nba.* jobs were not touched)"

say "3. New checkout at the verified commit"
mkdir -p "$HOME/Projects"
git clone -q --branch "$BR" --single-branch "$(git remote get-url origin)" "$NEW" || die "clone failed"
cd "$NEW" || die "cannot enter $NEW"
[ "$(git rev-parse HEAD)" = "$HEAD_OLD" ] || die "new checkout is not at $HEAD_OLD"
echo "new checkout: $NEW  branch $(git rev-parse --abbrev-ref HEAD)  commit $(git rev-parse --short HEAD)"

say "4. Rebuild .venv with the same dependency versions (no upgrades)"
"$OLDPY" -m pip freeze --disable-pip-version-check > "$TMP/constraints.txt" 2>/dev/null || die "could not read the old package versions"
"$BASEPY" -m venv "$NEW/.venv" || die "venv creation failed"
"$NEW/.venv/bin/python" -m pip install -q --disable-pip-version-check -r requirements.txt -c "$TMP/constraints.txt" || die "dependency install failed"
"$NEW/.venv/bin/python" -m pip freeze --disable-pip-version-check > "$TMP/new_freeze.txt"
if diff -q "$TMP/constraints.txt" "$TMP/new_freeze.txt" >/dev/null; then echo "package versions identical to the old venv"; else echo "version differences (old < , new >):"; diff "$TMP/constraints.txt" "$TMP/new_freeze.txt" | sed 's/^/   /'; fi
"$NEW/.venv/bin/python" -c "import fastapi, httpx, numpy, dotenv, anthropic; print('imports ok')" || die "imports failed in the new venv"

say "5. Configuration (values never printed)"
install -m 600 "$OLD/.env" "$NEW/.env" || die "could not copy .env"
echo "keys: $(cut -d= -f1 "$NEW/.env" | grep -v '^#' | grep -v '^$' | tr '\n' ' ')"
grep -q '^LEDGER_ENABLED=true$' "$NEW/.env" && echo "LEDGER_ENABLED=true present" || die "LEDGER_ENABLED=true missing in the new .env"
grep -q "$OLD" "$NEW/.env" && echo "WARNING: .env keys holding the old path: $(grep "$OLD" "$NEW/.env" | cut -d= -f1 | tr '\n' ' ')" || echo "no .env value points at the old checkout"
ls -l "$NEW/.env" | awk '{print "permissions:", $1}'

say "6. Runtime data (writers are stopped)"
mkdir -p "$NEW/data" "$NEW/logs"
if [ -f "$OLD/data/props.db" ]; then
  "$OLDPY" - "$OLD/data/props.db" "$NEW/data/props.db" <<'PYEOF' || die "dashboard store copy failed"
import sqlite3, sys
s = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True); d = sqlite3.connect(sys.argv[2]); s.backup(d)
ok = d.execute("PRAGMA integrity_check").fetchone()[0]
tabs = [r[0] for r in s.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
bad = [t for t in tabs if s.execute(f'select count(*) from "{t}"').fetchone() != d.execute(f'select count(*) from "{t}"').fetchone()]
print("dashboard store: integrity", ok, "| tables", len(tabs), "| row-count mismatches", bad or "none")
sys.exit(0 if ok == "ok" and not bad else 1)
PYEOF
fi
if [ -f "$OLD/data/ledger/ledger.db" ]; then
  cd "$OLD" && BK="$("$OLDPY" scripts/ledger_backup.py --dest "$TMP/bk" | tail -1)"; cd "$NEW"
  echo "$BK"; SRC="${BK#*: }"; echo "$BK" | grep -q "^backup verified" || die "ledger backup did not verify"
  mkdir -p "$NEW/data/ledger"; cp -p "$SRC/ledger.db" "$NEW/data/ledger/ledger.db"; cp -Rp "$SRC/mirror" "$NEW/data/ledger/mirror"
  [ -d "$OLD/data/ledger/reports" ] && cp -Rp "$OLD/data/ledger/reports" "$NEW/data/ledger/reports"
  "$NEW/.venv/bin/python" - "$OLD" "$NEW" <<'PYEOF' || die "ledger verification at the destination failed"
import sqlite3, sys
sys.path.insert(0, sys.argv[2])
from app.ledger import Ledger
o = sqlite3.connect("file:" + sys.argv[1] + "/data/ledger/ledger.db?mode=ro", uri=True)
n = Ledger(sys.argv[2] + "/data/ledger/ledger.db", sys.argv[2] + "/data/ledger/mirror")
chain = n.verify_chain()
cnt = lambda c: tuple(c.execute(f"select count(*) from {t}").fetchone()[0] for t in ("ledger", "ticks", "expected_games"))
same = cnt(o) == cnt(n.conn)
fr = n.latest_freeze()
print("ledger at destination: rows/ticks/expected games", cnt(n.conn), "| same as source:", same, "| chain ok:", chain["ok"], "| freeze:", "PRESENT" if fr else "none")
n.close(); sys.exit(0 if (same and chain["ok"] and not fr) else 1)
PYEOF
else echo "no ledger in the old checkout (the first tick creates it)"; fi

say "7. Remaining dependencies on the Documents checkout"
echo "text references (docs excluded):"; grep -rIl "Documents/Projects/git" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=docs --exclude-dir=evals 2>/dev/null | sed 's/^/   /' || true
echo "symlinks outside .venv/.git: $(find . -path ./.venv -prune -o -path ./.git -prune -o -type l -print | wc -l | tr -d ' ')"
grep -q "Documents" "$NEW/.venv/pyvenv.cfg" && die "the new venv still references Documents" || echo "new venv does not reference Documents"

say "8. Jobs with the new paths"
for spec in "com.chalkthat.props-scheduler:-m app.scheduler --print-launchd" "com.chalkthat.ledger-daily:scripts/ledger_daily.py --print-launchd" "com.chalkthat.ledger-check:scripts/ledger_check.py --print-launchd"; do
  l="${spec%%:*}"; cmd="${spec#*:}"
  "$NEW/.venv/bin/python" $cmd > "$LA/$l.plist" || die "could not generate $l"
  plutil -lint "$LA/$l.plist" >/dev/null || die "invalid plist for $l"
  grep -q "$OLD" "$LA/$l.plist" && die "$l still references the old path"
  grep -q "$NEW/.venv/bin/python" "$LA/$l.plist" && grep -q "<string>$NEW</string>" "$LA/$l.plist" || die "$l does not point at the new checkout and venv"
  launchctl bootstrap "gui/$UIDN" "$LA/$l.plist" || die "could not load $l"; echo "$l loaded -> $NEW"
done
[ "$(launchctl list | grep -c com.chalkthat.props-scheduler)" = 1 ] || die "duplicate or missing scheduler job"

say "9. Verify under launchd (not Terminal)"
for i in $(seq 1 18); do sleep 10; pgrep -f "app.scheduler" >/dev/null || break; done
launchctl kickstart "gui/$UIDN/com.chalkthat.ledger-daily" 2>&1 | head -1; launchctl kickstart "gui/$UIDN/com.chalkthat.ledger-check" 2>&1 | head -1
for i in $(seq 1 12); do sleep 5; pgrep -f "ledger_daily|ledger_check" >/dev/null || break; done
echo "job status (second column = last exit code, 0 is good):"; launchctl list | grep -E 'com\.chalkthat\.(props-scheduler|ledger-daily|ledger-check)'
echo "--- scheduler log (new checkout):"; tail -15 "$NEW/logs/scheduler.log" 2>/dev/null
echo "--- daily log:"; tail -6 "$NEW/logs/ledger_daily.log" 2>/dev/null
echo "--- health:"; "$NEW/.venv/bin/python" scripts/ledger_check.py --scope all | tail -12
[ "$(stat -f %m "$OLD/logs/scheduler.log" 2>/dev/null || echo none)" = "$M1" ] && [ "$(stat -f %m "$OLD/data/ledger/ledger.db" 2>/dev/null || echo none)" = "$M2" ] && echo "old runtime paths unchanged since the jobs were stopped: yes" || echo "WARNING: something wrote to the old runtime paths"
echo "scheduler processes now: $(pgrep -f 'app.scheduler' | wc -l | tr -d ' ')   scheduler jobs loaded: $(launchctl list | grep -c com.chalkthat.props-scheduler)"
cat <<TXT

Done. Operating checkout: $NEW   (original kept at $OLD, untouched)
ROLLBACK (only one instance may run): launchctl bootout gui/$UIDN/<each of the three labels>; copy the saved plists from
  $LA/chalkthat-old-plists-$STAMP/ back into $LA; launchctl bootstrap gui/$UIDN <each plist>. Note the old checkout still hits the
  Documents access problem, so rollback restores the old (non-working) state; prefer fixing the new one.
TXT
