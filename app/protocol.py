"""Constants of the prospective validation protocol (prospective-v1).

Everything here is hashed into the freeze manifest (scripts/freeze.py). After the freeze none of it may change; a necessary change
ends the test and starts a new one with a new freeze. The definitions are explained in docs/PROSPECTIVE_PROTOCOL.md.
"""
from __future__ import annotations

PROTOCOL_VERSION = "prospective-v1"

# ---- what is recorded
PRIMARY_SNAPSHOT = "close"                    # the Hardwood worker's pregame 'close' pull (about 35 minutes before tip-off)
MARKETS = ("pts", "ast", "reb", "fg3m", "pa", "pr", "ra", "pra", "stl", "blk")   # the ten dashboard markets
TRACK_A_MARKETS = ("pts", "reb", "ast")
SEASON_TYPES = ("regular",)                   # other season types are not in the expected population
CLOSE_WINDOW_MIN = 40                         # the recorder polls for close rows from this many minutes before tip until tip
CLOSE_FETCH_MAX_BEFORE_TIP_MIN = 90           # a close row pulled earlier than this before tip is invalid
MIN_PRIOR_GAMES = 5                           # played games before the game date needed for a forecast (same as the live sim)

# ---- freshness (forecasts are always computed fresh at record time from inputs retrieved under this rule)
INPUT_MIN_ET_HOUR = 8                         # game logs must be retrieved at or after 08:00 ET on the board date (late finals landed)
INPUT_MAX_AGE_HOURS = 15                      # and no more than this long before the forecast is computed
LEAGUE_MIN_PLAYERS = 40                       # fewest players with MIN_PRIOR_GAMES games to freeze a league snapshot for a date

# ---- coverage, predefined before the test starts
MIN_GAME_CONTRACTS = 30                       # a game is "covered" only with at least this many valid recorded contracts,
MIN_GAME_PLAYERS = 8                          # from at least this many players,
MIN_GAME_MARKETS = 3                          # in at least this many markets
COVERAGE_GAMES_MIN = 0.85                     # covered games / expected games
COVERAGE_CONTRACTS_MIN = 0.90                 # valid recorded contracts / close contracts observed for expected games
SETTLEABLE_MIN = 0.90                         # settled (incl. pushes) / matured recorded contracts
OVERALL_EXCLUSION_ALARM = 0.10                # void + unresolved share of matured contracts
GAME_EXCLUSION_ALARM = 0.25                   # same, within one game
DRY_RUN_COVERAGE_GAMES_MIN = 0.95             # stricter bars for the dry run
DRY_RUN_COVERAGE_CONTRACTS_MIN = 0.95

# ---- endpoint and settlement
ENDPOINT_GAMES = 300                          # one analysis, at the 300th covered game (by tip-off, then game id) once it has matured
MATURITY_HOURS = 48                           # a game's results count after tip-off + GAME_LENGTH_HOURS + this many hours
GAME_LENGTH_HOURS = 3
ABANDON_HOURS = 24                            # a game not final this long after tip-off is "not played"
RECHECK_DAYS = 14                             # settled games are re-read for statistic corrections for this many days after tip-off
SETTLED_STATUSES = ("settled", "push")        # settlement statuses with a usable result; the rest are exclusions
EXCLUSION_STATUSES = ("void_dnp", "void_not_played", "unresolved")

# ---- analysis
BOOT_REPS = 2000
BOOT_SEED = 9
DISCARD_MAX = 0.05                            # more discarded replicates than this makes an interval "unstable"
TARGET_B = 3.0                                # Track B design target, in percentage points
GATE_A = 0.02                                 # Track A design gate (relative CRPS improvement)
BIN_EDGES = (0.50, 0.55, 0.60, 0.65, 0.70, 1.0001)   # favorite-probability bins for the stratified check S1
MIN_ARM = 10                                  # smallest weighted count in each arm (agree / disagree) of a pooled stratum
S1_MIN_MARKETS = 3
S1_MIN_PROP_SHARE = 0.5
S1_MIN_GROUPS = 5
S2_MAX_ITER = 8


def constants() -> dict:
    return {k: v for k, v in globals().items() if k.isupper() and not k.startswith("_")}
