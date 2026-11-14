# NBA Research Agent (spike)

A local chat page backed by one research agent. You type a question; the agent decides what to look up and answers.

- **Web search** (Anthropic's server-side tool) for news, injuries, lineups and reporter notes.
- **Hardwood** (your Chalk That NBA API) for stats, splits, game logs, leaderboards, standings, team ratings, matchup ranks and DraftKings prop lines.
- **Props** are presented as research (line, hit rates, splits, matchup, availability), with evidence for both sides. It only states a lean when you ask, and labels it.

Local only. The server binds to `127.0.0.1` and has no login, because it holds your API keys. Don't expose it.

## Run it (all on the Pro)

High level: (1) make a venv and install, (2) check `.env`, (3) start the server, (4) open the page.

```bash
cd /Users/jayprox_pro/Documents/Projects/outside_git/nba-research-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

`.env` needs `ANTHROPIC_API_KEY` and `HARDWOOD_API_KEY` (see `.env.example`). Check the names without printing values:

```bash
sed -E 's/=.*/=<hidden>/' .env
```

Start it and open <http://127.0.0.1:8000>:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`http://127.0.0.1:8000/api/health` tells you whether both keys were found.

## Tests

```bash
python -m pytest -q
```

108 tests run offline (Python 3.9 and later). `tests/test_evals.py` covers the eval grading logic with fake data.

They run offline. The agent tests drive the real Anthropic SDK against a mocked HTTP transport, and the Hardwood tests use a mocked API, so no keys or network are needed. They do **not** prove the live services behave the same way (see "Not verified yet").

## How it works

```
browser ──POST /api/chat (history)──▶ FastAPI ──▶ ResearchAgent ──▶ Claude (Messages API, streaming)
   ▲                                                     │  ├─ web_search      (server tool, runs at Anthropic)
   └────────── SSE events ◀──────────────────────────────┘  └─ 10 Hardwood tools (run here, call Hardwood's API)
```

- The browser keeps the conversation and sends it with every request, so the server is stateless. Only the text of earlier turns is resent, not the raw tool results.
- One question runs a loop of up to 10 model calls: Claude asks for tools, the server runs them (in parallel when independent), results go back, repeat until it answers. `pause_turn` from long web searches is resumed automatically.
- The browser receives SSE events: `text`, `tool_start`, `tool_result`, `notice`, `sources`, `check`, `error`, `done`. The tool activity list and the sources list are built from them.
- Today's date (US Eastern) and the current season label are injected into the system prompt on every request.

### Files

| File | Purpose |
|---|---|
| `app/prompt.py` | System prompt: source labeling, web rules, Hardwood conventions, prop style. Most answer quality comes from here. |
| `app/hardwood.py` | Hardwood client, name resolution (players, teams), the 10 tool schemas and implementations, result trimming. |
| `app/agent.py` | The tool-use loop, search budget, prompt caching, cost and time guards, source collection, API error messages. |
| `app/verify.py` | Flag-only audit of each finished answer (see "Checks and guards"). |
| `evals/` | Live eval: six fixed questions graded automatically (see "Live eval"). |
| `app/server.py` | FastAPI app, SSE endpoint, JSONL logging. |
| `static/` | The chat page (`index.html`, `app.js`, `md.js`). No build step, no CDN. |
| `tests/` | Offline tests. |

### Checks and guards

- **Input checks.** Before any Hardwood call, tool inputs are checked against the tool schema (required fields, types, allowed values). The model gets a plain message ("Invalid value 'points' for 'stat'. Allowed values: ...") and nothing is sent to Hardwood. If a tool then fails because Hardwood's reply has an unexpected shape, the model is told that is a data problem, not that its input was wrong.
- **Answer audit** (`app/verify.py`, flag-only, no extra model cost). After the answer finishes, decimals and percentages are compared to the tool results and the earlier conversation. Unmatched ones are shown as a "⚠ Not found in the tool data" note under the answer, and logged. It also flags numbers when no tool ran, a rank written as a range ("ranks 26/30"), and web searches whose outlets the answer never names. It never edits the answer. **It is a net, not a proof:** a wrong number that equals some other value in a big data set passes, and a right number on the wrong player passes. Derived numbers (a change, a per-36 rate) only pass if the answer also shows the two numbers they come from.
- **Prop row check** (also `app/verify.py`). For every table row that starts with a player on the prop board, the answer's line, over-under records, matchup rank and every decimal (averages, allowed, vs avg, or an average minus the line) are compared with that player's own `prop_board` row, and any row under 10 season games must carry a † (on a board of five or more rows). Mismatches show as "Row check: ..." notes. This exists because a right number on the wrong row slips past the decimal check; in live runs it caught such slips in a 119-row table. Limits: about 1 in 15 wrong decimals can still pass by landing near another figure in the same row, and garbled cell text with no stray number is not caught.
- **Cost and time guards.** `MAX_COST_USD` (default $1) and `MAX_SECONDS` (default 300) stop a runaway question between model calls. The footer under each answer shows an estimated cost; prices are constants in `app/agent.py` (Sonnet 5.5 list prices, cache-write price assumed at 1.25x input). It is an estimate, not the bill.
- **Prompt caching** (`PROMPT_CACHE`, on by default) marks the tool list, system prompt and newest message so repeated tool rounds are billed at the cached rate. The footer shows how many tokens were cached.

### Reasoning (thinking) and output tokens

Sonnet 5.5 reasons by default (adaptive thinking, effort `high`) even when the request says nothing about it. The reasoning text is omitted from the response but it is billed as output and counts against `MAX_TOKENS`, so `usage.output_tokens` is much larger than the visible answer. In a live run of the full props board, one model call wrote about 13,000 visible characters (roughly 8,000 tokens) and used 15,700 output tokens. `THINKING` and `EFFORT` change this; the eval prints the effort in its header, so you can compare settings: `EFFORT=medium python -m evals.run`. The per-call trace (stop reason, output tokens, block types) is in the eval report for failing cases and in `logs/chat.jsonl` for every question. Source: Anthropic's thinking docs for Sonnet 5.5.

Measured on 2026-11-13 (single runs, so treat small differences as noise): the full eval cost about $0.54 at the default, $0.29 with `EFFORT=medium`, $0.18 with `EFFORT=low` and $0.29-0.49 with `THINKING=off` (the higher figure came after the whole-board prompt rule below made the agent pull every game). `EFFORT=low` did one injury search instead of three and left games uncovered, so it is not recommended. Lower settings also made the agent pull only the top 25 props unless told to cover every game, which the prompt now does. `THINKING=off` is the setting used for the checks in this README.

### Live eval

`python -m evals.run` (from the project folder, venv active) asks six fixed questions against the real APIs and grades each answer without a human: which tools ran, whether the audit found untraceable numbers, whether web searches happened and dates were given, whether props stayed research (no "best bet" language), whether the out-of-scope question was declined with no tool calls, and cost per question. Beyond the rules above, the props case needs at least four team-filtered `prop_board` calls (a whole-board request must cover every game, not the top slice) and fails if the prop row check flags anything. It prints a pass/fail table plus an estimated total (about $0.50 with `THINKING=off`), saves full answers to `evals/runs/` (git-ignored) and exits 1 if anything fails. Options: `--only props_board,out_of_scope`, `--list`. Run it after changing the prompt, the tools or the model. The cases and their rules are in `evals/cases.py`.

### Hardwood tools

`find_player`, `player_stats` (season / last5 / last10 / career / game_log, splits, prop `lines`), `team_stats`, `leaderboard`, `games_on_date`, `box_score`, `prop_board`, `player_props`, `matchup_ranks`, `league_table` (standings or team ratings). Tools take names (`"Jokic"`, `"Celtics"`); the server resolves them to Hardwood ids. Ambiguous names return the candidates so the agent can retry with an id.

`player_stats` and `team_stats` also take `opponent` for head-to-heads (Hardwood has no opponent split yet). The server loads the game log for each of the last N seasons (default 5, max 10, regular season plus playoffs), keeps only games against that team before applying any row limit, and returns them with a summary computed here and labeled as such. `seasons_back` or `seasons` change the search window. A search covers at most 10 seasons: a `seasons` list longer than that is refused with a message (never silently cut), and the result lists the seasons actually checked. The agent calls the window "the last N seasons", never a career total.

### Known gaps in the data

- **Injuries:** Hardwood has no injury feed yet (`current_injury` is always null). The prompt tells the agent to use web search for availability and to treat missing data as unknown.
- **Props:** DraftKings lines only, from the 2026-27 season on.
- Team ratings, pace and usage are estimates; the agent is told to label them.

## Configuration

All optional except the two keys. Set in `.env`; values there override variables already exported in your shell.

| Variable | Default | Notes |
|---|---|---|
| `MODEL` | `claude-sonnet-5-5` | |
| `MAX_TOKENS` | `16000` | Per model call; you only pay for tokens actually written. A full props board needs more than 8k. If an answer still hits it, the agent continues automatically (up to 2 times). |
| `MAX_COST_USD` | `1.0` | Stops a question once its estimated cost passes this (checked after each model call). |
| `MAX_SECONDS` | `300` | Stops starting new model calls after this long. |
| `EFFORT` | empty | Reasoning effort: `low`, `medium`, `high`, `xhigh`, `max`. Empty uses the API default (`high` on Sonnet 5.5). Lower means less hidden reasoning, so fewer output tokens and lower cost, and possibly lower quality. |
| `THINKING` | `adaptive` | `adaptive` is the model's default reasoning. `off` sends `thinking: between_tools` (Sonnet 5.5 rejects `disabled`): no up-front reasoning, progress notes between tool calls only. Needs `EFFORT` of `high` or lower. |
| `PROMPT_CACHE` | `true` | Caches tools, system prompt and the running conversation so later tool rounds are billed at the cheap cached rate. Set `false` to turn off. |
| `WEB_SEARCH_MAX_USES` | `4` | Per question, across the whole loop. `0` disables web search. Billed per search on top of tokens. |
| `WEB_SEARCH_TOOL_TYPE` | `web_search_20250305` | Change if Anthropic's docs list a newer version for your model. |
| `HARDWOOD_BASE_URL` | the Railway `backend-api` URL | Point at a local backend (`http://localhost:3000`) if you run one. |
| `CHAT_LOG` | `logs/chat.jsonl` | One JSON line per question: tools used, timings, token counts, errors. Empty disables it. |

## Not verified yet

The first live runs confirmed web search, the Hardwood key, the page and answer quality. Still open:

1. **Prompt caching savings.** Cached reads appear in the log and footer, but how much it saves on your usual questions is only an estimate so far (about 37% on one props board). The cache-write price is an assumption.
2. **The answer audit on live answers.** The decimal audit and the prop row check raised no false alarms on the live runs of 2026-11-13 (one nine-game slate). Other slates are untested. If either cries wolf on correct answers, loosen it in `app/verify.py`; if it stays silent while errors slip through, that is the "net, not proof" limit.
3. **The eval rules.** They were written against the mocked data and the answers seen so far. The first live run may fail a case for a reason that is the rule's fault; read the saved answer before blaming the agent.
4. **The cost guard default** ($1 per question) is far above anything seen (about $0.43 for the most expensive single question, a full props board at default settings), so it only catches runaways.

## Likely next steps

Move the loop onto the Claude Agent SDK (the tool functions in `app/hardwood.py` can be reused as-is), add the NFL and the other Chalk That apps as tool sets, and carry tool results across turns.
