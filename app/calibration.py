"""Recalibration of the raw simulation probability.

The backtests found the raw simulation overconfident (calibration slope around 0.15 to 0.2: when it says 70%, the over hits far
less often than that). A logistic recalibration, p_cal = sigmoid(a + b * logit(p_raw)), shrinks it toward 50/50. With a = 0 the
direction never changes, only the size of the tilt. Pure Python so the dashboard can apply it without numpy.
"""
from __future__ import annotations

import math
from typing import Optional

EPS = 1e-4

# Slope-only recalibration (a = 0), checked forward in time (scripts/backtest_calibrate.py). History: original sim, folds 0.10 to 0.19, all
# data 0.198; after the minutes model and spread (2,620 records) folds 0.22 to 0.29, all data 0.31; after the order fix and dropping the
# spread multipliers (3,067 records to 2026-11-14) folds 0.20 to 0.32, all data 0.16. The estimate moves with the sample, so 0.20 is
# used, inside every range seen. Re-fit with the Stage B re-run.
SLOPE = 0.20
FITTED_ON = "2026-10-30 to 2026-11-14, updated simulation"


def logit(p: float) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


def apply(p: Optional[float], a: float, b: float) -> Optional[float]:
    if p is None:
        return None
    return 1.0 / (1.0 + math.exp(-(a + b * logit(p))))
