import asyncio
import json
from datetime import datetime

import httpx
import pytest

from app.config import ET, current_season
from app.hardwood import TOOL_SCHEMAS, TOOLS, ToolContext, ToolError, describe_call, run_tool
from tests.helpers import GAME_ID, JOKIC_ID, JOKIC_SEASON_RESPONSE, TEAMS, FakeHardwood

CTX = ToolContext(now=datetime(2026, 11, 11, 21, 0, tzinfo=ET), season="2026-27")


def run(coro):
    return asyncio.run(coro)


def player_row(pid, name, team="DEN", active=True):
    return {"id": pid, "full_name": name, "listed_position": "C", "is_active": active, "team": team}


def players_route(rows_by_active):
    def route(request: httpx.Request):
        return httpx.Response(200, json={"data": rows_by_active[request.url.params["active"]], "meta": {"total": 1}})
    return route


# ---------------------------------------------------------------- resolution


def test_resolve_player_exact_and_accent_insensitive():
    fake = FakeHardwood({("GET", "/players"): players_route({"true": [player_row(JOKIC_ID, "Nikola Jokić")], "false": []})})
    hw = fake.client()
    assert run(hw.resolve_player("nikola jokic"))["id"] == JOKIC_ID


def test_resolve_player_uuid_skips_http():
    fake = FakeHardwood()
    run(fake.client().resolve_player(JOKIC_ID))
    assert fake.requests == []


def test_resolve_player_falls_back_to_retired():
    fake = FakeHardwood({("GET", "/players"): players_route({"true": [], "false": [player_row("x", "Old Guy", None, False)]})})
    assert run(fake.client().resolve_player("Old Guy"))["id"] == "x"


def test_resolve_player_ambiguous_lists_ids():
    rows = [player_row("id-a", "Jalen Williams", "OKC"), player_row("id-b", "Jaylin Williams", "OKC")]
    fake = FakeHardwood({("GET", "/players"): players_route({"true": rows, "false": []})})
    with pytest.raises(ToolError) as e:
        run(fake.client().resolve_player("Williams"))
    assert "id-a" in str(e.value) and "id-b" in str(e.value)


def test_resolve_player_not_found():
    fake = FakeHardwood({("GET", "/players"): players_route({"true": [], "false": []})})
    with pytest.raises(ToolError, match="No player found"):
        run(fake.client().resolve_player("Nobody"))


@pytest.mark.parametrize("ref,abbr", [("BOS", "BOS"), ("celtics", "BOS"), ("Boston Celtics", "BOS"), ("boston", "BOS"),
                                      ("knicks", "NYK"), ("4", "BOS"), ("Clippers", "LAC")])
def test_resolve_team(ref, abbr):
    assert run(FakeHardwood().client().resolve_team(ref))["abbreviation"] == abbr


@pytest.mark.parametrize("ref", ["LA", "Los Angeles"])
def test_resolve_team_la_is_ambiguous(ref):
    with pytest.raises(ToolError, match="ambiguous"):
        run(FakeHardwood().client().resolve_team(ref))


def test_resolve_team_unknown():
    with pytest.raises(ToolError, match="No team found"):
        run(FakeHardwood().client().resolve_team("Sonics"))


# ---------------------------------------------------------------- tools


def test_player_stats_builds_query_and_shapes_result():
    fake = FakeHardwood({("POST", "/query"): JOKIC_SEASON_RESPONSE})
    args = {"player": JOKIC_ID, "scope": "last10", "splits": {"venue": "away"}, "lines": {"pts": 26.5}}
    text, ok = run(run_tool(fake.client(), "player_stats", args, CTX))
    assert ok
    body = fake.bodies("/query")[0]
    assert body == {"entity": "player", "id": JOKIC_ID, "scope": "last10", "season_type": "regular",
                    "season": "2026-27", "splits": {"venue": "away"}, "lines": {"pts": 26.5}}
    out = json.loads(text)
    assert out["meta"]["sample_size"] == 10 and out["meta"]["notes"] == ["Small sample."]
    assert out["meta"]["data_synced_at"] == "2026-11-11T12:00:00Z"
    assert out["props"]["pts"]["over"] == 6


def test_player_stats_career_omits_season():
    fake = FakeHardwood({("POST", "/query"): JOKIC_SEASON_RESPONSE})
    run(run_tool(fake.client(), "player_stats", {"player": JOKIC_ID, "scope": "career"}, CTX))
    assert "season" not in fake.bodies("/query")[0]


