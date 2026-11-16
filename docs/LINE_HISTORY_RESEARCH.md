# Line history research (descriptive, outside the prospective experiment)

`scripts/line_history.py` scores one player's past games against a single threshold and splits them by context. It is separate from the
prospective validation: it never reads or writes the ledger, and it changes nothing in the simulator, calibration, signal rules, thresholds or
scheduler. Run it as `.venv/bin/python -m scripts.line_history --player "<name>" --market pts`. It reads Hardwood read-only and makes no paid calls.

## What the output is, and is not

- **Historical production context, not betting performance.** Every past game is scored against one number. The sportsbook never offered that number
  for those games, so an "over rate" here says how often he produced above a threshold, not whether a bet would have won.
- **The threshold has a source.** By default it is the latest stored board prop for that player and market. That is not necessarily today's line.
  The report, the CSV (`threshold`, `source_board_date` on every row) and `<name>_provenance.json` give the threshold, its source board date, the
  generation time, and whether it was selected from stored data or supplied manually (`--line`, with `--board-date`, default today in ET).
- **The source board date and later are excluded.** Games on that date and after are not history for that threshold. DNPs and games missing a required
  statistic are excluded, a genuine 0 is a valid value, and pushes are counted separately from overs and unders.
- **Opponent ranks are retrospective.** For finished seasons the rank is the end-of-season rank, which contains information from games after the one
  being looked at. For the current season it is the rank to date (as of the run), not a final rank. The CSV column `def_rank_basis` says which.
  Neither is the rank on the night of the game.
- **Minutes are retrospective.** They are what he actually played. They are context, not a pregame feature.
- **The position group is the current one** applied to older seasons. That is an approximation.
- **Older half / newer half is exploratory.** It re-looks at the same games split by date. It is not independent confirmation of anything.
- **Rest days** count the gap since his last PLAYED game, so they run high after missed games.

## Reconciling the earlier summary with the saved report

An earlier hand-off summary said defensive ranks covered "70 games". That was a separate cut: the 2025-26 and 2026-27 seasons only, which I tabulated
from the CSV by hand (over/under 18-10 against soft, 13-3 middle, 14-12 tough). It was not a limit of the data. The saved LeBron report has a defensive
rank for all 211 of 211 games, and the report now prints that coverage line (`Opponent defensive rank is available for N of M games`).

## Files written (all under `data/research/`, which git ignores)

`<player>_<market>_report.md`, `<player>_<market>_games.csv`, `<player>_<market>_provenance.json`, and `cache/` (Hardwood responses for finished seasons).
