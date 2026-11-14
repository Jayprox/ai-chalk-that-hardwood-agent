"""Hardwood (Chalk That NBA) client, tool schemas and tool implementations.

The model calls tools by NAME (player / team names), and this module resolves them to
Hardwood ids, so the agent never needs a lookup round-trip before every stat query.
Contract reference: docs/api.md in the ai-application-nba repo.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from zoneinfo import ZoneInfo

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_RESULT_CHARS = 14000

MARKETS = ["pts", "reb", "ast", "fg3m", "pra", "pr", "pa", "ra", "stl", "blk", "stocks", "tov"]


class ToolError(Exception):
    """A failure the model should see and can react to (bad name, API error, ...)."""


@dataclass
class ToolContext:
    now: datetime  # US Eastern
    season: str

    @property
    def today(self) -> str:
        return self.now.strftime("%Y-%m-%d")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


# --------------------------------------------------------------------------- client


class HardwoodClient:
    def __init__(self, base_url: str, api_key: str, *, http: Optional[httpx.AsyncClient] = None, timeout: float = 20.0):
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )
        self._teams: Optional[List[dict]] = None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _request(self, method: str, path: str, *, params: Optional[dict] = None, json_body: Optional[dict] = None) -> Any:
        r: Optional[httpx.Response] = None
        for attempt in (0, 1):
            try:
                r = await self._http.request(method, path, params=params, json=json_body)
            except httpx.TimeoutException:
                raise ToolError("Hardwood timed out. Try again or narrow the request.") from None
            except httpx.HTTPError as e:
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise ToolError(f"Could not reach Hardwood ({type(e).__name__}).") from None
            if r.status_code in (502, 503, 504) and attempt == 0:
                await asyncio.sleep(1.0)
                continue
            break
        assert r is not None
        if r.status_code == 401:
            raise ToolError("Hardwood rejected the API key (401). The key may be wrong or revoked.")
        if r.status_code >= 400:
            msg = None
            try:
                msg = r.json().get("error")
            except Exception:
                pass
            raise ToolError(f"Hardwood {r.status_code}: {msg or r.reason_phrase or 'request failed'}")
        try:
            return r.json()
        except ValueError:
            raise ToolError("Hardwood returned a non-JSON response.") from None

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: dict) -> Any:
        return await self._request("POST", path, json_body=body)

    # -- name resolution

    async def resolve_player(self, ref: str) -> dict:
        """Name or UUID -> {"id", "full_name", ...}. Raises ToolError if missing or ambiguous."""
        ref = str(ref).strip()
        if not ref:
            raise ToolError("player is empty")
        if UUID_RE.match(ref):
            return {"id": ref, "full_name": None}
        rows: List[dict] = []
        for active in ("true", "false"):
            res = await self.get("/players", {"q": ref, "active": active})
            rows = res.get("data") or []
            if rows:
                break
        if not rows:
            raise ToolError(f'No player found matching "{ref}".')
        exact = [r for r in rows if _norm(r.get("full_name", "")) == _norm(ref)]
        if len(exact) == 1:
            return exact[0]
        if len(rows) == 1:
            return rows[0]
        pool = exact or rows
        cands = "; ".join(f'{r["full_name"]} ({r.get("team") or "no team"}, id {r["id"]})' for r in pool[:6])
        raise ToolError(f'"{ref}" matches several players: {cands}. Retry with the id.')

    async def teams(self) -> List[dict]:
        if self._teams is None:
            res = await self.get("/teams")
            self._teams = res.get("data") or []
        return self._teams

    async def resolve_team(self, ref: Any) -> dict:
        """Name, nickname, city, abbreviation or id -> team row."""
        ref_s = str(ref).strip()
        teams = await self.teams()
        if ref_s.isdigit():
            t = next((t for t in teams if t["id"] == int(ref_s)), None)
            if t:
                return t
            raise ToolError(f"No team with id {ref_s}.")
        n = _norm(ref_s)
        if not n:
            raise ToolError("team is empty")
        if n in ("la", "los angeles"):
            raise ToolError('"' + ref_s + '" is ambiguous: Los Angeles Lakers (LAL) or LA Clippers (LAC).')
        tests = (
            lambda t: _norm(t["abbreviation"]) == n,
            lambda t: _norm(t["full_name"]) == n,
            lambda t: _norm(t["name"]) == n,
            lambda t: _norm(t["city"]) == n,
            lambda t: n in _norm(t["full_name"]),
        )
        for test in tests:
            found = [t for t in teams if test(t)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                names = ", ".join(f'{t["full_name"]} ({t["abbreviation"]})' for t in found)
                raise ToolError(f'"{ref_s}" matches several teams: {names}. Use the abbreviation.')
        raise ToolError(f'No team found for "{ref_s}". Use a team name or 3-letter abbreviation.')


# --------------------------------------------------------------------------- shaping helpers

GAME_LOG_KEYS = [
    "date", "season", "season_type", "venue", "opponent", "won", "minutes", "pts", "reb", "ast", "fg3m", "stl", "blk",
    "tov", "fgm", "fga", "plus_minus", "rest_days", "player_rest_days", "player_b2b_night", "b2b_night",
    "started", "props", "game_id", "opp_pts",
]
META_KEYS = ("sample_size", "record", "filters_applied", "notes", "qualifier")
BOX_PLAYER_KEYS = ["full_name", "started", "dnp", "dnp_reason", "minutes", "pts", "reb", "ast", "stl", "blk", "tov",
                   "fg3m", "fgm", "fga", "plus_minus"]


def _slim(row: dict, keys: List[str]) -> dict:
    return {k: row[k] for k in keys if k in row}


def shape_query(res: dict, game_log_limit: int = 10) -> dict:
    """Trim a POST /query response to what the model needs; keep sample size and notes."""
    query = res.get("query") or {}
    data = res.get("data")
    out: Dict[str, Any] = {"subject": res.get("subject"), "scope": query.get("scope")}
    if query.get("scope") == "game_log" and isinstance(data, list):
        out["games_total"] = len(data)
        out["data"] = [_slim(g, GAME_LOG_KEYS) for g in data[:game_log_limit]]
        if len(data) > game_log_limit:
            out["note"] = f"Showing the {game_log_limit} most recent of {len(data)} games."
    else:
        out["data"] = data
    if "props" in res:
        out["props"] = res["props"]
    meta = res.get("meta") or {}
    shaped = {k: meta[k] for k in META_KEYS if k in meta}
    shaped["data_synced_at"] = (meta.get("freshness") or {}).get("synced_at")
    out["meta"] = shaped
    return out


def _log_limit(a: dict) -> int:
    return max(1, min(int(a.get("game_log_limit") or 10), 30))


_ET = ZoneInfo("America/New_York")


def _tip_et(iso: Any) -> Optional[str]:
    """'2026-11-14T00:00:00Z' -> 'Fri Nov 13, 7:00 PM ET'. Done here so the model never converts time zones itself."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(_ET)
    hour = et.hour % 12 or 12
    return f"{et:%a} {et:%b} {et.day}, {hour}:{et.minute:02d} {'AM' if et.hour < 12 else 'PM'} ET"


