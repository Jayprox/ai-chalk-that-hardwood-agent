# Prospective validation protocol `prospective-v1`

Implements `docs/handoff_docs/historical_validation_plan.md` (revision 3) plus five corrections. Everything numeric lives in `app/protocol.py` and is hashed into the freeze manifest (`scripts/freeze.py`). After the freeze none of it changes; a necessary change ends the test and starts a new one with a new freeze. The historical replay is separate and unaffected.

## 1. What is recorded, and when

- **Unit:** game x player x market with a Hardwood **`close`** snapshot row holding a line and both prices. Ten markets (pts, ast, reb, fg3m, pa, pr, ra, pra, stl, blk). Regular-season games only.
- **When:** `app/record.py` runs inside every scheduler tick (every 5 minutes, only if `LEDGER_ENABLED=true`). A game is polled from 40 minutes before tip-off until tip-off. The first valid close row for a contract is recorded once; a record is never replaced.
- **Valid record:** `fetched_at` is before tip-off and no earlier than 90 minutes before it; both prices usable; a line present; the forecast is computed and the row written before tip-off (`recorded_at` < `tipoff_utc`). Anything else is logged in `rejections` with a reason and never repaired. Reasons that end a contract: wrong season type, close row fetched after tip-off, fewer than 5 earlier games, stale inputs, written after tip-off. Reasons that are re-checked on the next tick (the worker may still refresh the row with a valid pull): early or malformed close row, no league snapshot yet, inputs unavailable.
- **Forecast:** the unchanged simulator (`simulate_player`, shrink slope 0.20, seed from player and game), computed **fresh at record time** for the exact recorded `line`, plus Baseline S (prior-games mean per stat with the league negative-binomial spread, flat minutes, same eligibility). Agreement = simulator side equals the devigged market favorite. The dashboard `sims` cache is never read or written.

## 2. Original contract preserved (correction 1)

The recorded `line`, `over_price`, `under_price`, `fetched_at` and snapshot kind are the contract. If Hardwood later shows a different line or price (a manual overwrite is possible), the difference is logged in `source_diffs` and **nothing else happens**: the record stays, and it is settled against the saved line. Records are excluded only by the predefined rules in sections 5 and 6.

## 3. Exact inputs and freshness (correction 2)

- **Player inputs:** each player's game log rows dated before the board date (ET), oldest first, saved verbatim as a content-addressed zlib blob (`inputs`, SHA-256 of the canonical JSON, hash-checked on read). `input_hash` is stored on every record.
- **League parameters:** one frozen snapshot per board date, estimated by the unchanged `league_params` from the saved earlier-dated logs of that date's board players (at least 40 players with 5+ played games), saved as a blob (`league_hash`). It is created at the first record attempt of the date and not updated. A player added to the board later still gets his own saved inputs but does not change the league.
- **Three times per record:** `inputs_retrieved_at`, `league_created_at`, `forecast_computed_at`, then `recorded_at` (microsecond UTC), all stored in the hashed row.
- **Fresh vs cached:** inputs are retrieved at most once per board date and reused by later ticks (cached for the day), but only if the freshness rule holds at forecast time: retrieved at or after 08:00 ET on the board date (late finals have landed), before tip-off, and at most 15 hours before the forecast is computed. The league snapshot obeys the same rule. A violation is a `stale_inputs` rejection.
- `scripts/ledger_check.py --replay N` recomputes N saved forecasts from their saved inputs, parameters and seeds and compares the stored distributions.

## 4. Provenance on every record

`sim_version`, SHA-256 of `app/sim_fit.json`, the full parameter set, calibration slope, `code_sha` (hash of the hashes of the core files listed in `app/provenance.py`, including the analysis, settlement and coverage code), git commit, protocol version, freeze id, previous-row hash and row hash. The ledger is an append-only SQLite file (`data/ledger/ledger.db`, WAL, synchronous FULL) with triggers rejecting UPDATE and DELETE on every table, a hash chain, and a JSON-lines mirror (`data/ledger/mirror/`). It is separate from the dashboard store.

## 5. Eligibility independent of collection success (correction 4)

