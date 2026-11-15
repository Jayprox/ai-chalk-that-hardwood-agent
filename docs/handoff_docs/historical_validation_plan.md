# Validation plan: historical replay and prospective test (revision 3, planning only)

Prepared 2026-11-15. This revision changed no application code, model parameters, databases or account settings, started no collection or logging, ran no fitting job, and downloaded no historical odds. Hardwood is unchanged. Work beyond reading: public web lookups (section 2.4), one attempted free balance check (section 4.2), read-only inspection of the Hardwood worker and schema, and recalculations on saved rows.

## Summary of changes from revision 2

1. **Prospective test (new section 3):** immutable pregame ledger, one primary snapshot (the worker's `close` pull), alignment, late and missing rules, freeze procedure, decision rules. Described as using **no additional Odds API credits**, not as free.
2. **Historical corrections:** no revision history exists in retained evidence; replay uses retrospectively maintained statistics (2.3).
3. **Cunningham and Duren resolved** from the official NBA box score; DraftKings rule text recorded with its version and limits (2.4).
4. **Track A:** sign error fixed, exact two-way bootstrap, mutually exclusive categories (2.6).
5. **Track B:** H1 is a separation test only; added a market-adjusted stratified sensitivity analysis; precedence rules (2.6).
6. **Power:** relabeled as approximations; observed information gain from three to ten markets is far below proportional (2.7).
7. **Quota:** plan confirmed as 20K credits at $30/month; reset date still unknown; the one authorized balance check **failed** (DNS), so the balance remains the stale 12,697 (4).

**Evidence read:** `odds_history_pilot_report.md`; the unzipped `odds_history_pilot/` folder (the evidence zip was not found, hash the folder before use); `docs/EXPERIMENT_LEDGER.md`; `BACKLOG.md`; the independent reviews as shared in chat; the saved calibration and strong-rule rows; the Hardwood worker (`worker/src/odds.js`, `props.js`, `planner.js`, `scripts/props.js`), `db/schema.sql`, `db/migrations/006_prop_lines.sql` and `docs/architecture.md`.

## 1. Audit-resolution table

| # | Finding | Status | Evidence | Demonstrated error or risk |
|---|---|---|---|---|
| 1 | Live game-log ordering vs backtest ordering | **Fixed with evidence** | `app/simulation.py:92` (`simulate_player` sorts played games oldest-first) and `app/sim.py:155` (`enrich` sorts prior games by date). `tests/test_sim.py:170` shows that reversing the log order does not change the simulation or the stored distribution. `sim_version()` carries a code salt (`order-fix-1`), so stored sims are deleted and recomputed. | **Demonstrated error** (Hardwood returns logs newest-first, so the live minutes model read the oldest three games as "recent"). Backtests were never affected because they sort. Remaining gap: the test uses constructed reversed logs, not a captured API response, and I have not checked that the live store actually recomputed after the restart. |
| 2 | Same threshold for price, simulation and outcome | **Fixed with evidence for price vs simulation; outcome check partial** | `scripts/backtest_strong.py` `build_rows` (about line 89) counts and drops line mismatches and saves `rec_line`. Final run: 10,482 joined, 0 dropped. | Previously a **risk**, not a demonstrated error (0 mismatches found). The outcome is derived from the same backtest record as the simulation line, so equality holds by construction but was never compared with the provider's own settled `result`. |
| 3 | Spread validation used a minutes model fitted on the evaluation season | **Fixed with evidence** | `scripts/fit_sim.py:187-193` fits `mm_early` on rows before the cutoff only, for judging spreads. The pass rule is now "CRPS interval (player-cluster) below zero" plus coverage and PIT improvement. `app/sim_fit.json` has `disp_mult: {}`. `tests/test_fit_sim.py` covers the gate. | **Demonstrated leak** in the earlier fit. Moot for what ships, because no spread multiplier passes and none is shipped. The minutes model itself was always judged forward in time. |
| 4 | Full-season league-parameter look-ahead | **Unresolved** | `scripts/backtest_stage_a.py:100` computes `league_params` over all logs in the scored period. Listed in the ledger under "Known limits". | **Risk, magnitude not measured.** It is a handful of league-level numbers, so likely small, but this has not been measured. The plan below removes it by fitting league parameters before the evaluation window. |
| 5 | Dependence between contracts and the choice of resampling unit | **Partly fixed** | `backtest_strong.py` `diff_ci` resamples by player-game, game or date. Agree-minus-disagree gap: +2.4 [-0.5, +5.8] / [-0.2, +5.1] / [-1.8, +6.2]. The code that computes the printed hit-rate intervals (`group_stats`, using `wilson`) is unchanged and still ignores dependence. | **Demonstrated limitation:** the whole-board and top-10% hit-rate intervals in E7 are too narrow. The difference intervals are fine. Only 16 dates, so the date interval is unstable. Same-player dependence across games is not clustered. Fix the hit-rate intervals later, not in this phase. |
| 6 | Provenance of the updated calibration result | **Partly resolved** | The file exists: 2,340 test records, 3 folds with slopes 0.212 / 0.202 / 0.321, final slope 0.162 on 3,067 records (2026-10-30 to 2026-11-14). Brier raw 0.2610 vs shrunk 0.2505 (diff -0.0105 [-0.0183, -0.0026]); shrunk vs flat 50% +0.0005 [-0.0031, +0.0042]. | **Minor, unresolved:** the file does not record `sim_version`, the `sim_fit.json` hash or the commit, so it cannot prove which model produced it (my own run history says it was the final sim). The shipped `SLOPE = 0.20` is a judgment inside the fold range, not a fitted value. |

## 2. Historical replay plan

### 2.1 What has already been used

| Period | Used for | Status |
|---|---|---|
| 2025-26 full season | Habit screen (E5), minutes model fit and spread test (E6) | Feature structure and parameters were chosen on it. Not a clean holdout. |
| 2023-24, 2024-25 | Pooled habit screen (E5) | Informed the decision to stop adding features, not any fitted parameter. |
| 2026-27 to 2026-11-14 | E1-E4, E7, E8, calibration slope and display rules | Used for everything involving lines. The agree/disagree hypothesis was **generated** here. **Never counts as prospective evidence.** |
| 2025-11-12 (pilot) | Data-access check only | No outcomes scored. |

The minutes model's feature set was chosen using 2025-26 stat outcomes, so no past period is structurally untouched. Refitting coefficients removes parameter leakage, not structure-selection leakage. Only prospective data (section 3) is structurally untouched.

### 2.2 Periods

- **Fit:** Hardwood game logs before 2025-11-01. Refit every learned component (minutes model, league rates and dispersion).
- **Development, 2025-11-01 to 2025-12-31:** build and test the pipeline, fit the calibration slope, confirm identity and void rules, then freeze.
- **Evaluation, 2026-01-05 to 2026-03-31:** parameters refit on logs through 2026-01-04 and frozen. Players' own histories keep updating.

### 2.3 Cutoff convention, information cutoff, and statistic revisions

- Pregame cutoff = **the earlier recorded tip time minus 60 minutes** (provider commence time or Hardwood `tipoff_utc`). This reduces timing risk and cannot guarantee that either recorded schedule equals the true start.
- Tradeoff: the snapshot is about 65 minutes pregame. Injury and lineup news can land in that window, so the price is older than a later user would see. Accepted for leakage safety; every snapshot's age is reported.
- Preserve per game: discovery commence time, returned commence time, Hardwood tip time, requested cutoff, snapshot time, each bookmaker update time. Reject any contract whose update is later than the cutoff.
- **Model inputs use the same cutoff:** only logs dated before the game date (ET); every fitted parameter uses data strictly before its window.
- **Statistic revisions (checked, not estimated).** Retained evidence has no history of statistical values. `player_game_stats` carries `source` and `updated_at` only; `ingestion_runs` logs jobs, not row versions; the NBA.com reconcile "overwrites with NBA.com, reports rows that differed" (`docs/architecture.md`) without keeping the old values. The Nov 12, 2025 box shows `updated_at` of 2026-09-27 (the load date) and `box_score_synced_at` null. `updated_at` alone cannot show whether values changed. **Conclusion: the historical replay uses retrospectively maintained statistics and cannot reconstruct publication-time values.** No correction rate is claimed. The prospective test avoids this by saving the inputs used at forecast time (3.3).
- Do not mix in closing prices; E7 and E8 used closing lines and are not directly comparable.

### 2.4 The four pilot exceptions, settlement and sources

| Name | Resolution |
|---|---|
| Ron Holland | Alias to `Ronald Holland II` (DET box, player id `2d5d171f`), keyed on team, date and id, with a written reason. No fuzzy matching. |
| Bobby Portis | Alias to `Bobby Portis Jr.` after confirming the team in the MIL box. |
| Cade Cunningham | **Confirmed nonparticipant, contract void.** Official NBA box score lists him only in the Detroit inactive line: "Cunningham (Injury/Illness - Left Hip; Contusion)". |
| Jalen Duren | **Confirmed nonparticipant, contract void.** Official NBA box score: "DNP - Injury/Illness - Right Ankle; Sprain". |

- **Sources and dates:** official NBA box score PDF for 2025-11-12 CHI at DET (statsdmz.nba.com, copyright "2025-2026 NBA Properties", no publication timestamp); AP recap carried by CBS Sports dated Nov 13, 2025 ("missing their entire usual starting lineup of Cade Cunningham (hip), Jalen Duren (ankle)"). An ESPN page returned for a different game (Jan 7, 2026) was disregarded.
- **Useful side finding:** the official box also lists Isaac Jones "DNP - Coach's Decision". Neither he nor Duren is in Hardwood's box, so Hardwood omits DNP rows and a missing Hardwood row is consistent with non-participation. It is still only a flag; every void is confirmed against the official NBA box score where it can be, and the ledger records which source confirmed it.
- **DraftKings guidance:** DraftKings House Rules, implementation date August 26, 2025, as hosted by the Massachusetts Gaming Commission (file labeled 8.18.25). General Rules Section II(C)(x)(1): "Unless a market is labelled as 'all-in', 'run or not', or 'all bets action', if a Selection for a bet does not Participate in a specific Event, all bets on such Selection for that Event will be voided." Basketball Sport Rules, Participation: "Participation in a Game shall mean the player entering the Game according to the Game's official box score." **Uncertainty:** I read only part of the document (about 200,000 of 369,503 characters) and found no statement on whether later stat corrections count; this is a Massachusetts-hosted copy, jurisdiction rules can differ; and the version in force from November 2025 to March 2026 was not confirmed. Exact sportsbook returns therefore stay unverified. The void policy (excluded, never zero, never a loss) does not depend on returns.
- Alarms: stop and investigate if voids or unresolved cases exceed 10% of contracts, or if any single game has more than 25% of its contracts excluded. Report exclusions by game and by reason.

### 2.5 Comparisons

Baseline S (prior-games mean per stat, league-level negative-binomial spread, flat minutes, no minutes model, no opponent terms); the frozen simulator; the devigged market probability and posted line.

### 2.6 Tracks, definitions and decision rules (apply to both the replay and the prospective test)

**Track A, projection accuracy.**
- Per stat s (points, rebounds, assists): improvement T_s = 1 - (sum of CRPS of simulator) / (sum of CRPS of baseline) over eligible player-games. **Positive means the simulator is better; negative means worse.** Aggregate T = mean of the three T_s.
- Interval: **pigeonhole bootstrap** (Owen, 2007). Draw player clusters with replacement and, independently, game clusters with replacement; weight each player-game by (times its player was drawn) x (times its game was drawn); recompute T. 2,000 replicates, seed 9, 95% percentile interval [L, U]. This handles shared games and repeated players and is slightly conservative. Sensitivity: player-only and game-only bootstraps, and the analytic two-way cluster variance (Cameron-Gelbach-Miller).
- Per-stat intervals [L_s, U_s] come from the same replicates.
- **Mutually exclusive outcomes, checked in this order:**
  1. **Invalid:** coverage or integrity rule fails.
  2. **Harmful:** U < 0.
  3. **Pass:** L > 0 and T >= 0.02 and no stat has U_s < 0.
  4. **Positive below gate or mixed:** L > 0 and not Pass.
  5. **Worthwhile improvement ruled out:** L <= 0 and U < 0.02.
  6. **Inconclusive:** everything else (L <= 0 and U >= 0.02).
- 2% is a proposed gate and is not retuned on any evaluation data. Earlier percentage gains in squared error and minutes error do not transfer to CRPS.

**Track B, group separation (not proof of incremental skill or betting value).**
H1 asks only whether sim-agree props and sim-disagree props separate in the residual gap (hit minus devigged probability), sim direction frozen. It does not establish that the simulator adds information beyond the market, and it is not evidence of betting value. Agreement groups can differ in market mix and probability level, so a market-adjusted check is prespecified.

| Measure | Role |
|---|---|
| 1. Agree minus disagree residual gap | **H1, primary** |
| 2. Group hit minus devigged probability | Descriptive |
| 3. Agree group hit minus offered-price break-even | **H2, gated** |
| 4. Net return at offered prices (settlement rule stated) | Descriptive |

- **Interval:** same pigeonhole bootstrap over players and games, 2,000 replicates, seed 9, 95% percentile; Delta = point estimate, [L, U] = interval, target = +3.0 points (a design target, not derived from the vig; fixed now and not changed after seeing outcomes).
- **Prespecified market-adjusted sensitivity (S1):** stratify by market x 5-point bins of devigged favorite probability; within each stratum compute the agree-minus-disagree gap; combine with weights equal to stratum size (strata with fewer than 10 props in either arm are pooled with the adjacent bin); same bootstrap. **S2:** logistic regression of hit on logit(devigged probability) and market indicators with an agree indicator, intervals from the same bootstrap. Both are reported as [L_adj, U_adj].
- **Mutually exclusive outcomes, in this order:**
  1. **Invalid:** coverage or integrity rule fails.
  2. **Reversed:** U < 0.
  3. **Separation established:** L > 0 and Delta >= 3.0 and S1 has L_adj > 0.
  4. **Separation not robust to market mix:** L > 0 and Delta >= 3.0 and S1 has L_adj <= 0.
  5. **Positive below target:** L > 0 and Delta < 3.0.
  6. **Worthwhile effect ruled out:** L <= 0 and U < 3.0.
  7. **Inconclusive:** L <= 0 and U >= 3.0.
- **H2 is evaluated only if outcome 3 occurs.** The hurdle comes from actual offered prices (on the saved data about 3.5 points between break-even and devigged probability; agree group hit 54.8%, devigged 54.2%, break-even 57.8%). H2 passes if the lower bound of (hit minus break-even) is above zero. Outcomes 4 to 7 leave H2 untested and returns descriptive. Profit is never claimed.

### 2.7 Sample size (approximations)

All power figures are approximations conditional on the assumed variance, market mix and dependence. They are not guarantees.
- Historical replay (points, rebounds, assists only), assumptions: H1; two-sided 5% level; 80% target power; about 60/40 agree/disagree; game-cluster standard error 1.65 points at 116 games (re-estimated from the saved 2026-27 closing-line rows) scaled by the square root of games; a constant true difference; the pigeonhole interval will be somewhat wider.

| Games | Credits (30 per game) | SE | Minimum detectable (80%) | Power at +3.0 |
|---:|---:|---:|---:|---:|
| 120 | 3,600 | 1.63 | 4.6 | 45% |
| 200 | 6,000 | 1.26 | 3.5 | 66% |
| 260 | 7,800 | 1.11 | 3.1 | 77% |
| 300 | 9,000 | 1.03 | 2.9 | 83% |

- **Do not assume more markets mean proportionally more independent information.** On the same 116 games, the standard error was 1.65 with about 33 contracts per game (three markets) and 1.45 with about 90 (ten markets). Tripling contracts shrank the error by about 12%, not the roughly 40% independence would give, so contracts within a game are strongly dependent. Any figure for a ten-market sample is an extrapolation from this one ratio.
- Prospective sample (ten dashboard markets): SE of about 1.45 x sqrt(116/G), so about 1.10 at 200 games and 0.90 at 300 games, with approximate power at a true +3.0 of about 78% and 91%, and at +2.4 about 59% and 76%. These rest on one 116-game, 16-date estimate from closing-line data.
- If the true effect is near the 3-market estimate of +0.2, the replay cannot detect it at any affordable size, and a non-significant result would not show there is no effect.

## 3. Prospective test (no additional Odds API credits)

This uses lines the Hardwood worker already pulls and needs no additional Odds API credits. It is **not free**: it costs engineering time (section 5), a machine that stays awake through every pregame window, and review effort. It is separate from the historical 60-minute-cutoff experiment, uses different snapshots, and its results are reported separately.

### 3.1 Primary snapshot

- **Primary: the worker's `close` snapshot** (pulled once, in the 35 minutes before `tipoff_utc`; `prop_lines` row with `snapshot = 'close'`, DraftKings).
- Why close: it is the most recent price before the game, the worker designates it as "the line that grades the prop", it is one pull per game (the worker skips games whose `props_close_at` is set), and E7/E8 used closing boards. Cost: it is later than the historical 60-minute snapshot and not comparable to it.
- The opening snapshot is secondary and descriptive only.

### 3.2 Alignment, late and missing observations, exclusions

- **Unit:** game x player x market with a DraftKings `close` row holding both prices.
- **Forecast/price alignment:** the forecast is computed for the exact `line` in the close row and saved with that row's `line`, `over_price`, `under_price`, `book_updated_at` and `fetched_at`.
- **Valid record:** `fetched_at` < `tipoff_utc`, `book_updated_at` <= `fetched_at`, ledger `recorded_at` > `fetched_at` and < `tipoff_utc`. Anything else is an exclusion, counted by reason, never repaired.
- **Missing close pull** (worker failure, quota guard below 150, or the machine asleep): the game is excluded. **No fallback to the opening line.**
- **Line changed after record:** at settlement, compare each ledger record with the final Hardwood row. A mismatch (a manual overwrite is possible through `npm run props -- --snapshot close`) excludes the record and is reported; the ledger value is never replaced.
- Other exclusions: fewer than 5 prior games this season; sim exactly 0.5; unmatched player (`manual_review`); void or unresolved participation (2.4); tie at the line (push).
- **Coverage:** at least 85% of games with a valid close record and at least 90% of valid-record contracts settleable; otherwise **Invalid**.

### 3.3 Immutable pregame ledger (what is saved, and when)

Written before tip, append-only, in a store separate from the dashboard's `sims` table (which is deleted and recomputed when the sim version changes). Each record holds:
- identity: game, player, market; exact `line`, `over_price`, `under_price`, `book_updated_at`, `fetched_at`, snapshot kind, `tipoff_utc`, and the provider event id if available;
- forecasts saved at that time: the simulator's full pmf, `p_over` at the exact line, the frozen shrink slope and shrunk probability, the sim side and the **agreement decision** against the devigged favorite; the Baseline S distribution; the devigged market probability;
- version: `sim_version()`, SHA-256 of `app/sim_fit.json`, parameters, calibration slope, git commit and clean-tree flag, freeze record id;
- input provenance: the list of prior game ids and dates used, a hash of the exact statistic values read, and their `updated_at` values;
- `recorded_at` (UTC), the previous record's hash and this record's hash (hash chain).
Rules: inserts only (database triggers reject updates and deletes); a daily integrity check recomputes the chain; **primary predictions are never reconstructed after results are known**. Settlement is a separate table written after games, never touching ledger rows.

### 3.4 Freeze procedure

1. Preconditions: ledger and integrity check built and tested; a dry run covering at least two full slate days; every dry-run check passes (at least 95% of eligible close pulls recorded before tip, chain verifies, zero post-tip records, no overwritten lines).
2. Manifest: hashes of `app/sim_fit.json`, parameters, calibration slope, code revision, the analysis code, this plan and the metric definitions.
3. You commit the manifest with git. A freeze record carrying the commit hash is appended to the ledger.
4. **The freeze point is the first game date after the freeze record's timestamp.** Only records created after it count. No backdating. The 116 games examined through 2026-11-14, any games between then and the freeze, and every dry-run record are **excluded** and not counted as prospective evidence.
5. After the freeze nothing in the model, parameters, code or definitions changes. A necessary change ends the test and starts a new one with a new freeze.
6. Looks: integrity metrics only, never hit rates or returns, until the analysis.

### 3.5 Primary analysis and decision rules

- Population: ten dashboard markets, valid records only.
- **One analysis, at 300 eligible games** (a game counts if it has at least one valid record). Track B (H1, then S1, then H2 if reached) and Track A use the rules in 2.6. 200 games is a scheduling milestone for integrity review, not a look at outcomes.
- Secondary (not decisive): the three-market subset, by-market and by-month slices, date-clustered sensitivity, descriptive returns.
- **Timing:** 300 games is roughly five to six weeks, a scheduling estimate that depends on slate sizes, the valid-record rate and breaks. Do not read it as a promise.
- **Stop conditions:** coverage under the rule, a timestamp violation, a chain break, or a model change.

## 4. Collection budget and quota

### 4.1 Facts

- **Plan:** 20K credits, $30/month (confirmed by you). **Reset date: unknown.** Not in repo evidence; it comes from your account dashboard.
- **Existing consumption:** the Hardwood worker pulls up to 120 credits per game per call (10 per market, 12 markets): an opening pull from 09:00 ET (retried hourly, never later than 90 minutes before tip) and a closing pull in the 35 minutes before tip. Its guard pulls closing lines only below 1,000 credits and nothing below 150.
- **Readings:** 18,924 (2026-11-03T02:25Z log); 15,720 (2026-11-09T14:01Z log); 12,848 before the pilot (2026-11-15T18:04Z, derived from the pilot ledger); 12,697 after it. Measured burn about 494/day then 466/day, about 480/day on average, from three readings.

### 4.2 The authorized balance check

I ran `npm run props -- --check` in the worker folder exactly once. It **failed**: `getaddrinfo EAI_AGAIN api.the-odds-api.com`. The Pro's command sandbox could not resolve the host, so no request left the machine and no credits were consumed. No credentials were printed. **The balance is therefore still the stale 12,697.** I did not retry. To get a current value, run the same command from a normal terminal on the Pro (outside the sandbox) and share only the line `credits left: ...`, or read the balance from your dashboard.

### 4.3 Reserve and caps (conditional)

Available for historical pulls = verified balance - (days to reset x 480 x 1.15) - 1,000 guard. With 12,697 (stale):

| Days to reset | Production need with margin | Available |
|---:|---:|---:|
| 15 (about Dec 1) | 8,280 | about 3,400 |
| 20 | 11,040 | about 660 |
| 25 or more | over 13,800 | none |

A fresh 20K cycle leaves roughly 2,400 (with margin) to 4,600 (without) after production and the guard. The historical plan (about 8,600 credits: Phase A 2,100, Phase B 6,500) therefore does not fit one cycle. **All historical caps stay conditional on a verified balance and reset date.** The prospective test needs no additional credits. No upgrade is recommended; a need would exist only if you want the historical replay completed quickly after the verified numbers are in.

### 4.4 Historical collection scope (unchanged, conditional)

DraftKings, points/rebounds/assists, one snapshot per game at the 2.3 cutoff; blind selection by lowest SHA-256 of the provider event id per date; development about 60 games (2025-11-01 to 2025-12-31), evaluation up to 200 games (2026-01-05 to 2026-03-31); 30 credits per game plus one per discovery; no automatic paid retries. Storage separate from Hardwood in a dedicated folder with immutable raw files, hashes, request parameters (no keys), and a manifest.

## 5. Implementation needed to preserve pregame predictions (bounded scope, for approval)

All inside the agent repo. Hardwood, the simulator, the dashboard display and the scheduler's existing behavior are unchanged.
1. `app/ledger.py`: append-only store (SQLite file separate from the dashboard store, insert-only triggers) and the record format in 3.3; plus the hash chain.
2. A hook in the scheduler tick, after collection and enrichment: for each valid close-snapshot contract, write a record before tip. Read-only against Hardwood.
3. `app/baseline.py`: Baseline S distribution (small).
4. `scripts/freeze.py`: builds the manifest and freeze record.
5. `scripts/ledger_check.py`: integrity report (counts by exclusion reason, `recorded_at` before tip, chain verification, overwritten-line check, coverage).
6. Tests for each piece, including that no ledger row can be updated or deleted and that a sim-version change leaves the ledger untouched.
7. **Operational requirement:** the scheduler must be running on a machine that stays awake and online through the pregame windows (roughly late afternoon to late evening ET). A sleeping laptop silently shrinks coverage.
Not in scope now: the settlement and analysis scripts (written before the analysis, with their hash recorded at the freeze), and the scorecard.

**Earliest defensible freeze:** after approval, build and tests (about a day), then a two-slate dry run and a clean integrity report. If approval comes today, the earliest realistic freeze commit is the evening of Nov 18, with the first counted slate on Nov 19. That is a scheduling estimate; no date is committed until the dry run passes, and it is never backdated.

## 6. Product work independent of any predictive claim

- **Timestamped scorecard:** an immutable log of each prop's displayed signal at snapshot time, settled after the game, showing hit rate beside price-implied probability and offered-price break-even. Descriptive only. The prospective ledger can feed it later; the scorecard is not the evaluation.
- **Factual write-ups:** last-10 games, splits, head-to-head and market-implied probabilities, stated as facts with sources and timestamps.
- **Visible disclaimer:** research only, not betting advice.
- **Must stay unvalidated:** that the sim beats the market (shrunk sim vs flat 50% on Brier: +0.0005 [-0.0031, +0.0042]); that HIGH LEAN carries value; that sim agreement is a signal (+2.4 on ten markets, found after the fact, +0.2 on points, rebounds and assists); any return or profit statement; any teammate-absence or opponent effect.
- Research-product readiness is about accurate data and honest labels and is separate from evidence of betting advantage, of which there is none yet.

## 7. Decisions still needed from you (nothing below has started)

1. **Quota reset date** from your Odds API dashboard, and the current balance (run the check from a normal Pro terminal, or read the dashboard).
2. Approve the ledger implementation scope in section 5, including the always-awake-machine requirement (which machine will run the scheduler).
3. Approve the prospective protocol: close snapshot as primary, the validity and exclusion rules, one analysis at 300 games.
4. Approve the Track A and Track B definitions, bootstrap, categories, S1 sensitivity and the +3.0 and 2% thresholds, fixed before any evaluation outcome.
5. Approve the freeze procedure and that the earliest freeze is after a passing two-slate dry run, with all earlier games excluded.
6. Decide whether the historical replay goes ahead, and, once item 1 is known, approve Phase A with a hard stop (suggested 2,100 only if available credits allow).
7. Approve the refit on pre-period Hardwood logs (read-only requests and a fitting job) for the replay.
8. Approve starting the scorecard and factual write-ups as independent work.
9. Plan upgrade: not recommended; revisit only after item 1.
