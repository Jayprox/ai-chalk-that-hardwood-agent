import importlib
import json
import subprocess
import sys
from datetime import timedelta

import pytest

from app import protocol as P
from app.coverage import coverage_report, health_report, integrity_report, scope_info
from app.ledger import Ledger, ts
from tests.ledger_helpers import DATE, GAME, TIP, make_rec, seed_game
from app.config import ROOT

NOW = TIP + timedelta(hours=6)                         # every game below has tipped and finished
MATURE = TIP + timedelta(hours=P.GAME_LENGTH_HOURS + P.MATURITY_HOURS + 1)


def L_(tmp_path):
    return Ledger(tmp_path / "l.db")


def test_failed_collection_games_stay_in_the_denominator_with_a_reason(tmp_path):
    L = L_(tmp_path)
    seed_game(L, game="ok1")
    seed_game(L, game="ok2", tip=TIP + timedelta(hours=1))
    # collected game with too few contracts, a game whose close rows were never observed, and one the recorder never polled
    seed_game(L, game="thin", n_players=2, markets=("pts",), tip=TIP + timedelta(hours=2))
    L.register_game({"game_id": "never_seen_rows", "date_et": DATE, "tipoff_utc": ts(TIP + timedelta(hours=3)), "status": "final", "season_type": "regular"}, ts(TIP - timedelta(hours=9)))
    L.register_game({"game_id": "never_polled", "date_et": DATE, "tipoff_utc": ts(TIP + timedelta(hours=4)), "status": "final", "season_type": "regular"}, ts(TIP - timedelta(hours=9)))
    L.add_tick(ts(TIP + timedelta(hours=3) - timedelta(minutes=10)), ts(TIP), True, {})                  # a tick inside never_seen_rows' window only
    cov = coverage_report(L, "all", NOW + timedelta(hours=8), check_chain=False)
    assert cov["expected_games"] == 5 and cov["covered_games"] == 2 and cov["coverage_games"] == pytest.approx(0.4)
    assert cov["uncovered_reasons"] == {"below_minimum": 1, "no_close_rows_observed": 1, "window_not_polled": 1}
    assert not cov["ok"] and any("covered games" in r for r in cov["invalid_reasons"])
    assert {g["game_id"] for g in cov["uncovered_games"]} == {"thin", "never_seen_rows", "never_polled"}


def test_game_coverage_rule_needs_contracts_players_and_markets(tmp_path):
    L = L_(tmp_path)
    seed_game(L, game="enough", n_players=P.MIN_GAME_PLAYERS, markets=("pts", "reb", "ast", "pra"))                 # 32 contracts, 8 players, 4 markets
    seed_game(L, game="few_players", n_players=P.MIN_GAME_PLAYERS - 1, markets=("pts", "reb", "ast", "pra", "pa", "pr", "ra", "stl", "blk", "fg3m"), tip=TIP + timedelta(hours=1))
    seed_game(L, game="few_markets", n_players=20, markets=("pts", "reb"), tip=TIP + timedelta(hours=2))
    cov = coverage_report(L, "all", NOW + timedelta(hours=3), check_chain=False)
    by = {g["game_id"]: g for g in cov["uncovered_games"]}
    assert cov["covered_games"] == 1 and set(by) == {"few_players", "few_markets"}


def test_contract_coverage_uses_the_observed_universe_as_denominator(tmp_path):
    L = L_(tmp_path)
    seed_game(L, game="g", n_players=10)                                           # 30 recorded, 30 observed
    for i in range(10, 13):                                                         # 3 close contracts observed but never recorded
        L.add_universe("g", f"g-p{i}", "pts", DATE, ts(TIP - timedelta(minutes=2)), "pregame")
    L.add_universe("g", "g-late", "pts", DATE, ts(TIP + timedelta(minutes=2)), "late")     # first seen after tip-off: missed, counted
    cov = coverage_report(L, "all", NOW, check_chain=False)
    assert cov["observed_contracts"] == 34 and cov["recorded_contracts"] == 30 and cov["coverage_contracts"] == pytest.approx(30 / 34)