def _with_tip_et(row: dict) -> dict:
    """Replace tipoff_utc with tipoff_et (keeps the UTC value only if it can't be converted)."""
    if "tipoff_utc" not in row:
        return row
    out = dict(row)
    label = _tip_et(out.get("tipoff_utc"))
    if label:
        out.pop("tipoff_utc")
        out["tipoff_et"] = label
    return out


def _check_date(value: Optional[str], ctx: ToolContext) -> str:
    d = value or ctx.today
    if not DATE_RE.match(d):
        raise ToolError("date must look like 2026-11-11")
    return d


# --------------------------------------------------------------------------- head-to-head
# Hardwood has no opponent split yet (see the Hardwood backlog item), so for "X vs team Y" we load the
# game log for each season, keep only games against Y BEFORE trimming, and summarize here.

SEASON_RE = re.compile(r"^\d{4}-\d{2}$")
H2H_MAX_SEASONS = 10
H2H_DEFAULT_SEASONS = 5
H2H_PLAYER_AVG = ["minutes", "pts", "reb", "ast", "stl", "blk", "tov", "fg3m", "plus_minus"]
H2H_TEAM_AVG = ["pts", "opp_pts"]


def previous_season(label: str, back: int = 1) -> str:
    start = int(label[:4]) - back
    return f"{start}-{(start + 1) % 100:02d}"


