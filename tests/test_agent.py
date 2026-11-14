import asyncio
import json
from datetime import datetime

import httpx

from app.agent import MAX_ITERATIONS, ResearchAgent
from app.config import ET
from tests.helpers import (JOKIC_ID, api_error, JOKIC_SEASON_RESPONSE, FakeAnthropic, FakeHardwood, message_sse,
                           search_error_block, search_result_block, server_search_block, settings, text_block,
                           tool_use_block)

NOW = datetime(2026, 11, 11, 21, 0, tzinfo=ET)


def collect(anthropic_fake, hardwood_fake=None, history=None, **settings_kw):
    hardwood_fake = hardwood_fake or FakeHardwood({("POST", "/query"): JOKIC_SEASON_RESPONSE})
    agent = ResearchAgent(settings(**settings_kw), anthropic_fake.client(), hardwood_fake.client(), now_fn=lambda: NOW)

    async def go():
        return [ev async for ev in agent.run(history or [{"role": "user", "content": "How is Jokic doing?"}])]
    return asyncio.run(go())


def system_text(req):
    s = req["system"]
    return s if isinstance(s, str) else "".join(b["text"] for b in s)


def types(events):
    return [e["type"] for e in events]


def test_plain_answer_streams_text_and_reports_usage():
    fake = FakeAnthropic([message_sse([text_block("Jokic is great right now")], in_tokens=120, out_tokens=9)])
    events = collect(fake)
    assert "".join(e["delta"] for e in events if e["type"] == "text") == "Jokic is great right now"
    done = events[-1]
    assert done["type"] == "done" and done["ok"] and done["input_tokens"] == 120 and done["output_tokens"] == 9
    assert done["searches"] == 0
    # request shape: model, system prompt with today's date and season, hardwood tools + web search with a cap
    req = fake.requests[0]
    assert req["model"] == "claude-test" and req["stream"] is True
    assert "Wednesday, November 11, 2026" in system_text(req) and "2026-27" in system_text(req)
    names = [t["name"] for t in req["tools"]]
    assert "player_stats" in names and "web_search" in names
    ws = next(t for t in req["tools"] if t["name"] == "web_search")
    assert ws["type"] == "web_search_20250305" and ws["max_uses"] == 4