def test_game_log_is_trimmed_and_slimmed():
    rows = [{"date": f"2026-11-{d:02d}", "opponent": "BOS", "pts": 20 + d, "pf": 3, "oreb": 1} for d in range(1, 21)]
    fake = FakeHardwood({("POST", "/query"): {"query": {"scope": "game_log"}, "subject": {}, "data": rows,
                                              "meta": {"sample_size": 20}}})
    text, ok = run(run_tool(fake.client(), "player_stats",
                            {"player": JOKIC_ID, "scope": "game_log", "game_log_limit": 5}, CTX))
    out = json.loads(text)
    assert ok and out["games_total"] == 20 and len(out["data"]) == 5
    assert "pf" not in out["data"][0] and out["data"][0]["pts"] == 21


def test_game_log_limit_is_capped_at_30():
    rows = [{"date": "d", "pts": i} for i in range(50)]
    fake = FakeHardwood({("POST", "/query"): {"query": {"scope": "game_log"}, "data": rows, "meta": {}}})
    text, _ = run(run_tool(fake.client(), "player_stats", {"player": JOKIC_ID, "scope": "game_log", "game_log_limit": 999}, CTX))
    assert len(json.loads(text)["data"]) == 30


def test_team_stats_resolves_name():
    fake = FakeHardwood({("POST", "/query"): {"query": {"scope": "season"}, "data": {"gp": 9}, "meta": {"sample_size": 9}}})
    run(run_tool(fake.client(), "team_stats", {"team": "celtics", "scope": "last10", "splits": {"b2b": 2}}, CTX))
    body = fake.bodies("/query")[0]
    assert body["entity"] == "team" and body["id"] == 4 and body["scope"] == "last10" and body["splits"] == {"b2b": 2}


def test_leaderboard_body():
    fake = FakeHardwood({("POST", "/query"): {"query": {"scope": "leaderboard"}, "data": [{"rank": 1}], "meta": {}}})
    run(run_tool(fake.client(), "leaderboard", {"stat": "ast", "limit": 5}, CTX))
    body = fake.bodies("/query")[0]
    assert body["scope"] == "leaderboard" and body["stat"] == "ast" and body["limit"] == 5 and body["season"] == "2026-27"


def test_games_on_date_defaults_to_eastern_today_and_validates():
    fake = FakeHardwood({("GET", "/games"): {"data": [{"id": GAME_ID, "home": "NYK", "away": "BOS", "status": "scheduled", "x": 1}],
                                             "meta": {"prev_date": "2026-11-10", "next_date": "2026-11-12"}}})
    text, ok = run(run_tool(fake.client(), "games_on_date", {}, CTX))
    assert ok and fake.requests[-1].url.params["date"] == "2026-11-11"
    assert "x" not in json.loads(text)["games"][0]
    text, ok = run(run_tool(fake.client(), "games_on_date", {"date": "tomorrow"}, CTX))
    assert not ok and "date must look like" in text


def test_box_score_rejects_non_uuid_and_shapes():
    fake = FakeHardwood({("GET", f"/games/{GAME_ID}"): {"data": {
        "game": {"game_date_local": "2026-11-10", "status": "final", "home_score": 100, "away_score": 90},
        "teams": [{"abbreviation": "BOS", "pts": 90, "won": False, "venue_split": "away",
                   "players": [{"full_name": "A", "pts": 20, "pf": 2}] * 20}]}}})
    hw = fake.client()
    _, ok = run(run_tool(hw, "box_score", {"game_id": "abc"}, CTX))
    assert not ok
    text, ok = run(run_tool(hw, "box_score", {"game_id": GAME_ID}, CTX))
    out = json.loads(text)
    assert ok and len(out["teams"][0]["players"]) == 15 and "pf" not in out["teams"][0]["players"][0]


def test_prop_board_filters_sorts_and_limits():
    rows = [{"name": "Low Guy", "team": "BOS", "opponent": "NYK", "line": 10.5},
            {"name": "Star One", "team": "NYK", "opponent": "BOS", "line": 28.5},
            {"name": "Other Team", "team": "LAL", "opponent": "LAC", "line": 30.5}]
    fake = FakeHardwood({("GET", "/props"): {"data": rows, "games": [], "meta": {"date": "2026-11-11", "market_label": "Points", "book": "DraftKings"}}})
    hw = fake.client()
    out = json.loads(run(run_tool(hw, "prop_board", {"team": "Knicks"}, CTX))[0])
    assert [p["name"] for p in out["props"]] == ["Star One", "Low Guy"] and out["total_props"] == 2
    out = json.loads(run(run_tool(hw, "prop_board", {"limit": 1}, CTX))[0])
    assert [p["name"] for p in out["props"]] == ["Other Team"] and out["total_props"] == 3
    out = json.loads(run(run_tool(hw, "prop_board", {"player": "star"}, CTX))[0])
    assert out["shown"] == 1