def _h2h_seasons(a: dict, ctx: ToolContext) -> List[str]:
    if a.get("seasons"):
        seasons = [str(s).strip() for s in a["seasons"]]
    elif a.get("season") and not a.get("seasons_back"):
        seasons = [str(a["season"]).strip()]
    else:
        n = max(1, min(int(a.get("seasons_back") or H2H_DEFAULT_SEASONS), H2H_MAX_SEASONS))
        seasons = [previous_season(ctx.season, i) for i in range(n)]
    seasons = list(dict.fromkeys(seasons))
    if len(seasons) > H2H_MAX_SEASONS:
        # Never cut the list quietly: the dropped seasons would be the ones the caller listed last, often the current one.
        raise ToolError(f"A head-to-head search covers at most {H2H_MAX_SEASONS} seasons per call, but {len(seasons)} were "
                        f"listed. Pass the {H2H_MAX_SEASONS} you need, or use seasons_back (max {H2H_MAX_SEASONS}).")
    for s in seasons:
        if not SEASON_RE.match(s):
            raise ToolError(f"season must look like 2025-26, got {s!r}")
    return seasons


def _require_game_log_scope(scope: Optional[str]) -> None:
    if scope not in (None, "game_log"):
        raise ToolError("The opponent filter works with scope game_log: it returns the matching games plus a "
                        "summary. Drop scope or set it to game_log.")


def _summarize_games(games: List[dict], entity: str) -> dict:
    played = games if entity == "team" else [g for g in games if (g.get("minutes") or 0) > 0]
    out: Dict[str, Any] = {
        "games": len(played),
        "wins": sum(1 for g in played if g.get("won") is True),
        "losses": sum(1 for g in played if g.get("won") is False),
    }
    for key in (H2H_TEAM_AVG if entity == "team" else H2H_PLAYER_AVG):
        vals = [g[key] for g in played if isinstance(g.get(key), (int, float))]
        if vals:
            out[f"avg_{key}"] = round(sum(vals) / len(vals), 1)
    if entity == "player":
        fga = sum(g.get("fga") or 0 for g in played)
        if fga:
            fgm = sum(g.get("fgm") or 0 for g in played)
            out["fg"] = f"{fgm}/{fga}"
            out["fg_pct"] = round(fgm / fga, 3)
    return out


async def _head_to_head(hw: HardwoodClient, entity: str, subject: dict, opp: dict, a: dict, ctx: ToolContext) -> dict:
    seasons = _h2h_seasons(a, ctx)
    season_type = a.get("season_type", "all")
    sem = asyncio.Semaphore(5)

    async def one(season: str) -> dict:
        body: Dict[str, Any] = {"entity": entity, "id": subject["id"], "scope": "game_log",
                                "season": season, "season_type": season_type}
        if a.get("splits"):
            body["splits"] = a["splits"]
        async with sem:
            return await hw.post("/query", body)

    results = await asyncio.gather(*(one(s) for s in seasons), return_exceptions=True)
    games: List[dict] = []
    skipped: List[dict] = []
    synced = None
    for season, res in zip(seasons, results):
        if isinstance(res, ToolError):
            skipped.append({"season": season, "reason": str(res)})
            continue
        if isinstance(res, BaseException):
            raise res
        synced = ((res.get("meta") or {}).get("freshness") or {}).get("synced_at") or synced
        for g in res.get("data") or []:
            if g.get("opponent") == opp["abbreviation"]:
                games.append(g)
    if len(skipped) == len(seasons):
        raise ToolError(f"Could not load any season: {skipped[0]['reason']}")
    games.sort(key=lambda g: g.get("date") or "", reverse=True)
    limit = _log_limit(a)
    skipped_names = {x["season"] for x in skipped}
    result = {
        "head_to_head": {"subject": subject.get("full_name"), "opponent": opp["full_name"]},
        "season_type": season_type,
        "seasons_checked": [s for s in seasons if s not in skipped_names],
        "skipped_seasons": skipped,
        "games_found": len(games),
        "computed_summary": _summarize_games(games, entity),
        "games": [_slim(g, GAME_LOG_KEYS) for g in games[:limit]],
        "shown": min(limit, len(games)),
        "data_synced_at": synced,
        "note": ("computed_summary is calculated by this app from ALL games_found, not by a Hardwood split; wins/losses "
                 "are the subject's team result in those games. Older seasons may use former team abbreviations "
                 "(e.g. NJN), so games against relocated teams before about 2013-14 can be missing."),
    }
    if len(games) > limit:
        result["games_note"] = (f"games lists the {limit} most recent of {len(games)} (newest first), so the other "
                                f"{len(games) - limit} are all older than {games[limit - 1]['date']}.")
    return result


