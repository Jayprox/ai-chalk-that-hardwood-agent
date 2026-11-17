"""Settlement: turns recorded forecasts into results, written to the separate `settlements` table. Ledger rows are never touched.

Rules (constants in app/protocol.py):
  * The threshold is the exact `line` saved in the ledger row. A later Hardwood line or price change is logged elsewhere (source_diffs)
    and never changes a settlement.
  * actual = sum of the market's stats in the player's game-log row (or, if that row is missing, his box-score row) for that game, read
    after the game is final. Every required statistic must be present and numeric (a real 0 is valid); a missing component, including in a
    combo market, leaves the contract UNRESOLVED. Nothing is ever defaulted to zero.
  * actual == line  -> status `push` (actual kept, no over/under outcome).
  * DNP rule. Confirmed no participation -> `void_dnp` and the contract is void. Missing information -> unresolved (no settlement row until
    it can be decided; counted as `unresolved` once matured). A DNP is never scored as zero production or as a loss.
      Sufficient evidence of no participation: an explicit DNP / inactive flag on the player's box-score row (matched by player id) or log
      row, or a manual confirmation against an authoritative source saved in `verifications` (scripts/confirm_dnp.py).
      NOT sufficient: absence from the game log, missing minutes, a displayed 0 minutes (it may be rounded), or a box score that lists
      many players. Those stay unresolved for manual confirmation.
      Evidence of participation: minutes above 0 or any nonzero required statistic.
  * the game is not final ABANDON_HOURS after tip-off -> `void_not_played`.
  * statistic corrections: games are re-read for RECHECK_DAYS after tip-off. A changed (status, actual) is appended as a new version;
    the latest version wins and the history is kept.
Settlement reads only box-score and game-log facts after tip-off; it never feeds anything back into a forecast.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app import protocol as P
from app.collector import FINAL_STATUSES
from app.ledger import Ledger, parse_ts, ts
from app.simulation import MARKET_STATS

Clock = Callable[[], datetime]


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def actual_for(market: str, row: Optional[dict]) -> Optional[float]:
    """Sum of the market's statistics, or None if any required statistic is missing or not numeric. A genuine 0 is valid."""
    if not row or market not in MARKET_STATS:
        return None
    vals = [_num(row.get(k)) for k in MARKET_STATS[market]]
    return None if any(v is None for v in vals) else float(sum(vals))


def _played(row: dict) -> bool:
    """Evidence of participation: minutes above 0 or any nonzero statistic."""
    if (_num(row.get("minutes")) or 0) > 0:
        return True
    return any((_num(row.get(k)) or 0) > 0 for k in ("pts", "reb", "ast", "fg3m", "stl", "blk"))


def _explicit_dnp(row: Optional[dict]) -> Optional[dict]:
    if row and row.get("dnp") is True:
        return {"dnp": True, "dnp_reason": row.get("dnp_reason")}
    return None


def box_player(box: Optional[dict], player_id: str) -> Optional[dict]:
    for t in (((box or {}).get("data") or {}).get("teams")) or []:
        for p in t.get("players") or []:
            if str(p.get("player_id")) == str(player_id):
                return p
    return None


def decide_result(rec: dict, log_row: Optional[dict], game: dict, box: Optional[dict], now: datetime,
                  verification: Optional[dict] = None) -> Optional[Tuple[str, Optional[float], Optional[str], dict]]:
    """Pure. Returns (status, actual, outcome, detail), or None if the contract cannot be decided (it stays unresolved)."""
    tip = parse_ts(rec["tipoff_utc"])
    status = str(game.get("status") or "").strip().lower()
    if tip is None:
        return None
    if status not in FINAL_STATUSES:
        if now - tip >= timedelta(hours=P.ABANDON_HOURS):
            return "void_not_played", None, None, {"reason": "game not final", "game_status": status}
        return None
    brow = box_player(box, rec["player_id"])
    # 1. participation established: settle from the first row that has every required statistic
    for source, row in (("game_log", log_row), ("box_score", brow)):
        if row and _played(row):
            actual = actual_for(rec["market"], row)
            if actual is None:
                continue                                                  # a required statistic is missing in this row: try the other source
            line = float(rec["line"])
            detail = {"source": source, "minutes": row.get("minutes")}
            if actual == line:
                return "push", actual, None, detail
            return "settled", actual, "over" if actual > line else "under", detail
    if (log_row and _played(log_row)) or (brow and _played(brow)):
        return None                                                       # he played but no source has all the statistics: unresolved
    # 2. confirmed no participation
    for source, row in (("game_log", log_row), ("box_score", brow)):
        ev = _explicit_dnp(row)
        if ev:
            return "void_dnp", None, None, {"reason": f"explicit DNP flag in the {source}", **ev}
    if verification and verification.get("status") == "confirmed_dnp":
        return "void_dnp", None, None, {"reason": "manual confirmation", "source": verification["source"], "note": verification.get("note"),
                                        "checked_at": verification["checked_at"]}
    return None                                                           # missing rows, missing or zero minutes: not proof; manual confirmation needed


