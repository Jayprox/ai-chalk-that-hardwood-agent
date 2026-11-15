"""End to end: two real recorder days (the dry run), the freeze preconditions, then counted games and the one-shot analysis guard."""
import asyncio
import importlib
import json
import sys
from datetime import timedelta

import pytest

from app import protocol as P
from app.config import ROOT
from app.coverage import coverage_report, dry_run_acceptance, health_report, integrity_report
from app.ledger import Ledger, ts
from app.record import ledger_tick
from app.simulation import Params
from tests.ledger_helpers import DATE, GAME, TIP, Clock, FakeHW, close_row, make_rec, seed_game
from tests.test_backtest import make_players

sys.path.insert(0, str(ROOT / "scripts"))
freeze = importlib.import_module("freeze")
pa = importlib.import_module("prospective_analysis")

DAY2 = "2026-11-21"
TIP2 = TIP + timedelta(days=1)


def two_day_hw():
    players = make_players(n_players=45)
    games = [{"id": GAME, "date_et": DATE, "home": "OKC", "away": "DEN", "status": "scheduled", "tipoff_utc": "2026-11-21T00:00:00Z", "season_type": "regular"},
             {"id": "g-2", "date_et": DAY2, "home": "BOS", "away": "NYK", "status": "scheduled", "tipoff_utc": "2026-11-22T00:00:00Z", "season_type": "regular"}]
    rows = {m: [close_row(p, m) for p in players] + [close_row(p, m, game="g-2", fetched=TIP2 - timedelta(minutes=33)) for p in players] for m in ("pts", "reb", "ast")}
    return FakeHW(players=players, games=games, rows=rows)


class DayHW(FakeHW):
    """Serves only the rows of the board date being asked for."""
    def __init__(self, base):
        self.__dict__.update(base.__dict__)

    async def get(self, path, params=None):
        if path == "/props":
            date = params["date"]
            gid = GAME if date == DATE else "g-2"
            return {"data": [r for r in self.rows.get(params["market"], []) if r["game_id"] == gid]}
        return await super().get(path, params)


def run_dry_days(L):
    hw = DayHW(two_day_hw())
    for tip in (TIP, TIP2):
        asyncio.run(ledger_tick(hw, L, "2026-27", clock=Clock(start=tip - timedelta(minutes=30)), params=Params(draws=300)))
    for gid, d in ((GAME, DATE), ("g-2", DAY2)):                              # later ticks / the daily settle see the final status
        L.register_game({"game_id": gid, "date_et": d, "status": "final"}, ts(TIP2 + timedelta(hours=5)))


MATURED = TIP2 + timedelta(hours=P.GAME_LENGTH_HOURS + P.MATURITY_HOURS + 2)         # both dry-run games have matured


def settle_all(L, at, status="settled", only=None):
    for r in L.records():
        if only is None or r["seq"] in only:
            L.add_settlement(r["seq"], ts(at), status, 3.0 if status == "settled" else None, "over" if status == "settled" else None, "t", {})
    L.add_settle_run(ts(at), {"games": 2, "settled": 0, "changed": 0, "undecided": 0, "errors": []})