def test_client_tool_round_trip():
    fake = FakeAnthropic([
        message_sse([text_block("Checking."), tool_use_block("toolu_1", "player_stats", {"player": JOKIC_ID, "scope": "last10"})], "tool_use"),
        message_sse([text_block("He averaged 27.1 points over 10 games.")]),
    ])
    hw = FakeHardwood({("POST", "/query"): JOKIC_SEASON_RESPONSE})
    events = collect(fake, hw)

    # events: tool_start before the result, then final text
    assert "tool_start" in types(events)
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["name"] == "player_stats" and tr["ok"] and "scope=last10" in tr["label"] and isinstance(tr["ms"], int)
    assert "".join(e["delta"] for e in events if e["type"] == "text").endswith("over 10 games.")

    # second request carries the assistant tool_use and our tool_result with the same id
    second = fake.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert any(b["type"] == "tool_use" and b["id"] == "toolu_1" for b in second[1]["content"])
    result = second[2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1"
    assert json.loads(result["content"])["meta"]["sample_size"] == 10
    assert "is_error" not in result
    assert hw.bodies("/query")[0]["season"] == "2026-27"


def test_tool_error_is_returned_to_model_with_is_error():
    fake = FakeAnthropic([
        message_sse([tool_use_block("toolu_1", "player_stats", {"player": JOKIC_ID})], "tool_use"),
        message_sse([text_block("The lookup failed.")]),
    ])
    hw = FakeHardwood({("POST", "/query"): lambda r: httpx.Response(400, json={"error": "bad season"})})
    events = collect(fake, hw)
    assert next(e for e in events if e["type"] == "tool_result")["ok"] is False
    result = fake.requests[1]["messages"][2]["content"][0]
    assert result["is_error"] is True and "bad season" in result["content"]
    assert events[-1]["ok"] is True  # the turn itself still completes


def test_parallel_tool_calls_all_get_results():
    fake = FakeAnthropic([
        message_sse([tool_use_block("t1", "player_stats", {"player": JOKIC_ID}),
                     tool_use_block("t2", "team_stats", {"team": "BOS"})], "tool_use"),
        message_sse([text_block("Both done.")]),
    ])
    hw = FakeHardwood({("POST", "/query"): JOKIC_SEASON_RESPONSE})
    collect(fake, hw)
    ids = [b["tool_use_id"] for b in fake.requests[1]["messages"][2]["content"]]
    assert ids == ["t1", "t2"]


def test_web_search_events_and_sources():
    cite = {"type": "web_search_result_location", "cited_text": "ignored", "url": "https://espn.com/a", "title": "ESPN", "encrypted_index": "e"}
    fake = FakeAnthropic([message_sse([
        server_search_block("srvtoolu_1", "Jokic injury report"),
        search_result_block("srvtoolu_1", [{"url": "https://espn.com/a", "title": "ESPN", "age": "November 11, 2026"},
                                           {"url": "https://nba.com/b", "title": "NBA.com"}]),
        text_block("He is probable.", [cite]),
    ])])
    events = collect(fake)
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["name"] == "web_search" and tr["ok"] and tr["detail"] == "2 results" and "Jokic injury report" in tr["label"]
    sources = next(e for e in events if e["type"] == "sources")["sources"]
    by_url = {s["url"]: s for s in sources}
    assert by_url["https://espn.com/a"]["cited"] is True and by_url["https://espn.com/a"]["age"] == "November 11, 2026"
    assert by_url["https://nba.com/b"]["cited"] is False
    assert events[-1]["searches"] == 1


def test_failed_web_search_is_reported():
    fake = FakeAnthropic([message_sse([server_search_block("s1", "q"), search_error_block("s1", "max_uses_exceeded"),
                                       text_block("Could not search.")])])
    tr = next(e for e in collect(fake) if e["type"] == "tool_result")
    assert tr["ok"] is False and "max_uses_exceeded" in tr["detail"]


def test_pause_turn_is_resumed_with_content_echoed():
    fake = FakeAnthropic([
        message_sse([server_search_block("s1", "lakers news"), search_result_block("s1", [{"url": "https://a.com", "title": "A"}])], "pause_turn"),
        message_sse([text_block("Here is the news.")]),
    ])
    events = collect(fake)
    assert len(fake.requests) == 2
    second = fake.requests[1]["messages"]
    assert second[-1]["role"] == "assistant" and any(b["type"] == "server_tool_use" for b in second[-1]["content"])
    assert events[-1]["ok"] and "".join(e["delta"] for e in events if e["type"] == "text") == "Here is the news."


def test_search_budget_is_enforced_across_iterations():
    fake = FakeAnthropic([
        message_sse([server_search_block("s1", "q1"), search_result_block("s1", [{"url": "https://a.com", "title": "A"}]),
                     tool_use_block("t1", "player_stats", {"player": JOKIC_ID})], "tool_use"),
        message_sse([text_block("done")]),
    ])
    collect(fake, web_search_max_uses=1)
    first_tools = [t["name"] for t in fake.requests[0]["tools"]]
    second_tools = [t["name"] for t in fake.requests[1]["tools"]]
    assert "web_search" in first_tools and "web_search" not in second_tools


def test_web_search_can_be_disabled_and_budget_is_decremented():
    fake = FakeAnthropic([message_sse([text_block("hi")])])
    collect(fake, web_search_max_uses=0)
    assert "web_search" not in [t["name"] for t in fake.requests[0]["tools"]]

    fake = FakeAnthropic([
        message_sse([server_search_block("s1", "q"), search_result_block("s1", [{"url": "https://a.com", "title": "A"}]),
                     tool_use_block("t1", "player_stats", {"player": JOKIC_ID})], "tool_use"),
        message_sse([text_block("done")]),
    ])
    collect(fake, web_search_max_uses=3)
    assert next(t for t in fake.requests[1]["tools"] if t["name"] == "web_search")["max_uses"] == 2


def test_iteration_cap_stops_runaway_loops():
    fake = FakeAnthropic([message_sse([tool_use_block("t", "player_stats", {"player": JOKIC_ID})], "tool_use")])
    events = collect(fake)
    assert len(fake.requests) == MAX_ITERATIONS
    assert events[-2]["type"] == "error" and "without a final answer" in events[-2]["message"]
    assert events[-1]["ok"] is False


def test_max_tokens_gives_notice():
    events = collect(FakeAnthropic([message_sse([text_block("cut off mid")], "max_tokens")]))
    assert any(e["type"] == "notice" and "length limit" in e["message"] for e in events)
    assert events[-1]["ok"]


def test_auth_error_is_friendly_and_does_not_raise():
    events = collect(FakeAnthropic([api_error(401, "authentication_error", "invalid x-api-key")]))
    err = next(e for e in events if e["type"] == "error")
    assert "ANTHROPIC_API_KEY" in err["message"] and "test-key" not in err["message"]
    assert events[-1]["type"] == "done" and events[-1]["ok"] is False


def test_bad_request_mentioning_search_hints_at_console():
    resp = api_error(400, "invalid_request_error", "web search is not enabled for this organization")
    err = next(e for e in collect(FakeAnthropic([resp])) if e["type"] == "error")
    assert "Console" in err["message"]


def test_search_result_arriving_after_pause_turn_is_not_reported_as_failed():
    fake = FakeAnthropic([
        message_sse([server_search_block("s1", "injury report")], "pause_turn"),
        message_sse([search_result_block("s1", [{"url": "https://a.com", "title": "A"}, {"url": "https://b.com", "title": "B"}]),
                     text_block("Done.")]),
    ])
    events = collect(fake)
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["ok"] is True and results[0]["detail"] == "2 results" and "injury report" in results[0]["label"]
    assert events[-1]["searches"] == 1 and events[-1]["ok"] is True
    assert {s["url"] for s in next(e for e in events if e["type"] == "sources")["sources"]} == {"https://a.com", "https://b.com"}


def test_search_whose_result_never_arrives_is_reported_failed_exactly_once():
    fake = FakeAnthropic([message_sse([server_search_block("s1", "q")], "pause_turn"), message_sse([text_block("Done.")])])
    events = collect(fake)
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is False and results[0]["detail"] == "no result returned"
    assert types(events).index("tool_result") < types(events).index("done")


def _breakpoints(obj):
    return json.dumps(obj).count('"cache_control"')


def test_prompt_cache_marks_system_last_hardwood_tool_and_newest_message():
    fake = FakeAnthropic([
        message_sse([tool_use_block("toolu_1", "player_stats", {"player": JOKIC_ID})], "tool_use"),
        message_sse([text_block("Done.")]),
    ])
    collect(fake)
    first, second = fake.requests
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    names = [t["name"] for t in first["tools"]]
    assert names[-1] == "web_search" and "cache_control" not in first["tools"][-1]
    assert "cache_control" in first["tools"][-2]  # last Hardwood tool, before web_search
    assert first["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # second call: the breakpoint moves to the new tool_result; the old one is gone (never more than 3 in total)
    assert second["messages"][-1]["content"][-1]["type"] == "tool_result"
    assert second["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert _breakpoints(second["messages"]) == 1
    assert _breakpoints(second) == 3


def test_prompt_cache_skips_message_marker_after_pause_turn():
    fake = FakeAnthropic([
        message_sse([server_search_block("s1", "q"), search_result_block("s1", [{"url": "https://a.com", "title": "A"}])], "pause_turn"),
        message_sse([text_block("ok")]),
    ])
    events = collect(fake)
    second = fake.requests[1]
    assert second["messages"][-1]["role"] == "assistant"
    assert _breakpoints(second["messages"]) == 0 and "cache_control" in second["system"][0]
    assert events[-1]["ok"]


def test_cached_tokens_are_counted_in_usage():
    fake = FakeAnthropic([
        message_sse([tool_use_block("t1", "player_stats", {"player": JOKIC_ID})], "tool_use", in_tokens=50, cache_write=4000),
        message_sse([text_block("Done.")], in_tokens=30, cache_read=4200, cache_write=300, out_tokens=10),
    ])
    done = collect(fake)[-1]
    assert done["input_tokens"] == 50 + 4000 + 30 + 4200 + 300
    assert done["cache_read_tokens"] == 4200 and done["cache_write_tokens"] == 4300


def test_prompt_cache_can_be_turned_off():
    fake = FakeAnthropic([message_sse([text_block("hi")])])
    collect(fake, prompt_cache=False)
    req = fake.requests[0]
    assert isinstance(req["system"], str) and _breakpoints(req) == 0


def _answer_after_one_tool_call(text, history=None, **kw):
    fake = FakeAnthropic([
        message_sse([tool_use_block("t1", "player_stats", {"player": JOKIC_ID, "scope": "last10"})], "tool_use"),
        message_sse([text_block(text)]),
    ])
    return collect(fake, history=history, **kw)


def test_done_reports_estimated_cost():
    done = collect(FakeAnthropic([message_sse([text_block("hi")], in_tokens=120, out_tokens=9)]))[-1]
    assert done["est_cost_usd"] == 0.0003  # 120 * $2/M + 9 * $10/M


def test_check_event_flags_numbers_that_no_tool_returned():
    events = _answer_after_one_tool_call("Jokic averaged 31.7 points over 10 games.")
    check = next(e for e in events if e["type"] == "check")
    assert check["unverified"] == ["31.7"]
    assert types(events).index("check") < types(events).index("done")
    assert events[-1]["ok"] is True  # flag-only: the answer still completes


def test_grounded_answer_gets_no_check_event():
    events = _answer_after_one_tool_call("Jokic averaged 27.1 points, 12.0 rebounds and 10.2 assists.")
    assert "check" not in types(events)


def test_numbers_from_earlier_in_the_conversation_count_as_evidence():
    history = [{"role": "user", "content": "The Maxey line is 24.5, right?"}, {"role": "assistant", "content": "Yes."},
               {"role": "user", "content": "And Jokic's last 10?"}]
    events = _answer_after_one_tool_call("You said 24.5 earlier; Jokic averaged 27.1.", history=history)
    assert "check" not in types(events)


def test_cost_guard_stops_a_runaway_question_before_running_more_tools():
    fake = FakeAnthropic([message_sse([tool_use_block("t1", "player_stats", {"player": JOKIC_ID})], "tool_use", in_tokens=1_000_000)])
    hw = FakeHardwood({("POST", "/query"): JOKIC_SEASON_RESPONSE})
    events = collect(fake, hw, max_cost_usd=0.5)
    assert len(fake.requests) == 1 and not hw.bodies("/query")
    err = next(e for e in events if e["type"] == "error")
    assert "MAX_COST_USD" in err["message"] and "$2.00" in err["message"]
    assert events[-1]["ok"] is False and events[-1]["est_cost_usd"] == 2.0002  # 1M input + 20 output tokens


def test_time_guard_stops_starting_new_model_calls():
    fake = FakeAnthropic([message_sse([tool_use_block("t1", "player_stats", {"player": JOKIC_ID})], "tool_use")])
    events = collect(fake, max_seconds=0)
    assert len(fake.requests) == 1
    assert "MAX_SECONDS" in next(e for e in events if e["type"] == "error")["message"]
    assert events[-1]["ok"] is False


def test_audit_failure_never_breaks_an_answer(monkeypatch):
    import app.agent as agent_mod

    def boom(*a, **k):
        raise RuntimeError("audit bug")
    monkeypatch.setattr(agent_mod, "audit", boom)
    events = _answer_after_one_tool_call("Jokic averaged 31.7 points.")
    assert events[-1]["type"] == "done" and events[-1]["ok"] is True and "check" not in types(events)


def test_a_cut_off_answer_is_continued_seamlessly():
    from app.agent import CONTINUE_PROMPT
    fake = FakeAnthropic([
        message_sse([text_block("Part one of the answer, cut")], "max_tokens"),
        message_sse([text_block(" and the rest.")]),
    ])
    events = collect(fake)
    assert "".join(e["delta"] for e in events if e["type"] == "text") == "Part one of the answer, cut and the rest."
    assert not any(e["type"] == "notice" for e in events) and events[-1]["ok"]
    msgs = fake.requests[1]["messages"]
    assert msgs[-1]["role"] == "user" and CONTINUE_PROMPT in json.dumps(msgs[-1]["content"])
    assert msgs[-2]["role"] == "assistant"


def test_text_written_before_tool_calls_does_not_run_into_the_answer():
    fake = FakeAnthropic([
        message_sse([text_block("I'll pull the board."), tool_use_block("t1", "player_stats", {"player": JOKIC_ID})], "tool_use"),
        message_sse([text_block("**Hardwood:** here it is.")]),
    ])
    text = "".join(e["delta"] for e in collect(fake) if e["type"] == "text")
    assert text == "I'll pull the board.\n\n**Hardwood:** here it is."


def test_text_blocks_around_a_web_search_are_separated_but_a_continuation_still_joins():
    fake = FakeAnthropic([
        message_sse([text_block("Searching."), server_search_block("s1", "q"),
                     search_result_block("s1", [{"url": "https://a.com", "title": "A"}]),
                     text_block("Found it, cut")], "max_tokens"),
        message_sse([text_block(" and finished.")]),
    ])
    text = "".join(e["delta"] for e in collect(fake) if e["type"] == "text")
    assert text == "Searching.\n\nFound it, cut and finished."


def test_no_separator_is_added_when_the_first_text_block_starts_the_answer():
    text = "".join(e["delta"] for e in collect(FakeAnthropic([message_sse([text_block("Just the answer.")])])) if e["type"] == "text")
    assert text == "Just the answer."


def test_continuation_is_limited_and_then_gives_the_notice():
    from app.agent import MAX_CONTINUATIONS
    fake = FakeAnthropic([message_sse([text_block("never ending")], "max_tokens")])
    events = collect(fake)
    assert len(fake.requests) == 1 + MAX_CONTINUATIONS
    assert any(e["type"] == "notice" and "length limit" in e["message"] for e in events)


def test_a_cut_off_tool_call_is_not_continued():
    fake = FakeAnthropic([message_sse([tool_use_block("t1", "player_stats", {"player": JOKIC_ID})], "max_tokens")])
    events = collect(fake)
    assert len(fake.requests) == 1
    assert any(e["type"] == "notice" for e in events)


def test_continuation_respects_the_cost_limit():
    fake = FakeAnthropic([message_sse([text_block("cut")], "max_tokens", in_tokens=1_000_000)])
    events = collect(fake, max_cost_usd=0.5)
    assert len(fake.requests) == 1 and any(e["type"] == "notice" for e in events)


def test_hidden_reasoning_is_measured_and_the_cut_off_notice_names_the_cap():
    thinking = {"type": "thinking", "thinking": "x" * 120, "signature": "sig"}
    fake = FakeAnthropic([message_sse([thinking, text_block("The answer.")])])
    done = collect(fake)[-1]
    assert done["thinking_chars"] == 120

    events = collect(FakeAnthropic([message_sse([tool_use_block("t", "player_stats", {"player": JOKIC_ID})], "max_tokens", out_tokens=777)]),
                     max_tokens=555)
    note = next(e for e in events if e["type"] == "notice")["message"]
    assert "MAX_TOKENS=555" in note and "777 output tokens" in note


def test_cut_off_text_after_a_web_search_in_the_same_message_is_continued():
    fake = FakeAnthropic([
        message_sse([server_search_block("s1", "q"), search_result_block("s1", [{"url": "https://a.com", "title": "A"}]),
                     text_block("Board part one, cut")], "max_tokens"),
        message_sse([text_block(" and part two.")]),
    ])
    events = collect(fake)
    assert len(fake.requests) == 2 and not any(e["type"] == "notice" for e in events)
    assert "".join(e["delta"] for e in events if e["type"] == "text").endswith("part one, cut and part two.")


def test_done_carries_a_per_call_trace_for_diagnosing_token_use():
    thinking = {"type": "thinking", "thinking": "", "signature": "sig"}  # reasoning with its text omitted
    fake = FakeAnthropic([
        message_sse([thinking, tool_use_block("t1", "player_stats", {"player": JOKIC_ID})], "tool_use", out_tokens=900),
        message_sse([text_block("Done.")], out_tokens=40),
    ])
    done = collect(fake)[-1]
    assert [t["stop"] for t in done["iterations"]] == ["tool_use", "end_turn"]
    assert [t["out"] for t in done["iterations"]] == [900, 40]
    assert done["iterations"][0]["blocks"] == ["thinking", "tool_use"] and done["iterations"][0]["thinking_blocks"] == 1
    assert done["iterations"][1]["text_chars"] == len("Done.") and done["thinking_blocks"] == 1
    assert done["thinking_chars"] == 0  # the text was omitted, so the char count alone would have hidden this


def test_effort_and_thinking_are_sent_only_when_configured():
    fake = FakeAnthropic([message_sse([text_block("hi")])])
    collect(fake)
    assert "output_config" not in fake.requests[0] and "thinking" not in fake.requests[0]

    fake = FakeAnthropic([message_sse([text_block("hi")])])
    collect(fake, effort="medium")
    assert fake.requests[0]["output_config"] == {"effort": "medium"} and "thinking" not in fake.requests[0]

    fake = FakeAnthropic([message_sse([text_block("hi")])])
    collect(fake, effort="low", thinking="off")
    assert fake.requests[0]["output_config"] == {"effort": "low"} and fake.requests[0]["thinking"] == {"type": "between_tools"}


def test_thinking_settings_stay_constant_through_a_tool_loop():
    fake = FakeAnthropic([
        message_sse([tool_use_block("t1", "player_stats", {"player": JOKIC_ID})], "tool_use"),
        message_sse([text_block("Done.")]),
    ])
    collect(fake, effort="medium", thinking="off")
    first, second = fake.requests
    assert first["thinking"] == second["thinking"] and first["output_config"] == second["output_config"]
