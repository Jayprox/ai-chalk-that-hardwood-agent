import asyncio
from datetime import timedelta

import pytest

from app import protocol as P
from app.coverage import coverage_report, settlement_counts, scope_info
from app.ledger import Ledger, ts
from app.settle import decide_result, settle_once, settled_rows
from tests.ledger_helpers import DATE, GAME, TIP, Clock, FakeHW, box, make_rec, seed_game

AFTER = TIP + timedelta(hours=6)                                  # the game is over
MATURE = TIP + timedelta(hours=P.GAME_LENGTH_HOURS + P.MATURITY_HOURS + 1)


def logrow(gid=GAME, pts=3, reb=0, ast=0, minutes=30):
    return {"game_id": gid, "date": DATE, "minutes": minutes, "pts": pts, "reb": reb, "ast": ast, "fg3m": 0, "stl": 0, "blk": 0}


class SettleHW(FakeHW):
    def __init__(self, logs, status="final", boxes=None):
        super().__init__(players={pid: ("n", rows) for pid, rows in logs.items()}, boxes=boxes or {GAME: box(True, 8)})
        self.status = status

    async def get(self, path, params=None):
        if path.startswith("/games/"):
            b = self.boxes[path.split("/")[-1]]
            return {"data": {**b["data"], "game": {"status": self.status}}}
        return await super().get(path, params)


def settle(L, hw, now):
    return asyncio.run(settle_once(hw, L, "2026-27", clock=Clock(start=now)))


def one(L, **kw):
    seed_game(L, n_players=1, markets=("pts",), **kw)
    return L.records()[0]


def test_over_under_and_push_use_the_exact_recorded_line(tmp_path):
    L = Ledger(tmp_path / "l.db")
    seed_game(L, n_players=3, markets=("pts",), line=2.5)
    ids = [f"{GAME}-p{i}" for i in range(3)]
    hw = SettleHW({ids[0]: [logrow(pts=3)], ids[1]: [logrow(pts=2)], ids[2]: [logrow(pts=3)]})
    settle(L, hw, AFTER)
    res = {r["player_id"]: L.latest_settlement(r["seq"]) for r in L.records()}
    assert (res[ids[0]]["status"], res[ids[0]]["outcome"], res[ids[0]]["actual"]) == ("settled", "over", 3.0)
    assert (res[ids[1]]["status"], res[ids[1]]["outcome"]) == ("settled", "under")
    L2 = Ledger(tmp_path / "l2.db")
    seed_game(L2, n_players=1, markets=("pts",), line=3.0)                      # an integer line: a tie is a push, never a win or loss
    settle(L2, SettleHW({f"{GAME}-p0": [logrow(pts=3)]}), AFTER)
    s = L2.latest_settlement(1)
    assert s["status"] == "push" and s["outcome"] is None and s["actual"] == 3.0


def test_a_later_source_line_change_does_not_change_the_settlement(tmp_path):
    L = Ledger(tmp_path / "l.db")
    seed_game(L, n_players=1, markets=("pts",), line=2.5)
    hw = SettleHW({f"{GAME}-p0": [logrow(pts=3)]})
    hw.rows = {"pts": [{"player_id": f"{GAME}-p0", "game_id": GAME, "line": 3.5, "over_price": -300, "under_price": 250, "snapshot": "close"}]}
    settle(L, hw, AFTER)
    assert L.latest_settlement(1)["outcome"] == "over"                          # 3 > 2.5 (the saved threshold), not 3 < 3.5
    assert L.records()[0]["line"] == 2.5


def test_combo_markets_sum_their_stats(tmp_path):
    L = Ledger(tmp_path / "l.db")
    seed_game(L, n_players=1, markets=("pra",), line=14.5)
    settle(L, SettleHW({f"{GAME}-p0": [logrow(pts=8, reb=4, ast=3)]}), AFTER)
    s = L.latest_settlement(1)
    assert s["actual"] == 15.0 and s["outcome"] == "over"


def test_only_confirmed_nonparticipation_voids_everything_else_stays_unresolved(tmp_path):
    L = Ledger(tmp_path / "l.db")
    seed_game(L, n_players=6, markets=("pts",))
    ids = [f"{GAME}-p{i}" for i in range(6)]
    logs = {ids[0]: [logrow(minutes=0, pts=0)],            # zero minutes, no flag: could be rounding -> unresolved
            ids[1]: [],                                      # missing from the log and from a box with many players -> unresolved
            ids[2]: [logrow(minutes=0, pts=0)],            # explicit DNP flag in the box score -> void
            ids[3]: [],                                      # explicit DNP flag in the box, no log row -> void
            ids[4]: [{**logrow(pts=5), "minutes": None}],  # missing minutes but real stats: he played -> settled
            ids[5]: [logrow(pts=5)]}
    hw = SettleHW(logs, boxes={GAME: box(True, 8, dnp=[ids[2], ids[3]])})
    settle(L, hw, AFTER)
    st = {r["player_id"]: L.latest_settlement(r["seq"]) for r in L.records()}
    assert st[ids[0]] is None and st[ids[1]] is None                      # nothing written: unresolved, never zero or a loss
    assert st[ids[2]]["status"] == "void_dnp" and "explicit DNP" in st[ids[2]]["detail"] and st[ids[2]]["actual"] is None
    assert st[ids[3]]["status"] == "void_dnp"
    assert st[ids[4]]["status"] == "settled" and st[ids[5]]["status"] == "settled"
    c = settlement_counts(L, scope_info(L, "all"), MATURE)
    assert c["by_status"] == {"unresolved": 2, "void_dnp": 2, "settled": 2}


