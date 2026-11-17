"""FastAPI app: serves the chat page and streams the agent's events as SSE."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, List, Literal, Optional

from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import ResearchAgent
from .config import ROOT, Settings, load_settings
from .dashboard import make_router
from .hardwood import HardwoodClient

MAX_HISTORY = 20  # messages sent to the model per question (the browser keeps the full chat)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(min_length=1, max_length=100)


def prepare_history(messages: List[ChatMessage]) -> List[dict]:
    """Last MAX_HISTORY messages, starting on a user turn and ending on one."""
    msgs = [{"role": m.role, "content": m.content} for m in messages][-MAX_HISTORY:]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(status_code=422, detail="The last message must be from the user.")
    return msgs


def _write_log(path: Optional[Path], record: dict) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # logging must never break a chat


async def _sse(agent: ResearchAgent, history: List[dict], log_path: Optional[Path]) -> AsyncIterator[str]:
    record: dict = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question": history[-1]["content"], "tools": [], "answer_chars": 0,
    }
    try:
        async for ev in agent.run(history):
            kind = ev["type"]
            if kind == "text":
                record["answer_chars"] += len(ev["delta"])
            elif kind == "tool_result":
                record["tools"].append({k: ev.get(k) for k in ("name", "label", "ok", "ms")})
            elif kind == "check":
                record["check"] = {"unverified": ev["unverified"], "notes": ev["notes"]}
            elif kind == "error":
                record["error"] = ev["message"]
            elif kind == "done":
                record.update({k: ev[k] for k in ("elapsed_ms", "searches", "input_tokens", "output_tokens",
                                        "cache_read_tokens", "cache_write_tokens", "est_cost_usd", "thinking_chars", "thinking_blocks", "iterations")})
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    finally:
        _write_log(log_path, record)


def create_app(settings: Optional[Settings] = None, *, anthropic_client: Any = None, hardwood: Any = None) -> FastAPI:
    settings = settings or load_settings()
    owns_anthropic = anthropic_client is None and bool(settings.anthropic_api_key)
    owns_hardwood = hardwood is None and bool(settings.hardwood_api_key)
    if owns_anthropic:
        anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=180.0)
    if owns_hardwood:
        hardwood = HardwoodClient(settings.hardwood_base_url, settings.hardwood_api_key)
    agent = ResearchAgent(settings, anthropic_client, hardwood) if (anthropic_client and hardwood) else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if owns_hardwood:
            await hardwood.aclose()
        if owns_anthropic:
            await anthropic_client.close()

    app = FastAPI(title="NBA Research Agent", docs_url=None, redoc_url=None, lifespan=lifespan)
    static_dir = ROOT / "static"

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "ok": agent is not None,
            "anthropic_key_set": bool(settings.anthropic_api_key) or anthropic_client is not None,
            "hardwood_key_set": bool(settings.hardwood_api_key) or hardwood is not None,
            "model": settings.model,
            "web_search_max_uses": settings.web_search_max_uses,
        }

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        if agent is None:
            raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY or HARDWOOD_API_KEY is missing. Add it to .env and restart the server.")
        history = prepare_history(req.messages)
        return StreamingResponse(
            _sse(agent, history, settings.log_path),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    app.include_router(make_router(settings))

    @app.get("/dash")
    async def dash() -> FileResponse:
        return FileResponse(static_dir / "dash.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app
