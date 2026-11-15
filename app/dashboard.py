"""Read-only dashboard API over the SQLite store: games overview, market tabs, game detail and prop detail.

Everything here is derived from stored market data. Signal strength (market tilt) and its within-market rank are unvalidated.
The 'strong' flag: top STRONG_PCT percent of its market by tilt AND the simulation leans the same way as the market. This rule is
unvalidated. Props in the top tier whose simulation disagrees, has too little history, or has not run yet are marked `top10` only.
The simulation's probability is raw (the backtests found it overconfident), so only its direction drives the rule.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from app.collector import _parse_utc
from app.scheduler import Schedule, next_collection, game_phase
from app.config import Settings, now_et
from app.signal import add_percentiles, is_void
from app.calibration import FITTED_ON
from app.sim import sim_view
from app.store import Store

ET = ZoneInfo("America/New_York")
STRONG_PCT = 90.0
MARKET_TABS = [("pts", "Points"), ("ast", "Assists"), ("reb", "Rebounds"), ("fg3m", "3PM"), ("pa", "P+A"), ("pr", "P+R"),
               ("ra", "A+R"), ("pra", "P+A+R"), ("stl", "Steals"), ("blk", "Blocks")]
MARKET_LABEL = dict(MARKET_TABS)


def tip_label(tipoff_utc: Any) -> str:
    dt = _parse_utc(tipoff_utc)
    if dt is None:
        return "tip-off unknown"
    et = dt.astimezone(ET)
    return f"{et.hour % 12 or 12}:{et.minute:02d} {'AM' if et.hour < 12 else 'PM'} ET"


def clock_label(iso: Any) -> Optional[str]:
    dt = _parse_utc(iso)
    if dt is None:
        return None
    et = dt.astimezone(ET)
    return f"{et.hour % 12 or 12}:{et.minute:02d} {'AM' if et.hour < 12 else 'PM'} ET"


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def resolve_date(store: Store, requested: Optional[str]) -> dict:
    """The date to show: the requested one (default today, ET) if it has data, else the latest date with data."""
    want = requested or now_et().strftime("%Y-%m-%d")
    if store.has_data(want):
        return {"date": want, "requested": want, "fallback": False}
    latest = store.latest_date()
    if latest:
        return {"date": latest, "requested": want, "fallback": True}
    return {"date": want, "requested": want, "fallback": False}


def _num(v: Any) -> Any:
    return int(v) if isinstance(v, float) and v == int(v) else v


def _price(v: Any) -> Optional[str]:
    if v is None:
        return None
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return f"+{n}" if n > 0 else str(n)


def _extra(row: dict) -> dict:
    try:
        return json.loads(row.get("extra") or "{}") or {}
    except (TypeError, ValueError):
        return {}


def shape_prop(p: dict, games: Dict[str, dict], hist: Optional[List[dict]] = None, sims: Optional[dict] = None) -> dict:
    g = games.get(p["game_id"]) or {}
    void = is_void(p.get("result"))
    live = not void and g.get("state") != "final"
    top10 = p.get("pct") is not None and p["pct"] >= STRONG_PCT and live
    sv = sim_view((sims or {}).get((p["player_id"], p["game_id"], p["market"])), p["line"], p["side"])
    strong = top10 and sv["status"] == "agree"
    return {
        "player_id": p["player_id"], "game_id": p["game_id"], "market": p["market"], "market_label": MARKET_LABEL.get(p["market"], p["market"]),
        "name": p["name"], "team": p["team"], "opponent": p["opponent"], "venue": p["venue"],
        "game": f"{g.get('away')} @ {g.get('home')}" if g else None, "tip": tip_label(g.get("tipoff_utc")) if g else None,
        "state": g.get("state"), "line": _num(p["line"]), "open_line": _num(p["open_line"]), "line_move": _num(p["line_move"]),
        "over_price": _price(p["over_price"]), "under_price": _price(p["under_price"]), "snapshot_kind": p["snapshot_kind"],
        "p_over": p["p_over"], "side": (p["side"] or "").upper() or None, "tilt": p["strength"], "pct": p["pct"],
        "strong": strong, "top10": top10, "sim": sv["status"], "sim_p_over": sv["p_over"], "sim_p_over_raw": sv["p_over_raw"], "sim_side": sv["side"], "sim_mean": sv["mean"], "result": p["result"], "actual": _num(p["actual"]), "void": void, "active": bool(p["active"]),
        "spark": [h["strength"] for h in (hist or []) if h["strength"] is not None],
    }


def _games_by_id(store: Store, date: str) -> Dict[str, dict]:
    return {g["game_id"]: g for g in store.games_for(date)}


def build_overview(store: Store, date_info: dict, sched: Optional[Schedule] = None, now: Optional[datetime] = None) -> dict:
    date = date_info["date"]
    now = now or datetime.now(timezone.utc)
    nxt = next_collection(store, now, sched, date) if sched else None
    games = _games_by_id(store, date)
    props = add_percentiles(store.props(date))
    sims = store.sims_for(date)
    shaped = [shape_prop(p, games, sims=sims) for p in props]
    by_game: Dict[str, List[dict]] = {}
    for p in shaped:
        by_game.setdefault(p["game_id"], []).append(p)
    cards = []
    for g in games.values():
        ps = [p for p in by_game.get(g["game_id"], [])]
        ranked = [p for p in ps if p["pct"] is not None and not p["void"]]
        phase = game_phase(g, now, sched) if sched else g["state"]
        if phase in ("tipped", "final", "abandoned") or not nxt:
            refresh = "Lines closed" if phase == "tipped" else ""
        else:
            refresh = ("Refreshing now" if nxt["due_now"] else f"Next about {clock_label(_iso_utc(nxt['next_due']))}") if not nxt["settled"] else ""
        cards.append({"game_id": g["game_id"], "away": g["away"], "home": g["home"], "tip": tip_label(g["tipoff_utc"]), "state": g["state"], "refresh": refresh,
                      "props": len(ps), "strong": sum(1 for p in ps if p["strong"]), "updated": clock_label(g["updated_at"]),
                      "top": [{k: p[k] for k in ("name", "market_label", "side", "line", "pct", "tilt", "strong")} for p in ranked[:3]]})
    tabs = [{"key": "games", "label": "Games", "count": len(cards), "strong": 0}]
    for key, label in MARKET_TABS:
        ps = [p for p in shaped if p["market"] == key]
        tabs.append({"key": key, "label": label, "count": len(ps), "strong": sum(1 for p in ps if p["strong"])})
    run = store.last_run()
    return {**date_info, "games": cards, "tabs": tabs, "props_total": len(shaped),
            "last_run": {"at": clock_label(run["finished_at"]), "ok": bool(run["ok"]), "date": run["date_et"]} if run else None,
            "next_run": clock_label(_iso_utc(nxt["next_due"])) if nxt and nxt["next_due"] else None,
            "scheduler": ({"stale": nxt["stale"], "settled": nxt["settled"], "due_now": nxt["due_now"], "interval_min": nxt["interval_min"]} if nxt else None),
            "rule": {"strong_pct": STRONG_PCT, "sim": "required", "sims": sum(1 for p in shaped if p["sim"] in ("agree", "disagree")),
                     "sims_pending": sum(1 for p in shaped if p["sim"] == "pending" and p["top10"])}}


def build_market(store: Store, date_info: dict, market: str) -> dict:
    if market not in MARKET_LABEL:
        raise HTTPException(status_code=404, detail="Unknown market.")
    date = date_info["date"]
    games = _games_by_id(store, date)
    hist = store.histories(date, market)
    props = add_percentiles(store.props(date, market))
    sims = store.sims_for(date)
    shaped = [shape_prop(p, games, hist.get((p["player_id"], p["game_id"])), sims) for p in props]
    return {**date_info, "market": market, "label": MARKET_LABEL[market], "props": shaped, "strong": sum(1 for p in shaped if p["strong"]),
            "games": [{"game_id": g["game_id"], "label": f"{g['away']} @ {g['home']}", "state": g["state"]} for g in games.values()]}


def build_game(store: Store, date_info: dict, game_id: str) -> dict:
    g = store.game(game_id)
    if not g:
        raise HTTPException(status_code=404, detail="Unknown game.")
    games = {g["game_id"]: g}
    pool = add_percentiles(store.props(g["date_et"]))                       # ranks are within the whole market, not this game
    sims = store.sims_for(g["date_et"])
    shaped = [shape_prop(p, games, sims=sims) for p in pool if p["game_id"] == game_id]
    return {"date": g["date_et"], "game": {"game_id": game_id, "away": g["away"], "home": g["home"], "tip": tip_label(g["tipoff_utc"]),
            "state": g["state"], "status": g["status"], "props_open_at": clock_label(g["props_open_at"]), "props_close_at": clock_label(g["props_close_at"]),
            "home_score": g["home_score"], "away_score": g["away_score"]},
            "props": shaped, "strong": sum(1 for p in shaped if p["strong"]), "markets": [{"key": k, "label": l} for k, l in MARKET_TABS]}


def summary_text(p: dict, extra: dict, sim: Optional[dict] = None) -> List[dict]:
    """Plain, deterministic notes built only from stored numbers. The model-written write-up arrives in a later milestone."""
    out: List[dict] = []
    side = (p["side"] or "").lower()
    if p["p_over"] is not None and p["tilt"] is not None:
        mv = p["line_move"]
        move = "has not moved since the open" if not mv else f"moved {'up' if mv > 0 else 'down'} {abs(mv):g} from {p['open_line']}"
        out.append({"title": "Market read", "text": f"The line is {p['line']} and {move}. Removing the margin, the prices imply a {100 * p['p_over']:.1f}% chance of the over, "
                    f"so the market leans {side} by {p['tilt']:.1f} points. Among this date's {p['market_label']} props that ranks at the {p['pct']:.1f}th percentile of tilt."})
    l10 = extra.get("last10")
    if isinstance(l10, dict) and l10.get("over") is not None and l10.get("under") is not None:
        out.append({"title": "Recent form", "text": f"Against this line, the last ten games went {l10['over']} over and {l10['under']} under."})
    m = extra.get("matchup")
    if isinstance(m, dict) and m.get("rank") is not None and m.get("of"):
        vs = f" ({m['vs_avg']:+g} vs the league average)" if isinstance(m.get("vs_avg"), (int, float)) else ""
        out.append({"title": "Matchup", "text": f"The opponent ranks {m['rank']} of {m['of']} against this stat{vs}."})
    if sim and sim.get("p_over") is not None:
        out.append({"title": "Simulation", "text": f"Simulated from {sim['n_prior']} earlier games (minutes times per-minute rates): about {sim['mean']:.1f} with a spread of {sim['sd']:.1f}. "
                    f"It leans {sim['side'].lower()}{', the same way as the market' if sim['side'] == (p['side'] or '') else ', against the market'}. "
                    f"The raw simulation says {100 * sim['p_over_raw']:.0f}% for the over, but raw simulation probabilities were overconfident in backtests, "
                    f"so the page shows a shrunk {100 * sim['p_over']:.0f}% (fitted on {FITTED_ON}). Read the direction more than the number."})
    elif p.get("sim") == "none":
        out.append({"title": "Simulation", "text": "Not enough earlier games this season to simulate this player."})
    elif p.get("sim") == "pending":
        out.append({"title": "Simulation", "text": "Not run yet. It runs after the next collection."})
    out.append({"title": "Availability", "text": "Not checked yet. Who is out, and the source, will appear here once the availability researcher is added."})
    out.append({"title": "Caveats", "text": "Signal strength reads the sportsbook price. It is not a validated probability of hitting, and the simulation has not shown skill beyond the price. The green border (high lean) marks where the market is most confident and the sim agrees; it is an unvalidated rule, not a value signal."})
    return out


def build_prop(store: Store, player_id: str, game_id: str, market: str) -> dict:
    row = store.prop(player_id, game_id, market)
    if not row:
        raise HTTPException(status_code=404, detail="Unknown prop.")
    games = _games_by_id(store, row["date_et"])
    pool = add_percentiles(store.props(row["date_et"], market, active_only=False))
    ranked = next((p for p in pool if p["player_id"] == player_id and p["game_id"] == game_id), row)
    hist = store.history(player_id, game_id, market)
    srow = store.sims_for(row["date_et"]).get((player_id, game_id, market))
    shaped = shape_prop(ranked, games, hist, {(player_id, game_id, market): srow} if srow else None)
    sim = None
    if srow and srow["status"] == "ok" and srow.get("pmf"):
        sim = {"pmf": json.loads(srow["pmf"]), "mean": srow["mean"], "sd": srow["sd"], "n_prior": srow["n_prior"], "p_over": shaped["sim_p_over"], "p_over_raw": shaped["sim_p_over_raw"],
               "side": shaped["sim_side"], "line": shaped["line"]}
    return {"date": row["date_et"], "prop": shaped, "extra": _extra(row), "sim": sim,
            "history": [{"at": clock_label(h["collected_at"]), "t": h["collected_at"], "line": _num(h["line"]), "over_price": _price(h["over_price"]),
                         "under_price": _price(h["under_price"]), "p_over": h["p_over"], "tilt": h["strength"], "kind": h["snapshot_kind"]} for h in hist],
            "notes": summary_text(shaped, _extra(row), sim)}


def make_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/dash")
    sched = Schedule.from_settings(settings)

    def store_dep() -> Iterator[Store]:
        s = Store(settings.db_path)
        try:
            yield s
        finally:
            s.close()

    def date_dep(date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"), store: Store = Depends(store_dep)) -> dict:
        return resolve_date(store, date)

    @router.get("/overview")
    def overview(store: Store = Depends(store_dep), d: dict = Depends(date_dep)) -> dict:
        return build_overview(store, d, sched)

    @router.get("/market/{market}")
    def market(market: str, store: Store = Depends(store_dep), d: dict = Depends(date_dep)) -> dict:
        return build_market(store, d, market)

    @router.get("/game/{game_id}")
    def game(game_id: str, store: Store = Depends(store_dep), d: dict = Depends(date_dep)) -> dict:
        return build_game(store, d, game_id)

    @router.get("/prop/{player_id}/{game_id}/{market}")
    def prop(player_id: str, game_id: str, market: str, store: Store = Depends(store_dep)) -> dict:
        return build_prop(store, player_id, game_id, market)

    return router