# --------------------------------------------------------------------------- tools
# Each tool: async fn(hw, args, ctx) -> JSON-serialisable dict.


async def t_find_player(hw: HardwoodClient, a: dict, ctx: ToolContext) -> dict:
    res = await hw.get("/players", {"q": a["query"], "active": "false" if a.get("include_retired") else "true"})
    rows = res.get("data") or []
    return {
        "total": (res.get("meta") or {}).get("total", len(rows)),
        "players": [_slim(r, ["id", "full_name", "listed_position", "is_active", "team"]) for r in rows[:10]],
    }


async def t_player_stats(hw: HardwoodClient, a: dict, ctx: ToolContext) -> dict:
    p = await hw.resolve_player(a["player"])
    scope = a.get("scope")
    if a.get("opponent"):
        _require_game_log_scope(scope)
        opp = await hw.resolve_team(a["opponent"])
        return await _head_to_head(hw, "player", p, opp, a, ctx)
    scope = scope or "season"
    body: Dict[str, Any] = {"entity": "player", "id": p["id"], "scope": scope, "season_type": a.get("season_type", "regular")}
    if scope != "career":
        body["season"] = a.get("season") or ctx.season
    if a.get("splits"):
        body["splits"] = a["splits"]
    if a.get("lines"):
        body["lines"] = a["lines"]
    res = await hw.post("/query", body)
    return shape_query(res, _log_limit(a))


async def t_team_stats(hw: HardwoodClient, a: dict, ctx: ToolContext) -> dict:
    t = await hw.resolve_team(a["team"])
    if a.get("opponent"):
        _require_game_log_scope(a.get("scope"))
        opp = await hw.resolve_team(a["opponent"])
        return await _head_to_head(hw, "team", t, opp, a, ctx)
    body: Dict[str, Any] = {
        "entity": "team", "id": t["id"], "scope": a.get("scope", "season"),
        "season": a.get("season") or ctx.season, "season_type": a.get("season_type", "regular"),
    }
    if a.get("splits"):
        body["splits"] = a["splits"]
    res = await hw.post("/query", body)
    return shape_query(res, _log_limit(a))


async def t_leaderboard(hw: HardwoodClient, a: dict, ctx: ToolContext) -> dict:
    body = {
        "entity": "player", "scope": "leaderboard", "stat": a.get("stat", "pts"),
        "season": a.get("season") or ctx.season, "season_type": a.get("season_type", "regular"),
        "limit": int(a.get("limit") or 10),
    }
    return shape_query(await hw.post("/query", body))


async def t_games_on_date(hw: HardwoodClient, a: dict, ctx: ToolContext) -> dict:
    date = _check_date(a.get("date"), ctx)
    res = await hw.get("/games", {"date": date})
    games = [
        _with_tip_et(_slim(g, ["id", "date", "status", "season_type", "home", "home_score", "away", "away_score", "tipoff_utc",
                              "national_broadcasters", "arena", "series_round", "series_game_number"]))
        for g in (res.get("data") or [])
    ]
    meta = res.get("meta") or {}
    return {"date": date, "count": len(games), "games": games,
            "prev_date_with_games": meta.get("prev_date"), "next_date_with_games": meta.get("next_date"),
            "note": "Dates are the home venue's local date. tipoff_et is the tip-off in US Eastern. Scores are null until a game starts."}


