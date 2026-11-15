"""Market-price signal: turn the over and under prices into a probability and a 'signal strength'.

Signal strength (unvalidated) is how far the sportsbook's own price leans off 50/50, in percentage points, on the side it
favors. It is descriptive: the Stage A2 backtest found the market price is the strongest signal we have and that our
simulation adds no measurable skill beyond it. It is not a validated probability of hitting.
"""
from __future__ import annotations

from typing import Any, Optional


def american_to_prob(price: Any) -> Optional[float]:
    """American odds -> implied probability (with the book's margin still in it)."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p == 0 or p != p:
        return None
    return 100.0 / (p + 100.0) if p > 0 else -p / (-p + 100.0)


def devig(over_price: Any, under_price: Any) -> Optional[float]:
    """Probability of the over after removing the margin by proportional scaling. None if either price is unusable."""
    o, u = american_to_prob(over_price), american_to_prob(under_price)
    if o is None or u is None or o + u <= 0:
        return None
    return o / (o + u)


def signal(over_price: Any, under_price: Any) -> dict:
    """{'p_over', 'side', 'strength'}; strength = percentage points the favored side sits above 50%. All None without prices."""
    p = devig(over_price, under_price)
    if p is None:
        return {"p_over": None, "side": None, "strength": None}
    side = "over" if p > 0.5 else "under" if p < 0.5 else None
    return {"p_over": p, "side": side, "strength": round(abs(p - 0.5) * 100.0, 3)}


def line_move(line: Any, open_line: Any) -> Optional[float]:
    try:
        return round(float(line) - float(open_line), 3)
    except (TypeError, ValueError):
        return None


VOID_RESULTS = {"dnp", "push", "void"}


def is_void(result: Any) -> bool:
    """A prop that settles as no action (player did not play, or a push) is excluded from any hit-rate."""
    return str(result or "").strip().lower() in VOID_RESULTS


def add_percentiles(rows: list) -> list:
    """Add 'pct' (0-100) to each row: its signal strength ranked within its own market, ties share the mid-rank.

    Raw strength is biased toward low-count markets (a 0.5 line on a player who rarely hits it is priced -400), so cards are
    ordered and the strong rule is applied on this within-market rank instead. Rows without a strength get pct None.
    Mutates and returns the rows, sorted by pct (best first) then absolute line move.
    """
    by_market: dict = {}
    for r in rows:
        if r.get("strength") is not None:
            by_market.setdefault(r.get("market"), []).append(r["strength"])
    for vals in by_market.values():
        vals.sort()
    from bisect import bisect_left, bisect_right
    for r in rows:
        vals = by_market.get(r.get("market"))
        if r.get("strength") is None or not vals:
            r["pct"] = None
            continue
        lo, hi = bisect_left(vals, r["strength"]), bisect_right(vals, r["strength"])
        r["pct"] = round(100.0 * (lo + hi) / 2 / len(vals), 1)
    rows.sort(key=lambda r: (r["pct"] is None, -(r["pct"] or 0), -abs(r.get("line_move") or 0), r.get("name") or ""))
    return rows
