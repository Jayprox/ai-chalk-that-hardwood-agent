# Experiment ledger (props research dashboard + simulation)

Purpose: one place that says what has been tested, what it showed, and what was decided, so nobody (human, Claude or Codex) re-proposes a settled question. Add a row for every new experiment. Before proposing work, search this file.

Rule for any new proposal: cite which ledger rows it builds on, say what is NEW compared with them, state the success threshold and the stop condition BEFORE running, and say how it would change a decision. If none of that can be written, it is not ready.

Data facts: Hardwood has prop lines only from the start of the 2026-27 season (about 2026-10-20). Earlier seasons have game logs (stats, minutes, rest, opponent, venue, plus/minus) but no lines or results. Lines are DraftKings open and close snapshots only. Dates are ET boards. This season so far: about 16 finished game days.

Scoring conventions: Brier on raw simulated P(over) vs outcome (pushes and DNPs excluded); 95% intervals from a cluster bootstrap (cluster = player-game, or player where noted); forward chaining (fit on earlier dates, test on later) wherever something is fitted.

| ID | Question | Method and data | Result | Decision | Where |
|---|---|---|---|---|---|
| E1 | Does the simulation beat the market price? | Stage A/A2 backtests, 14 days of lines, logistic models on market and sim logits | Market price is the strongest signal; sim adds no measurable skill beyond it; raw sim overconfident (calibration slope about 0.13 to 0.29) | Sim direction only is used; any shown sim probability is shrunk and labeled | scripts/backtest_stage_a*.py |
| E2 | Do teammate absences (Stage B) add anything? | Upper-bound test with realized absences | Nothing out of sample | Availability researcher stays on the backlog; re-run Stage B in 4 to 6 weeks | scripts/backtest_stage_b.py |
| E3 | Recalibrate the displayed sim probability | Slope-only logistic recalibration, forward chained | Beats raw clearly (Brier -0.0106 [-0.0202, -0.0011]); slope per fold 0.10 to 0.19, then 0.22 to 0.29 after E6, then 0.20 to 0.32 (all data 0.16) after E9 fixes; final sim raw Brier 0.2610 -> 0.2505 | Shipped, slope 0.20 (app/calibration.py) | scripts/backtest_calibrate.py |
| E4 | Last-season prior in the sim | Paired, caps 300/600/1200 minutes, 2,397 records | No clear difference (all intervals span zero) | Built, left OFF (Params.last_cap_minutes = 0) | scripts/backtest_blend.py |
| E5 | Which pre-game facts explain the sim's stat error? | Habit screen: 2025-26 (13,910 player-games), then pooled 2023-24 to 2025-26 (38,788), ridge on the miss, forward chained, cluster by player | Role facts cut minutes error 13%; all facts together about 3% on pts/reb/ast, 1.2% on threes; opponent, form, schedule, head-to-head each under 1%; same sizes across 1 and 3 seasons | Minutes model shipped (E6); smaller terms not built | scripts/habit_screen.py |
| E6 | Ship the minutes model and wider spreads | Fit on 2025-26, checked forward; spreads fitted on early half, checked on later half | Minutes squared error -13.2% (95% interval clearly below zero); central 80% range for points 73% to 77%. Paired on this season's lines: Brier 0.2592 to 0.2559, diff -0.0033 [-0.0067, -0.0001] | CORRECTED by E9: only the minutes model ships (minutes error -11.9% on the clean refit); spread multipliers are NOT shipped (failed the stricter gate). Sim still no better than a flat 50% | scripts/fit_sim.py |
| E7 | Does the green border (top 10% tilt + sim agrees) mean value? | 10,044 finished props 2026-10-29 to 11-13; favored side of the closing price; hit vs implied; flat-stake return | Whole board 53.2% vs 53.5% implied; top 10% 55.5% vs 57.0%; strong 56.5% vs 57.8%; flat-stake about -7% to -9% (the vig). Tilt tracks price, no edge | Label changed to HIGH LEAN, described as not a value signal | scripts/backtest_strong.py |
| E8 | Does sim agreement beat what the price implies? | Same rows; gap = hit minus implied; slices by market, half, week, prior games, sim lean | Agree minus disagree +2.4 points [-0.5, +5.8] by player-game, [-0.2, +5.1] by game, [-1.8, +6.2] by date (10,482 props, 0 line mismatches, final sim); same +2.4 in both halves; no market clear alone; sim lean 60%+ gap +2.0 [-0.1, +4.3] | Not established; keep rule; re-run when about 4x the data exists. Run it as soon as there are more finished days: `python -m scripts.backtest_strong` | scripts/backtest_strong.py |
| E9 | Codex second opinion (A1-A4, B1-B2) | Read-only review of code and saved results, triaged item by item | A1 real leak (minutes fit on all rows before judging spreads); A2 real live bug (API logs newest-first, minutes model read the oldest 3 games as recent; backtests unaffected); A3 interval sensitivity; A4 line-match unchecked | A1 fixed (minutes fit on early rows only for judging) and spread gate tightened to CRPS interval below zero, so no spread shipped; A2 fixed by sorting in simulate_player and enrich plus version salt (stored sims recompute); A3 added game/date intervals; A4 added check, 0 mismatches; B2 covered by order-invariance tests; verdict accepted: pause feature work | scripts/fit_sim.py, scripts/backtest_strong.py, tests |

## Known limits (already acknowledged)
- Players in E5/E6 are those on boards in the last 30 days (survivorship); the flat minutes average runs about 0.6 minutes low in that sample.
- League dispersion and rate parameters are estimated from the same season being scored (small look-ahead), in every backtest.
- E7/E8 use closing lines, not the lines visible earlier in the day; the dashboard percentile is computed over the live board, the backtest over the closing board.
- The earlier E6 gate (at least 1% better) was replaced by 'CRPS interval below zero'; replay with a frozen pipeline needs a later holdout (Stage B re-run).
- 16 days is low-powered: effects under about 3 points cannot be resolved yet.
- `started` is empty in the game logs, so the starter-rate feature has no weight.

## Settled decisions (do not re-open without new evidence)
- Score is named signal strength (market tilt percentile within market); the green border is labeled HIGH LEAN and is not a value signal.
- Sim output shown to the user is shrunk toward 50/50; direction drives the border rule.
- No engine rewrite. No repair loop. Evaluation schedule and release gates are unchanged.