def test_postponed_and_abandoned_games_leave_the_denominator_but_are_listed(tmp_path):
    L = L_(tmp_path)
    seed_game(L, game="played")
    L.register_game({"game_id": "ppd", "date_et": DATE, "tipoff_utc": ts(TIP + timedelta(hours=1)), "status": "scheduled", "season_type": "regular"}, ts(TIP - timedelta(hours=9)))
    L.register_game({"game_id": "ppd", "date_et": DATE, "tipoff_utc": ts(TIP + timedelta(hours=1)), "status": "postponed", "season_type": "regular"}, ts(TIP))
    L.register_game({"game_id": "stuck", "date_et": DATE, "tipoff_utc": ts(TIP), "status": "live", "season_type": "regular"}, ts(TIP - timedelta(hours=9)))
    cov = coverage_report(L, "all", TIP + timedelta(hours=P.ABANDON_HOURS + 1), check_chain=False)
    assert cov["expected_games"] == 1 and sorted(cov["not_played_games"]) == ["ppd", "stuck"] and cov["coverage_games"] == 1.0


def test_other_season_types_are_out_of_population_and_not_counted(tmp_path):
    L = L_(tmp_path)
    seed_game(L, game="reg")
    seed_game(L, game="pre", season_type="preseason", n_players=2, tip=TIP + timedelta(hours=1))
    cov = coverage_report(L, "all", NOW + timedelta(hours=2), check_chain=False)
    assert cov["expected_games"] == 1 and cov["out_of_population_games"] == {"preseason": 1}


def test_games_registered_after_tip_off_are_flagged_but_still_expected(tmp_path):
    L = L_(tmp_path)
    seed_game(L, game="g1")
    L.register_game({"game_id": "late", "date_et": DATE, "tipoff_utc": ts(TIP), "status": "final", "season_type": "regular"}, ts(TIP + timedelta(hours=5)))
    cov = coverage_report(L, "all", NOW, check_chain=False)
    assert cov["expected_games"] == 2 and cov["registered_late_games"] == ["late"]


def test_games_that_have_not_tipped_are_not_yet_due(tmp_path):
    L = L_(tmp_path)
    seed_game(L, game="future", tip=TIP + timedelta(days=3), date="2026-11-23", status="scheduled", universe=False)
    cov = coverage_report(L, "all", NOW, check_chain=False)
    assert cov["expected_games"] == 0 and cov["ok"] is False


def test_scopes_separate_dry_run_from_counted(tmp_path):
    L = L_(tmp_path)
    seed_game(L, game="dry")
    L.add_freeze(ts(TIP + timedelta(hours=7)), "2026-11-21", {"m": 1}, None, None)
    d2 = TIP + timedelta(days=1)
    seed_game(L, game="counted", date="2026-11-21", tip=d2)
    now = d2 + timedelta(hours=6)
    dry, counted = coverage_report(L, "dry", now, check_chain=False), coverage_report(L, "counted", now, check_chain=False)
    assert (dry["expected_games"], dry["recorded_contracts"]) == (1, 30) and (counted["expected_games"], counted["recorded_contracts"]) == (1, 30)
    assert counted["first_counted_date"] == "2026-11-21"
    L2 = L_(tmp_path / "x") if False else Ledger(tmp_path / "none.db")
    assert coverage_report(L2, "counted", now, check_chain=False)["expected_games"] == 0               # no freeze: nothing counts


def test_endpoint_readiness_waits_for_maturity_and_orders_by_tip_then_id(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "ENDPOINT_GAMES", 3)
    L = L_(tmp_path)
    L.add_freeze("2026-11-01T00:00:00.000000Z", "2026-11-02", {"m": 1}, None, None)
    for i, gid in enumerate(["b", "a", "c", "d"]):
        seed_game(L, game=gid, tip=TIP + timedelta(minutes=i if gid != "a" else 0), n_players=10)             # a and b tip together: id breaks the tie
    early = coverage_report(L, "counted", TIP + timedelta(hours=10), check_chain=False)["endpoint"]
    assert early["covered_matured_games"] == 0 and not early["ready"]
    ep = coverage_report(L, "counted", MATURE + timedelta(hours=1), check_chain=False)["endpoint"]
    assert ep["ready"] and ep["game_ids"] == ["a", "b", "c"]                         # the first three by tip-off, then id; "d" is not substituted in