def test_matchup_ranks_shape():
    fake = FakeHardwood({("GET", "/rankings/matchups"): {"data": {"G": [{"team_id": 4, "abbr": "BOS", "games": 9,
                          "allowed": {"pts": 48.0}, "vs_avg": {"pts": 0.2}, "rank": {"pts": 14}, "label": {"pts": None}}], "F": [], "C": []},
                          "league_avg": {"G": {"pts": 47.8}}, "meta": {"notes": ["n"]}}})
    out = json.loads(run(run_tool(fake.client(), "matchup_ranks", {"team": "BOS"}, CTX))[0])
    assert out["defense"] == "Boston Celtics" and out["by_position"]["G"]["rank"]["pts"] == 14 and "F" not in out["by_position"]
    assert fake.requests[-1].url.params["team_id"] == "4"


def test_league_table_kinds():
    fake = FakeHardwood({
        ("GET", "/standings"): {"data": {"East": [{"rank": 1, "abbreviation": "BOS", "wins": 8, "losses": 1, "junk": 1}]}, "meta": {}},
        ("GET", "/rankings/teams"): {"data": [{"abbr": "BOS", "net_rtg": 9.1, "junk": 1}], "meta": {"notes": ["est."]}}})
    hw = fake.client()
    assert json.loads(run(run_tool(hw, "league_table", {"kind": "standings"}, CTX))[0])["standings"]["East"][0]["wins"] == 8
    assert json.loads(run(run_tool(hw, "league_table", {"kind": "team_ratings", "scope": "last10"}, CTX))[0])["teams"][0]["net_rtg"] == 9.1
    assert fake.requests[-1].url.params["scope"] == "last10"
    _, ok = run(run_tool(hw, "league_table", {"kind": "nope"}, CTX))
    assert not ok


def test_player_props_passthrough():
    fake = FakeHardwood({("GET", f"/players/{JOKIC_ID}/props"): {"data": {"upcoming": None, "record": {"pts": {"over": 1}}}, "meta": {"player": "Nikola Jokić", "book": "DraftKings"}}})
    out = json.loads(run(run_tool(fake.client(), "player_props", {"player": JOKIC_ID}, CTX))[0])
    assert out["player"] == "Nikola Jokić" and out["record"]["pts"]["over"] == 1


# ---------------------------------------------------------------- errors and plumbing


def test_hardwood_errors_become_tool_errors():
    hw = FakeHardwood({("POST", "/query"): lambda r: httpx.Response(400, json={"error": "season must look like 2025-26"})}).client()
    text, ok = run(run_tool(hw, "player_stats", {"player": JOKIC_ID, "season": "26"}, CTX))
    assert not ok and "400" in text and "season must look like" in text

    hw = FakeHardwood({("POST", "/query"): lambda r: httpx.Response(401, json={"error": "invalid_api_key"})}).client()
    text, ok = run(run_tool(hw, "player_stats", {"player": JOKIC_ID}, CTX))
    assert not ok and "rejected the API key" in text


def test_timeout_is_reported_not_raised():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)
    text, ok = run(run_tool(FakeHardwood({("POST", "/query"): boom}).client(), "player_stats", {"player": JOKIC_ID}, CTX))
    assert not ok and "timed out" in text


def test_retry_once_on_503():
    calls = []

    def flaky(request):
        calls.append(1)
        return httpx.Response(503, json={"error": "starting"}) if len(calls) == 1 else httpx.Response(200, json=JOKIC_SEASON_RESPONSE)
    _, ok = run(run_tool(FakeHardwood({("POST", "/query"): flaky}).client(), "player_stats", {"player": JOKIC_ID}, CTX))
    assert ok and len(calls) == 2


def test_run_tool_guards():
    hw = FakeHardwood().client()
    assert run(run_tool(hw, "nope", {}, CTX))[1] is False
    assert run(run_tool(hw, "player_stats", "not a dict", CTX))[1] is False
    text, ok = run(run_tool(hw, "player_stats", {}, CTX))
    assert not ok and "player" in text


def test_oversized_result_is_truncated():
    big = {"query": {"scope": "season"}, "data": {"blob": "x" * 50000}, "meta": {}}
    text, ok = run(run_tool(FakeHardwood({("POST", "/query"): big}).client(), "player_stats", {"player": JOKIC_ID}, CTX))
    assert ok and "[truncated" in text and len(text) < 15000