- **Expected-game manifest:** every tick registers yesterday's, today's and tomorrow's Hardwood schedule in `expected_games` (first-seen time, tip-off, season type; tip-off and status changes are kept in `manifest_events`). The daily job re-reads the last 14 days (never earlier than the first full day after the recorder first ran) and adds any missing game flagged **registered late** (it cannot be proven pre-outcome, stays expected, and counts as uncovered).
- **Expected game** = regular-season game with tip-off in the past. A game postponed, or not final 24 hours after tip-off, is `not_played`: listed, and removed from the denominator.
- **Covered game** = at least 30 valid recorded contracts from at least 8 players in at least 3 markets. Games never collected (`window_not_polled`, `no_close_rows_observed`, `no_valid_records`, `below_minimum`) stay in the denominator with their reason. **No game is ever substituted.**
- **Contract denominator** = every close contract observed for an expected game (`universe`), recorded or not, including those first seen after tip-off.
- **Coverage rules:** covered games / expected games at least 85%; recorded / observed contracts at least 90%; settled (incl. pushes) at least 90% of matured contracts; void + unresolved at most 10% of matured contracts and at most 25% within any game. Failing any of these makes the test **Invalid**. Dry-run bars: 95% and 95%.
- Coverage and health reports use statuses and counts only and never read actuals or results.

## 6. Settlement (separate `settlements` table; ledger rows untouched)

**Rule: confirmed no participation -> DNP and the contract is void. Missing information -> unresolved. A DNP is never scored as zero production or as a loss.**

- actual = sum of the market's statistics from the player's game-log row (falling back to his box-score row) after the game is final. Every required statistic must be present and numeric. A genuine numeric 0 is a valid value; a missing component, in a combo market especially, leaves the contract unresolved. Nothing is defaulted to zero. actual equals the saved line: `push`.
- **Sufficient evidence of no participation:** an explicit DNP / inactive flag on his box-score row (matched by player id) or log row, or a manual confirmation against an authoritative source (`scripts/confirm_dnp.py`, which stores source, time and note append-only). Result: `void_dnp`.
- **Not sufficient (stays unresolved for manual confirmation):** absence from the game log, missing minutes, a displayed 0 minutes (it may be rounded), a box score listing five or more players per team. When the log row is missing the box score is checked; if participation still cannot be established from retained evidence the contract stays unresolved.
- Evidence of participation: minutes above 0 or any nonzero required statistic; then the contract is settled from the first source that has every required statistic, or left unresolved if none does.
- The Cunningham and Duren pilot cases (2025-11-12, CHI at DET) are kept as evidence in `docs/evidence/pilot_dnp_confirmations.md`.
- Game not final 24 hours after tip-off: `void_not_played`. Unresolved contracts are counted as `unresolved` once matured and count against the exclusion limits.
- Games are re-read for 14 days; a changed result is appended as a new version, the latest version wins, history is kept. A game matures at tip-off + 3 h + 48 h = **51 hours after tip-off**. Stat sources are Hardwood only; manual confirmation is the only way an unresolved contract becomes a void.

## 7. Primary analysis (correction 3; `app/analysis.py`, hashed into the freeze)

One analysis at the **300th covered game** (ordered by tip-off then game id) once it has matured, using all valid settled contracts of those 300 games. `scripts/prospective_analysis.py` refuses to run earlier, refuses a second run (stores the first in the ledger), and has `--reproduce`.

