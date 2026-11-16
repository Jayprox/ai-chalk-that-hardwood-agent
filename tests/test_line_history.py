import asyncio

from scripts import line_history as lh


class FakeHW:
    def __init__(self, logs):
        self.logs, self.asked = logs, []

    async def post(self, path, body):
        self.asked.append(body["season"])
        return {"data": self.logs.get(body["season"], [])}


def g(date, pts, minutes=30, opp="SAC", venue="home"):
    return {"date": date, "opponent": opp, "venue": venue, "minutes": minutes, "pts": pts}


def test_seasons_are_walked_backwards_and_stop_at_the_first_empty_one(monkeypatch):
    monkeypatch.setattr(lh, "_cur", lambda: "2026-27")
    monkeypatch.setattr(lh, "_cache_get", lambda k: None)
    monkeypatch.setattr(lh, "_cache_put", lambda k, v: None)
    hw = FakeHW({"2026-27": [g("2026-11-01", 20)], "2025-26": [g("2026-01-01", 10)]})   # no 2024-25 games: the career ends there
    out = asyncio.run(lh.collect_logs(hw, "p", 4))
    assert list(out) == ["2026-27", "2025-26"] and hw.asked == ["2026-27", "2025-26", "2024-25"]   # never asks for 2023-24


def test_an_empty_current_season_is_skipped_not_treated_as_the_end(monkeypatch):
    monkeypatch.setattr(lh, "_cur", lambda: "2026-27")
    monkeypatch.setattr(lh, "_cache_get", lambda k: None)
    monkeypatch.setattr(lh, "_cache_put", lambda k, v: None)
    out = asyncio.run(lh.collect_logs(FakeHW({"2025-26": [g("2026-01-01", 10)]}), "p", 3))
    assert list(out) == ["2025-26"]


def test_rows_score_played_games_only_and_rest_counts_played_games():
    logs = {"2026-27": [g("2026-11-01", 20), g("2026-11-02", 0, minutes=0), g("2026-11-05", 17.5), g("2026-11-06", 10)]}
    rows = lh.build_rows(logs, "pts", 17.5, {"2026-27": {"SAC": 12}})
    assert [r["date"] for r in rows] == ["2026-11-01", "2026-11-05", "2026-11-06"]          # the DNP is not a zero
    assert [r["result"] for r in rows] == ["over", "push", "under"]
    assert [r["rest_days"] for r in rows] == [None, 3, 0] and rows[0]["def_rank"] == 12


def test_stat_line_ignores_pushes_in_the_percentage():
    s = lh.stat_line([{"result": "over", "value": 20}, {"result": "push", "value": 17.5}, {"result": "under", "value": 10}])
    assert (s["over"], s["under"], s["over_pct"]) == (1, 1, 50.0)


def test_the_board_date_and_later_games_are_excluded():
    logs = {"2026-27": [g("2026-11-14", 20), g("2026-11-15", 15), g("2026-11-16", 30)]}
    rows = lh.build_rows(logs, "pts", 17.5, {}, before="2026-11-15")
    assert [r["date"] for r in rows] == ["2026-11-14"]


import argparse
import json
from datetime import datetime, timezone

NOW = datetime(2026, 11, 15, 22, 0, tzinfo=timezone.utc)
PROP = {"line": 17.5, "date_et": "2026-11-12", "extra": json.dumps({"matchup": {"position": "F"}})}