def test_schemas_match_dispatch_table():
    assert {t["name"] for t in TOOL_SCHEMAS} == set(TOOLS)
    for t in TOOL_SCHEMAS:
        assert t["description"] and t["input_schema"]["type"] == "object"
        for req in t["input_schema"].get("required", []):
            assert req in t["input_schema"]["properties"]


def test_current_season_rollover():
    assert current_season(datetime(2026, 11, 11, tzinfo=ET)) == "2026-27"
    assert current_season(datetime(2027, 3, 1, tzinfo=ET)) == "2026-27"
    assert current_season(datetime(2026, 9, 15, tzinfo=ET)) == "2026-27"
    assert current_season(datetime(2026, 7, 1, tzinfo=ET)) == "2025-26"


def test_describe_call_truncates():
    assert describe_call("web_search", {"query": "x"}) == "web_search(query=x)"
    assert len(describe_call("t", {"a": "y" * 500})) == 160


# ---------------------------------------------------------------- head-to-head


def _g(date, opp, pts, won=True, minutes=36, season="2025-26", season_type="regular", **kw):
    return {"date": date, "season": season, "season_type": season_type, "opponent": opp, "venue": "away", "won": won,
            "minutes": minutes, "pts": pts, "reb": 8, "ast": 4, "stl": 1, "blk": 1, "tov": 3, "fg3m": 1,
            "fgm": 9, "fga": 17, "plus_minus": 5, "junk": 1, **kw}


def _h2h_route(per_season, entity_name="Joel Embiid"):
    def route(request):
        body = json.loads(request.content)
        rows = per_season.get(body["season"])
        if rows is None:
            return httpx.Response(400, json={"error": "no games for that season"})
        return httpx.Response(200, json={
            "query": {"scope": "game_log"}, "subject": {"type": "player", "name": entity_name}, "data": rows,
            "meta": {"sample_size": len(rows), "freshness": {"synced_at": "2026-11-11T12:00:00Z"}}})
    return route


def test_head_to_head_finds_games_beyond_the_old_30_row_cap():
    # 45 games in 2025-26; the two TOR games are the OLDEST (positions 40 and 44 of a newest-first log).
    season_25_26 = [_g(f"2026-03-{(i % 28) + 1:02d}", "BOS", 20, season="2025-26") for i in range(45)]
    season_25_26[40] = _g("2025-11-20", "TOR", 31, won=False)
    season_25_26[44] = _g("2025-10-25", "TOR", 27, won=True)
    per_season = {
        "2026-27": [_g("2026-11-01", "BOS", 18, season="2026-27") for _ in range(10)],
        "2025-26": season_25_26,
        "2024-25": [_g("2025-02-11", "TOR", 27, won=False, season="2024-25"), _g("2025-02-01", "NYK", 30, season="2024-25")],
        "2023-24": [],
        "2022-23": [],
    }
    fake = FakeHardwood({("POST", "/query"): _h2h_route(per_season)})
    text, ok = run(run_tool(fake.client(), "player_stats", {"player": JOKIC_ID, "opponent": "Raptors", "game_log_limit": 5}, CTX))
    out = json.loads(text)
    assert ok
    assert out["games_found"] == 3 and out["shown"] == 3
    # newest first, across seasons, including the two games that sat beyond the old 30-row cap
    assert [g["date"] for g in out["games"]] == ["2025-11-20", "2025-10-25", "2025-02-11"]
    assert out["seasons_checked"] == ["2026-27", "2025-26", "2024-25", "2023-24", "2022-23"]
    assert out["skipped_seasons"] == [] and out["data_synced_at"] == "2026-11-11T12:00:00Z"
    s = out["computed_summary"]
    assert s["games"] == 3 and s["wins"] == 1 and s["losses"] == 2
    assert s["avg_pts"] == 28.3 and s["avg_minutes"] == 36.0
    assert s["fg"] == "27/51" and s["fg_pct"] == 0.529
    assert "junk" not in out["games"][0] and out["games"][0]["season"] in ("2025-26", "2024-25")
    assert "not by a Hardwood split" in out["note"]
    # every season was requested as a game_log with regular season + playoffs
    bodies = fake.bodies("/query")
    assert {b["season"] for b in bodies} == set(per_season) and all(b["scope"] == "game_log" and b["season_type"] == "all" for b in bodies)