async def settle_once(hw: Any, ledger: Ledger, season: str, *, clock: Optional[Clock] = None, concurrency: int = 5) -> dict:
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = clock()
    summary: Dict[str, Any] = {"games": 0, "settled": 0, "changed": 0, "unchanged": 0, "undecided": 0, "errors": []}
    recs = [r for r in ledger.records() if (parse_ts(r["tipoff_utc"]) or now) <= now]
    recs = [r for r in recs if now - parse_ts(r["tipoff_utc"]) <= timedelta(days=P.RECHECK_DAYS)]
    by_game: Dict[str, List[dict]] = {}
    for r in recs:
        by_game.setdefault(r["game_id"], []).append(r)
    sem = asyncio.Semaphore(concurrency)

    async def game_info(gid: str):
        async with sem:
            return gid, await hw.get(f"/games/{gid}")

    async def log_of(pid: str):
        async with sem:
            res = await hw.post("/query", {"entity": "player", "id": pid, "scope": "game_log", "season": season, "season_type": "regular"})
            return pid, (res.get("data") or [])

    gids = sorted(by_game)
    infos: Dict[str, dict] = {}
    for gid, res in zip(gids, await asyncio.gather(*(game_info(g) for g in gids), return_exceptions=True)):
        if isinstance(res, BaseException):
            summary["errors"].append(f"game {gid}: {type(res).__name__}")
        else:
            infos[gid] = res[1]
    summary["games"] = len(infos)

    pids = sorted({r["player_id"] for g in infos for r in by_game[g]})
    logs: Dict[str, List[dict]] = {}
    for pid, res in zip(pids, await asyncio.gather(*(log_of(p) for p in pids), return_exceptions=True)):
        if isinstance(res, BaseException):
            summary["errors"].append(f"log {pid}: {type(res).__name__}")
        else:
            logs[pid] = res[1]

    for gid, box in infos.items():
        game = ((box.get("data") or {}).get("game")) or {}
        if game.get("status"):                                              # keeps the manifest's game status current for coverage (final / postponed)
            ledger.register_game({"game_id": gid, "date_et": by_game[gid][0]["date_et"], "status": game["status"]}, ts(now))
        for rec in by_game[gid]:
            if rec["player_id"] not in logs:
                continue
            row = next((g for g in logs[rec["player_id"]] if str(g.get("game_id")) == gid), None)
            res = decide_result(rec, row, game, box, now, ledger.latest_verification(rec["game_id"], rec["player_id"]))
            if res is None:
                summary["undecided"] += 1
                continue
            status, actual, outcome, detail = res
            last = ledger.latest_settlement(rec["seq"])
            if last and last["status"] == status and last["actual"] == actual and last["outcome"] == outcome:
                summary["unchanged"] += 1
                continue
            ledger.add_settlement(rec["seq"], ts(now), status, actual, outcome, "hardwood game_log + box", detail)
            summary["changed" if last else "settled"] += 1
    ledger.add_settle_run(ts(now), summary)
    return summary


def latest_results(ledger: Ledger, records: List[dict]) -> Dict[int, Optional[dict]]:
    """seq -> latest settlement row (or None)."""
    return {r["seq"]: ledger.latest_settlement(r["seq"]) for r in records}


def settled_rows(ledger: Ledger, game_ids: List[str], counted_only: bool = True) -> List[dict]:
    """Rows for the analysis: every recorded contract of the given games with its latest settlement. Unsettled contracts are included with
    status 'unresolved' so the analysis input shows them; the analysis itself uses only settled and push rows."""
    import json
    out: List[dict] = []
    wanted = set(game_ids)
    for r in ledger.records("freeze_id IS NOT NULL" if counted_only else "1=1"):
        if r["game_id"] not in wanted:
            continue
        s = ledger.latest_settlement(r["seq"])
        out.append({"seq": r["seq"], "player_id": r["player_id"], "game_id": r["game_id"], "market": r["market"], "line": r["line"],
                    "status": s["status"] if s else "unresolved", "actual": s["actual"] if s else None, "outcome": s["outcome"] if s else None,
                    "market_p_over": r["market_p_over"], "market_side": r["market_side"], "over_price": r["over_price"], "under_price": r["under_price"],
                    "agree": r["agree"], "sim_pmf": json.loads(r["sim_pmf"]), "base_pmf": json.loads(r["base_pmf"])})
    return out
