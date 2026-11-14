import asyncio
import re

from app.agent import ResearchAgent
from app.config import ET
from datetime import datetime
from evals.cases import CASES
from evals.run import evaluate, format_report, run_case
from tests.helpers import (FakeAnthropic, FakeHardwood, JOKIC_ID, JOKIC_SEASON_RESPONSE, message_sse, settings, text_block,
                           tool_use_block)

CASE = {c.id: c for c in CASES}
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=ET)
DECLINE = "That is out of scope: I only cover the NBA, so I can't pick a football game."


def events(text, *, tools=(), searches=0, cost=0.01, extra=()):
    ev = [{"type": "text", "delta": text}]
    for name, label in tools:
        ev.append({"type": "tool_result", "name": name, "label": label, "ok": True, "ms": 5, "detail": "ok"})
    ev += list(extra)
    ev.append({"type": "done", "ok": True, "elapsed_ms": 2000, "searches": searches, "est_cost_usd": cost})
    return ev


def failed(result):
    return {c.name for c in result.checks if not c.ok and c.severity == "fail"}


def test_report_shows_output_tokens_thinking_blocks_notices_and_the_per_call_trace():
    ev = events("partial answer " * 5, extra=[{"type": "notice", "message": "The answer hit the length limit (MAX_TOKENS=8192, 8200 output tokens used this question) and was cut off."}])
    ev[-1].update({"output_tokens": 8200, "thinking_blocks": 3, "iterations": [
        {"stop": "tool_use", "out": 900, "text_chars": 0, "thinking_blocks": 1, "blocks": ["thinking", "tool_use"]},
        {"stop": "max_tokens", "out": 7300, "text_chars": 4000, "thinking_blocks": 2, "blocks": ["thinking", "text"]}]})
    ev.insert(-1, {"type": "error", "message": "x"})
    r = evaluate(CASE["matchup_ranks"], ev, settings())
    assert (r.tokens_out, r.thinking_blocks) == (8200, 3)
    report = format_report([r])
    assert "8200" in report and "MAX_TOKENS=8192" in report
    assert "call 2  stop=max_tokens out=7300 text=4000c thinking_blocks=2 blocks=thinking,text" in report


def test_every_case_is_well_formed():
    assert len({c.id for c in CASES}) == len(CASES) == 6
    for c in CASES:
        assert c.question.strip() and c.max_cost_usd > 0
        for _label, pattern, severity in c.must_match + c.must_not_match:
            re.compile(pattern)
            assert severity in ("fail", "warn")


def test_a_good_out_of_scope_answer_passes():
    r = evaluate(CASE["out_of_scope"], events(DECLINE, cost=0.002), settings())
    assert r.passed and failed(r) == set()


def test_out_of_scope_fails_if_any_tool_runs():
    r = evaluate(CASE["out_of_scope"], events(DECLINE, tools=[("player_stats", "player_stats(player=x)")]), settings())
    assert "used no tools at all" in failed(r)


def test_missing_expected_tool_and_label_counts_fail():
    ans = "Top scorers: " + "x" * 60
    r = evaluate(CASE["scorers_trend"], events(ans, tools=[("leaderboard", "leaderboard(stat=pts)")] + [("player_stats", "player_stats(player=A, scope=last5)")] * 3), settings())
    assert "at least 8 calls with 'scope=last5'" in failed(r)
    assert "called leaderboard" not in failed(r)
    r = evaluate(CASE["scorers_trend"], events(ans), settings())
    assert {"called leaderboard", "called player_stats"} <= failed(r)


def test_pick_language_unverified_numbers_and_cost_all_fail_a_props_answer():
    check = {"type": "check", "unverified": ["31.7"], "notes": ["x"]}
    r = evaluate(CASE["props_board"], events("The best bet is Maxey over. " + "y" * 60, tools=[("prop_board", "prop_board()")], cost=0.9, extra=[check]), settings())
    assert {"no: tells the user what to bet", "every decimal traces to tool data (audit)", "cost <= $0.60"} <= failed(r)
    assert not r.passed


def test_errors_and_cut_off_answers_fail():
    ev = events("partial answer " * 5, extra=[{"type": "notice", "message": "The answer hit the length limit and was cut off."}, {"type": "error", "message": "boom"}])
    ev[-1]["ok"] = False
    r = evaluate(CASE["matchup_ranks"], ev, settings())
    assert {"finished without error", "not cut off by the length limit"} <= failed(r)


def test_injury_answer_needs_a_date_and_a_search():
    plain = "Embiid is questionable according to ESPN. " * 3
    r = evaluate(CASE["injuries_web"], events(plain, searches=1), settings())
    assert "gives a date for the reports" in failed(r)
    r = evaluate(CASE["injuries_web"], events(plain + "(ESPN, Nov 12)", searches=1), settings())
    assert r.passed
    r = evaluate(CASE["injuries_web"], events(plain + "(ESPN, Nov 12)", searches=0), settings())
    assert "ran at least 1 web search(es)" in failed(r)


def test_search_limit_is_checked_against_settings():
    r = evaluate(CASE["injuries_web"], events("ESPN, Nov 12. " * 5, searches=5), settings(web_search_max_uses=4))
    assert "web searches within the limit (4)" in failed(r)


