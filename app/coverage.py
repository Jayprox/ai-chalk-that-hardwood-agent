"""Coverage, integrity and endpoint reports. These never read outcomes (no actuals, no over/under results, no hit rates).

Eligibility is defined by the pre-outcome expected-game manifest (`expected_games`), not by what the recorder happened to collect:
  * expected game   = a regular-season game seen on the Hardwood schedule (today or tomorrow, at every tick) with a tip-off in the past;
                      games postponed or not final ABANDON_HOURS after tip-off are reported as `not_played` and leave the denominator;
  * covered game    = at least MIN_GAME_CONTRACTS valid recorded contracts from >= MIN_GAME_PLAYERS players in >= MIN_GAME_MARKETS markets;
  * contract denominator = every close contract ever observed for an expected game (the `universe`), recorded or not.
Games that were never collected stay in the denominator with a reason. Nothing is substituted. A game first seen after its tip-off is
flagged `registered_late` (it cannot be proven pre-outcome) but still counts as expected.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app import protocol as P
from app.ledger import Ledger, parse_ts, ts

NOT_PLAYED_STATUSES = {"postponed", "cancelled", "canceled", "suspended"}


def scope_info(ledger: Ledger, scope: str, from_date: Optional[str] = None) -> dict:
    """scope: 'counted' (after the latest freeze), 'dry' (before any freeze / not counted), or 'all'.

    `from_date` (YYYY-MM-DD, ET) starts the evaluated window for 'dry' and 'all': games and records on earlier dates are left out, so a dry run
    that began mid-slate is judged only from its first full slate day. It never applies to 'counted'.
    """
    fr = ledger.latest_freeze()
    first = fr["first_counted_date"] if fr else None
    if scope == "counted":
        return {"scope": scope, "first_counted_date": first, "freeze_id": fr["id"] if fr else None,
                "rec_where": "freeze_id IS NOT NULL", "game_where": "date_et >= ?", "game_args": (first,) if first else None}
    games, recs, args = [], [], []
    if scope == "dry":
        recs.append("freeze_id IS NULL")
        if first:
            games.append("date_et < ?")
            args.append(first)
    if from_date:
        games.append("date_et >= ?")
        recs.append(f"date_et >= '{from_date}'" if len(from_date) == 10 and from_date.replace("-", "").isdigit() else "1=0")
        args.append(from_date)
    return {"scope": scope, "first_counted_date": first, "freeze_id": None, "rec_where": " AND ".join(recs) or "1=1",
            "game_where": " AND ".join(games) or "1=1", "game_args": tuple(args)}


def scope_games(ledger: Ledger, info: dict) -> List[dict]:
    if info["game_args"] is None:
        return []
    return ledger._all(f"SELECT * FROM expected_games WHERE {info['game_where']} ORDER BY tipoff_utc, game_id", info["game_args"])


def game_matured(tip: Optional[datetime], now: datetime) -> bool:
    return tip is not None and now >= tip + timedelta(hours=P.GAME_LENGTH_HOURS + P.MATURITY_HOURS)


def game_rows(ledger: Ledger, info: dict, now: datetime) -> List[dict]:
    out: List[dict] = []
    recs: Dict[str, List[dict]] = {}
    for r in ledger._all(f"SELECT game_id, player_id, market, seq FROM ledger WHERE {info['rec_where']}"):
        recs.setdefault(r["game_id"], []).append(r)
    uni: Dict[str, Dict[str, int]] = {}
    for u in ledger._all("SELECT game_id, phase, COUNT(*) AS n FROM universe GROUP BY game_id, phase"):
        uni.setdefault(u["game_id"], {})[u["phase"]] = u["n"]
    ticks = [parse_ts(t["started_at"]) for t in ledger._all("SELECT started_at FROM ticks")]
    for g in scope_games(ledger, info):
        tip = parse_ts(g["tipoff_utc"])
        if tip is not None and tip > now:
            continue                                                   # not due yet
        status = (ledger.latest_status(g["game_id"]) or "").strip().lower()
        from app.collector import FINAL_STATUSES
        not_played = status in NOT_PLAYED_STATUSES or (tip is not None and status not in FINAL_STATUSES and now >= tip + timedelta(hours=P.ABANDON_HOURS))
        rs = recs.get(g["game_id"], [])
        in_pop = g["season_type"] in P.SEASON_TYPES
        window_ticks = sum(1 for t in ticks if t and tip and tip - timedelta(minutes=P.CLOSE_WINDOW_MIN) <= t < tip)
        n, players, markets = len(rs), len({r["player_id"] for r in rs}), len({r["market"] for r in rs})
        covered = n >= P.MIN_GAME_CONTRACTS and players >= P.MIN_GAME_PLAYERS and markets >= P.MIN_GAME_MARKETS
        u = uni.get(g["game_id"], {})
        reason = None
        if not covered:
            if not u:
                reason = "no_close_rows_observed" if window_ticks else "window_not_polled"
            elif n == 0:
                reason = "no_valid_records"
            else:
                reason = "below_minimum"
        out.append({"game_id": g["game_id"], "date_et": g["date_et"], "tipoff_utc": g["tipoff_utc"], "season_type": g["season_type"],
                    "in_population": in_pop, "status": status, "not_played": not_played, "contracts": n, "players": players, "markets": markets,
                    "universe_pregame": u.get("pregame", 0), "universe_late": u.get("late", 0), "window_ticks": window_ticks,
                    "covered": covered, "reason": reason, "matured": game_matured(tip, now),
                    "registered_late": bool(tip and (parse_ts(g["first_seen_at"]) or tip) >= tip)})
    return out


def settlement_counts(ledger: Ledger, info: dict, now: datetime) -> dict:
    """Status counts only (never actual values or over/under outcomes)."""
    recs = ledger._all(f"SELECT seq, game_id, tipoff_utc FROM ledger WHERE {info['rec_where']}")
    per_game: Dict[str, Dict[str, int]] = {}
    total: Dict[str, int] = {}
    for r in recs:
        if not game_matured(parse_ts(r["tipoff_utc"]), now):
            continue
        s = ledger.latest_settlement(r["seq"])
        st = s["status"] if s else "unresolved"
        total[st] = total.get(st, 0) + 1
        per_game.setdefault(r["game_id"], {}).setdefault(st, 0)
        per_game[r["game_id"]][st] += 1
    matured = sum(total.values())
    excl = sum(total.get(k, 0) for k in P.EXCLUSION_STATUSES)
    ok = sum(total.get(k, 0) for k in P.SETTLED_STATUSES)
    worst = [(g, round(sum(c.get(k, 0) for k in P.EXCLUSION_STATUSES) / sum(c.values()), 3)) for g, c in per_game.items()]
    return {"matured_contracts": matured, "by_status": total, "settleable_share": (ok / matured) if matured else None,
            "exclusion_share": (excl / matured) if matured else None,
            "games_over_exclusion_alarm": sorted([g for g, s in worst if s > P.GAME_EXCLUSION_ALARM])}


def coverage_report(ledger: Ledger, scope: str = "counted", now: Optional[datetime] = None, *, dry_run: bool = False, check_chain: bool = True,
                    from_date: Optional[str] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    info = scope_info(ledger, scope, from_date)
    games = game_rows(ledger, info, now)
    pop = [g for g in games if g["in_population"]]
    out_of_pop: Dict[str, int] = {}
    for g in games:
        if not g["in_population"]:
            out_of_pop[str(g["season_type"])] = out_of_pop.get(str(g["season_type"]), 0) + 1
    not_played = [g for g in pop if g["not_played"]]
    expected = [g for g in pop if not g["not_played"]]
    covered = [g for g in expected if g["covered"]]
    uni_total = sum(g["universe_pregame"] + g["universe_late"] for g in expected)
    rec_total = sum(g["contracts"] for g in expected)
    reasons: Dict[str, int] = {}
    for g in expected:
        if g["reason"]:
            reasons[g["reason"]] = reasons.get(g["reason"], 0) + 1
    rej: Dict[str, int] = {}
    ids = {g["game_id"] for g in expected}
    for r in ledger._all("SELECT game_id, reason, COUNT(*) AS n FROM rejections GROUP BY game_id, reason"):
        if r["game_id"] in ids:
            rej[r["reason"]] = rej.get(r["reason"], 0) + r["n"]
    settle = settlement_counts(ledger, info, now)
    cov_games = (len(covered) / len(expected)) if expected else None
    cov_contracts = (rec_total / uni_total) if uni_total else None
    min_g = P.DRY_RUN_COVERAGE_GAMES_MIN if dry_run else P.COVERAGE_GAMES_MIN
    min_c = P.DRY_RUN_COVERAGE_CONTRACTS_MIN if dry_run else P.COVERAGE_CONTRACTS_MIN
    bad: List[str] = []
    if cov_games is not None and cov_games < min_g:
        bad.append(f"covered games {cov_games:.1%} < {min_g:.0%}")
    if cov_contracts is not None and cov_contracts < min_c:
        bad.append(f"recorded contracts {cov_contracts:.1%} of observed < {min_c:.0%}")
    n_rec_bad = len(bad)
    if settle["settleable_share"] is not None and settle["settleable_share"] < P.SETTLEABLE_MIN:
        bad.append(f"settleable {settle['settleable_share']:.1%} of matured < {P.SETTLEABLE_MIN:.0%}")
    if settle["exclusion_share"] is not None and settle["exclusion_share"] > P.OVERALL_EXCLUSION_ALARM:
        bad.append(f"voids+unresolved {settle['exclusion_share']:.1%} of matured > {P.OVERALL_EXCLUSION_ALARM:.0%}")
    if settle["games_over_exclusion_alarm"]:
        bad.append(f"{len(settle['games_over_exclusion_alarm'])} game(s) with > {P.GAME_EXCLUSION_ALARM:.0%} exclusions")
    settle_bad = bad[n_rec_bad:]
    bad = bad[:n_rec_bad] + settle_bad
    chain = ledger.verify_chain() if check_chain else None
    if chain and not chain["ok"]:
        bad.append("ledger integrity: " + "; ".join(chain["problems"][:3]))
    matured_cov = sorted([g for g in covered if g["matured"]], key=lambda g: (g["tipoff_utc"], g["game_id"]))
    return {"scope": scope, "from_date": from_date, "first_counted_date": info["first_counted_date"], "as_of": ts(now), "dry_run_thresholds": dry_run,
            "expected_games": len(expected), "covered_games": len(covered), "coverage_games": cov_games,
            "not_played_games": [g["game_id"] for g in not_played], "out_of_population_games": out_of_pop,
            "uncovered_reasons": reasons, "uncovered_games": [{k: g[k] for k in ("game_id", "date_et", "reason", "contracts", "players", "markets", "window_ticks")} for g in expected if not g["covered"]],
            "registered_late_games": [g["game_id"] for g in expected if g["registered_late"]],
            "observed_contracts": uni_total, "recorded_contracts": rec_total, "coverage_contracts": cov_contracts,
            "rejections_by_reason": rej, "settlement": settle, "invalid_reasons": bad, "recording_reasons": bad[:n_rec_bad], "settlement_reasons": settle_bad,
            "expected_game_ids": [g["game_id"] for g in expected], "last_maturity_at": ts(max((parse_ts(g["tipoff_utc"]) for g in expected), default=now) + timedelta(hours=P.GAME_LENGTH_HOURS + P.MATURITY_HOURS)) if expected else None,
            "unmatured_games": [g["game_id"] for g in expected if not g["matured"]], "ok": not bad and bool(expected),
            "endpoint": {"target_games": P.ENDPOINT_GAMES, "covered_matured_games": len(matured_cov), "ready": len(matured_cov) >= P.ENDPOINT_GAMES,
                         "game_ids": [g["game_id"] for g in matured_cov[:P.ENDPOINT_GAMES]]},
            "by_date": _by_date(games)}


def _by_date(games: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for g in games:
        d = out.setdefault(g["date_et"], {"games": 0, "covered": 0, "contracts": 0, "not_played": 0})
        d["games"] += 1
        d["covered"] += int(g["covered"])
        d["contracts"] += g["contracts"]
        d["not_played"] += int(g["not_played"])
    return out


# ---- integrity

def integrity_report(ledger: Ledger, now: Optional[datetime] = None, scope: str = "all", from_date: Optional[str] = None) -> dict:
    """Timing and consistency checks on recorded rows. Reads no outcomes."""
    now = now or datetime.now(timezone.utc)
    info = scope_info(ledger, scope, from_date)
    problems: List[str] = []
    chain = ledger.verify_chain()
    problems += chain["problems"]
    recs = ledger._all(f"SELECT seq, game_id, player_id, market, tipoff_utc, fetched_at, recorded_at, forecast_computed_at, inputs_retrieved_at, league_created_at, "
                       f"date_et, input_hash, league_hash, sim_version, fit_sha256, params_json, slope, code_sha, protocol_version, snapshot FROM ledger WHERE {info['rec_where']}")
    post_tip = bad_order = stale = bad_snapshot = missing_input = 0
    combos = set()
    seen_inputs: Dict[str, bool] = {}
    for r in recs:
        tip, fetched, rec_at = parse_ts(r["tipoff_utc"]), parse_ts(r["fetched_at"]), parse_ts(r["recorded_at"])
        comp, ret, lg = parse_ts(r["forecast_computed_at"]), parse_ts(r["inputs_retrieved_at"]), parse_ts(r["league_created_at"])
        if tip is None or rec_at is None or rec_at >= tip:
            post_tip += 1
        if fetched is None or tip is None or rec_at is None or not (fetched < rec_at and fetched < tip):
            bad_order += 1
        if None in (comp, ret, lg) or not (ret <= comp <= rec_at and lg <= comp):
            bad_order += 1
        else:
            from app.record import fresh_enough
            if not fresh_enough(r["inputs_retrieved_at"], r["league_created_at"], comp, r["date_et"], tip):
                stale += 1
        if r["snapshot"] != P.PRIMARY_SNAPSHOT:
            bad_snapshot += 1
        for h in (r["input_hash"], r["league_hash"]):
            if h not in seen_inputs:
                try:
                    ledger.get_input(h)
                    seen_inputs[h] = True
                except (KeyError, ValueError):
                    seen_inputs[h] = False
            if not seen_inputs[h]:
                missing_input += 1
        combos.add((r["sim_version"], r["fit_sha256"], r["params_json"], r["slope"], r["code_sha"], r["protocol_version"]))
    for name, n in (("records written at or after tip-off", post_tip), ("records with inconsistent timestamps", bad_order),
                    ("records that break the freshness rule", stale), ("records not from the primary snapshot", bad_snapshot),
                    ("records whose saved inputs are missing or corrupt", missing_input)):
        if n:
            problems.append(f"{n} {name}")
    fr = ledger.latest_freeze()
    if fr and scope == "counted":
        import json
        man = json.loads(fr["manifest_json"])
        for c in combos:
            if c[0] != man["fingerprint"]["sim_version"] or c[1] != man["fingerprint"]["fit_sha256"] or c[4] != man["fingerprint"]["code_sha"]:
                problems.append("a counted record was produced by a model or code different from the freeze manifest")
                break
    elif len(combos) > 1 and scope == "counted":
        problems.append("counted records carry more than one model/code fingerprint")
    last_tick = ledger._one("SELECT started_at, ok FROM ticks ORDER BY id DESC LIMIT 1")
    return {"ok": not problems, "rows": len(recs), "chain_head": chain["head"], "problems": problems, "fingerprints": len(combos),
            "last_tick": last_tick, "records_after_tip": post_tip}


def replay_sample(ledger: Ledger, n: int = 10) -> dict:
    """Recompute a sample of forecasts from their saved inputs and compare with the stored row (proves the inputs really are the inputs)."""
    import json
    import numpy as np
    from app.baseline import baseline_draws
    from app.record import forecast_contract
    from app.sim import _seed
    from app.simulation import League, Params, simulate_player
    recs = ledger.records()
    step = max(1, len(recs) // max(1, n))
    mismatches: List[str] = []
    checked = 0
    for r in recs[::step][:n]:
        pj = json.loads(r["params_json"])
        params = Params(**{k: v for k, v in pj.items() if k != "minutes"})
        lobj = ledger.get_input(r["league_hash"])
        lg = League(rate=lobj["rate"], alpha=lobj["alpha"])
        prior = ledger.get_input(r["input_hash"])["games"]
        d = simulate_player(prior, lg, params, np.random.default_rng(_seed(params.seed, r["player_id"], r["game_id"])))
        b = baseline_draws(prior, lg, np.random.default_rng(_seed(params.seed + 101, r["player_id"], r["game_id"])), params.draws)
        f = forecast_contract(d, b, r["market"], r["line"], r["over_price"], r["under_price"], r["slope"])
        checked += 1
        if f["sim_pmf"] != r["sim_pmf"] or f["base_pmf"] != r["base_pmf"] or f["agree"] != r["agree"]:
            mismatches.append(f"seq {r['seq']}")
    return {"checked": checked, "mismatches": mismatches, "ok": not mismatches}


def health_report(ledger: Ledger, now: Optional[datetime] = None) -> dict:
    """Operational alerts. Reads only schedule, tick and record counts, never outcomes."""
    now = now or datetime.now(timezone.utc)
    alerts: List[str] = []
    last = ledger._one("SELECT started_at, ok, detail FROM ticks ORDER BY id DESC LIMIT 1")
    near = [g for g in ledger._all("SELECT game_id, tipoff_utc FROM expected_games") if (t := parse_ts(g["tipoff_utc"])) and now - timedelta(hours=1) <= t <= now + timedelta(hours=12)]
    age = None
    if last is None:
        if near:
            alerts.append("the recorder has never run")
    else:
        age = (now - parse_ts(last["started_at"])).total_seconds() / 60.0
        if near and age > 15:
            alerts.append(f"no recorder tick for {age:.0f} minutes while games are near (is the machine awake and online?)")
        if not last["ok"]:
            alerts.append("the last recorder tick reported errors")
    recent_bad = ledger._one("SELECT COUNT(*) AS n FROM ticks WHERE ok=0 AND started_at >= ?", (ts(now - timedelta(hours=3)),))["n"]
    if recent_bad >= 3:
        alerts.append(f"{recent_bad} failed recorder ticks in the last 3 hours")
    for g in game_rows_all_recent(ledger, now):
        if g["reason"] == "window_not_polled":
            alerts.append(f"game {g['game_id']} tipped without a single recorder tick in its pre-tip window")
        elif g["reason"] in ("no_close_rows_observed", "no_valid_records"):
            alerts.append(f"game {g['game_id']} tipped with no recorded contracts ({g['reason']})")
    chain = ledger.verify_chain()
    if not chain["ok"]:
        alerts.append("ledger integrity check failed: " + "; ".join(chain["problems"][:2]))
    return {"ok": not alerts, "alerts": alerts, "last_tick": last["started_at"] if last else None, "last_tick_age_min": age, "games_near": len(near)}


def game_rows_all_recent(ledger: Ledger, now: datetime) -> List[dict]:
    info = scope_info(ledger, "all")
    cutoff = ts(now - timedelta(hours=30))
    return [g for g in game_rows(ledger, info, now) if g["tipoff_utc"] and g["tipoff_utc"] >= cutoff[:19] and not g["not_played"] and g["in_population"]
            and now >= (parse_ts(g["tipoff_utc"]) + timedelta(minutes=5))]


def dry_run_acceptance(ledger: Ledger, from_date: str, now: Optional[datetime] = None) -> dict:
    """The two dry-run stages. Reads statuses and counts only.

    Stage 1 `recording` (after two full slate days): coverage and integrity at the dry-run thresholds (95% of games, 95% of contracts),
      >= 2 slate dates with covered games from `from_date`, zero records at or after tip-off, chain and mirror verify, recorder healthy.
    Stage 2 `final` (the only one the freeze accepts): stage 1 PLUS
      (a) every expected, played regular-season game on or after `from_date` has matured (tip-off + 3 h + 48 h = 51 h);
      (b) at least one contract matured and the existing settlement thresholds hold on the matured contracts, unchanged: settled or push
          >= 90%, void + unresolved <= 10% overall and <= 25% inside any game;
      (c) a settlement run with no errors started at or after the last of those games matured.
    A dry run with nothing matured yet therefore cannot pass stage 2.
    """
    now = now or datetime.now(timezone.utc)
    cov = coverage_report(ledger, "dry", now, dry_run=True, from_date=from_date)
    integ = integrity_report(ledger, now, "all", from_date)
    health = health_report(ledger, now)
    dates = sorted(d for d, v in cov["by_date"].items() if v["covered"] > 0)
    rec = list(cov["recording_reasons"]) + integ["problems"] + health["alerts"]
    if len(dates) < 2:
        rec.append(f"only {len(dates)} slate date(s) with covered games from {from_date}; at least 2 are required")
    fin = list(rec)
    if not cov["expected_games"]:
        fin.append("no expected games from the dry-run start date")
    if cov["unmatured_games"]:
        fin.append(f"{len(cov['unmatured_games'])} dry-run game(s) have not matured (51 hours after tip-off); last matures {cov['last_maturity_at']}")
    elif cov["expected_games"]:
        if not cov["settlement"]["matured_contracts"]:
            fin.append("no contract has matured, so settlement has not been evidenced")
        fin += cov["settlement_reasons"]
        last = parse_ts(cov["last_maturity_at"])
        ok_runs = [r for r in ledger.settle_runs() if r["errors"] == 0 and (parse_ts(r["run_at"]) or now) >= last]
        if last and not ok_runs:
            fin.append("no error-free settlement run has happened since the last dry-run game matured (run scripts/ledger_daily.py)")
    return {"from_date": from_date, "slate_dates": dates, "recording": {"ok": not rec, "reasons": rec},
            "final": {"ok": not fin, "reasons": fin}, "coverage": cov, "integrity_ok": integ["ok"], "health_ok": health["ok"]}
