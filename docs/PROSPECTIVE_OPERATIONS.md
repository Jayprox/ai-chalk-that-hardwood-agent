# Prospective ledger: operating instructions

Host: **the Pro** (the machine that runs the scheduler). "Pro terminal" means the repo folder, with the project's Python.

**Nothing here is switched on yet.** The ledger stays disabled (`LEDGER_ENABLED` unset or false) until JD authorizes enabling it and installing the helper jobs. Sleep settings are JD's own change; nothing in the repo alters them.

## High-level version

1. Pro: review the working tree and commit only the intended source files (section 1).
2. Pro, once JD authorizes: set `LEDGER_ENABLED=true` and install the two helper jobs (sections 2 and 4).
3. Pro: keep it awake, plugged in and online during the actual game windows each day (section 3).
4. Check health only with `ledger_check.py` (never outcomes). Back up daily with `ledger_backup.py` (section 6).
5. Dry run: stage 1 after two full slate days, stage 2 only after those games reach 51 h and settle (section 7). Freeze only after stage 2 passes and JD accepts.

## 1. Review and commit (Pro terminal, before enabling)

Do not `git add -A` or "commit everything". The tree holds credentials, runtime databases and unrelated files.
```
git status --short
git diff --stat
```
Add only the intended source files by explicit path, for example:
```
git add app/ scripts/_ledger_cli.py scripts/ledger_check.py scripts/ledger_daily.py scripts/ledger_backup.py scripts/settle.py \
        scripts/confirm_dnp.py scripts/freeze.py scripts/prospective_analysis.py tests/ docs/PROSPECTIVE_PROTOCOL.md \
        docs/PROSPECTIVE_OPERATIONS.md docs/evidence/ docs/freeze/.gitkeep .env.example .gitignore BACKLOG.md
git status --short      # confirm nothing else is staged
```
Keep OUT of commits: `.env` and any key, `data/` (all databases, ledger, backups, mirror), `logs/`, `props.db`, `evals/runs`, `_to_delete/`, `untitled folder`, `.DS_Store`, private or unreviewed evidence, and any file you did not mean to change. Check `git diff --cached --stat` for anything unexpected before `git commit`. The freeze later requires the files hashed in the manifest to be tracked and unmodified; unrelated untracked files do not block it.

## 2. Turn it on (Pro terminal, only after JD authorizes)

Add `LEDGER_ENABLED=true` to `.env` (not in git). Paths default to `data/ledger/ledger.db` and `data/ledger/mirror/`. The existing 5-minute scheduler job then records; each game is polled from 40 minutes before tip-off until tip-off. Install the helper jobs (JD authorizes this too):
```
python scripts/ledger_daily.py --print-launchd > ~/Library/LaunchAgents/com.chalkthat.ledger-daily.plist
python scripts/ledger_check.py --print-launchd > ~/Library/LaunchAgents/com.chalkthat.ledger-check.plist
launchctl load ~/Library/LaunchAgents/com.chalkthat.ledger-daily.plist
launchctl load ~/Library/LaunchAgents/com.chalkthat.ledger-check.plist
```
- **Daily job (09:30 Mac-local):** settles finished games, re-reads the last 14 days of the schedule, writes `data/ledger/reports/daily-YYYY-MM-DD.json`, notifies on any alert.
- **Health job (every 15 minutes):** `ledger_check.py --notify`.

## 3. Awake and online: follow the real game windows

Interval jobs do not run during sleep, and a sleeping Pro silently loses coverage. The requirement is **per game, not a fixed clock time**: from 45 minutes before the earliest tip-off of the day until the last tip-off, with the Pro plugged in and online. Weekend and holiday slates often start in the early afternoon or earlier, so check each day's schedule (the board or `python -m app.scheduler --once`) rather than assuming 3 PM ET. Also stay awake and online for the daily job (09:30 Mac-local) and until the last game of a dry-run date has been recorded.

How to stay awake is JD's decision. Options, none applied by this repo: System Settings, Battery/Energy, power adapter on and "Prevent automatic sleeping on power adapter when the display is off"; or run `caffeinate -s` in a terminal for the window. Also stay online, avoid closing the lid unless clamshell mode is set up, and do not let macOS install updates and restart on slate days.

## 4. Failure alerts and what to do

| Alert text | Meaning | Action (Pro) |
|---|---|---|
| no recorder tick for N minutes while games are near | asleep/offline or scheduler stopped | wake it; `launchctl list \| grep chalkthat`; `python -m app.scheduler --once` |
| game tipped without a single recorder tick in its pre-tip window | that game's coverage is lost (never backfilled) | none; it stays in the denominator |
| the last recorder tick reported errors | Hardwood unreachable or an error | check `logs/scheduler.log`; none if it clears |
| ledger integrity check failed | chain, mirror or trigger problem | stop; edit nothing; send `python scripts/ledger_check.py --json` |
| coverage below the bar | coverage problem | read the reasons; report |

## 5. Monitoring without looking at outcomes

`python scripts/ledger_check.py` (add `--scope counted` after the freeze, `--replay 20` to recompute saved forecasts from saved inputs). It prints counts, chain status, last tick, coverage, rejection reasons and settlement status counts. It never prints an actual, result, hit rate or return. `prospective_analysis.py` refuses before the endpoint.

Unresolved contracts (no DNP evidence, missing stats) appear as `unresolved` counts. When an authoritative source (official NBA box score) states a player did not play, record it: `python scripts/confirm_dnp.py --game <id> --player <id> --source "official NBA box score" --note "..."`. Missing data is not a confirmation.

## 6. Backups (consistent; do not copy a live database)

Never copy `ledger.db`, `-wal` and `-shm` with `cp` or Finder while the scheduler runs: the files change independently and the copy can be inconsistent. Use the SQLite online backup:
```
python scripts/ledger_backup.py                       # data/ledger/backups/<UTC stamp>/ ; exits 0 only if the copy's chain verifies
python scripts/ledger_backup.py --dest /Volumes/<external>/ledger-backups
```
Run it once a day while the test runs (after the daily job), and point `--dest` at a second disk or cloud-synced folder at least weekly. Backups are outside git. The mirror folder is copied alongside; the ledger check reports any row missing from it.

## 7. The dry run and acceptance

Dry-run records have no freeze id and never count. Acceptance has two stages (full rules in `PROSPECTIVE_PROTOCOL.md` section 8).

- **Stage 1, recording** (after at least two full slate days; judge from the first full slate date D):
  `python scripts/ledger_check.py --scope dry --from-date D --stage recording`
  This proves the recorder worked. It does **not** allow a freeze.
- **Stage 2, final acceptance:** every game on both slate dates must be 51 h past tip-off (last game's tip-off + 51 h), then run `python scripts/ledger_daily.py` (settles, error-free run recorded), confirm any unresolved contracts you can, and run
  `python scripts/ledger_check.py --scope dry --from-date D --stage final`
  It requires: stage 1 passing, all dry-run games matured, at least one matured contract, at least 90% of matured contracts settled or pushed, void + unresolved at most 10% overall and 25% per game, and an error-free settlement run after the last maturity. Nothing matured means no pass.

## 8. After acceptance (only when JD says go)

`python scripts/freeze.py --build`, review and commit `docs/freeze/manifest.json` with the intended source files, then `python scripts/freeze.py --record --commit <hash> --dry-from D`. After that, no edits to code, parameters or definitions. The single analysis is `python scripts/prospective_analysis.py` at the endpoint, when `ledger_check.py --scope counted` shows 300 covered, matured games.
