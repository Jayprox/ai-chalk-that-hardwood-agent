"""Test helpers: fake Anthropic streaming responses and a fake Hardwood API, both over httpx.MockTransport."""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

import httpx
from anthropic import AsyncAnthropic

try:  # newer anthropic SDKs are built on httpx2, older ones on httpx
    import httpx2 as ahttp
except ImportError:  # pragma: no cover
    import httpx as ahttp

from app.config import Settings
from app.hardwood import HardwoodClient

JOKIC_ID = "11111111-1111-4111-8111-111111111111"
GAME_ID = "22222222-2222-4222-8222-222222222222"

TEAMS = [
    {"id": 4, "abbreviation": "BOS", "city": "Boston", "name": "Celtics", "full_name": "Boston Celtics"},
    {"id": 3, "abbreviation": "NYK", "city": "New York", "name": "Knicks", "full_name": "New York Knicks"},
    {"id": 13, "abbreviation": "LAL", "city": "Los Angeles", "name": "Lakers", "full_name": "Los Angeles Lakers"},
    {"id": 18, "abbreviation": "LAC", "city": "LA", "name": "Clippers", "full_name": "LA Clippers"},
    {"id": 28, "abbreviation": "TOR", "city": "Toronto", "name": "Raptors", "full_name": "Toronto Raptors"},
]


# ---------------------------------------------------------------- Anthropic SSE


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def text_block(text: str, citations: List[dict] | None = None) -> dict:
    return {"type": "text", "text": text, "citations": citations or []}


def tool_use_block(tool_id: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def server_search_block(tool_id: str, query: str) -> dict:
    return {"type": "server_tool_use", "id": tool_id, "name": "web_search", "input": {"query": query}}


def search_result_block(tool_id: str, results: List[dict]) -> dict:
    return {"type": "web_search_tool_result", "tool_use_id": tool_id, "content": [
        {"type": "web_search_result", "url": r["url"], "title": r["title"],
         "encrypted_content": "enc", "page_age": r.get("age")} for r in results]}


def search_error_block(tool_id: str, code: str) -> dict:
    return {"type": "web_search_tool_result", "tool_use_id": tool_id,
            "content": {"type": "web_search_tool_result_error", "error_code": code}}


def message_sse(blocks: List[dict], stop_reason: str = "end_turn", in_tokens: int = 100, out_tokens: int = 20,
                cache_read: int = 0, cache_write: int = 0) -> bytes:
    """Build the SSE body for one streamed Messages response made of the given content blocks."""
    out = [_sse("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": in_tokens, "output_tokens": 1,
                  "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write}}})]
    for i, b in enumerate(blocks):
        t = b["type"]
        if t == "text":
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}))
            words = b["text"].split(" ")
            for j, w in enumerate(words):
                chunk = w if j == 0 else " " + w
                out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": chunk}}))
            for c in b.get("citations", []):
                out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "citations_delta", "citation": c}}))
        elif t in ("tool_use", "server_tool_use"):
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                "type": t, "id": b["id"], "name": b["name"], "input": {}}}))
            raw = json.dumps(b["input"])
            half = len(raw) // 2
            for part in (raw[:half], raw[half:]):
                out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": part}}))
        else:  # web_search_tool_result arrives whole
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": b}))
        out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                      "usage": {"output_tokens": out_tokens}}))
    out.append(_sse("message_stop", {"type": "message_stop"}))
    return "".join(out).encode()


class FakeAnthropic:
    """Serves queued responses; records every request body the SDK sends."""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)  # bytes (SSE body) or httpx.Response
        self.requests: List[Dict[str, Any]] = []

    def handler(self, request: "ahttp.Request") -> "ahttp.Response":
        self.requests.append(json.loads(request.content))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, ahttp.Response):
            return item
        return ahttp.Response(200, headers={"content-type": "text/event-stream"}, content=item)

    def client(self) -> AsyncAnthropic:
        return AsyncAnthropic(api_key="test-key", max_retries=0, base_url="http://anthropic.test",
                              http_client=ahttp.AsyncClient(transport=ahttp.MockTransport(self.handler)))


def api_error(status: int, err_type: str, message: str) -> "ahttp.Response":
    return ahttp.Response(status, json={"type": "error", "error": {"type": err_type, "message": message}})


# ---------------------------------------------------------------- Hardwood


class FakeHardwood:
    """Routes by (method, path). Each route is a dict/list, or a callable(request) -> httpx.Response."""

    def __init__(self, routes: Dict[tuple, Any] | None = None):
        self.routes = {("GET", "/teams"): {"data": TEAMS}, **(routes or {})}
        self.requests: List[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"error": "not found"})
        if callable(route):
            return route(request)
        return httpx.Response(200, json=route)

    def client(self) -> HardwoodClient:
        http = httpx.AsyncClient(base_url="http://hardwood.test", transport=httpx.MockTransport(self.handler))
        return HardwoodClient("http://hardwood.test", "ctnba_test", http=http)

    def bodies(self, path: str) -> List[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path and r.content]


def settings(**kw: Any) -> Settings:
    base = dict(anthropic_api_key="a", hardwood_api_key="h", hardwood_base_url="http://hardwood.test",
                model="claude-test", max_tokens=1000, web_search_max_uses=4, log_path=None)
    base.update(kw)
    return Settings(**base)


JOKIC_SEASON_RESPONSE = {
    "query": {"scope": "last10"},
    "subject": {"type": "player", "id": JOKIC_ID, "name": "Nikola Jokić"},
    "data": {"gp": 10, "pts": 27.1, "reb": 12.0, "ast": 10.2},
    "props": {"pts": {"line": 26.5, "over": 6, "under": 4, "push": 0, "games": 10}},
    "meta": {"sample_size": 10, "record": "7-3", "filters_applied": {"season": "2026-27"},
             "notes": ["Small sample."], "freshness": {"synced_at": "2026-11-11T12:00:00Z", "source": "nba_stats"}, "cached": False},
}