async def t_box_score(hw: HardwoodClient, a: dict, ctx: ToolContext) -> dict:
    gid = str(a["game_id"]).strip()
    if not UUID_RE.match(gid):
        raise ToolError("game_id must be a game UUID from games_on_date.")
    res = await hw.get(f"/games/{gid}")
    data = res.get("data") or {}
    game = data.get("game") or {}
    teams = []
    for t in data.get("teams") or []:
        teams.append({
            "abbreviation": t.get("abbreviation"), "full_name": t.get("full_name"), "pts": t.get("pts"),
            "won": t.get("won"), "venue": t.get("venue_split"),
            "players": [_slim(p, BOX_PLAYER_KEYS) for p in (t.get("players") or [])[:15]],
        })
    return {
        "game": _with_tip_et(_slim(game, ["season_type", "game_date_local", "status", "home_score", "away_score", "arena", "tipoff_utc"])),
        "teams": teams,
        "note": "teams[0] is the away team.",
    }


PROP_ROW_KEYS = ["name", "team", "opponent", "venue", "line", "over_price", "under_price", "open_line", "snapshot",
                 "last10", "season", "last_season", "matchup", "actual", "result"]


async def t_prop_board(hw: HardwoodClient, a: dict, ctx: ToolContext) -> dict:
    market = a.get("market", "pts")
    # Always send an explicit US-Eastern date. Left to Hardwood, a call made when today has no lines yet has returned the
    # previous day's finished board (seen at 12:41 AM ET), which reads like tonight's slate.
    params: Dict[str, Any] = {"market": market, "date": _check_date(a.get("date"), ctx)}
    res = await hw.get("/props", params)
    rows = res.get("data") or []
    if a.get("team"):
        team = (await hw.resolve_team(a["team"]))["abbreviation"]
        rows = [r for r in rows if team in (r.get("team"), r.get("opponent"))]
    if a.get("player"):
        needle = _norm(a["player"])
        rows = [r for r in rows if needle in _norm(r.get("name", ""))]
    rows = sorted(rows, key=lambda r: r.get("line") or 0, reverse=True)
    limit = max(1, min(int(a.get("limit") or 25), 60))
    wanted = [_slim(r, PROP_ROW_KEYS) for r in rows[:limit]]
    meta = res.get("meta") or {}

    def build(n: int) -> dict:
        return {
            "date": meta.get("date"), "market": meta.get("market_label") or market, "book": meta.get("book"),
            "total_props": len(rows), "shown": n, "props": wanted[:n],
            "games": [_with_tip_et(_slim(g, ["home", "away", "tipoff_utc", "status", "lines", "close_pulled"])) for g in (res.get("games") or [])],
            "prev_date": meta.get("prev_date"), "next_date": meta.get("next_date"), "notes": meta.get("notes"),
            "how_to_read": "last10/season = over/under/push counts against THIS line in games before this date. "
                           "matchup.rank 1 = the opponent allows the fewest of that stat to the player's position group "
                           "(the group's own totals, e.g. blk = blocks that group records).",
        }

    # Prop rows are ~500 chars each. Shrink the row count until the whole result fits, so the model always gets
    # valid JSON and an honest count (run_tool would otherwise slice the text mid-row).
    n = len(wanted)
    out = build(n)
    while n > 1 and len(_dump(out)) > MAX_RESULT_CHARS - 600:
        n = max(1, int(n * 0.8))
        out = build(n)
    if n < len(rows):
        out["note"] = (f"Showing {n} of {len(rows)} props (highest lines first). "
                       "Use the team or player filter, or ask for a specific market, to see the rest.")
    return out


async def t_player_props(hw: HardwoodClient, a: dict, ctx: ToolContext) -> dict:
    p = await hw.resolve_player(a["player"])
    res = await hw.get(f"/players/{p['id']}/props")
    return {"player": (res.get("meta") or {}).get("player") or p.get("full_name"), **(res.get("data") or {}),
            "book": (res.get("meta") or {}).get("book")}


async def t_matchup_ranks(hw: HardwoodClient, a: dict, ctx: ToolContext) -> dict:
    t = await hw.resolve_team(a["team"])
    res = await hw.get("/rankings/matchups", {"team_id": t["id"]})
    data = res.get("data") or {}
    out: Dict[str, Any] = {"defense": t["full_name"], "by_position": {}}
    for pos, rows in data.items():
        row = rows[0] if rows else None
        if row:
            out["by_position"][pos] = _slim(row, ["games", "allowed", "vs_avg", "rank", "label"])
    out["league_avg"] = res.get("league_avg")
    out["notes"] = (res.get("meta") or {}).get("notes")
    out["how_to_read"] = (
        "Each number is the OPPOSING position group's own per-game total in games against this defense: pts/reb/ast are what "
        "those players score, rebound and assist; stl and blk are steals and blocks THEY record; tov is turnovers THEY commit. "
        "rank 1 = the lowest total (fewest allowed); vs_avg > 0 = more than the league average. strong/weak labels are rank-based "
        "and written from a props view: 'weak' means opposing players tend to record MORE of that stat against this defense "
        "(for tov, stl and blk that is not necessarily bad defense).")
    return out