def test_exclusion_alarms_use_statuses_only(tmp_path):
    L = L_(tmp_path)
    seed_game(L, game="g", n_players=10)
    for r in L.records():
        L.add_settlement(r["seq"], ts(MATURE), "void_dnp" if r["seq"] <= 12 else "settled", None, None, "t", {})        # 12 of 30 voided = 40%
    cov = coverage_report(L, "all", MATURE + timedelta(hours=1), check_chain=False)
    assert cov["settlement"]["exclusion_share"] == pytest.approx(0.4)
    assert cov["settlement"]["games_over_exclusion_alarm"] == ["g"]
    assert any("voids+unresolved" in r for r in cov["invalid_reasons"]) and any("game(s)" in r for r in cov["invalid_reasons"])
    flat = json.dumps(cov)
    for forbidden in ('"actual"', '"outcome"', "hit_rate"):
        assert forbidden not in flat                                                # the report never carries outcomes


def test_dry_run_thresholds_are_stricter(tmp_path):
    L = L_(tmp_path)
    for i in range(10):
        seed_game(L, game=f"g{i}", tip=TIP + timedelta(minutes=i), n_players=10 if i else 2, markets=("pts", "reb", "ast") if i else ("pts",))   # 9 of 10 covered
    normal = coverage_report(L, "all", NOW, dry_run=False, check_chain=False)
    strict = coverage_report(L, "all", NOW, dry_run=True, check_chain=False)
    assert normal["coverage_games"] == 0.9 and not any("covered games" in r for r in normal["invalid_reasons"])
    assert any("covered games" in r for r in strict["invalid_reasons"])


def test_integrity_flags_late_records_stale_inputs_and_missing_blobs(tmp_path):
    L = L_(tmp_path)
    seed_game(L, game="g", n_players=1, markets=("pts",))                           # fake input hashes: the blobs do not exist
    rep = integrity_report(L, NOW)
    assert not rep["ok"] and any("missing or corrupt" in p for p in rep["problems"])
    L2 = L_(tmp_path / "late") if False else Ledger(tmp_path / "late.db")
    L2.append_record(make_rec(1, recorded=TIP + timedelta(seconds=1)))
    rep = integrity_report(L2, NOW)
    assert rep["records_after_tip"] == 1 and any("at or after tip-off" in p for p in rep["problems"])


def test_health_alerts_for_a_silent_recorder_and_unpolled_games(tmp_path):
    L = L_(tmp_path)
    seed_game(L, game="g", tip=TIP, status="scheduled", n_players=1, markets=("pts",), universe=False)
    near = TIP - timedelta(minutes=30)
    assert any("never run" in a for a in health_report(L, near)["alerts"])
    L.add_tick(ts(near - timedelta(minutes=40)), ts(near - timedelta(minutes=40)), True, {})
    assert any("no recorder tick" in a for a in health_report(L, near)["alerts"])
    L.add_tick(ts(near - timedelta(minutes=2)), ts(near - timedelta(minutes=2)), True, {})
    assert not any("no recorder tick" in a for a in health_report(L, near)["alerts"])
    L.add_tick(ts(near), ts(near), False, {"errors": ["x"]})
    assert any("reported errors" in a for a in health_report(L, near)["alerts"])
    L2 = Ledger(tmp_path / "h2.db")
    L2.register_game({"game_id": "g2", "date_et": DATE, "tipoff_utc": ts(TIP), "status": "final", "season_type": "regular"}, ts(TIP - timedelta(hours=9)))
    alerts = health_report(L2, TIP + timedelta(minutes=20))["alerts"]
    assert any("without a single recorder tick" in a for a in alerts)


