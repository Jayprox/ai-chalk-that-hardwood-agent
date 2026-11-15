import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.collector import collect_once
from app.dashboard import STRONG_PCT, summary_text, tip_label
from app.server import create_app
from app.store import Store
from tests.helpers import FakeAnthropic, FakeHardwood as ChatHW, message_sse, settings, text_block
from tests.test_collector import G1, G2, FakeHW, row

NOW = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)
DATE = "2026-11-13"


def board(n=12):
    """n points props with steadily stronger tilt toward the over; the last is the strongest."""
    rows = []
    for i in range(n):
        rows.append(row(f"p{i}", game=G1 if i % 2 == 0 else G2, name=f"Player {i}", line=10.5 + i, over=-105 - 10 * i, under=-105 + 8 * i + 1,
                        last10={"over": 6, "under": 4}, matchup={"rank": 20, "of": 30, "vs_avg": 0.8}))
    return rows


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "dash.db"
    store = Store(db)
    asyncio.run(collect_once(FakeHW({"pts": board(), "ast": [row("a1", line=6.5, over=-110, under=-110)]}), store, DATE, NOW))
    # a later collection moves one line, so that prop has two history rows
    moved = board()
    moved[11] = row("p11", game=G2, name="Player 11", line=22.5, over=-170, under=+140, last10={"over": 6, "under": 4}, matchup={"rank": 20, "of": 30, "vs_avg": 0.8})
    asyncio.run(collect_once(FakeHW({"pts": moved, "ast": [row("a1", line=6.5, over=-110, under=-110)]}), store, DATE, NOW + timedelta(hours=2)))
    store.close()
    app = create_app(settings(db_path=db), anthropic_client=FakeAnthropic([message_sse([text_block("hi")])]).client(), hardwood=ChatHW({}).client())
    return TestClient(app)


def test_overview_has_eleven_tabs_games_and_the_strong_counts(client):
    o = client.get("/api/dash/overview", params={"date": DATE}).json()
    assert [t["label"] for t in o["tabs"]] == ["Games", "Points", "Assists", "Rebounds", "3PM", "P+A", "P+R", "A+R", "P+A+R", "Steals", "Blocks"]
    pts = next(t for t in o["tabs"] if t["key"] == "pts")
    assert pts["count"] == 12 and pts["strong"] == 0
    assert o["fallback"] is False and o["last_run"]["ok"] and o["rule"]["sim"] == "required"
    g1 = next(g for g in o["games"] if g["game_id"] == G1)
    assert g1["tip"] == "7:00 PM ET" and g1["state"] == "active"          # 22:00Z is inside the three hours before a 00:00Z tip
    assert len(g1["top"]) == 3 and g1["top"][0]["pct"] >= g1["top"][-1]["pct"]
    assert next(g for g in o["games"] if g["game_id"] == G2)["state"] == "final"


def test_a_date_without_data_falls_back_to_the_latest_date_that_has_some(client):
    o = client.get("/api/dash/overview", params={"date": "2026-12-25"}).json()
    assert o["fallback"] is True and o["date"] == DATE and o["requested"] == "2026-12-25"
    assert client.get("/api/dash/overview", params={"date": "not-a-date"}).status_code == 422


def test_market_tab_is_sorted_by_rank_and_strong_needs_the_sim_to_agree(client):
    m = client.get("/api/dash/market/pts", params={"date": DATE}).json()
    props = m["props"]
    assert len(props) == 12 and [p["pct"] for p in props] == sorted([p["pct"] for p in props], reverse=True)
    for p in props:
        live = p["state"] != "final"
        assert p["top10"] == (live and p["pct"] >= STRONG_PCT) and p["sim"] == "pending"
        assert p["strong"] is False                               # no simulation stored, so nothing is strong yet
    assert m["strong"] == sum(p["strong"] for p in props)
    assert all(p["over_price"] and p["tip"] for p in props) and {g["label"] for g in m["games"]} == {"DEN @ OKC", "BOS @ LAL"}
    assert client.get("/api/dash/market/nope").status_code == 404
    assert client.get("/api/dash/market/stl", params={"date": DATE}).json()["props"] == []


def test_game_detail_uses_whole_market_ranks_and_unknown_games_404(client):
    d = client.get(f"/api/dash/game/{G1}", params={"date": DATE}).json()
    assert d["game"]["away"] == "DEN" and all(p["game_id"] == G1 for p in d["props"])
    whole = {p["player_id"]: p["pct"] for p in client.get("/api/dash/market/pts", params={"date": DATE}).json()["props"]}
    assert all(p["pct"] == whole[p["player_id"]] for p in d["props"] if p["market"] == "pts")
    assert len(d["markets"]) == 10 and client.get("/api/dash/game/nope").status_code == 404