async def t_league_table(hw: HardwoodClient, a: dict, ctx: ToolContext) -> dict:
    kind = a.get("kind", "standings")
    season = a.get("season") or ctx.season
    if kind == "standings":
        res = await hw.get("/standings", {"season": season})
        data = res.get("data") or {}
        keep = ["rank", "abbreviation", "wins", "losses", "pct", "gb", "last10", "streak", "home", "road"]
        return {"season": season,
                "standings": {conf: [_slim(r, keep) for r in rows] for conf, rows in data.items()},
                "notes": (res.get("meta") or {}).get("notes")}
    if kind == "team_ratings":
        res = await hw.get("/rankings/teams", {"season": season, "scope": a.get("scope", "season"),
                                               "season_type": "regular"})
        keep = ["abbr", "gp", "w", "l", "pts", "opp_pts", "off_rtg", "def_rtg", "net_rtg", "pace", "ranks"]
        return {"season": season, "teams": [_slim(r, keep) for r in (res.get("data") or [])],
                "notes": (res.get("meta") or {}).get("notes"),
                "how_to_read": "Ratings and pace are estimates. Sorted by net rating. Rank 1 = best."}
    raise ToolError('kind must be "standings" or "team_ratings".')


TOOLS: Dict[str, Callable[[HardwoodClient, dict, ToolContext], Awaitable[dict]]] = {
    "find_player": t_find_player,
    "player_stats": t_player_stats,
    "team_stats": t_team_stats,
    "leaderboard": t_leaderboard,
    "games_on_date": t_games_on_date,
    "box_score": t_box_score,
    "prop_board": t_prop_board,
    "player_props": t_player_props,
    "matchup_ranks": t_matchup_ranks,
    "league_table": t_league_table,
}


# --------------------------------------------------------------------------- schemas shown to the model

_SEASON = {"type": "string", "description": "Season label such as 2026-27. Defaults to the current season."}
_SEASON_TYPE = {"type": "string", "enum": ["regular", "play_in", "playoffs", "all", "cup"],
                "description": "Defaults to regular. Types never mix unless 'all'."}
_SPLITS = {
    "type": "object",
    "description": "Optional filters, applied BEFORE the window (last10 + venue away = his last 10 road games).",
    "properties": {
        "venue": {"type": "string", "enum": ["home", "away"]},
        "player_rest": {"type": ["integer", "string"], "description": "Days since HIS last game: 0, 1, 2 or '3+'."},
        "player_b2b": {"type": "integer", "enum": [1, 2], "description": "Night 1 or 2 of HIS back-to-back."},
        "rest": {"type": ["integer", "string"], "description": "Days since the TEAM's last game: 0, 1, 2 or '3+'."},
        "b2b": {"type": "integer", "enum": [1, 2], "description": "Night 1 or 2 of the TEAM's back-to-back."},
        "national_tv": {"type": "string", "enum": ["major", "nba_tv", "local"]},
        "altitude": {"type": "boolean", "description": "Games at arenas 4,000 ft or higher (Denver, Utah)."},
    },
    "additionalProperties": False,
}