@pytest.fixture
def frozen_env(tmp_path, monkeypatch):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    run_dry_days(L)
    after = MATURED + timedelta(hours=1)
    settle_all(L, MATURED)
    monkeypatch.setattr(freeze, "_now", lambda: after)
    monkeypatch.setattr(freeze, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(freeze, "settings", lambda: type("S", (), {"ledger_path": tmp_path / "l.db", "ledger_mirror_dir": tmp_path / "m"})())
    monkeypatch.setattr(freeze, "git_info", lambda *a, **k: {"commit": "abc123", "clean": True})
    return L, tmp_path, after


def test_two_real_slate_days_pass_the_dry_run_checks(tmp_path):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    run_dry_days(L)
    now = TIP2 + timedelta(hours=6)
    cov = coverage_report(L, "dry", now, dry_run=True, from_date=DATE)
    assert cov["expected_games"] == 2 and cov["covered_games"] == 2 and cov["coverage_contracts"] == 1.0 and cov["ok"], cov["invalid_reasons"]
    assert sorted(cov["by_date"]) == [DATE, DAY2]
    assert integrity_report(L, now)["ok"] and health_report(L, now)["ok"]
    assert all(r["freeze_id"] is None for r in L.records())                     # dry-run records are never counted


def test_freeze_refuses_until_every_precondition_holds_then_records_once(frozen_env, monkeypatch, capsys):
    L, tmp, after = frozen_env
    L.close()
    man_text = None

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["freeze.py", *argv])
        return freeze.main()
    assert run("--build") == 0 and (tmp / "manifest.json").exists()
    assert run("--record") == 1 and "--commit is required" in capsys.readouterr().out
    assert run("--record", "--commit", "abc123") == 1 and "--dry-from is required" in capsys.readouterr().out
    assert run("--record", "--commit", "other", "--dry-from", DATE, "--dry-to", DAY2) == 1 and "not the commit you named" in capsys.readouterr().out
    monkeypatch.setattr(freeze, "git_info", lambda *a, **k: {"commit": "abc123", "clean": False})
    assert run("--record", "--commit", "abc123", "--dry-from", DATE, "--dry-to", DAY2) == 1 and "untracked or has uncommitted" in capsys.readouterr().out
    monkeypatch.setattr(freeze, "git_info", lambda *a, **k: {"commit": "abc123", "clean": True})
    (tmp / "manifest.json").write_text("{}")                                    # a manifest that does not match the code
    assert run("--record", "--commit", "abc123", "--dry-from", DATE, "--dry-to", DAY2) == 1 and "does not match the current code" in capsys.readouterr().out
    assert run("--build") == 0
    assert run("--record", "--commit", "abc123", "--dry-from", DATE, "--dry-to", DAY2) == 0
    out = capsys.readouterr().out
    assert "first counted date 2026-11-25" in out
    L2 = Ledger(tmp / "l.db", tmp / "m")
    fr = L2.latest_freeze()
    assert fr["first_counted_date"] == "2026-11-25" and fr["git_commit"] == "abc123" and json.loads(fr["manifest_json"])["protocol"] == P.PROTOCOL_VERSION
    L2.close()
    assert run("--record", "--commit", "abc123", "--dry-from", DATE, "--dry-to", DAY2) == 1 and "already exists" in capsys.readouterr().out


