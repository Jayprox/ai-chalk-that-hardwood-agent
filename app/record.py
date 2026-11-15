"""The pregame recorder: writes one immutable forecast per Hardwood close-snapshot contract, before tip-off.

    python -m app.record [--date YYYY-MM-DD]      one tick (the scheduler calls this every few minutes)

Rules (docs/PROSPECTIVE_PROTOCOL.md, constants in app/protocol.py):
  * Only the worker's `close` rows count. A game is polled from CLOSE_WINDOW_MIN minutes before tip-off until tip-off.
  * Forecasts are FRESHLY computed at record time. The dashboard's `sims` cache is never read.
  * Inputs are the player's game logs dated BEFORE the game's date, retrieved once per board date at or after 08:00 ET and at most
    INPUT_MAX_AGE_HOURS before the forecast; the exact rows are saved in the ledger (content-addressed, hash-checked).
  * League parameters are one frozen snapshot per board date, estimated only from those same earlier-dated games, and saved.
  * A record is valid only if it is written before tip-off; anything else is logged in `rejections` and never repaired.
  * A later change of Hardwood's line or price is logged in `source_diffs`; the recorded contract is never changed or dropped.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from app import protocol as P
from app.baseline import baseline_draws
from app.calibration import apply as calibrate
from app.collector import FINAL_STATUSES, LIVE_STATUSES, _team
from app.config import ET
from app.ledger import Ledger, canon, parse_ts, ts
from app.signal import signal
from app.simulation import MARKET_STATS, League, Params, is_played, league_params, simulate_player

# Rejections that end a contract for the day. Everything else (a bad or early close row, a missing league snapshot, unavailable inputs) is
# logged once and re-evaluated at each tick, because the worker may still refresh the row before tip-off with a valid pull.
TERMINAL = {"season_type_not_expected", "fetched_after_tip", "insufficient_history", "stale_inputs", "recorded_after_tip", "unknown_market"}

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def norm_game(g: dict, date_et: str) -> Optional[dict]:
    gid = g.get("id") or g.get("game_id")
    if not gid:
        return None
    return {"game_id": str(gid), "date_et": date_et, "home": _team(g.get("home")), "away": _team(g.get("away")), "tipoff_utc": g.get("tipoff_utc"),
            "status": g.get("status"), "season_type": g.get("season_type")}


def pmf_of(draws: np.ndarray) -> List[float]:
    from app.sim import pmf_from_draws
    return pmf_from_draws(draws)


def forecast_contract(sim_draws: Dict[str, np.ndarray], base_draws: Dict[str, np.ndarray], market: str, line: float,
                      over_price: Any, under_price: Any, slope: float) -> dict:
    """Pure: the simulator and baseline distributions for one market and the agreement decision at the exact recorded line."""
    from app.sim import pmf_to_over_prob
    total = sum(sim_draws[k] for k in MARKET_STATS[market])
    btotal = sum(base_draws[k] for k in MARKET_STATS[market])
    pmf, bpmf = pmf_of(total), pmf_of(btotal)
    p_raw = pmf_to_over_prob(pmf, line)
    sim_side = None if p_raw is None or p_raw == 0.5 else ("over" if p_raw > 0.5 else "under")
    sig = signal(over_price, under_price)
    agree = None if sim_side is None or sig["side"] is None else int(sim_side == sig["side"])
    return {"market_p_over": sig["p_over"], "market_side": sig["side"], "sim_pmf": canon(pmf), "sim_mean": round(float(total.mean()), 3),
            "sim_sd": round(float(total.std()), 3), "sim_p_over_raw": p_raw, "sim_p_over_cal": calibrate(p_raw, 0.0, slope), "sim_side": sim_side,
            "agree": agree, "base_pmf": canon(bpmf), "base_mean": round(float(btotal.mean()), 3)}


# ---- fetching (read-only Hardwood calls)

async def get_props(hw: Any, date_et: str, concurrency: int = 5) -> Tuple[List[dict], List[str]]:
    sem = asyncio.Semaphore(concurrency)
    errors: List[str] = []

    async def one(m: str):
        async with sem:
            return m, await hw.get("/props", {"market": m, "date": date_et})

    rows: List[dict] = []
    for m, res in zip(P.MARKETS, await asyncio.gather(*(one(m) for m in P.MARKETS), return_exceptions=True)):
        if isinstance(res, BaseException):
            errors.append(f"props {m}: {type(res).__name__}")
            continue
        for r in (res[1].get("data") or []):
            if r.get("player_id") and r.get("game_id"):
                rows.append({**r, "market": m})
    return rows, errors


async def ensure_inputs(hw: Any, ledger: Ledger, date_et: str, season: str, pids: List[str], clock: Clock, concurrency: int = 5) -> List[str]:
    """Retrieve and save each player's earlier-dated game log once per board date. Returns error strings."""
    need = sorted(p for p in set(pids) if ledger.player_input(date_et, p) is None)
    if not need:
        return []
    sem = asyncio.Semaphore(concurrency)
    errors: List[str] = []

    async def one(pid: str):
        async with sem:
            res = await hw.post("/query", {"entity": "player", "id": pid, "scope": "game_log", "season": season, "season_type": "regular"})
            return pid, (res.get("data") or [])

    results = await asyncio.gather(*(one(p) for p in need), return_exceptions=True)
    t = clock()
    for pid, res in zip(need, results):
        if isinstance(res, BaseException):
            errors.append(f"log {pid}: {type(res).__name__}")
            continue
        games = sorted((g for g in res[1] if g.get("date") and g["date"] < date_et), key=lambda g: g["date"])    # earlier dates only, oldest first
        obj = {"player_id": pid, "date_et": date_et, "season": season, "retrieved_at": ts(t), "endpoint": "POST /query scope=game_log", "games": games}
        h = ledger.add_input("player_log", obj, ts(t))
        ledger.index_input(date_et, pid, h, ts(t), len(games))
    return errors


