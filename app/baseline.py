"""Baseline S: the simple statistical comparator for the prospective test (fixed in the protocol, not tuned).

Each player's prior-games mean per stat, with the league snapshot's negative-binomial spread, flat minutes (the game mean already
averages them), no minutes model, no opponent terms. It uses the same prior games, the same eligibility (at least MIN_PRIOR_GAMES
played games) and the same league snapshot as the simulator, so the two are compared on identical information.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from app.protocol import MIN_PRIOR_GAMES
from app.simulation import STAT_KEYS, League, is_played


def baseline_draws(prior: List[dict], lg: League, rng: np.random.Generator, draws: int = 4000,
                   min_prior: int = MIN_PRIOR_GAMES) -> Optional[Dict[str, np.ndarray]]:
    played = sorted((g for g in prior if is_played(g)), key=lambda g: g.get("date") or "")
    if len(played) < min_prior:
        return None
    out: Dict[str, np.ndarray] = {}
    for k in STAT_KEYS:
        mu = float(np.mean([(g.get(k) or 0) for g in played]))
        if mu <= 0:
            out[k] = np.zeros(draws)
            continue
        r = 1.0 / lg.alpha[k]
        p = r / (r + mu)
        out[k] = rng.negative_binomial(r, p, draws).astype(float)
    return out