def test_head_to_head_limit_applies_after_filtering_but_summary_covers_all():
    rows = [_g(f"2026-01-{d:02d}", "TOR", 20 + d) for d in range(1, 13)]
    fake = FakeHardwood({("POST", "/query"): _h2h_route({"2025-26": rows})})
    out = json.loads(run(run_tool(fake.client(), "player_stats",
                                  {"player": JOKIC_ID, "opponent": "TOR", "seasons": ["2025-26"], "game_log_limit": 3}, CTX))[0])
    assert out["games_found"] == 12 and out["shown"] == 3 and len(out["games"]) == 3
    assert out["computed_summary"]["games"] == 12
    assert [g["date"] for g in out["games"]] == ["2026-01-12", "2026-01-11", "2026-01-10"]


def test_head_to_head_skips_failed_seasons_and_errors_if_all_fail():
    fake = FakeHardwood({("POST", "/query"): _h2h_route({"2026-27": [_g("2026-11-01", "TOR", 25, season="2026-27")]})})
    out = json.loads(run(run_tool(fake.client(), "player_stats", {"player": JOKIC_ID, "opponent": "TOR"}, CTX))[0])
    assert out["games_found"] == 1 and out["seasons_checked"] == ["2026-27"]
    assert [x["season"] for x in out["skipped_seasons"]] == ["2025-26", "2024-25", "2023-24", "2022-23"]
    assert "no games for that season" in out["skipped_seasons"][0]["reason"]

    fake = FakeHardwood({("POST", "/query"): _h2h_route({})})
    text, ok = run(run_tool(fake.client(), "player_stats", {"player": JOKIC_ID, "opponent": "TOR"}, CTX))
    assert not ok and "Could not load any season" in text


def test_head_to_head_season_window_options():
    fake = FakeHardwood({("POST", "/query"): _h2h_route({s: [] for s in ["2026-27", "2025-26", "2024-25", "2023-24", "2022-23",
                                                                          "2021-22", "2020-21", "2019-20", "2018-19", "2017-18", "2016-17"]})})
    hw = fake.client()

    def seasons_for(args):
        fake.requests.clear()
        run(run_tool(hw, "player_stats", {"player": JOKIC_ID, "opponent": "TOR", **args}, CTX))
        return sorted(b["season"] for b in fake.bodies("/query"))

    assert len(seasons_for({})) == 5
    assert len(seasons_for({"seasons_back": 3})) == 3
    assert len(seasons_for({"seasons_back": 99})) == 10  # capped
    assert seasons_for({"season": "2024-25"}) == ["2024-25"]
    assert seasons_for({"seasons": ["2023-24", "2022-23", "2023-24"]}) == ["2022-23", "2023-24"]  # de-duplicated
    text, ok = run(run_tool(hw, "player_stats", {"player": JOKIC_ID, "opponent": "TOR", "seasons": ["24"]}, CTX))
    assert not ok and "season must look like" in text

    # a list over the cap is refused, never cut: the seasons listed last (often the current one) would vanish silently
    eleven = [f"{y}-{(y + 1) % 100:02d}" for y in range(2016, 2027)]
    fake.requests.clear()
    text, ok = run(run_tool(hw, "player_stats", {"player": JOKIC_ID, "opponent": "TOR", "seasons": eleven}, CTX))
    assert not ok and "at most 10 seasons" in json.loads(text)["error"] and not fake.bodies("/query")
    # exactly ten, current season included, is searched in full
    assert seasons_for({"seasons": eleven[1:]}) == sorted(eleven[1:])
    assert seasons_for({"seasons": eleven[:-1] + eleven[:1]}) == sorted(eleven[:-1])  # duplicates count once


def test_head_to_head_rejects_other_scopes_and_ambiguous_opponents():
    hw = FakeHardwood({("POST", "/query"): _h2h_route({})}).client()
    text, ok = run(run_tool(hw, "player_stats", {"player": JOKIC_ID, "opponent": "TOR", "scope": "last10"}, CTX))
    assert not ok and "game_log" in text
    text, ok = run(run_tool(hw, "player_stats", {"player": JOKIC_ID, "opponent": "LA"}, CTX))
    assert not ok and "ambiguous" in text
    text, ok = run(run_tool(hw, "player_stats", {"player": JOKIC_ID, "opponent": "Sonics"}, CTX))
    assert not ok and "No team found" in text


