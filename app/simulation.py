"""The simulation core: minutes times a shrunk per-minute rate, drawn as negative binomials.

Shared by the dashboard pipeline (app/sim.py) and the backtest (scripts/backtest_stage_a.py), so the model that is scored is the
model that runs. Uses only a player's earlier games. See scripts/backtest_stage_a.py for the known simplifications.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from app import minutes_model

import numpy as np

STAT_KEYS = ["pts", "reb", "ast", "fg3m", "stl", "blk"]
MARKET_STATS = {
    "pts": ("pts",), "reb": ("reb",), "ast": ("ast",), "fg3m": ("fg3m",), "stl": ("stl",), "blk": ("blk",),
    "pa": ("pts", "ast"), "pr": ("pts", "reb"), "ra": ("reb", "ast"), "pra": ("pts", "reb", "ast"),
    "stocks": ("stl", "blk"),
}


# From app/sim_fit.json (scripts/fit_sim.py); empty means 1.0 everywhere.
DEFAULT_DISP_MULT: Dict[str, float] = dict(minutes_model.LOADED_DISP_MULT)


@dataclass
class Params:
    min_prior: int = 5            # earlier games needed before a prop is simulated
    prior_minutes: float = 100.0  # strength of the pull toward the league per-minute rate (about 3 games)
    min_sd: float = 3.0           # floor on the minutes standard deviation
    draws: int = 4000
    seed: int = 7
    # Last season as a prior. The player's own per-minute rates and minutes from last season stand in for the league average,
    # worth at most this many minutes of play (about 20 games at 30 min = 600). 0 turns it off (league-average pull only).
    last_cap_minutes: float = 0.0
    # Minutes model: adds a fitted adjustment (role, trend, volatility, blowout and foul tendency) to the flat average of past minutes.
    # True uses minutes_model.DEFAULT_MODEL; `minutes` overrides it (the fit script uses this); False keeps the flat average.
    use_minutes_model: bool = True
    minutes: Optional[minutes_model.MinutesModel] = None
    # Per-stat multiplier on the negative-binomial dispersion alpha (more than 1 widens the distribution). Fitted by scripts/fit_sim.py.
    disp_mult: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_DISP_MULT))


@dataclass
class League:
    rate: Dict[str, float] = field(default_factory=dict)   # stat per minute
    alpha: Dict[str, float] = field(default_factory=dict)  # negative-binomial dispersion (variance = mu + alpha * mu^2)




def is_played(g: dict) -> bool:
    return (g.get("minutes") or 0) > 0



def league_params(players: Iterable[List[dict]]) -> League:
    players = [[g for g in logs if is_played(g)] for logs in players]
    tot_min = sum(g["minutes"] for logs in players for g in logs)
    lg = League()
    if tot_min <= 0:
        raise ValueError("no minutes played in the data")
    for k in STAT_KEYS:
        lg.rate[k] = sum((g.get(k) or 0) for logs in players for g in logs) / tot_min
        num = den = 0.0
        for logs in players:
            n = len(logs)
            m_sum = sum(g["minutes"] for g in logs)
            if n < 3 or m_sum <= 0:
                continue
            r = sum((g.get(k) or 0) for g in logs) / m_sum  # this player's own season rate
            corr = n / (n - 1)                              # degrees-of-freedom correction for the in-sample mean
            for g in logs:
                mu = r * g["minutes"]
                y = g.get(k) or 0
                num += corr * (y - mu) ** 2 - mu
                den += mu ** 2
        lg.alpha[k] = min(max(num / den, 0.02), 1.5) if den > 0 else 0.1
    return lg


def simulate_player(prior: List[dict], lg: League, params: Params, rng: np.random.Generator,
                    last: Optional[List[dict]] = None) -> Optional[Dict[str, np.ndarray]]:
    """Draws of every stat for one upcoming game, using only the player's earlier games.

    `last` is the same player's previous-season game log. With params.last_cap_minutes > 0 it adds a prior: its per-minute rates
    are blended in (weight capped at last_cap_minutes of play, on top of the usual league-average pull) and its minutes feed the
    minutes distribution as at most that many minutes' worth of games. A player with no usable last season behaves as before.
    """
    # oldest first: Hardwood returns game logs newest first, and the minutes model's recent-trend feature depends on the order
    played = sorted((g for g in prior if is_played(g)), key=lambda g: g.get("date") or "")
    if len(played) < params.min_prior:
        return None
    mins = np.array([g["minutes"] for g in played], dtype=float)
    lplayed = [g for g in (last or []) if is_played(g)] if params.last_cap_minutes > 0 else []
    lmin = np.array([g["minutes"] for g in lplayed], dtype=float)
    lw = min(1.0, params.last_cap_minutes / float(lmin.sum())) if len(lmin) else 0.0   # share of last season that counts
    if len(lmin):
        w = np.concatenate([np.ones(len(mins)), np.full(len(lmin), lw)])
        allm = np.concatenate([mins, lmin])
        mean = float((w * allm).sum() / w.sum())
        sd = float(np.sqrt((w * (allm - mean) ** 2).sum() / max(w.sum() - 1.0, 1.0)))
    else:
        mean, sd = float(mins.mean()), float(mins.std(ddof=1))
    mm = params.minutes or (minutes_model.DEFAULT_MODEL if params.use_minutes_model else None)
    if mm is not None:
        mean = float(np.clip(mean + mm.adjust(played), 0.0, 44.0))
    m = np.clip(rng.normal(mean, max(params.min_sd, sd), params.draws), 0.0, 48.0)
    tot_min = float(mins.sum())
    out: Dict[str, np.ndarray] = {}
    for k in STAT_KEYS:
        s = float(sum((g.get(k) or 0) for g in played))
        num, den = s + params.prior_minutes * lg.rate[k], tot_min + params.prior_minutes
        if len(lmin):
            num += lw * float(sum((g.get(k) or 0) for g in lplayed))
            den += lw * float(lmin.sum())
        rate = num / den
        mu = rate * m
        r = 1.0 / (lg.alpha[k] * params.disp_mult.get(k, 1.0))
        p = r / (r + mu)
        out[k] = rng.negative_binomial(r, p).astype(float)
    return out


def prob_over(draws: np.ndarray, line: float) -> Optional[float]:
    """P(over) given the result is not a push (a whole-number line can push)."""
    push = float((draws == line).mean())
    if push >= 1.0:
        return None
    return float((draws > line).mean()) / (1.0 - push)