def test_prop_detail_has_step_history_notes_and_extra(client):
    d = client.get(f"/api/dash/prop/p11/{G2}/pts").json()
    assert [h["line"] for h in d["history"]][-1] == 22.5 and len(d["history"]) == 2
    titles = [n["title"] for n in d["notes"]]
    assert titles[:3] == ["Market read", "Recent form", "Matchup"] and "Caveats" in titles
    assert "6 over and 4 under" in d["notes"][1]["text"] and d["extra"]["matchup"]["rank"] == 20
    assert d["prop"]["side"] in ("OVER", "UNDER") and d["prop"]["spark"]
    assert client.get(f"/api/dash/prop/zzz/{G1}/pts").status_code == 404


def test_pages_and_assets_are_served_and_the_chat_page_is_untouched(client):
    page = client.get("/dash")
    assert page.status_code == 200 and "Props Research" in page.text and page.headers["cache-control"] == "no-store"
    assert client.get("/static/dash.js").status_code == 200 and client.get("/static/dash.css").status_code == 200
    assert "NBA Research Agent" in client.get("/").text


def test_an_empty_database_gives_an_empty_overview_not_an_error(tmp_path):
    app = create_app(settings(db_path=tmp_path / "empty.db"), anthropic_client=FakeAnthropic([]).client(), hardwood=ChatHW({}).client())
    o = TestClient(app).get("/api/dash/overview").json()
    assert o["games"] == [] and o["last_run"] is None and o["props_total"] == 0


def test_summary_text_copes_with_missing_data_and_tip_label_is_eastern():
    base = {"side": None, "p_over": None, "tilt": None, "line": 5, "line_move": None, "open_line": None, "market_label": "Points", "pct": None}
    assert [n["title"] for n in summary_text(base, {})] == ["Availability", "Caveats"]
    assert tip_label("2026-11-14T00:30:00Z") == "7:30 PM ET" and tip_label("junk") == "tip-off unknown"


def _put_sims(db, spec):
    import json
    store = Store(db)
    rows = []
    for pid, mass_at in spec.items():
        pmf = [0.0] * 60
        pmf[mass_at] = 1.0
        rows.append({"player_id": pid, "game_id": G1, "market": "pts", "date_et": DATE, "status": "ok", "mean": float(mass_at), "sd": 3.0,
                     "n_prior": 12, "pmf": json.dumps(pmf), "computed_at": "2026-11-13T20:00:00Z"})
    store.upsert_sims(rows)
    store.close()


def test_strong_needs_top_ten_percent_and_a_sim_that_agrees(tmp_path):
    db = tmp_path / "s.db"
    store = Store(db)
    asyncio.run(collect_once(FakeHW({"pts": board(40)}), store, DATE, NOW))
    store.close()
    app = create_app(settings(db_path=db), anthropic_client=FakeAnthropic([message_sse([text_block("hi")])]).client(), hardwood=ChatHW({}).client())
    c = TestClient(app)
    top = next(p for p in c.get("/api/dash/market/pts", params={"date": DATE}).json()["props"] if p["state"] != "final" and p["top10"])
    pid, line = top["player_id"], top["line"]
    assert top["side"] == "OVER"
    _put_sims(db, {pid: int(line) + 8})                          # simulation far above the line: leans over, agrees
    got = {p["player_id"]: p for p in c.get("/api/dash/market/pts", params={"date": DATE}).json()["props"]}
    assert got[pid]["sim"] == "agree" and got[pid]["strong"] and got[pid]["sim_side"] == "OVER" and got[pid]["sim_p_over_raw"] == 1.0 and 0.5 < got[pid]["sim_p_over"] < 0.95
    _put_sims(db, {pid: max(int(line) - 8, 0)})                  # simulation far below: disagrees, still top 10 but not strong
    got = {p["player_id"]: p for p in c.get("/api/dash/market/pts", params={"date": DATE}).json()["props"]}
    assert got[pid]["sim"] == "disagree" and got[pid]["top10"] and not got[pid]["strong"]
    o = c.get("/api/dash/overview", params={"date": DATE}).json()
    assert next(t for t in o["tabs"] if t["key"] == "pts")["strong"] == 0


def test_prop_detail_carries_the_distribution_and_a_simulation_note(tmp_path):
    db = tmp_path / "d.db"
    store = Store(db)
    asyncio.run(collect_once(FakeHW({"pts": board()}), store, DATE, NOW))
    store.close()
    _put_sims(db, {"p10": 30})
    app = create_app(settings(db_path=db), anthropic_client=FakeAnthropic([message_sse([text_block("hi")])]).client(), hardwood=ChatHW({}).client())
    d = TestClient(app).get(f"/api/dash/prop/p10/{G1}/pts").json()
    assert d["sim"]["n_prior"] == 12 and len(d["sim"]["pmf"]) == 60 and d["sim"]["side"] == "OVER"
    note = next(n for n in d["notes"] if n["title"] == "Simulation")
    assert "12 earlier games" in note["text"] and "overconfident" in note["text"] and "shrunk" in note["text"]
    other = TestClient(app).get(f"/api/dash/prop/p8/{G1}/pts").json()
    assert other["sim"] is None and next(n for n in other["notes"] if n["title"] == "Simulation")["text"].startswith("Not run yet")