def test_freeze_manifest_is_complete_and_contains_no_credentials():
    sys.path.insert(0, str(ROOT / "scripts"))
    freeze = importlib.import_module("freeze")
    from app.provenance import secrets_in
    man = freeze.build()
    text = json.dumps(man)
    assert not secrets_in(text, ["ctnba_SECRETCANARY1234567890"])
    assert man["constants"]["ENDPOINT_GAMES"] == 300 and man["constants"]["TARGET_B"] == 3.0 and man["constants"]["GATE_A"] == 0.02
    files = man["fingerprint"]["files"]
    assert all(files[f] for f in ("app/analysis.py", "app/settle.py", "app/coverage.py", "app/record.py", "app/ledger.py", "app/baseline.py", "app/sim_fit.json",
                                  "scripts/prospective_analysis.py", "scripts/freeze.py", "app/store.py", "app/hardwood.py", "requirements.txt"))
    from app.provenance import CORE_FILES
    assert {"app/store.py", "app/hardwood.py", "requirements.txt"} <= set(CORE_FILES)      # input processing and dependency definitions are hashed
    assert all((ROOT / f).is_file() for f in CORE_FILES)                                    # every hashed file exists (so it can be tracked)
    assert man["fingerprint"]["slope"] == 0.2 and man["fingerprint"]["fit_sha256"]


def test_cli_refuses_to_run_the_analysis_before_a_freeze_or_endpoint(tmp_path):
    import os
    env = {"LEDGER_PATH": str(tmp_path / "l.db"), "LEDGER_MIRROR_DIR": str(tmp_path / "m"), "PATH": "/usr/bin:/bin"}
    if os.environ.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts" / a[0]), *a[1:]], capture_output=True, text=True, env={**env, "HOME": str(tmp_path)}, cwd=str(ROOT))
    r = run("prospective_analysis.py")
    assert r.returncode == 2 and "No freeze record" in r.stdout
    L = Ledger(tmp_path / "l.db")
    L.add_freeze(ts(TIP), "2026-11-21", {"m": 1}, None, None)
    L.close()
    r = run("prospective_analysis.py")
    assert r.returncode == 2 and "Endpoint not reached" in r.stdout
    r = run("ledger_check.py", "--json")
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["integrity"]["rows"] == 0 and out["coverage"]["expected_games"] == 0 and out["coverage"]["ok"] is False   # nothing to judge yet
    assert run("ledger_check.py", "--require-data").returncode == 1
    txt = run("ledger_check.py").stdout
    assert "ALL CHECKS PASS" not in txt and "recording health only" in txt           # an empty ledger is never reported as a pass


def test_verdict_separates_no_observations_from_evaluated_health():
    sys.path.insert(0, str(ROOT / "scripts"))
    v = importlib.import_module("ledger_check").verdict
    assert v([], None, 0, 0).startswith("NOT YET VERIFIED")
    assert v([], "2026-11-21T00:00:00Z", 0, 0).startswith("NO FAILURES, BUT NOT YET EVALUATED")
    assert v([], "2026-11-21T00:00:00Z", 40, 0).startswith("NO FAILURES, BUT NOT YET EVALUATED")      # records but no tipped expected game
    assert v([], "2026-11-21T00:00:00Z", 40, 3).startswith("RECORDING HEALTH PASS")
    assert v(["x"], None, 0, 0) == "PROBLEMS:"


def test_launchd_jobs_are_valid_plists_and_scripts_print_them(tmp_path):
    import plistlib
    for script, key in (("ledger_check.py", "StartInterval"), ("ledger_daily.py", "StartCalendarInterval")):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--print-launchd"], capture_output=True, text=True, cwd=str(ROOT))
        assert r.returncode == 0
        plist = plistlib.loads(r.stdout.encode())
        assert key in plist and plist["ProgramArguments"][-1] == "--notify" and plist["WorkingDirectory"] == str(ROOT)