def test_team_head_to_head():
    def route(request):
        body = json.loads(request.content)
        assert body["entity"] == "team" and body["id"] == 4 and body["scope"] == "game_log"
        rows = [{"date": "2026-02-01", "season": body["season"], "opponent": "NYK", "won": True, "pts": 118, "opp_pts": 109},
                {"date": "2026-01-01", "season": body["season"], "opponent": "NYK", "won": False, "pts": 100, "opp_pts": 104},
                {"date": "2026-01-15", "season": body["season"], "opponent": "MIA", "won": True, "pts": 120, "opp_pts": 90}]
        return httpx.Response(200, json={"query": {"scope": "game_log"}, "data": rows, "meta": {}})
    out = json.loads(run(run_tool(FakeHardwood({("POST", "/query"): route}).client(), "team_stats",
                                  {"team": "celtics", "opponent": "knicks", "seasons": ["2025-26"]}, CTX))[0])
    s = out["computed_summary"]
    assert out["head_to_head"] == {"subject": "Boston Celtics", "opponent": "New York Knicks"}
    assert s["games"] == 2 and s["wins"] == 1 and s["losses"] == 1 and s["avg_pts"] == 109.0 and s["avg_opp_pts"] == 106.5


def test_schemas_expose_head_to_head_params():
    for name in ("player_stats", "team_stats"):
        schema = next(t for t in TOOL_SCHEMAS if t["name"] == name)
        assert {"opponent", "seasons_back", "seasons"} <= set(schema["input_schema"]["properties"])
        assert "head-to-head" in schema["description"]


def test_prop_board_shrinks_to_fit_and_stays_valid_json():
    stat = {"over": 6, "under": 4, "push": 0, "games": 10, "avg": 27.9}
    rows = [{"name": f"Player {i:03d}", "team": "BOS", "opponent": "NYK", "venue": "away", "line": 40 - i * 0.1,
             "over_price": -115, "under_price": -105, "open_line": 25.5, "snapshot": "close",
             "last10": stat, "season": stat, "last_season": stat,
             "matchup": {"position": "F", "position_label": "Forwards", "rank": 27, "of": 30, "allowed": 51.2,
                         "vs_avg": 3.1, "label": "weak", "games": 12, "opponent": "NYK"}} for i in range(152)]
    fake = FakeHardwood({("GET", "/props"): {"data": rows, "games": [], "meta": {"date": "2026-11-11", "market_label": "Points"}}})
    text, ok = run(run_tool(fake.client(), "prop_board", {"limit": 60}, CTX))
    out = json.loads(text)  # still valid JSON: not sliced mid-row
    assert ok and "[truncated" not in text and len(text) <= 14000
    assert 1 < out["shown"] < 60 and out["shown"] == len(out["props"]) and out["total_props"] == 152
    assert out["props"][0]["name"] == "Player 000" and "Showing" in out["note"]
    # a request that fits is returned whole, with no note
    out = json.loads(run(run_tool(fake.client(), "prop_board", {"limit": 10}, CTX))[0])
    assert out["shown"] == 10 and out["props"][-1]["name"] == "Player 009"
    # an empty board is a normal answer
    empty = FakeHardwood({("GET", "/props"): {"data": [], "games": [], "meta": {"date": "2026-11-12"}}})
    out = json.loads(run(run_tool(empty.client(), "prop_board", {"date": "2026-11-12"}, CTX))[0])
    assert out["total_props"] == 0 and out["shown"] == 0 and out["props"] == []


def test_matchup_explanation_says_stats_are_the_opposing_groups_own_totals():
    fake = FakeHardwood({("GET", "/rankings/matchups"): {"data": {"G": [{"team_id": 4, "abbr": "BOS", "games": 9,
                          "allowed": {"blk": 2.3}, "vs_avg": {"blk": 0.8}, "rank": {"blk": 29}, "label": {"blk": "weak"}}]},
                          "league_avg": {}, "meta": {}}})
    text = json.loads(run(run_tool(fake.client(), "matchup_ranks", {"team": "BOS"}, CTX))[0])["how_to_read"]
    assert "OPPOSING position group's own" in text and "blocks THEY record" in text


def test_head_to_head_says_hidden_games_are_older():
    rows = [_g(f"2026-01-{d:02d}", "TOR", 20 + d) for d in range(1, 13)]
    fake = FakeHardwood({("POST", "/query"): _h2h_route({"2025-26": rows})})
    out = json.loads(run(run_tool(fake.client(), "player_stats",
                                  {"player": JOKIC_ID, "opponent": "TOR", "seasons": ["2025-26"], "game_log_limit": 3}, CTX))[0])
    assert out["games_note"] == "games lists the 3 most recent of 12 (newest first), so the other 9 are all older than 2026-01-10."
    out = json.loads(run(run_tool(fake.client(), "player_stats",
                                  {"player": JOKIC_ID, "opponent": "TOR", "seasons": ["2025-26"], "game_log_limit": 30}, CTX))[0])
    assert "games_note" not in out


# ---------------------------------------------------------------- fuzzing the tool boundary

