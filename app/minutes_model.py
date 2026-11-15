"""A better prediction of tomorrow's minutes than a flat average of past minutes.

The habit screen (scripts/habit_screen.py) found that facts computable from a player's own earlier games explain much of the miss in
a flat minutes average: how often he starts, whether his recent minutes are trending up or down, how volatile his minutes are, how
lopsided his games get (absolute plus/minus) and how foul-prone he is. A ridge regression on those, fitted on a full earlier season,
predicts the adjustment to add to the flat average. Pure Python/NumPy, no schedule inputs, so it works for any player with a game log.

The fitted constants live in DEFAULT_MODEL (written by scripts/fit_sim.py). A model with no coefficients adds nothing.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

FEATURES = ["started_rate", "min_trend", "min_sd", "pm_abs", "pf_rate"]


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def features(played: Sequence[dict]) -> List[float]:
    """Feature vector from a player's earlier played games (oldest first). Needs at least two games."""
    mins = np.array([g["minutes"] for g in played], dtype=float)
    tot, mean = float(mins.sum()), float(mins.mean())
    known = [g.get("started") for g in played if g.get("started") is not None]
    return [
        float(np.mean([1.0 if v else 0.0 for v in known])) if known else 0.5,
        float(np.mean(mins[-3:]) - mean),
        float(mins.std(ddof=1)) if len(mins) > 1 else 0.0,
        float(np.mean([abs(_num(g.get("plus_minus"))) for g in played])),
        float(sum(_num(g.get("pf")) for g in played) / max(tot, 1.0) * 36.0),
    ]


@dataclass
class MinutesModel:
    mean: List[float] = field(default_factory=list)     # feature means from the fitting data
    scale: List[float] = field(default_factory=list)    # feature standard deviations
    coef: List[float] = field(default_factory=list)     # ridge weights on the standardized features
    intercept: float = 0.0
    max_adjust: float = 12.0                            # never move the average by more than this many minutes
    fitted_on: str = ""

    def adjust(self, played: Sequence[dict]) -> float:
        """Minutes to add to the flat average of the player's earlier games."""
        if not self.coef or len(played) < 2:
            return 0.0
        x = (np.array(features(played)) - np.array(self.mean)) / (np.array(self.scale) + 1e-9)
        return float(np.clip(float(x @ np.array(self.coef)) + self.intercept, -self.max_adjust, self.max_adjust))


def fit(X: np.ndarray, y: np.ndarray, lam: float = 50.0, fitted_on: str = "") -> MinutesModel:
    """Ridge fit of the miss (actual minutes minus the flat average) on standardized features."""
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-9
    A = (X - mu) / sd
    ym = float(y.mean())
    w = np.linalg.solve(A.T @ A + lam * np.eye(A.shape[1]), A.T @ (y - ym))
    return MinutesModel(mean=[float(v) for v in mu], scale=[float(v) for v in sd], coef=[float(v) for v in w], intercept=ym, fitted_on=fitted_on)


FIT_PATH = Path(__file__).with_name("sim_fit.json")


def load_fit(path: Path = FIT_PATH) -> Tuple[Optional[MinutesModel], Dict[str, float]]:
    """The minutes model and the per-stat dispersion multipliers written by scripts/fit_sim.py, or (None, {}) when there is no fit."""
    try:
        d = json.loads(Path(path).read_text())
        return MinutesModel(**d["minutes_model"]), {k: float(v) for k, v in (d.get("disp_mult") or {}).items()}
    except (OSError, ValueError, KeyError, TypeError):
        return None, {}


def save_fit(model: MinutesModel, disp_mult: Dict[str, float], meta: dict, path: Path = FIT_PATH) -> None:
    Path(path).write_text(json.dumps({"minutes_model": asdict(model), "disp_mult": disp_mult, "meta": meta}, indent=1))


# Loaded from app/sim_fit.json (written by scripts/fit_sim.py). Without that file the minutes model adds nothing and spreads are unchanged.
DEFAULT_MODEL, LOADED_DISP_MULT = load_fit()
