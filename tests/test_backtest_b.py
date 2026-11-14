import numpy as np

from scripts.backtest_stage_a2 import analyze, attach_board, build_features
from scripts.backtest_stage_b import MODELS_B, absence_features, format_stage_b, minutes_mechanism


def _logs(pid, minutes_by_game, started=True):
    return [{"game_id": f"g{i}", "date": f"2026-11-{i + 1:02d}", "minutes": m, "started": started} for i, m in enumerate(minutes_by_game)]


def _team(star_games, other_games=None, n=10):
    other_games = other_games or [30.0] * n
    players = {"star": ("Star", _logs("star", star_games)), "wing": ("Wing", _logs("wing", other_games, started=False))}
    return players, {"star": "OKC", "wing": "OKC"}


def test_a_rotation_player_missing_a_game_is_absent_for_teammates_only():
    star = [34.0] * 7 + [0.0, 34.0, 34.0]            # sits game 7
    players, teams = _team(star)
    feats, summary = absence_features(players, teams)
    assert feats[("wing", "g7")]["absent_names"] == ["Star"]
    assert abs(feats[("wing", "g7")]["absent_min"] - 34.0) < 1e-9 and feats[("wing", "g7")]["absent_starters"] == 1
    assert feats[("wing", "g6")]["absent_names"] == []
    assert ("star", "g7") not in feats                  # he did not play, so there is no row for him
    assert summary["with_any_absent"] == 1


def test_early_games_and_bench_players_never_count_as_absent():
    players, teams = _team([34.0] * 10, other_games=[30.0, 30.0, 30.0, 0.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0])
    feats, _ = absence_features(players, teams)
    assert feats[("star", "g3")]["absent_names"] == []   # wing has only 3 earlier games
    players, teams = _team([34.0] * 7 + [0.0, 34.0, 34.0])
    players["star"] = ("Star", [dict(g, minutes=(10.0 if g["minutes"] else 0.0)) for g in players["star"][1]])
    feats, _ = absence_features(players, teams)
    assert feats[("wing", "g7")]["absent_names"] == []   # 10 normal minutes is not a rotation player


def test_a_player_is_never_his_own_absent_teammate_and_unmapped_players_are_skipped():
    players, teams = _team([34.0] * 10)
    teams.pop("wing")
    feats, summary = absence_features(players, teams)
    assert all(not v["absent_names"] for v in feats.values()) and summary["teams"] == 1


def test_mechanism_finds_extra_minutes_when_a_teammate_sits_and_nothing_when_it_does_not_matter():
    rng = np.random.default_rng(0)
    n_games = 40
    star = [0.0 if i % 4 == 3 and i > 6 else 34.0 for i in range(n_games)]
    for effect in (6.0, 0.0):
        wing = [30.0 + (effect if star[i] == 0 else 0.0) + rng.normal(0, 1.0) for i in range(n_games)]
        players = {"star": ("Star", _logs("star", star)), "wing": ("Wing", _logs("wing", wing, started=False))}
        feats, _ = absence_features(players, {"star": "OKC", "wing": "OKC"})
        # many players so the player-level bootstrap has something to resample
        for k in range(12):
            players[f"w{k}"] = (f"W{k}", _logs(f"w{k}", [30.0 + (effect if star[i] == 0 else 0.0) + rng.normal(0, 1.0) for i in range(n_games)], started=False))
        feats, _ = absence_features(players, {p: "OKC" for p in players})
        m = minutes_mechanism(players, feats, reps=200)
        if effect:
            assert m["slope_lo"] > 0 and abs(m["slope_per_absent_minute"] - 6.0 / 34.0) < 0.04
        else:
            assert m["slope_lo"] <= 0 <= m["slope_hi"]
    assert "MECHANISM" in format_stage_b({"player_games": 1, "teams": 1, "with_any_absent": 0, "with_absent_starter": 0}, m, [])


def test_missing_absence_columns_default_to_zero_and_the_four_models_run():
    rng = np.random.default_rng(1)
    recs = []
    boards = {}
    for i in range(1600):
        p = float(np.clip(rng.normal(0.47, 0.04), 0.3, 0.7))
        recs.append({"player_id": f"p{i}", "game_id": "g", "market": "pts", "date": f"2026-11-{1 + i % 12:02d}", "line": 20.5,
                     "over": int(rng.random() < p), "p_sim": p, "p_hitrate": 0.5, "sim_mean": 20.0, "sim_sd": 5.0, "min_trend": 0.0,
                     "venue": "home", "rest_days": 1, "started": True})
        boards[(f"p{i}", "g", "pts")] = {"over_price": -110, "under_price": -110, "matchup": None, "last10": None}
    rows, _ = attach_board(recs, boards)
    for i, r in enumerate(rows):
        r["absent_min"], r["absent_starters"] = (20.0, 1) if i % 3 == 0 else (0.0, 0)
    rows[0].pop("absent_min")
    f = build_features(rows)
    assert f["absent_min"][0] == 0.0 and f["absent_min"][1] == 0.0 and f["absent_min"][3] == 20 / 30
    res = analyze(f, reps=100, models=MODELS_B)
    names = [r["model"] for r in res["table"]]
    assert all(m in names for m, _c in MODELS_B) and res["n_test"] > 500
