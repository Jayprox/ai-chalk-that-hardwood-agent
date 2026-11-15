import json
import sqlite3
from pathlib import Path
from datetime import timedelta

import pytest

from app.ledger import APPEND_ONLY, LEDGER_FIELDS, Ledger, canon, ts
from tests.ledger_helpers import DATE, GAME, TIP, make_rec, seed_game


def test_every_table_rejects_update_and_delete(tmp_path):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    seed_game(L, n_players=2)
    L.reject(ts(TIP), "bad_line", GAME, "p", "pts")
    L.source_diff(ts(TIP), GAME, "p", "pts", "line_changed", 1, 2)
    L.register_game({"game_id": GAME, "date_et": DATE, "tipoff_utc": ts(TIP), "status": "scheduled"}, ts(TIP))
    L.register_game({"game_id": GAME, "date_et": DATE, "tipoff_utc": ts(TIP + timedelta(minutes=30)), "status": "final"}, ts(TIP))   # a tip-off and status change
    L.add_tick(ts(TIP), ts(TIP), True, {})
    L.add_settlement(1, ts(TIP), "settled", 3.0, "over", "t", {})
    L.add_verification(GAME, "p", "x", "t", ts(TIP))
    L.add_freeze(ts(TIP), DATE, {"a": 1}, None, None)
    L.add_analysis_run(ts(TIP), 1, "g", {"a": 1}, "c")
    L.add_settle_run(ts(TIP), {"games": 1, "settled": 1, "changed": 0, "undecided": 0, "errors": []})
    L.add_input("player_log", {"a": 1}, ts(TIP))
    L.index_input(DATE, "p", "h", ts(TIP), 1)
    L.add_league_snapshot(DATE, "h", ts(TIP), 1)
    for t in APPEND_ONLY:
        n = L.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n > 0, t
        for sql in (f"UPDATE {t} SET rowid = rowid", f"DELETE FROM {t}"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                L.conn.execute(sql)


def test_duplicate_contract_is_not_written_twice(tmp_path):
    L = Ledger(tmp_path / "l.db")
    assert L.append_record(make_rec(1)) is not None
    assert L.append_record(make_rec(1, line=9.5)) is None            # a different line for the same contract does not replace or add a row
    assert len(L.records()) == 1 and L.records()[0]["line"] == 2.5


def test_hash_chain_detects_tampering_and_missing_mirror(tmp_path):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    seed_game(L, n_players=3)
    assert L.verify_chain()["ok"]
    # an attacker (or a bug) who bypasses the triggers still breaks the chain
    L.conn.execute("DROP TRIGGER ledger_no_update")
    L.conn.execute("UPDATE ledger SET line = 99 WHERE seq = 2")
    bad = L.verify_chain()
    assert not bad["ok"] and any("seq 2" in p for p in bad["problems"]) and any("triggers" in p for p in bad["problems"])
    # deleting a mirror line is reported
    L2 = Ledger(tmp_path / "l2.db", tmp_path / "m2")
    seed_game(L2, n_players=2)
    f = next((tmp_path / "m2").glob("*.jsonl"))
    f.write_text("")
    assert any("mirror" in p for p in L2.verify_chain()["problems"])


def test_restart_preserves_the_ledger_and_the_chain(tmp_path):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    n = seed_game(L, n_players=4)
    head = L.verify_chain()["head"]
    L.close()
    L = Ledger(tmp_path / "l.db", tmp_path / "m")                      # a restart
    v = L.verify_chain()
    assert v["ok"] and v["rows"] == n and v["head"] == head
    assert L.append_record(make_rec(99)) is not None and L.verify_chain()["ok"]


def test_mirror_has_one_line_per_row_with_the_same_hash(tmp_path):
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    seed_game(L, n_players=3)
    lines = [json.loads(x) for f in (tmp_path / "m").glob("*.jsonl") for x in f.read_text().splitlines()]
    assert [x["row_hash"] for x in lines] == [r["row_hash"] for r in L.records()]


def test_freeze_assigns_counted_records_only_from_the_first_counted_date(tmp_path):
    L = Ledger(tmp_path / "l.db")
    seed_game(L, game="dry", n_players=2)                               # before any freeze
    L.add_freeze("2026-11-20T12:00:00.000000Z", "2026-11-21", {"m": 1}, "abc", True)
    seed_game(L, game="same-day", date="2026-11-20", n_players=2, tip=TIP)                       # recorded after the freeze but on an earlier date
    nxt = TIP + timedelta(days=1)
    seed_game(L, game="counted", date="2026-11-21", tip=nxt, n_players=2)
    by = {}
    for r in L.records():
        by.setdefault(r["game_id"], set()).add(r["freeze_id"])
    assert by == {"dry": {None}, "same-day": {None}, "counted": {1}}


def test_a_record_cannot_be_backdated_into_a_freeze_that_did_not_exist_yet(tmp_path):
    L = Ledger(tmp_path / "l.db")
    L.add_freeze("2026-11-22T12:00:00.000000Z", "2026-11-21", {"m": 1}, None, None)
    L.append_record(make_rec(1, date="2026-11-21", recorded=TIP + timedelta(days=1) - timedelta(minutes=28), tip=TIP + timedelta(days=1)))
    assert L.records()[0]["freeze_id"] is None                           # recorded before the freeze record's timestamp: never counted


def test_fields_are_the_declared_set(tmp_path):
    L = Ledger(tmp_path / "l.db")
    L.append_record(make_rec(1))
    cols = {r[1] for r in L.conn.execute("PRAGMA table_info(ledger)")}
    assert set(LEDGER_FIELDS) <= cols and {"prev_hash", "row_hash", "seq"} <= cols


def test_backup_is_a_verified_consistent_copy_taken_while_the_ledger_is_open(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from ledger_backup import backup
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    seed_game(L, n_players=6)
    n = len(L.records())
    r = backup(tmp_path / "l.db", tmp_path / "m", tmp_path / "bk", stamp="s1")        # the source stays open (WAL) during the copy
    assert r["ok"], r["problems"]
    c = Ledger(tmp_path / "bk" / "s1" / "ledger.db", tmp_path / "bk" / "s1" / "mirror")
    assert len(c.records()) == n and c.verify_chain()["ok"]
    c.close()
    with pytest.raises(FileExistsError):
        backup(tmp_path / "l.db", tmp_path / "m", tmp_path / "bk", stamp="s1")
    L.close()


def test_confirm_dnp_script_saves_an_append_only_confirmation_and_refuses_unknown_contracts(tmp_path, monkeypatch, capsys):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import confirm_dnp
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    seed_game(L, n_players=3)
    rec = L.records()[0]
    L.close()
    monkeypatch.setattr(confirm_dnp, "settings", lambda: None)
    monkeypatch.setattr(confirm_dnp, "open_ledger", lambda s: Ledger(tmp_path / "l.db", tmp_path / "m"))
    run = lambda *a: (monkeypatch.setattr(sys, "argv", ["confirm_dnp.py", *a]), confirm_dnp.main())[1]
    assert run("--game", rec["game_id"], "--player", "nobody", "--source", "x") == 2
    assert run("--game", rec["game_id"], "--player", rec["player_id"], "--source", "official NBA box score", "--note", "inactive") == 0
    L = Ledger(tmp_path / "l.db", tmp_path / "m")
    v = L.latest_verification(rec["game_id"], rec["player_id"])
    assert v["status"] == "confirmed_dnp" and v["source"] == "official NBA box score"
    L.close()
