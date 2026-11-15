# Backlog

Nothing here is started without approval.

## Product
- **Game props: Moneyline, Points Total, Points Spread** (asked 2026-11-14). Hardwood has no spread, total or moneyline anywhere (checked 2026-11-13), so this needs a separate odds source (The Odds API is what Hardwood itself uses for player props). Decide the source first, then add a Game lines section to the Games tab and game detail.
- Sim probability enrichment (the strong border's sim-agrees condition), then re-run the Stage A2 backtest on live data.
- Write-ups (written on first open or on a material change), with last-10 bars from game logs.
- Availability researcher per game, with a daily cost cap (the Games tab shows "not checked" until then).
- Model scorecard: live hit rates by rank bucket, with DNP and pushes voided.
- Re-run Stage B (teammate absences) after 4 to 6 weeks of data.

## Data requests for Hardwood
- Opponent split, career splits, cross-season game log, game_log limit.
- `/props` with no date returned the previous day's board at 12:41 AM ET.

## Chat agent backlog
- Giannis stale-team cross-check, "forces turnovers" wording, auto web search for movers, availability search per game, higher MAX_TOKENS.

## Last-season prior for the simulation (tested 2026-11-14, not enabled)
Built behind `Params.last_cap_minutes` (default 0 = off) with `python -m scripts.backtest_blend`. First test on 2,397 paired records
(53 of 56 players had a last season): no clear difference at caps of 300, 600 or 1200 minutes (95% intervals all span zero), and every
version scored slightly worse than a flat 50% on Brier (calibration slope 0.14 to 0.20, i.e. overconfident). Not wired into the dashboard.
Re-run with the Stage B re-run in 4 to 6 weeks, when there are more games per player and more dates.

## Paused 2026-11-14 while the simulation is hardened
- Milestone 5: model-written write-ups with last-10 bars (needs the sim settled first, since write-ups quote it).
- Then the model scorecard and the availability researcher (with a daily cost cap).
- Claude Doc e50304cc-fe70: add the Stage B results, the Codex-style assessment items (CI vs minimum detectable effect, evidence vs hypothesis, selection-bias check, stop conditions) and the dashboard decisions.

## Simulation hardening, 2026-11-14 (done)
- Habit screen on the full 2025-26 season (13,910 player-games, scored forward in time): role facts cut minutes error by 13%; opponent and recent-form facts gave 0.3 to 0.8% on points, rebounds and assists; schedule, head-to-head, TV and altitude mostly did nothing.
- Shipped: a fitted minutes model (app/minutes_model.py) and wider spreads for points, assists and threes (app/sim_fit.json from `python -m scripts.fit_sim --write`). Paired result on this season's lines: Brier 0.2592 -> 0.2559, interval just below zero. Still slightly worse than a flat 50%, and the market-beating question is unchanged.
- Not shipped (small gains, not tested in the sim): opponent-allowed and recent-form terms; the last-season prior; `started` is empty in the logs so the starter-rate feature carries no weight yet.
- Open: points spread is still a little narrow (central 80% range holds 77%); re-fit everything with the Stage B re-run in 4 to 6 weeks.
- Habit screen pooled over 2023-24, 2024-25 and 2025-26 (38,788 player-games, scored forward in time): effect sizes matched the single-season run (minutes -14%, points/rebounds/assists about -3%, threes -1.2%, all groups together). More seasons only tightened the intervals, so further history has little value. Opponent, form, schedule and head-to-head terms each add well under 1% on points; not worth building unless a cheap path appears. Mean minutes miss is +0.6 (the flat average runs low for players still on current boards). `python -m scripts.habit_screen --seasons 2023-24,2024-25,2025-26`.

## Strong-rule check (2026-11-14, `python -m scripts.backtest_strong`)
10,044 finished props, 2026-10-29 to 2026-11-13, favored side of the closing price. Whole board hit 53.2% vs 53.5% implied (the price is well calibrated; flat-stake return -6.9%, about the vig).
Top 10% by tilt: 55.5% vs 57.0% implied. Strong (top 10% + sim agrees): 56.5% vs 57.8% implied, -8.4% flat-stake. Agree vs disagree inside the top 10%: +3.4 points [-4.4, +10.8], no clear difference. Tilt tracks the price: hit rate rises with tilt but no faster than the price implies, so the green border marks where the market is most confident, not where it is wrong. Not a value signal.
Whole board, sim agrees with the market 54.9% (price implies 54.1%) vs disagrees 50.5% (implies 52.6%): +4.4 points [+1.3, +7.4]. Suggestive, found after the fact; check by market and by half of the dates, and re-test with the Stage B re-run before acting. Possible use: show sim agreement on every prop, not only the top 10%.
- Stress test of the sim-agreement signal (same data, measured as hit rate minus the rate the price implies): agree minus disagree +2.4 points [-0.5, +5.4], no clear difference. The earlier +4.4 was partly just higher-priced favorites. Same +2.4 in both halves of the dates; most markets lean positive but no market is clear on its own (steals +8.0 [-4.0, +21.5], blocks -15.0 on 48 rows, noise). Sim leaning 60%+ on the favorite: +2.0 [-0.1, +4.3]; the other sim-lean bands -1 to -2. Not established; about 4x the data (around the start of January) would settle it. Re-run `python -m scripts.backtest_strong` then (`--rows` reuses a saved rows file). Keep the strong rule as is until then.

## Codex second opinion (2026-11-15, ledger E9)
- Fixed: live newest-first ordering bug (sorted now, sims recompute on next tick), minutes-fit leak in spread judging, line-match check, game/date-clustered intervals.
- Spread multipliers no longer shipped (stricter gate); minutes model stays. Calibration slope now 0.20.
- Strong-rule re-run (final sim, 10,482 props): agree minus disagree +2.4 [-0.5, +5.8]; still not established. Re-run around early January.
- Codex verdict: pause feature work; next is the scorecard and carefully labeled write-ups.

## Prospective validation system (built 2026-11-15; plan: docs/PROSPECTIVE_PROTOCOL.md, operations: docs/PROSPECTIVE_OPERATIONS.md)
- Built and tested; the two-slate dry run and the freeze are pending (the counted test has not started).
- Optional improvements, deliberately not done (fixing them after the freeze would end the test):
  - Analytic two-way cluster variance (Cameron-Gelbach-Miller) as a third interval beside the pigeonhole bootstrap.
  - Automatic confirmation of voids against the official NBA box score (today only Hardwood's log and box score are used; a manual spot check is advised).
  - Cross-check settlement against Hardwood's own `/props` `actual` and `result` fields and log disagreements.
  - Date-clustered and by-month sensitivity slices in the primary analysis report.
  - A second machine (or hosted runner) as a standby recorder; today one sleeping Mac shrinks coverage.
  - Scorecard UI fed by the ledger (separate work; descriptive only).