def test_dnp_rule_cases_directly():
    rec = {"tipoff_utc": ts(TIP), "market": "pts", "line": 2.5, "player_id": "x"}
    fin = {"status": "final"}
    big = box(True, 12)                                                   # many players with minutes on both teams
    assert decide_result(rec, None, fin, big, AFTER) is None              # absent from log: not proof, even with a full box
    assert decide_result(rec, None, fin, None, AFTER) is None             # no box at all
    assert decide_result(rec, logrow(minutes=0, pts=0), fin, big, AFTER) is None      # displayed 0 minutes may be rounded
    assert decide_result(rec, {**logrow(pts=0), "minutes": None}, fin, big, AFTER) is None    # missing minutes
    assert decide_result(rec, {**logrow(pts=2), "minutes": 0}, fin, big, AFTER)[0] == "settled"      # stats show he played, whatever the minutes say
    assert decide_result(rec, {**logrow(minutes=0, pts=0), "dnp": True, "dnp_reason": "inactive"}, fin, big, AFTER)[0] == "void_dnp"
    conf = {"status": "confirmed_dnp", "source": "official NBA box score", "note": "inactive: ankle", "checked_at": ts(AFTER)}
    res = decide_result(rec, None, fin, big, AFTER, conf)
    assert res[0] == "void_dnp" and res[3]["source"] == "official NBA box score" and res[1] is None and res[2] is None
    assert decide_result(rec, logrow(pts=4), fin, big, AFTER, conf)[0] == "settled"          # played beats a mistaken confirmation
    assert decide_result(rec, None, fin, big, AFTER, {**conf, "status": "confirmed_played"}) is None


def test_box_score_row_settles_when_the_log_row_is_missing_or_incomplete():
    rec = {"tipoff_utc": ts(TIP), "market": "pra", "line": 14.5, "player_id": "x"}
    fin = {"status": "final"}
    b = box(True, 8, played={"x": {"minutes": 30, "pts": 8, "reb": 4, "ast": 3}})
    r = decide_result(rec, None, fin, b, AFTER)
    assert r[0] == "settled" and r[1] == 15.0 and r[3]["source"] == "box_score"
    r = decide_result(rec, {"minutes": 30, "pts": 8, "reb": 4}, fin, b, AFTER)           # the log lacks assists: fall back to the box
    assert r[1] == 15.0 and r[3]["source"] == "box_score"
    assert decide_result(rec, {"minutes": 30, "pts": 8, "reb": 4}, fin, box(True, 8), AFTER) is None   # nowhere has every component: unresolved


def test_actual_for_never_turns_missing_data_into_zero():
    from app.settle import actual_for
    assert actual_for("pts", {"pts": 0}) == 0.0                           # a genuine zero is valid
    assert actual_for("pts", {"pts": None}) is None and actual_for("pts", {}) is None and actual_for("pts", {"pts": "x"}) is None
    assert actual_for("pra", {"pts": 8, "reb": 0, "ast": 3}) == 11.0
    assert actual_for("pra", {"pts": 8, "ast": 3}) is None and actual_for("pa", {"pts": 8, "ast": None}) is None
    assert actual_for("nonsense", {"pts": 1}) is None and actual_for("pts", None) is None and actual_for("pts", {"pts": True}) is None


def test_manual_confirmation_is_used_and_kept_as_evidence(tmp_path):
    L = Ledger(tmp_path / "l.db")
    seed_game(L, n_players=1, markets=("pts",))
    pid = f"{GAME}-p0"
    settle(L, SettleHW({pid: []}), AFTER)
    assert L.latest_settlement(1) is None                                 # unresolved
    L.add_verification(GAME, pid, "confirmed_dnp", "official NBA box score", ts(AFTER), "listed inactive")
    settle(L, SettleHW({pid: []}), AFTER + timedelta(hours=1))
    s = L.latest_settlement(1)
    assert s["status"] == "void_dnp" and "official NBA box score" in s["detail"] and "listed inactive" in s["detail"]
    assert L.latest_verification(GAME, pid)["source"] == "official NBA box score"


