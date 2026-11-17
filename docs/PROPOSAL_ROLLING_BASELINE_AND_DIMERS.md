# Proposal review: rolling baseline and Dimers benchmark (plans only; nothing here has been run)

Status: for approval. Separate from prospective validation: no ledger access, no simulator, calibration, signal, threshold, scheduler or launchd change.

## 1. Rolling baseline: `actual - mean of the previous 10 played games`

Recommendation: **do not run a rolling-baseline experiment. No hypothesis is justified.** Reasons, from the repo:

- **The sim-vs-prior-games-mean question is already pre-registered and running.** `PROSPECTIVE_PROTOCOL.md` defines Baseline S (prior-games mean per stat, league negative-binomial spread, flat minutes) and Track A (discrete CRPS for points, rebounds, assists; simulator vs Baseline S; pass needs T >= 0.02 with L > 0 and no stat with U_s < 0). A 10-game rolling mean is a close cousin of Baseline S. Running a held-out MAE comparison on 2026-27 games now would duplicate Track A and overlap its counted window, which is peeking.
- **The contextual screens are already done at scale.** E5 (38,788 player-games, three seasons, forward chained, cluster by player): opponent, form, schedule and head-to-head each reduced stat error by under 1%; role facts cut minutes error about 13% and that shipped as E6. A residual `actual - rolling mean` regressed on defense rank, rest, venue or opponent repeats E5 with a weaker baseline. `docs/EXPERIMENT_LEDGER.md` says not to re-open that without new evidence, and there is none.
- **"Why did a line hit" cannot be tested here.** E1/E7/E8 show the closing price already carries what is knowable (tilt tracks price, no edge). Hardwood has lines only from about 2026-10-20, so historical lines do not exist to explain.

How the baseline behaves, for the record (needed if anyone revisits it):