def ensure_league(ledger: Ledger, date_et: str, season: str, clock: Clock) -> Optional[dict]:
    """One frozen league snapshot per board date, built only from the saved earlier-dated logs of that date's players."""
    snap = ledger.league_snapshot(date_et)
    if snap:
        return snap
    logs, used = [], []
    for r in ledger._all("SELECT player_id, input_hash FROM input_index WHERE date_et=? ORDER BY player_id", (date_et,)):
        games = ledger.get_input(r["input_hash"])["games"]
        if sum(1 for g in games if is_played(g)) >= P.MIN_PRIOR_GAMES:
            logs.append(games)
            used.append([r["player_id"], r["input_hash"]])
    if len(logs) < P.LEAGUE_MIN_PLAYERS:
        return None
    lg = league_params(logs)
    t = clock()
    obj = {"date_et": date_et, "season": season, "created_at": ts(t), "n_players": len(logs), "players": used, "rate": lg.rate, "alpha": lg.alpha,
           "method": "league_params over players with >= MIN_PRIOR_GAMES played games dated before the board date"}
    h = ledger.add_input("league", obj, ts(t))
    ledger.add_league_snapshot(date_et, h, ts(t), len(logs))
    return ledger.league_snapshot(date_et)


# ---- validity of one close row (before any computation)

def check_row(row: dict, tip: Optional[datetime]) -> Optional[str]:
    if tip is None:
        return "no_tip_time"
    if row.get("market") not in P.MARKETS:
        return "unknown_market"
    if row.get("line") is None:
        return "bad_line"
    if signal(row.get("over_price"), row.get("under_price"))["p_over"] is None:
        return "bad_prices"
    fetched = parse_ts(row.get("fetched_at"))
    if fetched is None:
        return "no_fetched_at"
    if fetched >= tip:
        return "fetched_after_tip"
    if tip - fetched > timedelta(minutes=P.CLOSE_FETCH_MAX_BEFORE_TIP_MIN):
        return "fetched_too_early"
    return None


def fresh_enough(retrieved_at: Optional[str], league_at: Optional[str], computed: datetime, date_et: str, tip: datetime) -> bool:
    r, l = parse_ts(retrieved_at), parse_ts(league_at)
    if r is None or l is None:
        return False
    day_start = datetime.strptime(date_et, "%Y-%m-%d").replace(hour=P.INPUT_MIN_ET_HOUR, tzinfo=ET).astimezone(timezone.utc)
    cap = timedelta(hours=P.INPUT_MAX_AGE_HOURS)
    return all(day_start <= x < tip and timedelta(0) <= computed - x <= cap for x in (r, l))


