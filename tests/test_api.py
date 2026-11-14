import json

from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.server import MAX_HISTORY, ChatMessage, create_app, prepare_history
from tests.helpers import JOKIC_ID, JOKIC_SEASON_RESPONSE, FakeAnthropic, FakeHardwood, message_sse, settings, text_block, tool_use_block


def sse_events(response):
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def make_client(anthropic_fake, **settings_kw):
    hw = FakeHardwood({("POST", "/query"): JOKIC_SEASON_RESPONSE})
    app = create_app(settings(**settings_kw), anthropic_client=anthropic_fake.client(), hardwood=hw.client())
    return TestClient(app)


def test_health_and_index():
    client = make_client(FakeAnthropic([message_sse([text_block("hi")])]))
    body = client.get("/api/health").json()
    assert body["ok"] and body["anthropic_key_set"] and body["hardwood_key_set"] and body["web_search_max_uses"] == 4
    assert "key" not in json.dumps({k: v for k, v in body.items() if not k.endswith("_set")}).lower()
    page = client.get("/")
    assert page.status_code == 200 and "NBA Research Agent" in page.text and page.headers["cache-control"] == "no-store"
    assert client.get("/static/app.js").status_code == 200 and client.get("/static/md.js").status_code == 200


def test_missing_keys_report_503_not_crash():
    client = TestClient(create_app(Settings()))
    health = client.get("/api/health").json()
    assert health["ok"] is False and health["anthropic_key_set"] is False and health["hardwood_key_set"] is False
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_chat_streams_sse_with_tool_round_trip_and_logs(tmp_path):
    log = tmp_path / "logs" / "chat.jsonl"
    fake = FakeAnthropic([
        message_sse([tool_use_block("t1", "player_stats", {"player": JOKIC_ID, "scope": "last10"})], "tool_use"),
        message_sse([text_block("27.1 points over 10 games.")]),
    ])
    client = make_client(fake, log_path=log)
    with client.stream("POST", "/api/chat", json={"messages": [{"role": "user", "content": "How is Jokic scoring?"}]}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = sse_events(r)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "tool_start" and "tool_result" in kinds and kinds[-1] == "done"
    assert "".join(e["delta"] for e in events if e["type"] == "text") == "27.1 points over 10 games."

    record = json.loads(log.read_text().strip())
    assert record["question"] == "How is Jokic scoring?" and record["tools"][0]["name"] == "player_stats"
    assert record["answer_chars"] == len("27.1 points over 10 games.") and "ts" in record
    assert "a" not in record.get("api_key", "")  # no key material in the log


def test_validation_errors():
    client = make_client(FakeAnthropic([message_sse([text_block("hi")])]))
    assert client.post("/api/chat", json={"messages": []}).status_code == 422
    assert client.post("/api/chat", json={"messages": [{"role": "system", "content": "x"}]}).status_code == 422
    assert client.post("/api/chat", json={"messages": [{"role": "user", "content": ""}]}).status_code == 422
    assert client.post("/api/chat", json={"messages": [{"role": "assistant", "content": "hello"}]}).status_code == 422


def test_prepare_history_trims_and_starts_on_user_turn():
    msgs = []
    for i in range(30):
        msgs.append(ChatMessage(role="user", content=f"q{i}"))
        msgs.append(ChatMessage(role="assistant", content=f"a{i}"))
    msgs.append(ChatMessage(role="user", content="last"))
    out = prepare_history(msgs)
    assert len(out) <= MAX_HISTORY and out[0]["role"] == "user" and out[-1]["content"] == "last"


def test_load_settings_parsing():
    s = load_settings({"ANTHROPIC_API_KEY": " k ", "HARDWOOD_API_KEY": "h", "WEB_SEARCH_MAX_USES": "2", "CHAT_LOG": ""})
    assert s.anthropic_api_key == "k" and s.web_search_max_uses == 2 and s.log_path is None
    assert load_settings({}).log_path.name == "chat.jsonl"
    assert load_settings({"HARDWOOD_BASE_URL": "https://x.test/"}).hardwood_base_url == "https://x.test"
    try:
        load_settings({"MAX_TOKENS": "lots"})
    except ValueError as e:
        assert "MAX_TOKENS" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_effort_and_thinking_settings_are_validated():
    import pytest
    from app.config import load_settings
    base = {"ANTHROPIC_API_KEY": "a", "HARDWOOD_API_KEY": "h", "CHAT_LOG": ""}
    s = load_settings({**base})
    assert (s.effort, s.thinking) == ("", "adaptive")
    s = load_settings({**base, "EFFORT": " Medium ", "THINKING": "OFF"})
    assert (s.effort, s.thinking) == ("medium", "off")
    with pytest.raises(ValueError, match="EFFORT must be one of"):
        load_settings({**base, "EFFORT": "turbo"})
    with pytest.raises(ValueError, match="THINKING must be one of"):
        load_settings({**base, "THINKING": "disabled"})
    with pytest.raises(ValueError, match="only works with EFFORT low, medium or high"):
        load_settings({**base, "THINKING": "off", "EFFORT": "max"})
    assert load_settings({**base, "THINKING": "off", "EFFORT": "high"}).effort == "high"
