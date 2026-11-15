You are an independent reviewer giving a SECOND OPINION on an NBA props research simulation. You are not building anything. This is ONE round: you review, you report, you stop.

## Where to look
Repo: /Users/jayprox_pro/Documents/Projects/git/ai-chalk-that-hardwood-agent
Read first, in this order: docs/EXPERIMENT_LEDGER.md (everything already tested, with results and decisions), BACKLOG.md, app/simulation.py, app/minutes_model.py, app/sim.py, app/calibration.py, scripts/habit_screen.py, scripts/fit_sim.py, scripts/backtest_calibrate.py, scripts/backtest_strong.py. Saved results are in evals/runs/ (JSON). Do not read other files unless a finding requires it.

## What the system is
A minutes-driven Monte Carlo: expected minutes (flat average plus a fitted adjustment) times a shrunk per-minute rate, with negative-binomial noise per stat, giving a distribution for each player-game and each prop market (points, rebounds, assists, threes, steals, blocks and their sums). A dashboard ranks props by the sportsbook's own devigged price tilt, and draws a green "high lean" border when the prop is in the top 10% of its market AND the simulation leans the same way. Prop lines exist only from about 2026-10-20 (about 16 finished days). Earlier seasons have game logs (stats, minutes, rest, opponent, venue, plus/minus) but no lines.

## What has been established (do not redo, details in the ledger)
The market price is well calibrated; the sim adds no measured skill beyond it; the raw sim is overconfident and is shrunk for display; a fitted minutes model cut minutes error 13% and slightly improved Brier on this season's lines; the green border is NOT a value signal (hit rates equal the price's implied rate, flat-stake returns are about the vig); sim agreement beats price-implied hit rate by about +2.4 points but the interval includes zero (not established).

## What I want from you
Only three things:

A. METHOD AUDIT. Look for real errors in how E3 to E8 were measured: leakage or look-ahead, selection or survivorship bias, multiple comparisons or post-hoc slicing, wrong clustering, mismatch between what the backtest scores and what the dashboard shows, closing-line versus earlier-line effects, anything in the code that does not do what its docstring says. For each finding give file and function, the mechanism, the likely direction and rough size of the bias, and how to confirm it. If something is only a risk and not a demonstrated error, label it RISK.

B. NEW EXPERIMENTS. Propose at most THREE experiments that are NOT already in the ledger and could change a decision (ship, drop or keep a feature, or change how the dashboard presents something). For each: the hypothesis; the data that exists today; the design (split, metric, comparison); the success threshold and the stop condition, both stated before running; the effort; and which decision it would change. Prefer experiments that can be run on data we already have. Do not propose experiments that need data we cannot get (historical lines before 2026-10-20) unless you say how to obtain it.

C. VERDICT. In five lines or fewer: is further simulation work worth doing before more finished game days accumulate, or should effort move to the other milestones (model-written write-ups, a scorecard, an availability researcher)? Say what would change your mind.

## Rules
- Read-only. Do not edit files, run long jobs, call paid APIs or rewrite the engine. You may run the existing scripts' tests or small checks on saved results if needed.
- Every item you raise must be tagged NEW, ALREADY TESTED (cite the ledger ID and say why you still raise it, with evidence) or RESTATES (of a ledger row). Do not re-propose ledger rows without new evidence.
- If you find nothing worth reporting in A or B, say "nothing found" for that section. Do not invent items to fill space.
- Be specific and quantitative. No generic advice (more data, more features, better models) unless tied to a named gap.
- Maximum length: about 700 words, plus a short table if useful.

## Output format (use exactly these headers)
A. Method audit
B. New experiments (up to 3)
C. Verdict
Open questions for the owner (only if a decision truly needs them)