def args(**kw):
    base = dict(line=None, board_date=None, position=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_a_stored_threshold_is_labelled_with_its_own_board_date_not_called_todays():
    prov = lh.build_provenance(args(), PROP, "pts", NOW)
    assert prov["threshold"] == 17.5 and prov["threshold_source"] == "stored" and prov["source_board_date"] == "2026-11-12"
    assert prov["position"] == "F" and prov["position_source"] == "stored" and prov["generated_at"] == "2026-11-15T22:00:00Z"
    text = "\n".join(lh.provenance_lines(prov))
    threshold_line = lh.provenance_lines(prov)[0]
    assert "2026-11-12" in text and "not necessarily today's" in threshold_line and "supplied manually" not in threshold_line


def test_a_manual_threshold_is_labelled_manual_and_defaults_the_board_date_to_today_et(monkeypatch):
    monkeypatch.setattr(lh, "now_et", lambda: datetime(2026, 11, 15, 14, 0))
    prov = lh.build_provenance(args(line=20.5, position="C"), PROP, "pts", NOW)
    assert prov["threshold"] == 20.5 and prov["threshold_source"] == "manual" and prov["source_board_date"] == "2026-11-15" and prov["position_source"] == "manual"
    assert "supplied manually" in lh.provenance_lines(prov)[0]
    assert lh.build_provenance(args(line=20.5, board_date="2026-11-01", position="C"), None, "pts", NOW)["source_board_date"] == "2026-11-01"
    assert lh.build_provenance(args(), None, "pts", NOW) is None                # nothing stored and nothing supplied
    assert lh.build_provenance(args(), {"line": 10, "date_et": "2026-11-12", "extra": None}, "pts", NOW) is None   # no position anywhere


def test_an_empty_eligible_dataset_reports_cleanly_instead_of_crashing():
    prov = lh.build_provenance(args(), PROP, "pts", NOW)
    logs = {"2026-27": [g("2026-11-12", 30), g("2026-11-13", 0, minutes=0)]}      # only the board date and a DNP
    rows = lh.build_rows(logs, "pts", 17.5, {}, before="2026-11-12")
    assert rows == []
    md = lh.report("Nobody", prov, rows, logs)
    assert "No eligible games" in md and "2026-11-12" in md and "Provenance" in md and "None" not in md


def test_zero_is_a_valid_value_a_missing_component_is_skipped_and_pushes_stay_separate():
    logs = {"2026-27": [g("2026-11-01", 0, minutes=12), {"date": "2026-11-02", "opponent": "SAC", "venue": "home", "minutes": 30, "pts": 20},
                        {"date": "2026-11-03", "opponent": "SAC", "venue": "home", "minutes": 30, "pts": 10, "reb": None},
                        {"date": "2026-11-04", "opponent": "SAC", "venue": "home", "minutes": 30, "pts": 5, "reb": 5}]}
    pts = lh.build_rows(logs, "pts", 10.0, {})
    assert [r["value"] for r in pts] == [0.0, 20.0, 10.0, 5.0] and [r["result"] for r in pts] == ["under", "over", "push", "under"]
    pr = lh.build_rows(logs, "pr", 10.0, {})                                      # points + rebounds: only the games with both are scored
    assert [r["date"] for r in pr] == ["2026-11-04"] and pr[0]["value"] == 10.0 and pr[0]["result"] == "push"
    s = lh.stat_line(pts)
    assert (s["over"], s["under"], s["push"]) == (1, 2, 1) and "1 push" in lh._fmt(s)


def test_the_report_labels_the_retrospective_context_and_rank_basis(monkeypatch):
    monkeypatch.setattr(lh, "_cur", lambda: "2026-27")
    prov = lh.build_provenance(args(), PROP, "pts", NOW)
    logs = {"2026-27": [g("2026-11-01", 20), g("2026-11-02", 10)], "2025-26": [g("2026-01-01", 25)]}
    rows = lh.build_rows(logs, "pts", 17.5, {"2026-27": {"SAC": 5}, "2025-26": {"SAC": 9}}, before="2026-11-12")
    assert {(r["season"], r["def_rank_basis"]) for r in rows} == {("2026-27", "to-date"), ("2025-26", "end-of-season")}
    md = lh.report("P", prov, rows, logs)
    for phrase in ("not historical sportsbook betting performance", "contain information from games after", "to-date as of the run, not final",
                   "not a pregame feature", "an approximation", "not independent confirmation", "rank is available for 3 of 3 games"):
        assert phrase in md, phrase