TOOL_SCHEMAS: List[dict] = [
    {
        "name": "find_player",
        "description": "Search players by name (accent- and suffix-insensitive). Only needed to disambiguate; the other tools accept names directly.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "include_retired": {"type": "boolean", "description": "Default false (active players only)."}},
            "required": ["query"]},
    },
    {
        "name": "player_stats",
        "description": (
            "Hardwood stats for one player: per-game averages and shooting splits for a season, last 5, last 10 or career, "
            "or a recent game log. Returns sample_size and notes. Use 'lines' to get over/under/push counts at specific "
            "prop lines over exactly the selected games (works for season, last5, last10, career), e.g. "
            "{'pts': 26.5} with scope last10 and splits {venue: 'away'}."),
        "input_schema": {"type": "object", "properties": {
            "player": {"type": "string", "description": "Player name (e.g. 'Nikola Jokic') or Hardwood id."},
            "scope": {"type": "string", "enum": ["season", "last5", "last10", "career", "game_log"]},
            "season": _SEASON, "season_type": _SEASON_TYPE, "splits": _SPLITS,
            "lines": {"type": "object", "additionalProperties": {"type": "number"},
                      "description": "market -> line. Markets: " + ", ".join(MARKETS) + "."},
            "game_log_limit": {"type": "integer", "description": "For game_log: how many recent games to return (default 10, max 30)."}},
            "required": ["player"]},
    },
    {
        "name": "team_stats",
        "description": "Hardwood stats for one team (box-score averages, est. offensive/defensive rating, record) for a season, last 5, last 10, or a game log. Accepts splits such as rest, b2b, venue.",
        "input_schema": {"type": "object", "properties": {
            "team": {"type": "string", "description": "Team name, nickname or 3-letter abbreviation."},
            "scope": {"type": "string", "enum": ["season", "last5", "last10", "game_log"]},
            "season": _SEASON, "season_type": _SEASON_TYPE, "splits": _SPLITS,
            "game_log_limit": {"type": "integer"}},
            "required": ["team"]},
    },
    {
        "name": "leaderboard",
        "description": "League leaders in a per-game stat for a season (qualifier: 70% of team games played).",
        "input_schema": {"type": "object", "properties": {
            "stat": {"type": "string", "enum": ["pts", "reb", "ast", "stl", "blk", "fg3m", "tov", "minutes", "plus_minus", "usg_pct"]},
            "season": _SEASON,
            "season_type": {"type": "string", "enum": ["regular", "playoffs", "all"]},
            "limit": {"type": "integer", "description": "1-50, default 10."}}},
    },
    {
        "name": "games_on_date",
        "description": "The scoreboard for one date (home venue's local date): matchups, status, scores, tip-off, national TV, and game ids for box_score. Defaults to today (US Eastern).",
        "input_schema": {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}}},
    },
    {
        "name": "box_score",
        "description": "Full box score for one game by id (get ids from games_on_date). Teams[0] is the away team.",
        "input_schema": {"type": "object", "properties": {"game_id": {"type": "string"}}, "required": ["game_id"]},
    },
    {
        "name": "prop_board",
        "description": (
            "DraftKings player-prop board for one US-Eastern date (default: today). If the date has no lines yet, the result says so and gives prev_date/next_date. "
            "Each row has the line, prices, opening line, over/under counts at that line for the last 10 games and the season, "
            "and the opponent's matchup rank for the player's position. Counts, not picks."),
        "input_schema": {"type": "object", "properties": {
            "market": {"type": "string", "enum": MARKETS, "description": "Default pts."},
            "date": {"type": "string", "description": "YYYY-MM-DD (Eastern)."},
            "team": {"type": "string", "description": "Only props for games involving this team."},
            "player": {"type": "string", "description": "Name filter."},
            "limit": {"type": "integer", "description": "Rows to return, sorted by line descending (default 25, max 60)."}}},
    },
    {
        "name": "player_props",
        "description": "One player's upcoming prop lines (all markets) plus his over/under record against every line Hardwood has for finished games.",
        "input_schema": {"type": "object", "properties": {"player": {"type": "string"}}, "required": ["player"]},
    },
    {
        "name": "matchup_ranks",
        "description": "What one team's DEFENSE allows per game to guards, forwards and centers in every prop market, with league rank (1 = allows the fewest) and vs-league-average.",
        "input_schema": {"type": "object", "properties": {"team": {"type": "string", "description": "The defending team."}}, "required": ["team"]},
    },
    {
        "name": "league_table",
        "description": "League-wide table: 'standings' (by conference) or 'team_ratings' (est. offensive/defensive/net rating and pace for all 30 teams).",
        "input_schema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["standings", "team_ratings"]},
            "season": _SEASON,
            "scope": {"type": "string", "enum": ["season", "last10"], "description": "team_ratings only."}},
            "required": ["kind"]},
    },
]