def log_source_diffs(ledger: Ledger, rows: List[dict], observed_at: str) -> int:
    """Compare freshly observed Hardwood rows with the recorded contracts and log any difference. Never touches the record."""
    n = 0
    for r in rows:
        rec = ledger.record(str(r["game_id"]), str(r["player_id"]), r["market"])
        if rec is None:
            continue
        checks = (("line_changed", rec["line"], r.get("line")), ("over_price_changed", rec["over_price"], r.get("over_price")),
                  ("under_price_changed", rec["under_price"], r.get("under_price")), ("snapshot_changed", rec["snapshot"], r.get("snapshot")))
        for kind, was, now in checks:
            if now is not None and was != now and not (isinstance(was, (int, float)) and isinstance(now, (int, float)) and abs(was - now) < 1e-9):
                ledger.source_diff(observed_at, rec["game_id"], rec["player_id"], r["market"], kind, was, now)
                n += 1
    return n


# ---- the tick

async def ledger_tick(hw: Any, ledger: Ledger, season: str, *, clock: Optional[Clock] = None, params: Optional[Params] = None,
                      concurrency: int = 5, fp: Optional[dict] = None) -> dict:
    clock = clock or _utcnow
    started = clock()
    summary: Dict[str, Any] = {"games_seen": 0, "window_games": 0, "close_rows": 0, "recorded": 0, "already": 0, "rejected": {}, "errors": []}
    ok = True
    try:
        await _tick(hw, ledger, season, clock, params or Params(), concurrency, summary, fp)
    except Exception as e:  # noqa: BLE001
        ok = False
        summary["errors"].append(f"{type(e).__name__}: {e}")
    ledger.add_tick(ts(started), ts(clock()), ok and not summary["errors"], summary)
    return summary