BAD_VALUES = [None, "", "   ", 0, -5, 10**12, 3.5, True, "abc", "x" * 5000, [], ["a"], {}, {"a": 1}, "2026-13-45",
              "../../etc/passwd", "'; DROP TABLE players; --", "\u0000", "💥", float("inf")]


class _Anything(FakeHardwood):
    """Answers every path with an empty-but-valid payload (except /teams, which keeps the team list)."""

    def handler(self, request):
        self.requests.append(request)
        if request.url.path == "/teams":
            return httpx.Response(200, json={"data": TEAMS})
        return httpx.Response(200, json={"data": [], "meta": {}, "query": {}, "subject": {}})


def test_every_tool_survives_garbage_arguments():
    from app.hardwood import TOOL_SCHEMAS, run_tool
    fake = _Anything()
    problems = []
    for schema in TOOL_SCHEMAS:
        name, props = schema["name"], list(schema["input_schema"].get("properties", {}))
        variants = [{}, None, "not a dict", [], 7]
        variants += [{p: v} for p in props for v in BAD_VALUES]
        variants += [{p: BAD_VALUES[i % len(BAD_VALUES)] for i, p in enumerate(props)}]
        for args in variants:
            try:
                text, ok = run(run_tool(fake.client(), name, args, CTX))
                assert isinstance(text, str) and isinstance(ok, bool)
                err = json.loads(text).get("error", "") if not ok else ""
                # bad arguments must be rejected with a clear message, never fall through to a crash handler
                assert not any(k in err for k in ("unexpectedly", "could not read", "Bad input")), err
            except Exception as e:  # noqa: BLE001 - the point is that nothing escapes
                problems.append((name, repr(args)[:80], repr(e)[:160]))
    assert not problems, problems[:5]


def test_unknown_tool_name_is_a_clean_error():
    from app.hardwood import run_tool
    text, ok = run(run_tool(FakeHardwood().client(), "drop_database", {"x": 1}, CTX))
    assert ok is False and "unknown tool" in text.lower()


def test_missing_required_and_bad_enum_inputs_get_clear_errors_without_calling_the_api():
    fake = FakeHardwood()
    assert json.loads(run(run_tool(fake.client(), "player_stats", {}, CTX))[0])["error"] == "Missing required input 'player'."
    assert json.loads(run(run_tool(fake.client(), "player_stats", {"player": ""}, CTX))[0])["error"] == "Missing required input 'player'."
    text, ok = run(run_tool(fake.client(), "leaderboard", {"stat": "points"}, CTX))
    err = json.loads(text)["error"]
    assert ok is False and "Invalid value 'points' for 'stat'" in err and "pts, reb, ast" in err
    _, ok = run(run_tool(fake.client(), "player_stats", {"player": JOKIC_ID, "scope": "last7"}, CTX))
    assert ok is False
    assert fake.requests == []  # nothing was sent to Hardwood


def test_a_field_missing_from_hardwoods_reply_is_not_blamed_on_the_model():
    fake = FakeHardwood({("GET", "/standings"): {"data": [{"team": "x"}]}})
    text, ok = run(run_tool(fake.client(), "league_table", {"kind": "standings"}, CTX))
    # whatever the tool does with a malformed row, it must not claim the model's input was missing
    assert "Missing required input" not in text


MALFORMED_REPLIES = [
    {}, [], None, "text", 0, {"data": None}, {"data": "x"}, {"data": [1, 2, 3]}, {"data": {"rows": None}},
    {"data": [{}]}, {"data": [None]}, {"data": {}, "meta": None}, {"error": "boom"}, {"data": [[]]},
]


class _Replies(FakeHardwood):
    def __init__(self, payload, raw=None):
        super().__init__()
        self.payload, self.raw = payload, raw

    def handler(self, request):
        self.requests.append(request)
        if request.url.path == "/teams":
            return httpx.Response(200, json={"data": TEAMS})
        if self.raw is not None:
            return httpx.Response(200, content=self.raw)
        return httpx.Response(200, json=self.payload)


VALID_ARGS = {
    "find_player": {"query": "Jokic"}, "player_stats": {"player": JOKIC_ID}, "team_stats": {"team": "BOS"},
    "leaderboard": {"stat": "pts"}, "games_on_date": {"date": "2026-11-12"}, "box_score": {"game_id": GAME_ID},
    "prop_board": {}, "player_props": {"player": JOKIC_ID}, "matchup_ranks": {"team": "BOS"},
    "league_table": {"kind": "standings"},
}