- **Season boundary:** do not carry games across the offseason; roles, rosters and minutes reset (E4: last-season prior made no clear difference). Use only same-season games.
- **Warm-up eligibility:** require at least 10 prior played games in the same season, which drops each season's first 10 games for everyone, and apply the identical rows to every predictor being compared.
- **Role-change lag:** a 10-game window trails trades, injury returns and starter changes by up to 10 games, so errors cluster right after a role change (LeBron's 25.7 to 15.1 average over three seasons is the extreme case). A fixed window cannot fix that.
- **Skew:** points, rebounds and assists are right-skewed and floor at 0; steals, blocks and threes are zero-inflated. A mean is not the MAE-optimal point (a median is), so a MAE contest between a mean-based baseline and any other predictor is slanted by the choice of centre.
- **Dependence:** consecutive 10-game windows share 9 games, so residuals are strongly autocorrelated; teammates in one game share pace and blowouts; one player contributes many rows. Any interval needs a cluster bootstrap by player (the ledger convention) and a date-cluster check, with Holm across the primary markets.

When I would reconsider: after Track A reports, and only for a specific, written, pregame-only hypothesis that Track A and E5 leave open. At that point the spec would have to name the data, pregame features only (end-of-season ranks and actual minutes stay descriptive), a chronological development and held-out period (acknowledging 2025-26 and 2026-27 through 2026-11-13 have been examined), the unadjusted rolling baseline as comparator on held-out MAE, an effect threshold fixed in advance, player-clustered uncertainty with Holm correction, success / inconclusive / stop conditions, and the decision it would change.

`scripts/line_history.py` stays a descriptive display tool. Do not mine its splits.

## 2. Dimers projections benchmark (one slate, descriptive)

Supplied file: `nba_player_projections_1.csv`. What it contains, checked against the stored 2026-11-15 board:

- 194 player rows, 9 games (the whole slate). Columns: mean projections for PTS, REB, AST, 3PM, BLK, STL, TO, PRA, PR, PA, plus DFS and FB%. No minutes, no RA, no stocks, no uncertainty, no retrieval time.
- `Matchup` reads "away vs. home" (for example `IND vs. PHI` is IND at PHI; `OKC vs. SAC` is OKC at SAC).
- **Timestamps are not pregame for most games.** `Last Updated` is one value per game, and against scheduled tip it is 0 s to 8 min after tip for 8 of the 9 games (for example IND/PHI 18:08:04Z vs an 18:00Z tip; LAL/PHX 21:04:53Z vs 21:00Z). Only OKC/SAC (01:49:33Z vs 02:00Z) is before tip, by about 10.5 minutes. The file was therefore retrieved around or after those tips; nothing in it shows what information the numbers used.
- **Identity:** one row has a blank first name (LAC "Miller", 3.40 PTS) and cannot be matched. Names differ from the board by suffix and accents (Jabari Smith / Jr., Jaime Jaquez / Jr., Luka Doncic / Dončić). Join on normalized name plus team plus game date, never name alone.
- **Population differs:** seven players the board prices are absent from the file (De'Aaron Fox, Andrew Nembhard, Kyrie Irving, Zach LaVine, De'Andre Hunter, Tre Johnson, Ryan Rollins), consistent with the file dropping players expected out. That is availability information our side lacks (E2), so a naive accuracy comparison would confound availability with model quality.

Proposed comparison (separate script, later; outputs under `data/research/dimers/`; never imported by the scheduler, ledger or prospective code; not an input to or criterion of the experiment):

1. **Record provenance at download:** the retrieval time in UTC and the source, which I cannot infer from the file. Dimers' `Last Updated` goes in the table as given.
2. **Alignment rule:** a projection counts as pregame only if both its `Last Updated` and the retrieval time are at or before the game's scheduled tip. Rows that fail are kept and labeled "not pregame", not dropped silently.
3. **Join:** player + team + game date to the stored board; stat map PTS/REB/AST to pts/reb/ast, PRA/PR/PA to pra/pr/pa, 3PM to fg3m; BLK, STL, TO not compared (TO is not a market).
4. **Pregame comparison (no results needed):** Dimers mean minus the line, our sim mean minus the line (display store `sims`, not the ledger), and Dimers minus our sim, per market, with counts of unmatched players on each side.
5. **After the games:** actual stat from Hardwood game logs; played games only (DNP and missing-stat rows excluded and counted); MAE of Dimers mean, sim mean and the rolling-10 mean on the same intersection of players, reported with player-clustered intervals.
6. **Not done:** no over/under probabilities inferred from projected means, no hit rates or returns, no claim of superiority. One slate is descriptive evidence only; a second and third slate would be needed before anything stronger is said, and still outside the experiment.

## Dimers file provenance (supplied by JD, 2026-11-15)

- File: `nba_player_projections_1.csv` (194 player rows, columns PTS, REB, AST, 3PM, BLK, STL, TO, PRA, PR, PA, DFS, FB, Last Updated).
- Retrieved by JD at about 6:00 PM PT on 2026-11-15 (02:00 UTC on 2026-11-16). Source: Dimers; the page URL has not been recorded.
- Each row carries its own `Last Updated` (UTC), from 2026-11-15T18:08Z to 2026-11-16T01:49Z, so projections are not one simultaneous snapshot.
- Alignment caveats found on inspection: rows for the three early games (IND@PHI 18:08Z, LAL@PHX 21:04Z, CHA@LAC 21:30Z) were last updated at or just after tip-off, so they may not be pre-tip numbers and must be excluded from any pre-tip comparison unless shown otherwise. One row has an empty first name ("Miller", LAC), so name matching must use team plus last name and flag ambiguity rather than guess.
- Status: provenance recorded only. No comparison has been run and none is approved.

## Dimers file 2 provenance (2026-11-16)

- File: `nba_player_projections_3.csv` (185 rows), from dimers.com/nba/player-projections, downloaded by JD about 9:43 AM PT on 2026-11-16 (screenshot shows the page header "Last update November 16, 2026, 12:16 PM ET").
- Every row's `Last Updated` is 16:17:06Z to 16:17:08Z (11:17 AM ET), one snapshot. The one-hour gap between the page header and the row timestamps is unexplained.
- All 8 games of the slate tip at 7:00 PM ET or later, so every row is pre-tip.
- Descriptive comparison run on this file (agreement only, no outcomes read): outputs in `data/research/dimers_2026-11-16_*` (git-ignored). JD approved running it on 2026-11-16. The comparison proposal as a whole is still not an experiment and does not count toward prospective validation.
