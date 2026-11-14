"""Live eval: ask the fixed questions against the real APIs and grade the answers.

    python -m evals.run                  # all cases
    python -m evals.run --only props_board,out_of_scope
    python -m evals.run --list

Needs ANTHROPIC_API_KEY and HARDWOOD_API_KEY in .env, like the app. Each full run costs roughly a dollar
(it prints the real estimate at the end). Exit code is 1 if any "fail" check fails.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import ROOT, Settings, load_settings
from app.agent import ResearchAgent

from .cases import CASES, Case


@dataclass
class Check:
    name: str
    ok: bool
    severity: str = "fail"  # "fail" or "warn"
    detail: str = ""


@dataclass
class CaseResult:
    id: str
    question: str
    checks: List[Check] = field(default_factory=list)
    answer: str = ""
    tools: List[dict] = field(default_factory=list)
    searches: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    audit_notes: List[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    thinking_chars: int = 0
    thinking_blocks: int = 0
    trace: List[dict] = field(default_factory=list)
    notices: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks if c.severity == "fail")

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "warn"]


def evaluate(case: Case, events: List[dict], settings: Settings) -> CaseResult:
    """Grade one run from its event stream. Pure: no network, so it can be unit-tested."""
    answer = "".join(e["delta"] for e in events if e["type"] == "text")
    done = next((e for e in reversed(events) if e["type"] == "done"), None)
    results = [e for e in events if e["type"] == "tool_result"]
    client_tools = [e for e in results if e["name"] != "web_search"]
    searches = (done or {}).get("searches", 0)
    cost = (done or {}).get("est_cost_usd", 0.0) or 0.0
    errors = [e["message"] for e in events if e["type"] == "error"]
    notices = [e["message"] for e in events if e["type"] == "notice"]
    check_ev = next((e for e in events if e["type"] == "check"), {"unverified": [], "notes": []})

    r = CaseResult(id=case.id, question=case.question, answer=answer, searches=searches, cost_usd=cost,
                   tools=[{k: e.get(k) for k in ("name", "label", "ok", "ms", "detail")} for e in results],
                   audit_notes=check_ev["notes"], notices=notices,
                   tokens_in=(done or {}).get("input_tokens", 0), tokens_out=(done or {}).get("output_tokens", 0),
                   thinking_chars=(done or {}).get("thinking_chars", 0),
                   thinking_blocks=(done or {}).get("thinking_blocks", 0), trace=(done or {}).get("iterations", []), seconds=round((done or {}).get("elapsed_ms", 0) / 1000, 1))
    add = lambda name, ok, severity="fail", detail="": r.checks.append(Check(name, bool(ok), severity, detail))  # noqa: E731

    add("finished without error", done is not None and done.get("ok") and not errors, detail="; ".join(errors))
    add("not cut off by the length limit", not any("length limit" in n for n in notices))
    add("gave an answer", len(answer.strip()) > 40, detail=f"{len(answer.strip())} chars")

    ran = {e["name"] for e in client_tools}
    for t in case.expect_tools:
        add(f"called {t}", t in ran, detail=f"ran: {sorted(ran) or 'nothing'}")
    for needle, minimum in case.label_counts.items():
        n = sum(1 for e in client_tools if needle in (e.get("label") or ""))
        # A short answer on a case that expects a long one usually means the data was empty (no board posted yet),
        # so there was nothing to make extra calls for: warn instead of failing.
        thin = bool(case.min_answer_chars) and len(answer) < case.min_answer_chars
        add(f"at least {minimum} calls with {needle!r}", n >= minimum, "warn" if thin else "fail", detail=f"{n} found")
    if case.min_searches:
        add(f"ran at least {case.min_searches} web search(es)", searches >= case.min_searches, detail=f"{searches} ran")
    add(f"web searches within the limit ({settings.web_search_max_uses})", searches <= settings.web_search_max_uses)
    if case.no_tools:
        add("used no tools at all", not client_tools and searches == 0,
            detail=f"{len(client_tools)} tool calls, {searches} searches")

    failed = [e for e in client_tools if not e.get("ok")]
    add(f"tool failures <= {case.max_tool_failures}", len(failed) <= case.max_tool_failures,
        detail="; ".join(f"{e['label']}: {e.get('detail')}" for e in failed[:3]))

    unverified = check_ev["unverified"]
    add("every decimal traces to tool data (audit)", not unverified, detail=", ".join(unverified))
    add("no audit notes", not check_ev["notes"], "warn", detail=" | ".join(check_ev["notes"]))
    row_notes = [n for n in check_ev["notes"] if n.startswith("Row check")]
    add("prop table rows match the prop board (audit)", not row_notes, detail=" | ".join(row_notes))

    if case.min_answer_chars:
        add(f"answer is at least {case.min_answer_chars} chars (data may not have exercised this path)",
            len(answer) >= case.min_answer_chars, "warn", detail=f"{len(answer)} chars; if the data was empty, rerun when it isn't")
    for label, pattern, severity in case.must_match:
        add(label, re.search(pattern, answer) is not None, severity)
    for label, pattern, severity in case.must_not_match:
        hit = re.search(pattern, answer)
        add(f"no: {label}", hit is None, severity, detail=f"matched {hit.group(0)!r}" if hit else "")

    add(f"cost <= ${case.max_cost_usd:.2f}", cost <= case.max_cost_usd, detail=f"~${cost:.3f}")
    return r


async def run_case(agent: ResearchAgent, case: Case, settings: Settings) -> CaseResult:
    t0 = time.perf_counter()
    events: List[dict] = []
    try:
        async for ev in agent.run([{"role": "user", "content": case.question}]):
            events.append(ev)
    except Exception as e:  # noqa: BLE001 - a crash is a failed case, not a crashed eval
        events.append({"type": "error", "message": f"{type(e).__name__}: {e}"})
    result = evaluate(case, events, settings)
    if not result.seconds:
        result.seconds = round(time.perf_counter() - t0, 1)
    return result


def format_report(results: List[CaseResult]) -> str:
    lines = [f"{'case':<16} {'result':<7} {'warns':<5} {'tools':<5} {'srch':<4} {'cost':<7} {'secs':<6} {'out tok':<8} {'think':<5}", "-" * 72]
    for r in results:
        lines.append(f"{r.id:<16} {'PASS' if r.passed else 'FAIL':<7} {len(r.warnings):<5} {len(r.tools):<5} "
                     f"{r.searches:<4} ${r.cost_usd:<6.3f} {r.seconds:<6} {r.tokens_out:<8} {r.thinking_blocks:<5}")
    lines.append("-" * 72)
    lines.append(f"{sum(r.passed for r in results)}/{len(results)} passed · est. total ${sum(r.cost_usd for r in results):.2f} "
                 "(estimate; the Anthropic Console has the real bill)")
    for r in results:
        bad = [c for c in r.checks if not c.ok]
        if bad:
            lines.append(f"\n{r.id}: {r.question}")
            for c in bad:
                lines.append(f"  {'FAIL' if c.severity == 'fail' else 'warn'}  {c.name}" + (f"  [{c.detail}]" if c.detail else ""))
            lines.extend(f"  note  {n}" for n in r.notices)
            for i, t in enumerate(r.trace, 1):
                lines.append(f"  call {i:<2} stop={t.get('stop')} out={t.get('out')} text={t.get('text_chars')}c "
                             f"thinking_blocks={t.get('thinking_blocks')} blocks={','.join(t.get('blocks', []))}")
    return "\n".join(lines)


async def main_async(only: Optional[List[str]], out_dir: Path) -> int:
    from anthropic import AsyncAnthropic
    from app.hardwood import HardwoodClient

    settings = load_settings()
    missing = [n for n, v in (("ANTHROPIC_API_KEY", settings.anthropic_api_key), ("HARDWOOD_API_KEY", settings.hardwood_api_key)) if not v]
    if missing:
        print("Missing in .env: " + ", ".join(missing))
        return 2
    cases = [c for c in CASES if not only or c.id in only]
    if not cases:
        print("No matching cases. Use --list to see the ids.")
        return 2

    print(f"model={settings.model} max_tokens={settings.max_tokens} max_cost=${settings.max_cost_usd:.2f} "
          f"web_search_max={settings.web_search_max_uses} cache={settings.prompt_cache} "
          f"effort={settings.effort or 'default(high)'} thinking={settings.thinking}")
    client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=180.0)
    hardwood = HardwoodClient(settings.hardwood_base_url, settings.hardwood_api_key)
    agent = ResearchAgent(settings, client, hardwood)
    results: List[CaseResult] = []
    try:
        for i, case in enumerate(cases, 1):
            print(f"[{i}/{len(cases)}] {case.id} ...", flush=True)
            results.append(await run_case(agent, case, settings))
    finally:
        await hardwood.aclose()
        await client.close()

    print("\n" + format_report(results))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"model": settings.model, "effort": settings.effort or "default", "thinking": settings.thinking, "results": [asdict(r) | {"passed": r.passed} for r in results]},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nFull answers and checks saved to {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return 0 if all(r.passed for r in results) else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--list", action="store_true", help="list the cases and exit")
    ap.add_argument("--out", default=str(ROOT / "evals" / "runs"), help="where to save the JSON report")
    args = ap.parse_args()
    if args.list:
        for c in CASES:
            print(f"{c.id:<16} {c.question}")
        return
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    sys.exit(asyncio.run(main_async(only, Path(args.out))))


if __name__ == "__main__":
    main()
