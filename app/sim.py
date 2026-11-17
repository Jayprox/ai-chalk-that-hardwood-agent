"""Simulation enrichment: a minutes-times-rate distribution for every prop on a date's board.

    python -m app.sim [--date YYYY-MM-DD] [--db path]

For each (player, game) with props that is not tipped yet, load the player's season game log (one Hardwood call, no model calls,
no cost), simulate the game from his EARLIER games only, and store the distribution of every market's stat as a small pmf. The
probability of the over is then recomputed from the stored pmf whenever the line moves, with no new calls.

The backtests found this simulation overconfident and adding no measurable skill beyond the market price, so the dashboard shows
only its direction and a raw probability labeled as such, and uses it for one thing: the 'strong' border needs the simulation to
lean the same way as the market.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from app.calibration import SLOPE, apply as calibrate
from app.collector import _iso, game_state
from app.store import Store

LEAGUE_FULL_MIN_PLAYERS = 40    # refresh the stored league parameters when at least this many players are loaded
LEAGUE_SMALL_MIN_PLAYERS = 10   # fewest players we will estimate league parameters from
MIN_PRIOR_GAMES = 5


# ---- pure helpers (no numpy), used by the dashboard

def pmf_to_over_prob(pmf: List[float], line: Any) -> Optional[float]:
    """P(stat > line) given the result is not a push. None when the line can only push or the pmf is empty."""
    try:
        line = float(line)
    except (TypeError, ValueError):
        return None
    if not pmf:
        return None
    over = sum(p for v, p in enumerate(pmf) if v > line)
    push = pmf[int(line)] if line.is_integer() and 0 <= int(line) < len(pmf) else 0.0
    if push >= 0.999999:
        return None
    return over / (1.0 - push)


def sim_view(sim_row: Optional[dict], line: Any, market_side: Optional[str]) -> dict:
    """What the dashboard shows for one prop: status pending | none | agree | disagree, the recalibrated probability, the raw one and the direction."""
    import json
    if sim_row is None:
        return {"status": "pending", "p_over": None, "p_over_raw": None, "side": None, "mean": None, "sd": None, "n_prior": None}
    if sim_row["status"] != "ok" or not sim_row.get("pmf"):
        return {"status": "none", "p_over": None, "p_over_raw": None, "side": None, "mean": None, "sd": None, "n_prior": sim_row.get("n_prior")}
    p = pmf_to_over_prob(json.loads(sim_row["pmf"]), line)
    side = None if p is None or p == 0.5 else ("OVER" if p > 0.5 else "UNDER")
    status = "none" if side is None else ("agree" if market_side and side == market_side.upper() else "disagree")
    return {"status": status, "p_over": calibrate(p, 0.0, SLOPE), "p_over_raw": p, "side": side, "mean": sim_row["mean"], "sd": sim_row["sd"], "n_prior": sim_row["n_prior"]}


# ---- enrichment

def sim_version() -> str:
    """Identifies the simulation model in use (fitted minutes model, spreads, calibration-free parameters). Stored sims from another
    version are recomputed, so a refit never leaves old distributions on the board."""
    import hashlib
    from app import minutes_model
    from app.simulation import Params
    p = Params()
    blob = json.dumps({"fit": minutes_model.FIT_PATH.read_text() if minutes_model.FIT_PATH.exists() else "", "prior": p.prior_minutes,
                       "min_sd": p.min_sd, "min_prior": p.min_prior, "last": p.last_cap_minutes, "code": "order-fix-1"}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def pmf_from_draws(draws) -> List[float]:
    import numpy as np
    counts = np.bincount(np.asarray(draws).astype(int))
    pmf = counts / counts.sum()
    return [round(float(x), 5) for x in pmf]


def _seed(base: int, player_id: str, game_id: str) -> int:
    return base + zlib.crc32(f"{player_id}|{game_id}".encode())


async def enrich(hw: Any, store: Store, date_et: str, season: str, now: Optional[datetime] = None, params: Any = None,
                 concurrency: int = 5) -> dict:
    import numpy as np
    from app.simulation import MARKET_STATS, Params, is_played, league_params, simulate_player

    now = now or datetime.now(timezone.utc)
    stamp = _iso(now)
    params = params or Params()
    games = {g["game_id"]: g for g in store.games_for(date_et)}
    version = sim_version()
    if store.get_meta("sim_version") != version:                  # a refit or parameter change: drop stored distributions
        with store.tx() as c:
            c.execute("DELETE FROM sims")
        store.set_meta("sim_version", version, stamp)
    existing = store.sims_for(date_et)
    need: Dict[Tuple[str, str], Set[str]] = {}
    skipped_started = 0
    for p in store.props(date_et):
        if p["market"] not in MARKET_STATS:
            continue
        g = games.get(p["game_id"])
        if g is None or game_state(g["status"], g["tipoff_utc"], now) in ("tipped", "final"):
            skipped_started += 1
            continue
        if (p["player_id"], p["game_id"], p["market"]) not in existing:
            need.setdefault((p["player_id"], p["game_id"]), set()).add(p["market"])
    summary: Dict[str, Any] = {"date": date_et, "pairs": len(need), "players_loaded": 0, "sims": 0, "no_sim": 0, "errors": [],
                               "skipped_started": skipped_started, "league": None, "warnings": []}
    if not need:
        return summary

    pids = sorted({pid for pid, _g in need})
    sem = asyncio.Semaphore(concurrency)

    async def one(pid: str):
        async with sem:
            res = await hw.post("/query", {"entity": "player", "id": pid, "scope": "game_log", "season": season, "season_type": "regular"})
            return pid, (res.get("data") or [])

    loaded: Dict[str, List[dict]] = {}
    for pid, res in zip(pids, await asyncio.gather(*(one(p) for p in pids), return_exceptions=True)):
        if isinstance(res, BaseException):
            summary["errors"].append(f"log {pid}: {type(res).__name__}")
        else:
            loaded[pid] = res[1]
    summary["players_loaded"] = len(loaded)

    usable = [logs for logs in loaded.values() if sum(1 for g in logs if is_played(g)) >= MIN_PRIOR_GAMES]
    key = f"league:{season}"
    stored = store.get_meta(key)
    if len(usable) >= LEAGUE_FULL_MIN_PLAYERS or (stored is None and len(usable) >= LEAGUE_SMALL_MIN_PLAYERS):
        from app.simulation import League
        lg = league_params(usable)
        store.set_meta(key, {"rate": lg.rate, "alpha": lg.alpha, "players": len(usable), "as_of": date_et}, stamp)
        summary["league"] = f"estimated from {len(usable)} players"
    elif stored is not None:
        from app.simulation import League
        lg = League(rate=stored["rate"], alpha=stored["alpha"])
        summary["league"] = f"stored ({stored['players']} players, as of {stored['as_of']})"
    else:
        summary["warnings"].append(f"only {len(usable)} players with {MIN_PRIOR_GAMES}+ games and no stored league parameters; simulation skipped")
        return summary

    rows: List[dict] = []
    for (pid, gid), markets in need.items():
        logs = loaded.get(pid)
        if logs is None:
            continue                                                    # fetch failed: try again next time
        prior = sorted((g for g in logs if g.get("date") and g["date"] < date_et), key=lambda g: g["date"])
        n_prior = sum(1 for g in prior if is_played(g))
        draws = simulate_player(prior, lg, params, np.random.default_rng(_seed(params.seed, pid, gid)))
        for market in markets:
            base = {"player_id": pid, "game_id": gid, "market": market, "date_et": date_et, "n_prior": n_prior, "computed_at": stamp}
            if draws is None:
                rows.append({**base, "status": "none", "mean": None, "sd": None, "pmf": None})
                summary["no_sim"] += 1
                continue
            total = sum(draws[k] for k in MARKET_STATS[market])
            rows.append({**base, "status": "ok", "mean": round(float(total.mean()), 3), "sd": round(float(total.std()), 3),
                         "pmf": json.dumps(pmf_from_draws(total))})
            summary["sims"] += 1
    store.upsert_sims(rows)
    return summary


async def main_async(args: argparse.Namespace) -> int:
    from app.config import current_season, load_settings, now_et
    from app.hardwood import HardwoodClient

    s = load_settings()
    if not s.hardwood_api_key:
        print("HARDWOOD_API_KEY is not set in .env")
        return 2
    date = args.date or now_et().strftime("%Y-%m-%d")
    store = Store(args.db or s.db_path)
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    try:
        out = await enrich(hw, store, date, current_season(now_et()))
    finally:
        await hw.aclose()
        store.close()
    print(f"Simulated {out['date']}: {out['sims']} prop distributions for {out['pairs']} player-games ({out['players_loaded']} game logs loaded), "
          f"{out['no_sim']} without enough earlier games, {out['skipped_started']} already started. League parameters: {out['league']}.")
    for w in out["warnings"] + out["errors"]:
        print("  note:", w)
    return 0 if not out["errors"] else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date")
    ap.add_argument("--db")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