def test_pilot_confirmations_are_preserved_as_evidence():
    from app.config import ROOT
    text = (ROOT / "docs" / "evidence" / "pilot_dnp_confirmations.md").read_text()
    for needle in ("Cade Cunningham", "Jalen Duren", "official NBA box score", "2025-11-12", "Left Hip", "Right Ankle"):
        assert needle in text


def test_every_settle_run_is_logged(tmp_path):
    L = Ledger(tmp_path / "l.db")
    seed_game(L, n_players=1, markets=("pts",))
    settle(L, SettleHW({f"{GAME}-p0": [logrow()]}), AFTER)
    runs = L.settle_runs()
    assert len(runs) == 1 and runs[0]["errors"] == 0 and runs[0]["settled"] == 1


def test_games_that_never_finish_are_voided_after_the_abandon_window_only(tmp_path):
    rec = {"tipoff_utc": ts(TIP), "market": "pts", "line": 2.5}
    live = {"status": "live"}
    assert decide_result(rec, None, live, None, TIP + timedelta(hours=P.ABANDON_HOURS - 1)) is None
    assert decide_result(rec, None, live, None, TIP + timedelta(hours=P.ABANDON_HOURS + 1))[0] == "void_not_played"
    assert decide_result(rec, logrow(), live, None, TIP + timedelta(hours=2)) is None          # not final: nothing is settled early


def test_stat_corrections_add_a_new_version_and_the_latest_wins(tmp_path):
    L = Ledger(tmp_path / "l.db")
    seed_game(L, n_players=1, markets=("pts",), line=2.5)
    pid = f"{GAME}-p0"
    settle(L, SettleHW({pid: [logrow(pts=3)]}), AFTER)
    settle(L, SettleHW({pid: [logrow(pts=3)]}), AFTER + timedelta(hours=1))
    assert len(L.settlement_history(1)) == 1                                    # unchanged data: no new version
    out = settle(L, SettleHW({pid: [logrow(pts=2)]}), AFTER + timedelta(hours=30))      # the league corrected the box score
    hist = L.settlement_history(1)
    assert out["changed"] == 1 and [h["version"] for h in hist] == [1, 2]
    assert [h["outcome"] for h in hist] == ["over", "under"] and L.latest_settlement(1)["outcome"] == "under"
    rows = settled_rows(L, [GAME], counted_only=False)
    assert rows[0]["outcome"] == "under"


def test_no_contract_is_lost_silently_every_matured_contract_has_a_status(tmp_path):
    L = Ledger(tmp_path / "l.db")
    seed_game(L, n_players=5, markets=("pts",))
    ids = [f"{GAME}-p{i}" for i in range(5)]
    logs = {ids[0]: [logrow(pts=3)], ids[1]: [logrow(pts=2)], ids[2]: [logrow(pts=3)], ids[3]: [logrow(minutes=0, pts=0)]}      # p4 has no log rows at all (the fetch itself works)
    logs[ids[4]] = []
    settle(L, SettleHW(logs, boxes={GAME: box(True, 8, dnp=[ids[3]])}), AFTER)      # p3 has an explicit DNP flag; p4 cannot be verified
    info = scope_info(L, "all")
    c = settlement_counts(L, info, MATURE)
    assert c["matured_contracts"] == 5 and sum(c["by_status"].values()) == 5
    assert c["by_status"] == {"settled": 3, "void_dnp": 1, "unresolved": 1}
    assert c["settleable_share"] == pytest.approx(0.6) and c["exclusion_share"] == pytest.approx(0.4)
    rows = settled_rows(L, [GAME], counted_only=False)
    assert len(rows) == 5 and {r["status"] for r in rows} == {"settled", "void_dnp", "unresolved"}


def test_settlement_never_modifies_ledger_rows(tmp_path):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    seed_game(L, n_players=3, markets=("pts",))
    head = L.verify_chain()["head"]
    settle(L, SettleHW({f"{GAME}-p{i}": [logrow(pts=3)] for i in range(3)}), AFTER)
    v = L.verify_chain()
    assert v["ok"] and v["head"] == head and len(L._all("SELECT * FROM settlements")) == 3


def test_a_failed_log_fetch_leaves_the_contract_unsettled_not_voided(tmp_path):
    L = Ledger(tmp_path / "l.db")
    seed_game(L, n_players=2, markets=("pts",))
    out = settle(L, _Flaky({f"{GAME}-p0": [logrow()], f"{GAME}-p1": [logrow()]}), AFTER)
    assert out["errors"] and len(L._all("SELECT * FROM settlements")) == 1


class _Flaky(SettleHW):
    async def post(self, path, body):
        if body["id"].endswith("p1"):
            raise RuntimeError("boom")
        return await super().post(path, body)
