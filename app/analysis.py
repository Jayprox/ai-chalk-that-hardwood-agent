"""The primary analysis of the prospective test, fixed before the freeze (definitions in docs/PROSPECTIVE_PROTOCOL.md).

Pure functions on settled rows (numpy only). scripts/prospective_analysis.py loads the rows and enforces the endpoint; nothing here
reads a clock, a network or the ledger, so the same rows always give the same result (seed protocol.BOOT_SEED).

Row fields: player_id, game_id, market, status ('settled' | 'push'), actual, outcome ('over' | 'under' | None), market_p_over,
market_side, over_price, under_price, agree (1 | 0 | None), sim_pmf (list), base_pmf (list).

Definitions
  Track A (projection accuracy). Discrete CRPS of each recorded distribution against the actual, for points, rebounds and assists.
    T_s = 1 - sum(CRPS simulator) / sum(CRPS baseline S) over rows of stat s (positive: simulator better); T = mean of the three T_s.
  Track B (group separation). For a settled, non-push row with a market favorite: hit = the favorite's side won; p_fav = devigged
    favorite probability; residual = hit - p_fav. H1 = 100 * (mean residual of agree rows - mean residual of disagree rows).
  S1 (market-adjusted). Strata = market x favorite-probability bin (protocol.BIN_EDGES). Within a market, bins are scanned from the lowest to
    the highest, accumulating until both arms hold at least MIN_ARM weighted rows; a deficient remainder is merged into the stratum below it;
    a market that never reaches MIN_ARM in both arms is dropped (insufficient overlap). S1 = size-weighted mean of the stratum gaps. It is
    estimable only with >= S1_MIN_MARKETS markets, >= S1_MIN_GROUPS strata and >= S1_MIN_PROP_SHARE of the weighted rows retained.
    In the bootstrap the pooling is redone on each replicate's weights. A replicate where S1 is not estimable is discarded.
  S2 (sensitivity, no decision role). Weighted logistic regression of hit on logit(p_fav), market indicators and agree; reported as the
    average marginal effect of agree in points (IRLS, at most S2_MAX_ITER steps, warm-started; a non-converged replicate is discarded).
  H2 (gated, evaluated only when H1 is "separation established"): lower bound of 100 * mean(hit - offered-price break-even) over agree rows.
  Intervals. Pigeonhole bootstrap (Owen 2007): player clusters and game clusters are resampled independently with replacement; a row's weight
    is (times its player was drawn) x (times its game was drawn). BOOT_REPS replicates, seed BOOT_SEED, 95% percentile interval over
    non-discarded replicates; more than DISCARD_MAX discarded makes that interval "unstable" and its decision rule inconclusive.
  Limits. Dependence across consecutive dates and between teammates in different games is not modeled; the pigeonhole interval treats
    players and games as crossed independent clusters. The analytic two-way cluster variance is not implemented.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app import protocol as P
from app.signal import american_to_prob

NAN = float("nan")


# ---- CRPS

def crps_pmf(pmf: Sequence[float], y: float) -> float:
    """Discrete CRPS of a pmf on 0..K against an integer observation y: sum over x of (F(x) - 1[y <= x])^2, extended past K with F = 1."""
    p = np.asarray(pmf, dtype=float)
    top = int(max(len(p) - 1, math.ceil(y)))
    full = np.zeros(top + 1)
    full[:len(p)] = p
    cdf = np.cumsum(full)
    cdf[len(p) - 1:] = 1.0 if len(p) else 0.0
    xs = np.arange(top + 1)
    return float(np.sum((cdf - (xs >= y)) ** 2))


def profit_per_unit(price: Any) -> Optional[float]:
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p == 0:
        return None
    return p / 100.0 if p > 0 else 100.0 / -p


# ---- preparation

class Data:
    def __init__(self, rows: List[dict]):
        self.rows = rows
        players = sorted({r["player_id"] for r in rows})
        games = sorted({r["game_id"] for r in rows})
        pidx = {p: i for i, p in enumerate(players)}
        gidx = {g: i for i, g in enumerate(games)}
        self.nP, self.nG = len(players), len(games)
        self.markets = sorted({r["market"] for r in rows})
        # Track A
        a_rows = [r for r in rows if r["status"] in P.SETTLED_STATUSES and r["market"] in P.TRACK_A_MARKETS and r.get("sim_pmf") and r.get("base_pmf")
                  and r.get("actual") is not None]
        self.a_pi = np.array([pidx[r["player_id"]] for r in a_rows], dtype=int)
        self.a_gi = np.array([gidx[r["game_id"]] for r in a_rows], dtype=int)
        self.a_stat = np.array([P.TRACK_A_MARKETS.index(r["market"]) for r in a_rows], dtype=int)
        self.a_cs = np.array([crps_pmf(r["sim_pmf"], r["actual"]) for r in a_rows])
        self.a_cb = np.array([crps_pmf(r["base_pmf"], r["actual"]) for r in a_rows])
        # Track B
        b_rows = [r for r in rows if r["status"] == "settled" and r.get("outcome") in ("over", "under") and r.get("market_side") in ("over", "under")
                  and r.get("agree") in (0, 1) and r.get("market_p_over") is not None]
        self.b_rows = b_rows
        self.b_pi = np.array([pidx[r["player_id"]] for r in b_rows], dtype=int)
        self.b_gi = np.array([gidx[r["game_id"]] for r in b_rows], dtype=int)
        pov = np.array([r["market_p_over"] for r in b_rows], dtype=float)
        side_over = np.array([r["market_side"] == "over" for r in b_rows], dtype=bool)
        self.p_fav = np.where(side_over, pov, 1.0 - pov)
        self.hit = np.array([1.0 if r["outcome"] == r["market_side"] else 0.0 for r in b_rows])
        self.resid = self.hit - self.p_fav
        self.agree = np.array([r["agree"] for r in b_rows], dtype=float)
        self.mkt = np.array([self.markets.index(r["market"]) for r in b_rows], dtype=int)
        self.bin = np.clip(np.digitize(self.p_fav, P.BIN_EDGES[1:-1]), 0, len(P.BIN_EDGES) - 2)
        prices = [r["over_price"] if r["market_side"] == "over" else r["under_price"] for r in b_rows]
        self.be = np.array([american_to_prob(x) if american_to_prob(x) is not None else NAN for x in prices])
        self.profit = np.array([profit_per_unit(x) if profit_per_unit(x) is not None else NAN for x in prices])
        self.nB = len(b_rows)
        # S2 design
        cols = [np.ones(self.nB), np.log(np.clip(self.p_fav, 1e-6, 1 - 1e-6) / (1 - np.clip(self.p_fav, 1e-6, 1 - 1e-6)))]
        for m in range(1, len(self.markets)):
            cols.append((self.mkt == m).astype(float))
        cols.append(self.agree)
        self.X = np.column_stack(cols) if self.nB else np.zeros((0, 3))


# ---- statistics under a weight vector

def track_a(d: Data, w_a: np.ndarray) -> Tuple[float, List[float]]:
    ts_: List[float] = []
    for s in range(len(P.TRACK_A_MARKETS)):
        m = d.a_stat == s
        den = float(np.sum(w_a[m] * d.a_cb[m]))
        ts_.append(1.0 - float(np.sum(w_a[m] * d.a_cs[m])) / den if den > 0 else NAN)
    return (float(np.mean(ts_)) if all(np.isfinite(ts_)) else NAN), ts_


def gap(w: np.ndarray, a: np.ndarray, r: np.ndarray) -> float:
    wa, wd = w * a, w * (1.0 - a)
    sa, sd = wa.sum(), wd.sum()
    if sa <= 0 or sd <= 0:
        return NAN
    return float((np.sum(wa * r) / sa - np.sum(wd * r) / sd) * 100.0)


def pool_market(nA: np.ndarray, nD: np.ndarray, sA: np.ndarray, sD: np.ndarray) -> List[Tuple[float, float, float, float]]:
    strata: List[Tuple[float, float, float, float]] = []
    cur = [0.0, 0.0, 0.0, 0.0]
    for b in range(len(nA)):
        cur = [cur[0] + nA[b], cur[1] + nD[b], cur[2] + sA[b], cur[3] + sD[b]]
        if cur[0] >= P.MIN_ARM and cur[1] >= P.MIN_ARM:
            strata.append((cur[0], cur[1], cur[2], cur[3]))
            cur = [0.0, 0.0, 0.0, 0.0]
    if any(cur):
        if not strata:
            return []                                                  # insufficient overlap: the market is dropped
        last = strata[-1]
        strata[-1] = (last[0] + cur[0], last[1] + cur[1], last[2] + cur[2], last[3] + cur[3])
    return strata


def s1_stat(d: Data, w: np.ndarray, detail: bool = False) -> Any:
    nM, nBn = len(d.markets), len(P.BIN_EDGES) - 1
    cell = d.mkt * nBn + d.bin
    k = nM * nBn
    nA = np.bincount(cell, weights=w * d.agree, minlength=k).reshape(nM, nBn)
    nD = np.bincount(cell, weights=w * (1 - d.agree), minlength=k).reshape(nM, nBn)
    sA = np.bincount(cell, weights=w * d.agree * d.resid, minlength=k).reshape(nM, nBn)
    sD = np.bincount(cell, weights=w * (1 - d.agree) * d.resid, minlength=k).reshape(nM, nBn)
    num = den = 0.0
    strata_n = 0
    kept_markets: List[str] = []
    dropped: List[str] = []
    for m in range(nM):
        st = pool_market(nA[m], nD[m], sA[m], sD[m])
        if not st:
            dropped.append(d.markets[m])
            continue
        kept_markets.append(d.markets[m])
        for a, dd, ra, rd in st:
            size = a + dd
            num += size * (ra / a - rd / dd) * 100.0
            den += size
            strata_n += 1
    total = float(w.sum())
    ok = (len(kept_markets) >= P.S1_MIN_MARKETS and strata_n >= P.S1_MIN_GROUPS and total > 0 and den / total >= P.S1_MIN_PROP_SHARE and den > 0)
    value = num / den if ok else NAN
    if detail:
        return {"estimable": bool(ok), "value": value, "strata": strata_n, "markets_kept": kept_markets, "markets_dropped": dropped,
                "share_retained": (den / total) if total > 0 else None}
    return value


def logistic(d: Data, w: np.ndarray, beta0: Optional[np.ndarray] = None) -> Tuple[Optional[np.ndarray], bool]:
    X, y = d.X, d.hit
    beta = np.zeros(X.shape[1]) if beta0 is None else beta0.copy()
    converged = False
    for _ in range(P.S2_MAX_ITER):
        eta = np.clip(X @ beta, -30, 30)
        mu = 1.0 / (1.0 + np.exp(-eta))
        v = np.maximum(mu * (1 - mu), 1e-9)
        W = w * v
        z = eta + (y - mu) / v
        A = (X * W[:, None]).T @ X + 1e-6 * np.eye(X.shape[1])
        try:
            new = np.linalg.solve(A, (X * W[:, None]).T @ z)
        except np.linalg.LinAlgError:
            return None, False
        step = float(np.max(np.abs(new - beta)))
        beta = new
        if step < 1e-6:
            converged = True
            break
    return beta, converged


def s2_ame(d: Data, w: np.ndarray, beta: np.ndarray) -> float:
    X1, X0 = d.X.copy(), d.X.copy()
    X1[:, -1], X0[:, -1] = 1.0, 0.0
    sig = lambda e: 1.0 / (1.0 + np.exp(-np.clip(e, -30, 30)))
    tot = w.sum()
    return float(np.sum(w * (sig(X1 @ beta) - sig(X0 @ beta))) / tot * 100.0) if tot > 0 else NAN


def h2_stat(d: Data, w: np.ndarray) -> float:
    m = d.agree == 1
    ok = m & np.isfinite(d.be)
    den = float(np.sum(w[ok]))
    return float(np.sum(w[ok] * (d.hit[ok] - d.be[ok])) / den * 100.0) if den > 0 else NAN


# ---- the bootstrap

def interval(x: np.ndarray) -> Dict[str, Any]:
    fin = x[np.isfinite(x)]
    discard = 1.0 - len(fin) / len(x) if len(x) else 1.0
    if len(fin) < 2:
        return {"lo": None, "hi": None, "discard": round(discard, 4), "stable": False}
    lo, hi = np.percentile(fin, [2.5, 97.5])
    return {"lo": float(lo), "hi": float(hi), "discard": round(discard, 4), "stable": discard <= P.DISCARD_MAX}


def point_estimates(d: Data) -> dict:
    w_a, w_b = np.ones(len(d.a_cs)), np.ones(d.nB)
    T, Ts = track_a(d, w_a)
    beta, conv = logistic(d, w_b) if d.nB > len(d.markets) + 3 else (None, False)
    return {"T": T, "T_s": Ts, "H1": gap(w_b, d.agree, d.resid) if d.nB else NAN, "S1": s1_stat(d, w_b, True) if d.nB else None,
            "S2": s2_ame(d, w_b, beta) if beta is not None and conv else NAN, "H2": h2_stat(d, w_b) if d.nB else NAN, "beta": beta, "converged": conv}


def bootstrap(d: Data, pe: dict, reps: int = P.BOOT_REPS, seed: int = P.BOOT_SEED) -> dict:
    rng = np.random.default_rng(seed)
    nS = len(P.TRACK_A_MARKETS)
    out = {k: np.full(reps, NAN) for k in ("T", "H1", "S1", "S2", "H2", "T_player", "T_game", "H1_player", "H1_game")}
    out["T_s"] = np.full((reps, nS), NAN)
    uniformP, uniformG = np.full(d.nP, 1.0 / d.nP), np.full(d.nG, 1.0 / d.nG)
    for i in range(reps):
        a = rng.multinomial(d.nP, uniformP).astype(float)
        b = rng.multinomial(d.nG, uniformG).astype(float)
        wa, wb = a[d.a_pi] * b[d.a_gi], a[d.b_pi] * b[d.b_gi]
        out["T"][i], out["T_s"][i] = track_a(d, wa)
        out["T_player"][i], _ = track_a(d, a[d.a_pi])
        out["T_game"][i], _ = track_a(d, b[d.a_gi])
        if d.nB:
            out["H1"][i] = gap(wb, d.agree, d.resid)
            out["H1_player"][i] = gap(a[d.b_pi], d.agree, d.resid)
            out["H1_game"][i] = gap(b[d.b_gi], d.agree, d.resid)
            out["S1"][i] = s1_stat(d, wb)
            out["H2"][i] = h2_stat(d, wb)
            if pe["beta"] is not None and pe["converged"]:
                beta, conv = logistic(d, wb, pe["beta"])
                if conv and beta is not None:
                    out["S2"][i] = s2_ame(d, wb, beta)
    return out


# ---- decisions

def _num(x: Any) -> Optional[float]:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)


def decide_track_a(T: Optional[float], iv: dict, ivs: List[dict], invalid: bool) -> dict:
    if invalid:
        return {"category": "invalid", "why": "coverage or integrity rule failed"}
    if T is None or not iv["stable"] or iv["lo"] is None or any((not s["stable"]) or s["hi"] is None for s in ivs):
        return {"category": "inconclusive", "why": "an interval is unstable or not estimable (discarded replicates above the limit)"}
    L, U = iv["lo"], iv["hi"]
    if U < 0:
        return {"category": "harmful", "why": "upper bound below 0"}
    if L > 0 and T >= P.GATE_A and all(s["hi"] >= 0 for s in ivs):
        return {"category": "pass", "why": "lower bound above 0, estimate at or above the gate, no stat clearly worse"}
    if L > 0:
        return {"category": "positive_below_gate_or_mixed", "why": "lower bound above 0 but not a pass"}
    if U < P.GATE_A:
        return {"category": "worthwhile_improvement_ruled_out", "why": "lower bound at or below 0 and upper bound below the gate"}
    return {"category": "inconclusive", "why": "interval includes 0 and the gate"}


def decide_track_b(h1: Optional[float], iv: dict, s1_iv: dict, s1_ok: bool, invalid: bool) -> dict:
    if invalid:
        return {"category": "invalid", "why": "coverage or integrity rule failed"}
    if h1 is None or not iv["stable"] or iv["lo"] is None:
        return {"category": "inconclusive", "why": "the H1 interval is unstable or not estimable"}
    L, U = iv["lo"], iv["hi"]
    if U < 0:
        return {"category": "reversed", "why": "upper bound below 0"}
    if L > 0 and h1 >= P.TARGET_B:
        if s1_ok and s1_iv["stable"] and s1_iv["lo"] is not None and s1_iv["lo"] > 0:
            return {"category": "separation_established", "why": "H1 lower bound above 0, estimate at or above target, S1 lower bound above 0"}
        return {"category": "separation_not_robust_to_market_mix", "why": "S1 lower bound at or below 0, unstable or not estimable"}
    if L > 0:
        return {"category": "positive_below_target", "why": "lower bound above 0, estimate below target"}
    if U < P.TARGET_B:
        return {"category": "worthwhile_effect_ruled_out", "why": "lower bound at or below 0 and upper bound below target"}
    return {"category": "inconclusive", "why": "interval includes 0 and the target"}


def descriptive(d: Data) -> dict:
    out: Dict[str, Any] = {}
    for name, m in (("agree", d.agree == 1), ("disagree", d.agree == 0)):
        n = int(m.sum())
        out[name] = {"n": n, "hit": float(d.hit[m].mean()) if n else None, "devig": float(d.p_fav[m].mean()) if n else None,
                     "break_even": float(np.nanmean(d.be[m])) if n and np.isfinite(d.be[m]).any() else None,
                     "net_return_per_unit": (float(np.nanmean(np.where(d.hit[m] == 1, d.profit[m], -1.0))) if n and np.isfinite(d.profit[m]).any() else None)}
    by_market = {}
    for mi, name in enumerate(d.markets):
        m = d.mkt == mi
        by_market[name] = {"n": int(m.sum()), "H1_point": gap(np.ones(int(m.sum())), d.agree[m], d.resid[m]) if m.sum() else None}
    three = np.isin(d.mkt, [d.markets.index(x) for x in P.TRACK_A_MARKETS if x in d.markets])
    return {"groups": out, "by_market": by_market, "three_market_H1_point": gap(np.ones(int(three.sum())), d.agree[three], d.resid[three]) if three.any() else None}


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return [_clean(v) for v in o.tolist()]
    if isinstance(o, (float, np.floating)):
        return None if not math.isfinite(o) else round(float(o), 6)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return o


def run_primary(rows: List[dict], *, coverage_ok: bool = True, integrity_ok: bool = True, reps: int = P.BOOT_REPS, seed: int = P.BOOT_SEED) -> dict:
    """The one analysis. `rows` are the settled rows of the endpoint games. Returns a JSON-able result."""
    invalid = not (coverage_ok and integrity_ok)
    d = Data(rows)
    n_games = len({r["game_id"] for r in rows})
    res: Dict[str, Any] = {"protocol": P.PROTOCOL_VERSION, "reps": reps, "seed": seed, "rows": len(rows), "games": n_games, "players": d.nP,
                           "track_a_rows": int(len(d.a_cs)), "track_b_rows": d.nB, "invalid_inputs": invalid}
    if len(d.a_cs) < 2 or d.nB < 2 or d.nP < 2 or d.nG < 2:
        res["track_a"] = {"category": "invalid" if invalid else "inconclusive", "why": "too few rows"}
        res["track_b"] = {"category": "invalid" if invalid else "inconclusive", "why": "too few rows"}
        return _clean(res)
    pe = point_estimates(d)
    bs = bootstrap(d, pe, reps, seed)
    ivT = interval(bs["T"])
    ivTs = [interval(bs["T_s"][:, s]) for s in range(len(P.TRACK_A_MARKETS))]
    ivH1, ivS1, ivS2, ivH2 = interval(bs["H1"]), interval(bs["S1"]), interval(bs["S2"]), interval(bs["H2"])
    s1_ok = bool(pe["S1"] and pe["S1"]["estimable"])
    ta = decide_track_a(_num(pe["T"]), ivT, ivTs, invalid)
    tb = decide_track_b(_num(pe["H1"]), ivH1, ivS1, s1_ok, invalid)
    h2 = None
    if tb["category"] == "separation_established":
        h2 = {"evaluated": True, "estimate": pe["H2"], "interval": ivH2,
              "passes": bool(ivH2["stable"] and ivH2["lo"] is not None and ivH2["lo"] > 0)}
    else:
        h2 = {"evaluated": False, "note": "H2 is evaluated only when H1 is 'separation established'; returns stay descriptive"}
    res["track_a"] = {**ta, "T": pe["T"], "T_interval": ivT, "per_stat": {s: {"T": pe["T_s"][i], "interval": ivTs[i]} for i, s in enumerate(P.TRACK_A_MARKETS)},
                      "sensitivity": {"player_only": interval(bs["T_player"]), "game_only": interval(bs["T_game"])}}
    res["track_b"] = {**tb, "H1": pe["H1"], "H1_interval": ivH1, "S1": pe["S1"], "S1_interval": ivS1, "S2_ame": pe["S2"], "S2_interval": ivS2,
                      "S2_converged": pe["converged"], "H2": h2, "sensitivity": {"player_only": interval(bs["H1_player"]), "game_only": interval(bs["H1_game"])},
                      "descriptive": descriptive(d)}
    return _clean(res)