def test_freeze_refuses_a_dry_run_with_one_slate_date(tmp_path, monkeypatch, capsys):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    asyncio.run(ledger_tick(DayHW(two_day_hw()), L, "2026-27", clock=Clock(start=TIP - timedelta(minutes=30)), params=Params(draws=300)))
    L.close()
    monkeypatch.setattr(freeze, "_now", lambda: TIP + timedelta(hours=6))
    monkeypatch.setattr(freeze, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(freeze, "settings", lambda: type("S", (), {"ledger_path": tmp_path / "l.db", "ledger_mirror_dir": tmp_path / "m"})())
    monkeypatch.setattr(freeze, "git_info", lambda *a, **k: {"commit": "abc", "clean": True})
    monkeypatch.setattr(sys, "argv", ["freeze.py", "--build"])
    freeze.main()
    monkeypatch.setattr(sys, "argv", ["freeze.py", "--record", "--commit", "abc", "--dry-from", DATE, "--dry-to", DAY2])
    assert freeze.main() == 1 and "at least 2 are required" in capsys.readouterr().out


def test_records_after_the_freeze_count_and_the_analysis_runs_once(frozen_env, monkeypatch, capsys):
    L, tmp, after = frozen_env
    L.close()
    monkeypatch.setattr(sys, "argv", ["freeze.py", "--build"])
    freeze.main()
    monkeypatch.setattr(sys, "argv", ["freeze.py", "--record", "--commit", "abc123", "--dry-from", DATE, "--dry-to", DAY2])
    assert freeze.main() == 0
    L = Ledger(tmp / "l.db", tmp / "m")
    n_dry = len(L.records())
    monkeypatch.setattr(P, "ENDPOINT_GAMES", 6)
    monkeypatch.setattr(P, "BOOT_REPS", 60)
    d3 = MATURED + timedelta(days=1)
    for i in range(6):
        seed_game(L, game=f"c{i}", date="2026-11-26", tip=d3 + timedelta(minutes=i), n_players=12)
    for r in L.records("freeze_id IS NOT NULL"):
        L.add_settlement(r["seq"], ts(d3), "settled", float(3 + r["seq"] % 3), "over" if r["seq"] % 2 else "under", "t", {})
    assert len(L.records("freeze_id IS NOT NULL")) == 6 * 36 and len(L.records()) == n_dry + 6 * 36 and not [r for r in L.records()[:n_dry] if r["freeze_id"]]
    L.close()
    monkeypatch.setattr(pa, "settings", lambda: type("S", (), {"ledger_path": tmp / "l.db", "ledger_mirror_dir": tmp / "m"})())
    monkeypatch.setattr(pa, "ROOT", tmp)
    monkeypatch.setattr(pa, "integrity_report", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(sys, "argv", ["prospective_analysis.py"])
    monkeypatch.setattr(pa, "_now", lambda: d3 + timedelta(days=5))
    capsys.readouterr()
    assert pa.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"track_a", "track_b"}
    L = Ledger(tmp / "l.db")
    assert len(L.analysis_runs()) == 1
    L.close()
    saved = list((tmp / "data" / "ledger" / "analysis").glob("primary-*.json"))
    assert len(saved) == 1
    assert pa.main() == 2 and "already been run" in capsys.readouterr().out          # one analysis only
    monkeypatch.setattr(sys, "argv", ["prospective_analysis.py", "--reproduce"])
    assert pa.main() == 0 and "identical" in capsys.readouterr().out
    monkeypatch.setattr(pa, "_now", lambda: d3 + timedelta(hours=1))                  # before maturity: refuses
    monkeypatch.setattr(sys, "argv", ["prospective_analysis.py"])
    assert pa.main() == 2


def test_a_mid_slate_start_is_judged_only_from_the_first_full_day(tmp_path):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    run_dry_days(L)
    L.register_game({"game_id": "early", "date_et": DATE, "tipoff_utc": ts(TIP - timedelta(hours=4)), "status": "final", "season_type": "regular"}, ts(TIP + timedelta(hours=20)))   # tipped before the recorder ran
    now = TIP2 + timedelta(hours=6)
    whole = coverage_report(L, "dry", now, dry_run=True, from_date=DATE)
    from_day2 = coverage_report(L, "dry", now, dry_run=True, from_date=DAY2)
    assert whole["expected_games"] == 3 and not whole["ok"] and whole["registered_late_games"] == ["early"]
    assert from_day2["expected_games"] == 1 and from_day2["ok"] and sorted(from_day2["by_date"]) == [DAY2]


def test_recording_stage_can_pass_while_the_final_stage_cannot_before_maturity(tmp_path):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    run_dry_days(L)
    soon = TIP2 + timedelta(hours=6)                                              # both slate days recorded, nothing matured
    acc = dry_run_acceptance(L, DATE, soon)
    assert acc["recording"]["ok"], acc["recording"]["reasons"]
    assert not acc["final"]["ok"] and any("have not matured" in r for r in acc["final"]["reasons"])
    assert acc["coverage"]["settlement"]["matured_contracts"] == 0                # "nothing matured" is never read as a pass


def test_final_stage_needs_every_dry_run_game_matured_a_settlement_run_and_the_unchanged_thresholds(tmp_path):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    run_dry_days(L)
    last = TIP2 + timedelta(hours=P.GAME_LENGTH_HOURS + P.MATURITY_HOURS)
    just_before = last - timedelta(minutes=1)
    assert any("not matured" in r for r in dry_run_acceptance(L, DATE, just_before)["final"]["reasons"])         # 51 h exactly, not before
    after = last + timedelta(minutes=1)
    r = dry_run_acceptance(L, DATE, after)["final"]
    assert not r["ok"] and any("no contract has matured" in x or "settleable" in x for x in r["reasons"])           # matured but never settled
    settle_all(L, after, only={x["seq"] for x in L.records()[:100]})                                            # 100 of 270 settled, the rest unresolved
    r = dry_run_acceptance(L, DATE, after + timedelta(minutes=1))["final"]
    assert not r["ok"] and any("settleable" in x for x in r["reasons"]) and any("voids+unresolved" in x for x in r["reasons"])
    L2 = Ledger(tmp_path / "l2.db", tmp_path / "m2")
    run_dry_days(L2)
    for rec in L2.records():
        L2.add_settlement(rec["seq"], ts(after), "settled", 3.0, "over", "t", {})
    no_run = dry_run_acceptance(L2, DATE, after + timedelta(minutes=1))["final"]
    assert not no_run["ok"] and any("settlement run" in x for x in no_run["reasons"])
    L2.add_settle_run(ts(after), {"games": 2, "settled": 0, "changed": 0, "undecided": 0, "errors": ["x"]})
    assert not dry_run_acceptance(L2, DATE, after + timedelta(minutes=1))["final"]["ok"]                          # a run with errors does not count
    L2.add_settle_run(ts(after + timedelta(seconds=30)), {"games": 2, "settled": 0, "changed": 0, "undecided": 0, "errors": []})
    ok = dry_run_acceptance(L2, DATE, after + timedelta(minutes=1))["final"]
    assert ok["ok"], ok["reasons"]


def test_one_game_over_the_exclusion_limit_blocks_final_acceptance(tmp_path):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    run_dry_days(L)
    after = TIP2 + timedelta(hours=60)
    for rec in L.records():
        L.add_settlement(rec["seq"], ts(after), "unresolved" if rec["game_id"] == GAME and rec["seq"] <= 50 else "settled", None, None, "t", {})
    L.add_settle_run(ts(after), {"games": 2, "settled": 0, "changed": 0, "undecided": 0, "errors": []})
    r = dry_run_acceptance(L, DATE, after + timedelta(minutes=1))["final"]
    assert not r["ok"] and any("game(s) with" in x for x in r["reasons"])


def test_freeze_refuses_when_nothing_has_matured(tmp_path, monkeypatch, capsys):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    run_dry_days(L)
    L.close()
    monkeypatch.setattr(freeze, "_now", lambda: TIP2 + timedelta(hours=6))
    monkeypatch.setattr(freeze, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(freeze, "settings", lambda: type("S", (), {"ledger_path": tmp_path / "l.db", "ledger_mirror_dir": tmp_path / "m"})())
    monkeypatch.setattr(freeze, "git_info", lambda *a, **k: {"commit": "abc", "clean": True})
    monkeypatch.setattr(sys, "argv", ["freeze.py", "--build"])
    freeze.main()
    monkeypatch.setattr(sys, "argv", ["freeze.py", "--record", "--commit", "abc", "--dry-from", DATE, "--dry-to", DAY2])
    assert freeze.main() == 1 and "have not matured" in capsys.readouterr().out
    L = Ledger(tmp_path / "l.db")
    assert L.latest_freeze() is None
    L.close()


def test_git_clean_is_judged_only_on_the_hashed_files(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("git"):
        pytest.skip("git not available")
    from app.provenance import git_info
    g = lambda *a: subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a], capture_output=True, text=True, check=True)
    g("init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    g("add", "a.py")
    g("commit", "-q", "-m", "init")
    (tmp_path / "unrelated.txt").write_text("private")                          # an untracked file elsewhere
    assert git_info(tmp_path)["clean"] is False and git_info(tmp_path, paths=["a.py"])["clean"] is True
    (tmp_path / "a.py").write_text("x = 2\n")
    assert git_info(tmp_path, paths=["a.py"])["clean"] is False
    (tmp_path / "b.py").write_text("y")
    g("checkout", "a.py")
    assert git_info(tmp_path, paths=["a.py", "b.py"])["clean"] is False         # a hashed file that was never committed


def test_fixed_cohort_stops_later_slate_dates_from_holding_maturity_out_of_reach(tmp_path):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    run_dry_days(L)
    late = TIP2 + timedelta(days=2)                                            # a third slate date the recorder keeps adding (recorded, not matured)
    seed_game(L, game="g-3", date="2026-11-23", tip=late, n_players=12, status="scheduled")
    after = MATURED + timedelta(hours=1)
    settle_all(L, MATURED)
    open_ended = dry_run_acceptance(L, DATE, after)["final"]
    assert not open_ended["ok"] and any("have not matured" in r for r in open_ended["reasons"])            # the growing window never matures
    fixed = dry_run_acceptance(L, DATE, after, DAY2)
    assert fixed["final"]["ok"], fixed["final"]["reasons"]
    assert fixed["slate_dates"] == [DATE, DAY2] and fixed["to_date"] == DAY2
    # the cohort does not weaken any rule: an unsettled contract inside the cohort still blocks it
    L2 = Ledger(tmp_path / "l2.db", tmp_path / "m2")
    run_dry_days(L2)
    L2.add_settle_run(ts(MATURED), {"games": 2, "settled": 0, "changed": 0, "undecided": 0, "errors": []})
    assert not dry_run_acceptance(L2, DATE, after, DAY2)["final"]["ok"]
    assert not dry_run_acceptance(L, DATE, TIP2 + timedelta(hours=6), DAY2)["final"]["ok"]                  # nothing matured yet in the cohort


def test_freeze_requires_the_fixed_cohort_end_date(tmp_path, monkeypatch, capsys):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    run_dry_days(L)
    L.close()
    monkeypatch.setattr(freeze, "_now", lambda: MATURED)
    monkeypatch.setattr(freeze, "settings", lambda: type("S", (), {"ledger_path": tmp_path / "l.db", "ledger_mirror_dir": tmp_path / "m"})())
    monkeypatch.setattr(freeze, "git_info", lambda *a, **k: {"commit": "abc", "clean": True})
    monkeypatch.setattr(sys, "argv", ["freeze.py", "--record", "--commit", "abc", "--dry-from", DATE])
    assert freeze.main() == 1 and "--dry-to is required" in capsys.readouterr().out
