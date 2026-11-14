"""The research agent: one Claude tool-use loop, streamed as UI events.

Events yielded by ResearchAgent.run (plain dicts, sent to the browser as SSE):
  {"type": "text", "delta": str}
  {"type": "tool_start", "name": str}
  {"type": "tool_result", "name", "label", "ok", "ms", "detail"}
  {"type": "notice", "message": str}
  {"type": "sources", "sources": [{"title", "url", "age", "cited"}]}
  {"type": "error", "message": str}
  {"type": "done", "ok", "elapsed_ms", "searches", "input_tokens", "output_tokens",
                     "cache_read_tokens", "cache_write_tokens", "est_cost_usd", "thinking_chars",
                     "thinking_blocks", "iterations": [{"stop", "out", "blocks", "text_chars", "thinking_blocks"}]}
  {"type": "check", "unverified": [...], "notes": [...]}  (only when the audit has something to flag)
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import anthropic
from anthropic import AsyncAnthropic

from .config import Settings, current_season, now_et
from .hardwood import TOOL_SCHEMAS, HardwoodClient, ToolContext, describe_call, run_tool
from .prompt import build_system_prompt
from .verify import audit

MAX_ITERATIONS = 10  # model calls per question (tool rounds + pause_turn continuations)


def friendly_api_error(e: anthropic.APIError) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "Anthropic rejected ANTHROPIC_API_KEY (401). Check the key in .env."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "Anthropic denied this request (403). The key may lack access to the model or to web search."
    if isinstance(e, anthropic.RateLimitError):
        return "Anthropic rate limit hit. Wait a moment and try again."
    if isinstance(e, anthropic.BadRequestError):
        msg = (getattr(e, "message", "") or "")[:300]
        hint = " Web search may need to be enabled for your organization in the Anthropic Console." if "search" in msg.lower() else ""
        return f"Anthropic rejected the request: {msg}{hint}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API. Check your connection."
    status = getattr(e, "status_code", "")
    return f"Anthropic API error {status}: {(getattr(e, 'message', '') or '')[:200]}"


def _search_result_summary(block: Any) -> tuple:
    """(ok, detail) for a web_search_tool_result block."""
    content = getattr(block, "content", None)
    if isinstance(content, list):
        return True, f"{len(content)} results"
    code = getattr(content, "error_code", "unknown")
    return False, f"search failed: {code}"


def _cache_last_message(messages: List[dict]) -> List[dict]:
    """Copy of the messages with a cache breakpoint on the last block.

    Each tool round resends everything so far; the breakpoint lets the API reuse the part it has already seen.
    Only plain user messages are marked (the original list is never changed, so breakpoints don't pile up). After a
    pause_turn the last message is the assistant's own SDK blocks, which are left alone."""
    if not messages or messages[-1].get("role") != "user":
        return messages
    last = messages[-1]
    content = last["content"]
    marker = {"type": "ephemeral"}
    if isinstance(content, str) and content.strip():
        blocks = [{"type": "text", "text": content, "cache_control": marker}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        blocks = [*content[:-1], {**content[-1], "cache_control": marker}]
    else:
        return messages
    return [*messages[:-1], {**last, "content": blocks}]


# USD per million tokens for the configured model (Sonnet 5.5 list prices). The cache-write price is an assumed
# 1.25x of input; reads are 0.05x. Estimates only: the Anthropic Console has the real bill.
PRICE_PER_MTOK = {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write": 2.5}
PRICE_PER_SEARCH = 0.01


def estimate_cost(uncached_in: int, cache_read: int, cache_write: int, out: int, searches: int) -> float:
    p = PRICE_PER_MTOK
    return (uncached_in * p["input"] + cache_read * p["cache_read"] + cache_write * p["cache_write"]
            + out * p["output"]) / 1_000_000 + searches * PRICE_PER_SEARCH


MAX_CONTINUATIONS = 2
CONTINUE_PROMPT = ("Your last message was cut off by the length limit. Continue exactly where it stopped, even mid-sentence or "
                   "mid-table-row. Do not repeat anything already written and do not add an introduction.")


class ResearchAgent:
    def __init__(self, settings: Settings, client: AsyncAnthropic, hardwood: HardwoodClient,
                 now_fn: Callable[[], Any] = now_et):
        self.settings = settings
        self.client = client
        self.hardwood = hardwood
        self._now = now_fn

    def _tools(self, searches_used: int) -> List[dict]:
        tools: List[dict] = [dict(t) for t in TOOL_SCHEMAS]
        if self.settings.prompt_cache and tools:
            # Breakpoint on the last Hardwood tool: those definitions never change, even when the web search budget does.
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        remaining = self.settings.web_search_max_uses - searches_used
        if remaining > 0:
            tools.append({"type": self.settings.web_search_tool_type, "name": "web_search", "max_uses": remaining})
        return tools

    async def _exec_client_tool(self, block: Any, ctx: ToolContext) -> Dict[str, Any]:
        t0 = time.perf_counter()
        text, ok = await run_tool(self.hardwood, block.name, block.input, ctx)
        ms = int((time.perf_counter() - t0) * 1000)
        return {"id": block.id, "name": block.name, "input": block.input, "text": text, "ok": ok, "ms": ms}

    async def run(self, history: List[dict]) -> AsyncIterator[dict]:
        t_start = time.perf_counter()
        now = self._now()
        ctx = ToolContext(now=now, season=current_season(now))
        system = build_system_prompt(now, ctx.season, self.settings.web_search_max_uses)
        messages: List[dict] = [dict(m) for m in history]
        searches = 0
        tokens_in = tokens_out = cache_read = cache_write = uncached_in = 0
        continuations = 0
        join_next = False  # True right after a length cut: the next text block continues the sentence
        trace: List[dict] = []  # one entry per model call: what came back and what it cost in output tokens
        thinking_chars = 0  # reasoning the model did that never shows in the text but still costs output tokens
        answer_parts: List[str] = []
        tool_texts: List[str] = []
        tool_calls = 0
        cost = 0.0
        sources: Dict[str, dict] = {}
        pending_searches: Dict[str, Any] = {}  # server_tool_use blocks still waiting for their result
        error: Optional[str] = None
        finished = False

        try:
            for iteration in range(MAX_ITERATIONS):
                if iteration and time.perf_counter() - t_start > self.settings.max_seconds:
                    error = f"Stopped after {int(self.settings.max_seconds)} seconds without a final answer (MAX_SECONDS). Try a narrower question."
                    break
                cache = self.settings.prompt_cache
                kwargs = dict(model=self.settings.model, max_tokens=self.settings.max_tokens,
                              system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cache else system,
                              tools=self._tools(searches),
                              messages=_cache_last_message(messages) if cache else messages)
                # Sent as extra_body so older SDK versions don't need to know these fields. Constant for the whole
                # question: the API warns that changing thinking settings mid tool-loop degrades results.
                extra: Dict[str, Any] = {}
                if self.settings.effort:
                    extra["output_config"] = {"effort": self.settings.effort}
                if self.settings.thinking == "off":
                    extra["thinking"] = {"type": "between_tools"}
                if extra:
                    kwargs["extra_body"] = extra
                async with self.client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        etype = getattr(event, "type", None)
                        if etype == "content_block_delta" and getattr(event.delta, "type", None) == "text_delta":
                            answer_parts.append(event.delta.text)
                            yield {"type": "text", "delta": event.delta.text}
                        elif etype == "content_block_start" and event.content_block.type == "text":
                            # A new text block after earlier text (a line written before tool calls, then the answer)
                            # would otherwise run into it ("...the board.The board has..."). A continuation after a
                            # length cut is the one case that must join seamlessly.
                            if answer_parts and not join_next and not "".join(answer_parts[-3:]).endswith("\n\n"):
                                answer_parts.append("\n\n")
                                yield {"type": "text", "delta": "\n\n"}
                            join_next = False
                        elif etype == "content_block_start" and event.content_block.type in ("tool_use", "server_tool_use"):
                            yield {"type": "tool_start", "name": event.content_block.name}
                    final = await stream.get_final_message()

                u = final.usage
                read = getattr(u, "cache_read_input_tokens", 0) or 0
                write = getattr(u, "cache_creation_input_tokens", 0) or 0
                cache_read += read
                cache_write += write
                uncached_in += u.input_tokens or 0
                tokens_in += (u.input_tokens or 0) + read + write  # input_tokens alone excludes cached tokens
                tokens_out += u.output_tokens
                content = final.content

                # Server-side web searches already ran inside this call: report them.
                results = {b.tool_use_id: b for b in content if b.type == "web_search_tool_result"}
                for b in content:
                    if b.type == "server_tool_use":
                        searches += 1
                        res = results.get(b.id)
                        if res is None:
                            # A paused turn can end right after the search call; the result then arrives in the
                            # continuation. Hold it instead of reporting a failure.
                            pending_searches[b.id] = b
                            continue
                        ok, detail = _search_result_summary(res)
                        yield {"type": "tool_result", "name": b.name, "label": describe_call(b.name, b.input),
                               "ok": ok, "ms": None, "detail": detail}
                for tid, res in results.items():
                    b = pending_searches.pop(tid, None)
                    if b is not None:
                        ok, detail = _search_result_summary(res)
                        yield {"type": "tool_result", "name": b.name, "label": describe_call(b.name, b.input),
                               "ok": ok, "ms": None, "detail": detail}
                self._collect_sources(content, sources)
                trace.append({
                    "stop": final.stop_reason, "out": u.output_tokens or 0, "blocks": [b.type for b in content],
                    "text_chars": sum(len(b.text or "") for b in content if b.type == "text"),
                    "thinking_blocks": sum(1 for b in content if b.type in ("thinking", "redacted_thinking")),
                })
                thinking_chars += sum(len(getattr(b, "thinking", None) or getattr(b, "data", None) or "") for b in content
                                      if b.type in ("thinking", "redacted_thinking"))

                cost = estimate_cost(uncached_in, cache_read, cache_write, tokens_out, searches)
                if cost > self.settings.max_cost_usd and final.stop_reason in ("tool_use", "pause_turn"):
                    error = (f"Stopped: this question has cost an estimated ${cost:.2f}, over the ${self.settings.max_cost_usd:.2f} "
                             "limit (MAX_COST_USD). Try a narrower question.")
                    break

                messages.append({"role": "assistant", "content": content})

                if final.stop_reason == "tool_use":
                    calls = [b for b in content if b.type == "tool_use"]
                    done_calls = await asyncio.gather(*(self._exec_client_tool(b, ctx) for b in calls))
                    tool_calls += len(done_calls)
                    tool_texts.extend(c["text"] for c in done_calls)
                    tool_results = []
                    for c in done_calls:
                        yield {"type": "tool_result", "name": c["name"], "label": describe_call(c["name"], c["input"]),
                               "ok": c["ok"], "ms": c["ms"], "detail": f"{len(c['text'])} chars"}
                        tool_results.append({"type": "tool_result", "tool_use_id": c["id"], "content": c["text"],
                                             **({} if c["ok"] else {"is_error": True})})
                    messages.append({"role": "user", "content": tool_results})
                    continue

                if final.stop_reason == "pause_turn":
                    continue  # the API paused a long turn; send it back as-is to resume

                if final.stop_reason == "max_tokens":
                    # Pick the answer up where it stopped, unless the cut landed inside a tool call.
                    # (earlier tool blocks in the same message are complete, e.g. web searches that ran before the text)
                    resumable = bool(content) and content[-1].type == "text" and bool(content[-1].text)
                    if resumable and continuations < MAX_CONTINUATIONS and cost <= self.settings.max_cost_usd:
                        continuations += 1
                        join_next = True
                        messages.append({"role": "user", "content": CONTINUE_PROMPT})
                        continue
                    yield {"type": "notice", "message": (f"The answer hit the length limit (MAX_TOKENS={self.settings.max_tokens}, "
                                                         f"{tokens_out} output tokens used this question) and was cut off. Ask it to continue or narrow the question.")}
                elif final.stop_reason == "refusal":
                    yield {"type": "notice", "message": "The model declined to answer that."}
                finished = True
                break

            if not finished and error is None:
                error = f"Stopped after {MAX_ITERATIONS} model calls without a final answer. Try a narrower question."
        except anthropic.APIError as e:
            error = friendly_api_error(e)

        for b in pending_searches.values():  # a search whose result never arrived
            yield {"type": "tool_result", "name": b.name, "label": describe_call(b.name, b.input),
                   "ok": False, "ms": None, "detail": "no result returned"}

        if finished and error is None:
            try:
                report = audit("".join(answer_parts),
                               tool_texts + [m["content"] for m in history if isinstance(m.get("content"), str)],
                               tool_calls=tool_calls, searches=searches,
                               source_hosts=[urlparse(s["url"]).hostname or "" for s in sources.values()])
            except Exception:  # an audit problem must never break an answer
                report = None
            if report and (report["unverified"] or report["notes"]):
                yield {"type": "check", **report}
        if error:
            yield {"type": "error", "message": error}
        if sources:
            yield {"type": "sources", "sources": list(sources.values())}
        yield {"type": "done", "ok": error is None, "elapsed_ms": int((time.perf_counter() - t_start) * 1000),
               "searches": searches, "input_tokens": tokens_in, "output_tokens": tokens_out,
               "cache_read_tokens": cache_read, "cache_write_tokens": cache_write,
               "est_cost_usd": round(cost, 4), "thinking_chars": thinking_chars,
               "thinking_blocks": sum(t["thinking_blocks"] for t in trace), "iterations": trace}

    @staticmethod
    def _collect_sources(content: List[Any], sources: Dict[str, dict]) -> None:
        def add(url: Optional[str], title: Optional[str], age: Optional[str], cited: bool) -> None:
            if not url:
                return
            entry = sources.setdefault(url, {"title": title or url, "url": url, "age": age, "cited": False})
            entry["cited"] = entry["cited"] or cited
            if age and not entry.get("age"):
                entry["age"] = age

        for b in content:
            if b.type == "web_search_tool_result" and isinstance(b.content, list):
                for r in b.content:
                    add(getattr(r, "url", None), getattr(r, "title", None), getattr(r, "page_age", None), False)
            elif b.type == "text":
                for c in getattr(b, "citations", None) or []:
                    add(getattr(c, "url", None), getattr(c, "title", None), None, True)