_H2H_PROPS = {
    "opponent": {"type": "string", "description": "Head-to-head: team name or abbreviation. Returns every game vs that team across recent seasons (newest first) plus a computed summary. Use this instead of scanning logs yourself. Implies scope game_log."},
    "seasons_back": {"type": "integer", "description": "With opponent: how many seasons to search back from the current one (default 5, max 10)."},
    "seasons": {"type": "array", "items": {"type": "string"}, "description": "With opponent: explicit season labels to search, e.g. ['2024-25','2023-24'] (overrides seasons_back)."},
}
for _t in TOOL_SCHEMAS:
    if _t["name"] in ("player_stats", "team_stats"):
        _t["input_schema"]["properties"].update(_H2H_PROPS)
        _t["description"] += " Set 'opponent' for a head-to-head: all games vs one team across seasons (season_type defaults to 'all', i.e. regular season plus playoffs)."


# --------------------------------------------------------------------------- dispatch


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _type_ok(value: Any, kind: Optional[str]) -> bool:
    """Loose JSON-schema type check: numeric strings are fine for numbers (models sometimes send "5")."""
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind in ("integer", "number"):
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return math.isfinite(value) and (kind == "number" or float(value).is_integer())
        if isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                return False
            return math.isfinite(number) and (kind == "number" or number.is_integer())
        return False
    return True


def _check_inputs(name: str, args: dict) -> Optional[str]:
    """Clear, early errors for missing, mistyped or out-of-enum inputs, before any API call is made.

    With this in front, a TypeError or KeyError raised inside a tool means Hardwood sent data the tool can't read,
    not that the model sent a bad request."""
    schema = next((t["input_schema"] for t in TOOL_SCHEMAS if t["name"] == name), {})
    for key in schema.get("required", []):
        if args.get(key) in (None, ""):
            return f"Missing required input {key!r}."
    for key, spec in schema.get("properties", {}).items():
        value = args.get(key)
        if value is None:
            continue
        if not _type_ok(value, spec.get("type")):
            return f"Input {key!r} must be of type {spec.get('type')}, got {type(value).__name__}."
        if spec.get("type") == "array" and not all(_type_ok(v, (spec.get("items") or {}).get("type")) for v in value):
            return f"Every item of {key!r} must be of type {(spec.get('items') or {}).get('type')}."
        allowed = spec.get("enum")
        if allowed and value not in allowed:
            return f"Invalid value {value!r} for {key!r}. Allowed values: {', '.join(allowed)}."
    return None


async def run_tool(hw: HardwoodClient, name: str, args: Any, ctx: ToolContext) -> Tuple[str, bool]:
    """Run one tool. Returns (json_text, ok). Never raises."""
    fn = TOOLS.get(name)
    if fn is None:
        return _dump({"error": f"Unknown tool {name!r}."}), False
    if not isinstance(args, dict):
        return _dump({"error": "Tool input must be an object."}), False
    problem = _check_inputs(name, args)
    if problem:
        return _dump({"error": problem}), False
    try:
        text, ok = _dump(await fn(hw, args, ctx)), True
    except ToolError as e:
        text, ok = _dump({"error": str(e)}), False
    except (KeyError, TypeError, AttributeError, IndexError, ArithmeticError) as e:
        # Inputs were checked above, so this means Hardwood's reply had a shape this tool can't read.
        text, ok = _dump({"error": f"Hardwood returned data this tool could not read ({type(e).__name__}). "
                                   "Say that the lookup failed rather than guessing; retrying the same call is unlikely to help."}), False
    except ValueError as e:
        text, ok = _dump({"error": f"Bad input for {name}: {e}."}), False
    except Exception as e:  # keep the loop alive; the model sees a generic failure
        text, ok = _dump({"error": f"Tool failed unexpectedly ({type(e).__name__})."}), False
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + " ...[truncated: result too large, narrow the request]"
    return text, ok


def describe_call(name: str, args: Any) -> str:
    """Short human label for the UI, e.g. player_stats(player=Jokic, scope=last10)."""
    if not isinstance(args, dict):
        return name
    parts = []
    for k, v in args.items():
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"{k}={s}")
    out = f"{name}({', '.join(parts)})"
    return out if len(out) <= 160 else out[:157] + "..."