def test_warnings_do_not_fail_a_case_but_show_in_the_report():
    ev = events("Brown has played Toronto in 12 games over several seasons, computed by the app. " * 2, tools=[("player_stats", "player_stats(player=Brown, opponent=TOR)")],
                extra=[{"type": "check", "unverified": [], "notes": ["Web search ran, but..."]}])
    r = evaluate(CASE["head_to_head"], ev, settings())
    assert r.passed and [c.name for c in r.warnings] == ["no audit notes"]
    report = format_report([r])
    assert "PASS" in report and "no audit notes" in report and "1/1 passed" in report


def _agent(fake_anthropic, fake_hardwood):
    return ResearchAgent(settings(), fake_anthropic.client(), fake_hardwood.client(), now_fn=lambda: NOW)


def test_run_case_end_to_end_with_fake_apis():
    agent = _agent(FakeAnthropic([message_sse([text_block(DECLINE)])]), FakeHardwood())
    r = asyncio.run(run_case(agent, CASE["out_of_scope"], settings()))
    assert r.passed, [(c.name, c.detail) for c in r.checks if not c.ok]

    fake = FakeAnthropic([message_sse([tool_use_block("t1", "player_stats", {"player": JOKIC_ID})], "tool_use"),
                          message_sse([text_block(DECLINE)])])
    r = asyncio.run(run_case(_agent(fake, FakeHardwood({("POST", "/query"): JOKIC_SEASON_RESPONSE})), CASE["out_of_scope"], settings()))
    assert not r.passed and "used no tools at all" in failed(r)


def test_a_crashing_agent_is_a_failed_case_not_a_crashed_eval():
    class Boom:
        async def run(self, history):
            raise RuntimeError("kaput")
            yield  # pragma: no cover

    r = asyncio.run(run_case(Boom(), CASE["matchup_ranks"], settings()))
    assert not r.passed and any("kaput" in c.detail for c in r.checks)


def test_a_short_props_answer_warns_that_the_board_may_have_been_empty():
    short = "No points props are posted yet. " * 3
    r = evaluate(CASE["props_board"], events(short, tools=[("prop_board", "prop_board()")]), settings())
    assert r.passed and any("may not have exercised" in c.name for c in r.warnings)
    long = "| Maxey | 25.5 | 7-3 |\n" * 200
    r = evaluate(CASE["props_board"], events(long + " availability unclear", tools=[("prop_board", "prop_board()")]), settings())
    assert not any("may not have exercised" in c.name for c in r.warnings)


def test_a_long_board_answer_from_one_prop_call_fails_but_a_per_game_walk_passes():
    long = "| Maxey | 25.5 | 7-3 |\n" * 200 + " availability unclear"
    one = evaluate(CASE["props_board"], events(long, tools=[("prop_board", "prop_board(market=pts, limit=60)")]), settings())
    assert not one.passed and "at least 4 calls with 'team='" in failed(one)
    many = [("prop_board", "prop_board(market=pts, limit=60)")] + [("prop_board", f"prop_board(team=T{i}, limit=60)") for i in range(5)]
    assert evaluate(CASE["props_board"], events(long, tools=many), settings()).passed


def test_a_prop_row_that_disagrees_with_the_board_fails_the_case():
    ev = events("x" * 200, tools=[("prop_board", "prop_board()")])
    ev.insert(-1, {"type": "check", "unverified": [], "notes": ["Row check: Kobe Sanders shows matchup rank 14 but Hardwood has 9."]})
    r = evaluate(CASE["scorers_trend"], ev, settings())
    assert "prop table rows match the prop board (audit)" in failed(r)


def test_career_wording_warnings_ignore_sentences_that_deny_it():
    def warns(text):
        r = evaluate(CASE["head_to_head"], events(text + " " + "x" * 80, tools=[("player_stats", "player_stats(opponent=TOR)")]), settings())
        return [c.name for c in r.warnings]
    assert not [w for w in warns("This is not a full career total, since earlier seasons were not searched.") if "career" in w]
    assert not [w for w in warns("It isn't his whole career. 38 games found in the last 10 seasons.") if "career" in w]
    assert any("career" in w for w in warns("Brown's career average against Toronto is 19.0 points."))
    assert any("career" in w for w in warns("That covers his full career."))


def test_a_dagger_name_list_outside_the_tables_warns_but_the_one_line_note_does_not():
    base = "| Player | Line |\n|---|---|\n" + "| Maxey (PHI) † | 25.5 |\n" * 60 + " availability unclear. "
    tools = [("prop_board", f"prop_board(team=T{i})") for i in range(5)]
    listed = evaluate(CASE["props_board"], events(base + "\n**† Season games under 10:** Jalen Suggs (3), Keegan Murray (4)\n", tools=tools), settings())
    assert any("lists the † players" in c.name for c in listed.warnings)
    note = evaluate(CASE["props_board"], events(base + "\nRows marked † have fewer than 10 season games, so their last-10 window spans two seasons.\n", tools=tools), settings())
    assert not any("lists the † players" in c.name for c in note.warnings)