- **Track A:** discrete CRPS against the actual for points, rebounds, assists; T_s = 1 - sum CRPS(simulator) / sum CRPS(Baseline S); T = mean of the three. Positive = simulator better. Categories in order: invalid, harmful (U < 0), pass (L > 0, T >= 0.02, no stat with U_s < 0), positive below gate or mixed, worthwhile improvement ruled out (L <= 0, U < 0.02), inconclusive.
- **Track B:** for settled non-push contracts with a market favorite: hit = the favorite's side won; p_fav = devigged favorite probability; residual = hit - p_fav. **H1** = 100 x (mean residual of agree minus disagree). Categories in order: invalid, reversed (U < 0), separation established (L > 0, H1 >= 3.0 points, S1 lower bound > 0), separation not robust to market mix (L > 0, H1 >= 3.0, S1 lower bound <= 0 or S1 not estimable), positive below target, worthwhile effect ruled out (L <= 0, U < 3.0), inconclusive.
- **S1 (deterministic market adjustment):** strata = market x favorite-probability bin with edges 0.50, 0.55, 0.60, 0.65, 0.70, above. Within a market, bins are scanned from lowest to highest, accumulating until each arm holds at least 10 (weighted) contracts; a deficient remainder merges into the stratum below; a market that never reaches 10 in both arms is dropped (insufficient overlap). S1 = size-weighted mean of stratum gaps. It is estimable only with at least 3 markets, 5 strata and 50% of the weighted contracts retained. The pooling is redone on every bootstrap replicate's weights; a replicate where S1 is not estimable is discarded.
- **S2 (sensitivity, no decision role):** weighted logistic regression of hit on logit(p_fav), market indicators and agree; reported as the average marginal effect of agree (IRLS, at most 8 steps, warm start, non-converged replicates discarded).
- **H2 (gated):** evaluated only when H1 is "separation established": lower bound of 100 x mean(hit - offered-price break-even) over agree contracts above 0. Returns are descriptive only; profit is never claimed.
- **Bootstrap:** pigeonhole (Owen 2007): players and games resampled independently with replacement; a contract's weight is (times its player was drawn) x (times its game was drawn); 2,000 replicates, seed 9, 95% percentile interval over non-discarded replicates. More than 5% discarded makes the interval **unstable** and its category inconclusive. Player-only and game-only intervals are reported as sensitivity.
- **Resampling limits (stated, not fixed):** players and games are treated as crossed independent clusters; dependence across consecutive dates, between teammates in different games, and from shared injury news is not modeled; the analytic two-way cluster variance is not implemented; with a few hundred games the interval is approximate.
- **Descriptive extras (never decisive):** group hit / devigged / break-even / net return per unit, per-market H1 point estimates, three-market H1.

## 8. Freeze and no backdating

**Dry-run acceptance has two stages. Neither can pass because nothing has matured.**

*Stage 1, recording (after at least two full slate days, judged from `--dry-from`, the first date the recorder ran for the whole slate):* `ledger_check.py --scope dry --from-date D --stage recording`. Passes at the dry-run thresholds (95% of expected games and 95% of observed contracts recorded), every covered game has at least 30 contracts, 8 players and 3 markets, zero records after tip-off, no duplicates, chain and mirror verify, at least 2 slate dates. This proves the recorder worked. It does not allow a freeze.

*Stage 2, final acceptance (`--stage final`):* all of the following, with the thresholds unchanged:
1. Stage 1 still passes.
2. **Every expected game of every dry-run slate date (on or after `--dry-from`) has matured, i.e. 51 hours have passed since its tip-off** (postponed games are excluded as such). With two slate days the last game's maturity is its tip-off + 51 h.
3. At least one contract has matured; a ledger with nothing matured never passes.
4. Settlement evidence: matured contracts that settled or pushed are at least 90% of matured contracts; void + unresolved are at most 10% overall and at most 25% in any single game. Unresolved contracts must be confirmed manually (section 6) or they count against these limits.
5. A settlement run with no errors has been recorded after the last maturity (`settle_runs`), so the results reflect the final box scores.

Freeze steps:
1. Stage 2 passes.
2. `python scripts/freeze.py --build` writes `docs/freeze/manifest.json` (constants, model fingerprint, hashes of every core file, this document, the operations document, the plan and the tests). You review and commit it with the intended source files.
3. `python scripts/freeze.py --record --commit <hash> --dry-from <first full slate date>` refuses unless HEAD is that commit, the files hashed in the manifest are tracked and unmodified, the manifest equals a fresh rebuild, no freeze exists, and stage 2 passes. It appends the freeze record and sets **first counted date = the next ET calendar date after that moment**.
4. A record counts only if its date is on or after that date **and** it was written after the freeze record (`freeze_id` is assigned by the ledger itself). Dry-run records and every game examined earlier never count.
5. After the freeze nothing in the model, parameters, code or definitions changes. During the test, look only at `scripts/ledger_check.py` (integrity and coverage). Not hit rates, not returns.