async def _tick(hw: Any, ledger: Ledger, season: str, clock: Clock, params: Params, concurrency: int, summary: dict, fp: Optional[dict]) -> None:
    from app.provenance import fingerprint, params_dict
    from app.sim import _seed

    now = clock()
    et = now.astimezone(ET)
    today, tomorrow = et.strftime("%Y-%m-%d"), (et + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday = (et - timedelta(days=1)).strftime("%Y-%m-%d")
    games_today: List[dict] = []
    for d in (yesterday, today, tomorrow):                                # yesterday only keeps the status of late finishes current
        try:
            res = await hw.get("/games", {"date": d})
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(f"games {d}: {type(e).__name__}")
            continue
        for g in (res.get("data") or []):
            ng = norm_game(g, d)
            if ng:
                ledger.register_game(ng, ts(clock()))
                if d != yesterday:
                    summary["games_seen"] += 1
                if d == today:
                    games_today.append(ng)

    window: Dict[str, dict] = {}
    for g in games_today:
        tip = parse_ts(g.get("tipoff_utc"))
        if tip and tip - timedelta(minutes=P.CLOSE_WINDOW_MIN) <= now < tip and str(g.get("status") or "").strip().lower() not in FINAL_STATUSES | LIVE_STATUSES:
            window[g["game_id"]] = {**g, "tip": tip}
    summary["window_games"] = len(window)
    if not window:
        return

    rows, errs = await get_props(hw, today, concurrency)
    summary["errors"].extend(errs)
    observed = clock()
    obs_ts = ts(observed)
    for r in rows:
        if r.get("snapshot") == P.PRIMARY_SNAPSHOT and ledger.expected_game(str(r["game_id"])):
            tip = parse_ts(ledger.expected_game(str(r["game_id"]))["tipoff_utc"])
            ledger.add_universe(str(r["game_id"]), str(r["player_id"]), r["market"], today, obs_ts, "pregame" if tip and observed < tip else "late")
    summary["source_diffs"] = log_source_diffs(ledger, rows, obs_ts)

    def reject(reason: str, g: str, p: str = "", m: str = "", detail: Any = None) -> None:
        summary["rejected"][reason] = summary["rejected"].get(reason, 0) + 1
        ledger.reject(ts(clock()), reason, g, p, m, detail)

    cands: List[dict] = []
    for r in rows:
        gid = str(r["game_id"])
        if gid not in window or r.get("snapshot") != P.PRIMARY_SNAPSHOT:
            continue
        summary["close_rows"] += 1
        pid, m = str(r["player_id"]), r["market"]
        if ledger.has_record(gid, pid, m):
            summary["already"] += 1
            continue
        if any(x in TERMINAL for x in ledger.rejected_reasons(gid, pid, m)):
            continue
        g = window[gid]
        if g.get("season_type") not in P.SEASON_TYPES:
            reject("season_type_not_expected", gid, pid, m, g.get("season_type"))
            continue
        bad = check_row(r, g["tip"])
        if bad:
            reject(bad, gid, pid, m, {"fetched_at": r.get("fetched_at")})
            continue
        cands.append({**r, "tip": g["tip"], "game": g})
    if not cands:
        return

    board_pids = sorted({str(r["player_id"]) for r in rows})
    first_needed = board_pids if ledger.league_snapshot(today) is None else sorted({str(c["player_id"]) for c in cands})
    summary["errors"].extend(await ensure_inputs(hw, ledger, today, season, first_needed, clock, concurrency))
    league = ensure_league(ledger, today, season, clock)
    if league is None:
        for c in cands:
            reject("league_unavailable", str(c["game_id"]), str(c["player_id"]), c["market"])
        return
    lobj = ledger.get_input(league["input_hash"])
    lg = League(rate=lobj["rate"], alpha=lobj["alpha"])
    fp = {**(fp or fingerprint()), "params": params_dict(params)}        # the parameters actually used for this forecast
    by_pg: Dict[Tuple[str, str], List[dict]] = {}
    for c in cands:
        by_pg.setdefault((str(c["player_id"]), str(c["game_id"])), []).append(c)

    for (pid, gid), cs in by_pg.items():
        idx = ledger.player_input(today, pid)
        if idx is None:
            for c in cs:
                reject("inputs_unavailable", gid, pid, c["market"])
            continue
        computed = clock()
        tip = cs[0]["tip"]
        if not fresh_enough(idx["retrieved_at"], league["created_at"], computed, today, tip):
            for c in cs:
                reject("stale_inputs", gid, pid, c["market"], {"retrieved_at": idx["retrieved_at"], "league_created_at": league["created_at"]})
            continue
        prior = ledger.get_input(idx["input_hash"])["games"]
        draws = simulate_player(prior, lg, params, np.random.default_rng(_seed(params.seed, pid, gid)))
        base = baseline_draws(prior, lg, np.random.default_rng(_seed(params.seed + 101, pid, gid)), params.draws)
        if draws is None or base is None:
            for c in cs:
                reject("insufficient_history", gid, pid, c["market"])
            continue
        n_prior = sum(1 for g in prior if is_played(g))
        computed = clock()
        for c in cs:
            f = forecast_contract(draws, base, c["market"], float(c["line"]), c.get("over_price"), c.get("under_price"), fp["slope"])
            rec = {"game_id": gid, "player_id": pid, "market": c["market"], "date_et": today, "name": c.get("name"), "team": c.get("team"),
                   "line": float(c["line"]), "over_price": c.get("over_price"), "under_price": c.get("under_price"), "snapshot": c.get("snapshot"),
                   "fetched_at": c.get("fetched_at"), "observed_at": obs_ts, "tipoff_utc": c["game"].get("tipoff_utc"), **f, "n_prior": n_prior,
                   "input_hash": idx["input_hash"], "league_hash": league["input_hash"], "inputs_retrieved_at": idx["retrieved_at"],
                   "league_created_at": league["created_at"], "forecast_computed_at": ts(computed), "sim_version": fp["sim_version"],
                   "fit_sha256": fp["fit_sha256"], "params_json": canon(fp["params"]), "slope": fp["slope"], "code_sha": fp["code_sha"],
                   "git_commit": fp["git"]["commit"], "protocol_version": P.PROTOCOL_VERSION}
            written = clock()
            rec["recorded_at"] = ts(written)
            if written >= tip:
                reject("recorded_after_tip", gid, pid, c["market"], {"recorded_at": rec["recorded_at"]})
                continue
            if ledger.append_record(rec) is not None:
                summary["recorded"] += 1


async def main_async(args: argparse.Namespace) -> int:
    from app.config import current_season, load_settings, now_et
    from app.hardwood import HardwoodClient

    s = load_settings()
    if not s.hardwood_api_key:
        print("HARDWOOD_API_KEY is not set in .env")
        return 2
    ledger = Ledger(s.ledger_path, s.ledger_mirror_dir)
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    try:
        out = await ledger_tick(hw, ledger, current_season(now_et()))
    finally:
        await hw.aclose()
        ledger.close()
    print(canon(out))
    return 0 if not out["errors"] else 1


def main() -> None:
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args()
    sys.exit(asyncio.run(main_async(argparse.Namespace())))


if __name__ == "__main__":
    main()
