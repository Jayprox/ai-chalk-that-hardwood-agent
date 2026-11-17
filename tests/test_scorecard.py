import csv
import sqlite3

import pytest

from scripts import scorecard as sc

SCHEMA = """
CREATE TABLE games (game_id TEXT PRIMARY KEY, date_et TEXT, tipoff_utc TEXT);
CREATE TABLE props_current (player_id TEXT, game_id TEXT, market TEXT, date_et TEXT, name TEXT, team TEXT, line REAL, actual REAL, result TEXT);
CREATE TABLE sims (player_id TEXT, game_id TEXT, market TEXT, date_et TEXT, status TEXT, mean REAL, computed_at TEXT);
"""
TIP = "2026-11-16T00:00:00.000Z"


def make_db(tmp_path, props, sims):
    p = tmp_path / "props.db"
    c = sqlite3.connect(p)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO games VALUES ('g1','2026-11-15',?)", (TIP,))
    for pid, m, name, team, line, actual in props:
        c.execute("INSERT INTO props_current VALUES (?,?,?,?,?,?,?,?,?)", (pid, "g1", m, "2026-11-15", name, team, line, actual, "SECRET"))
    for pid, m, mean, at in sims:
        c.execute("INSERT INTO sims VALUES (?,?,?,?,?,?,?)", (pid, "g1", m, "2026-11-15", "ok", mean, at))
    c.commit()
    c.close()
    return p


def dimers_file(tmp_path, rows):
    p = tmp_path / "d.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["First Name", "Last Name", "Team", "Matchup", "PTS", "REB", "AST", "3PM", "BLK", "STL", "TO", "PRA", "PR", "PA", "DFS", "FB", "Last Updated"])
        for first, last, team, pts, upd in rows:
            w.writerow([first, last, team, "X vs. Y", pts, 5, 5, 1, 0, 1, 2, 30, 25, 25, 40, "1%", upd])
    return p


def test_error_stats_use_only_pre_tip_inputs_and_skip_dnp(tmp_path):
    db = make_db(tmp_path,
                 [("a", "pts", "Al One", "AAA", 20.5, 25), ("b", "pts", "Bo Two", "BBB", 10.5, 8), ("c", "pts", "Cy Three", "CCC", 12.5, 0),
                  ("c", "reb", "Cy Three", "CCC", 4.5, 0), ("d", "pts", "Di Four", "DDD", 15.5, None)],
                 [("a", "pts", 22.0, "2026-11-15T20:00:00Z"), ("b", "pts", 9.0, "2026-11-16T00:30:00Z")])      # b's sim was computed after tip
    d = sc.read_dimers(dimers_file(tmp_path, [("Al", "One", "AAA", 24.0, "2026-11-15T20:00:00Z"), ("Bo", "Two", "BBB", 9.0, "2026-11-16T00:05:00Z")]))
    rows, ex = sc.build_rows(db, "2026-11-15", d)
    by = {r["player_id"]: r for r in rows}
    assert set(by) == {"a", "b"}                                   # c did not play (all zeros), d has no final stat
    assert ex["did_not_play"] == 2 and ex["no_final_stat"] == 1
    assert by["a"]["sim_mean"] == 22.0 and by["a"]["dimers"] == 24.0
    assert by["b"]["sim_mean"] is None and by["b"]["dimers"] is None   # both arrived after tip-off
    assert ex["sim_after_tip"] == 1 and ex["dimers_after_tip"] == 1


def test_scoring_is_projection_error_only_and_never_reads_the_result_column(tmp_path):
    db = make_db(tmp_path, [("a", "pts", "Al One", "AAA", 20.5, 25), ("b", "pts", "Bo Two", "BBB", 10.5, 8)],
                 [("a", "pts", 22.0, "2026-11-15T20:00:00Z"), ("b", "pts", 9.0, "2026-11-15T20:00:00Z")])
    rows, _ = sc.build_rows(db, "2026-11-15", None)
    assert all("result" not in r for r in rows)
    s = sc.score(rows)["pts"]["sim_mean+line"]
    assert s["n"] == 2
    assert s["sim_mean"]["mae"] == pytest.approx((3.0 + 1.0) / 2) and s["sim_mean"]["bias"] == pytest.approx((-3.0 + 1.0) / 2)
    assert s["line"]["mae"] == pytest.approx((4.5 + 2.5) / 2)
    text = sc.summary_text(rows)
    assert "hit rate" in text.lower() and "no hit rates" in text.lower()    # states what it does not do


def test_refuses_to_open_anything_under_the_ledger(tmp_path):
    led = tmp_path / "ledger"
    led.mkdir()
    with pytest.raises(SystemExit):
        sc.build_rows(led / "ledger.db", "2026-11-15", None)


def test_write_and_reload_round_trips_and_blank_sources_stay_blank(tmp_path):
    rows = [{"date": "2026-11-15", "market": "pts", "player_id": "a", "name": "Al", "team": "AAA", "game_id": "g1", "line": 20.5,
             "sim_mean": None, "dimers": 24.0, "actual": 25.0}]
    sc.write_rows(rows, "2026-11-15", tmp_path)
    back = sc.read_all_rows(tmp_path)
    assert back[0]["sim_mean"] is None and back[0]["dimers"] == 24.0 and back[0]["actual"] == 25.0