def test_every_tool_survives_malformed_hardwood_replies():
    problems = []
    fakes = [_Replies(p) for p in MALFORMED_REPLIES] + [_Replies(None, raw=b""), _Replies(None, raw=b"<html>502</html>"),
                                                       _Replies(None, raw=b"\xff\xfe")]
    for fake in fakes:
        for name, args in VALID_ARGS.items():
            try:
                text, ok = run(run_tool(fake.client(), name, args, CTX))
                assert isinstance(text, str) and isinstance(ok, bool)
                err = json.loads(text).get("error", "") if not ok else ""
                # our request was valid, so the model must never be told its input was wrong
                assert not any(k in err for k in ("unexpectedly", "Bad input", "Missing required", "must be of type")), err
            except Exception as e:  # noqa: BLE001
                problems.append((name, repr(fake.payload)[:40], repr(e)[:160]))
    assert not problems, problems[:5]
    assert set(VALID_ARGS) == {t["name"] for t in TOOL_SCHEMAS}  # new tools must be added to this fuzz


def test_numeric_strings_are_accepted_for_integer_inputs_but_words_are_not():
    fake = _Anything()
    _, ok = run(run_tool(fake.client(), "leaderboard", {"stat": "pts", "limit": "5"}, CTX))
    assert ok is True
    text, ok = run(run_tool(fake.client(), "leaderboard", {"stat": "pts", "limit": "five"}, CTX))
    assert ok is False and "'limit' must be of type integer" in json.loads(text)["error"]
    text, ok = run(run_tool(fake.client(), "player_stats", {"player": JOKIC_ID, "seasons": ["2025-26", 7]}, CTX))
    assert ok is False and "Every item of 'seasons'" in json.loads(text)["error"]


def test_data_the_tool_cannot_read_is_reported_as_a_data_problem():
    fake = _Replies({"data": "x"})
    text, ok = run(run_tool(fake.client(), "player_props", {"player": JOKIC_ID}, CTX))
    assert ok is False and "Hardwood returned data this tool could not read" in json.loads(text)["error"]


def test_tip_off_times_are_converted_to_eastern_in_code():
    from app.hardwood import _tip_et
    assert _tip_et("2026-11-14T00:00:00Z") == "Fri Nov 13, 7:00 PM ET"      # standard time
    assert _tip_et("2026-07-01T23:30:00+00:00") == "Wed Jul 1, 7:30 PM ET"  # daylight time
    assert _tip_et("2026-11-13T17:05:00Z") == "Fri Nov 13, 12:05 PM ET"
    assert _tip_et("2026-11-13T05:00:00Z") == "Fri Nov 13, 12:00 AM ET"
    assert _tip_et(None) is None and _tip_et("garbage") is None and _tip_et(12) is None


def test_games_and_prop_board_report_tip_off_in_eastern_not_utc():
    games = {"data": [{"id": GAME_ID, "date": "2026-11-13", "status": "scheduled", "home": "IND", "away": "NYK", "tipoff_utc": "2026-11-14T00:00:00Z"}], "meta": {}}
    out = json.loads(run(run_tool(FakeHardwood({("GET", "/games"): games}).client(), "games_on_date", {"date": "2026-11-13"}, CTX))[0])
    assert out["games"][0]["tipoff_et"] == "Fri Nov 13, 7:00 PM ET" and "tipoff_utc" not in out["games"][0]
    board = {"data": [], "games": [{"home": "IND", "away": "NYK", "tipoff_utc": "2026-11-14T00:00:00Z", "lines": 0}], "meta": {"date": "2026-11-13"}}
    out = json.loads(run(run_tool(FakeHardwood({("GET", "/props"): board}).client(), "prop_board", {}, CTX))[0])
    assert out["games"][0]["tipoff_et"] == "Fri Nov 13, 7:00 PM ET"
    bad = {"data": [{"id": GAME_ID, "tipoff_utc": "not a time"}], "meta": {}}
    out = json.loads(run(run_tool(FakeHardwood({("GET", "/games"): bad}).client(), "games_on_date", {}, CTX))[0])
    assert out["games"][0]["tipoff_utc"] == "not a time"  # unconvertible values are passed through, not hidden


def test_prop_board_always_sends_an_explicit_eastern_date():
    fake = FakeHardwood({("GET", "/props"): {"data": [], "games": [], "meta": {"date": "2026-11-11"}}})
    run(run_tool(fake.client(), "prop_board", {}, CTX))
    run(run_tool(fake.client(), "prop_board", {"date": "2026-11-09"}, CTX))
    sent = [dict(r.url.params) for r in fake.requests if r.url.path == "/props"]
    assert sent[0]["date"] == "2026-11-11" and sent[1]["date"] == "2026-11-09"  # CTX.now is Nov 11, 9 PM ET
