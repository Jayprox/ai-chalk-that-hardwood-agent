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


# ---- last-10 bar chart data (display only)

def _g(date, pts, reb=0, ast=0, minutes=30, **kw):
    return {"date": date, "opponent": "SAC", "venue": "home", "minutes": minutes, "pts": pts, "reb": reb, "ast": ast, **kw}


def test_last_games_takes_the_last_ten_played_before_the_board_date_oldest_first():
    from app.dashboard import last_games
    log = [_g(f"2026-11-{d:02d}", 10 + d) for d in range(1, 13)]            # 12 games, Nov 1..12
    log.append(_g("2026-11-13", 99))                                        # the board date itself is excluded
    log.append(_g("2026-11-05", 0, minutes=0))                              # did not play: skipped, never a zero
    out = last_games(log, "pts", 17.5, "2026-11-13")
    assert [g["date"] for g in out] == [f"2026-11-{d:02d}" for d in range(3, 13)]
    assert out[0]["value"] == 13 and out[-1]["value"] == 22
    assert [g["result"] for g in out] == ["under"] * 5 + ["over"] * 5       # values 13..22 against 17.5


def test_last_games_combo_markets_need_every_component_and_a_tie_is_a_push():
    from app.dashboard import last_games
    log = [_g("2026-11-01", 10, 4, 3.5 - 0.5), _g("2026-11-02", 10, 5, None), _g("2026-11-03", 8, 6, 3)]
    out = last_games(log, "pra", 17, "2026-11-13")
    assert [g["date"] for g in out] == ["2026-11-01", "2026-11-03"]         # the game missing assists is skipped, not zero-filled
    assert [g["value"] for g in out] == [17, 17] and [g["result"] for g in out] == ["push", "push"]
    assert last_games(log, "nope", 17, "2026-11-13") == []


class _FakeLogHW:
    calls = 0

    def __init__(self, log=None, fail=False):
        self.log, self.fail = log, fail

    async def post(self, path, body):
        type(self).calls += 1
        assert body["scope"] == "game_log" and body["season_type"] == "regular" and body["season"] == "2026-27"
        if self.fail:
            raise RuntimeError("secret upstream detail")
        return {"data": self.log}

    async def teams(self):
        return [{"id": 1, "abbreviation": "SAC"}, {"id": 2, "abbreviation": "PHX"}]

    async def get(self, path, params=None):
        assert path == "/rankings/matchups"
        if params["team_id"] == 2:
            raise RuntimeError("down")
        return {"data": {"C": [{"rank": {"pts": 7, "pra": 5}}], "G": [{"rank": {"pts": 30}}]}}

    async def aclose(self):
        pass


def test_def_ranks_use_the_players_position_and_market_and_tolerate_failures():
    import app.dashboard as dash
    dash._def_cache.clear()
    out = asyncio.run(dash._def_ranks(_FakeLogHW(), ["SAC", "PHX", "ZZZ"], "C", "pra", DATE))
    assert out == {"SAC": 5, "PHX": None, "ZZZ": None}
    assert asyncio.run(dash._def_ranks(_FakeLogHW(), ["SAC"], None, "pra", DATE)) == {"SAC": None}


def test_recent_endpoint_returns_counts_caches_and_hides_upstream_errors(client, monkeypatch):
    import app.dashboard as dash
    dash._recent_cache.clear()
    _FakeLogHW.calls = 0
    log = [_g(f"2026-11-{d:02d}", 8 + d) for d in range(1, 11)]             # values 9..18; the line for Player 3 is 13.5
    monkeypatch.setattr(dash, "_hw_factory", lambda s: _FakeLogHW(log))
    pid = next(p for p in client.get("/api/dash/market/pts", params={"date": DATE}).json()["props"] if p["name"] == "Player 3")
    url = f"/api/dash/recent/{pid['player_id']}/{pid['game_id']}/pts"
    r = client.get(url).json()
    assert r["line"] == 13.5 and len(r["games"]) == 10 and (r["over"], r["under"], r["push"]) == (5, 5, 0)
    client.get(url)
    assert _FakeLogHW.calls == 1                                           # second view is served from the cache
    dash._recent_cache.clear()
    monkeypatch.setattr(dash, "_hw_factory", lambda s: _FakeLogHW(fail=True))
    bad = client.get(url)
    assert bad.status_code == 502 and "secret" not in bad.text
    assert client.get("/api/dash/recent/nobody/none/pts").status_code == 404


def test_recent_endpoint_follows_a_moved_line_without_refetching_the_game_log(tmp_path, monkeypatch):
    import app.dashboard as dash
    dash._recent_cache.clear()
    _FakeLogHW.calls = 0
    log = [_g(f"2026-11-{d:02d}", 8 + d) for d in range(1, 11)]             # values 9..18
    monkeypatch.setattr(dash, "_hw_factory", lambda s: _FakeLogHW(log))
    db = tmp_path / "moved.db"
    store = Store(db)

    def at(line, hours):
        asyncio.run(collect_once(FakeHW({"pts": [row("p3", game=G1, name="Player 3", line=line, over=-110, under=-110, last10={"over": 6, "under": 4})]}), store, DATE, NOW + timedelta(hours=hours)))

    at(13.5, 0)
    store.close()
    c = TestClient(create_app(settings(db_path=db), anthropic_client=FakeAnthropic([message_sse([text_block("hi")])]).client(), hardwood=ChatHW({}).client()))
    url = f"/api/dash/recent/p3/{G1}/pts"
    first = c.get(url).json()
    assert first["line"] == 13.5 and (first["over"], first["under"]) == (5, 5)
    store = Store(db)
    at(15.5, 2)                                                             # the line moves later the same day
    store.close()
    second = c.get(url).json()
    assert second["line"] == 15.5 and (second["over"], second["under"], second["push"]) == (3, 7, 0)
    assert [g["result"] for g in second["games"]].count("over") == 3
    assert _FakeLogHW.calls == 1                                            # outcomes re-scored from the cached log, not refetched


def test_dashboard_page_loads_the_chart_helper_before_the_main_script(client):
    html = client.get("/dash").text
    assert html.index("/static/recent_chart.js") < html.index("/static/dash.js")
    assert client.get("/static/recent_chart.js").status_code == 200
