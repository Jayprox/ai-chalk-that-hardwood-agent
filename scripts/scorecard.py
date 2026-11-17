"""Descriptive projection scorecard: simulator vs Dimers vs the posted line, scored against final box-score stats.

SEPARATE FROM PROSPECTIVE VALIDATION. Never opens data/ledger, never reads the `result` column, never computes hit rates
or returns. It measures projection error only (bias, mean absolute error, RMSE). Nothing here changes the model.

    python -m scripts.scorecard --date 2026-11-16 --dimers ~/Downloads/nba_player_projections_3.csv --retrieved "2026-11-16 09:43 PT"
    python -m scripts.scorecard --date 2026-11-16                 # repeat the day without Dimers (simulator and line only)
    python -m scripts.scorecard --summary                          # cumulative scorecard over every day collected so far

Fairness rules, applied per player-game:
  - the simulator mean counts only if it was computed before that game's tip-off
  - a Dimers row counts only if its own `Last Updated` is before that game's tip-off (and a file with no such rows adds nothing)
  - the posted line is the stored line for the prop (a pre-tip number the market published)
  - scored only where all the sources present for that row can be compared on the same rows ("common rows")
  - a player-game with no final stat, or whose stats are all zero (did not play), is excluded and counted
Review is meant to be periodic (weekly), not daily; the summary says how many days it covers.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
import sqlite3
import statistics as st
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research" / "scorecard"
MARKETS = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM", "blk": "BLK", "stl": "STL", "pra": "PRA", "pr": "PR", "pa": "PA"}
ROW_FIELDS = ["date", "market", "player_id", "name", "team", "game_id", "line", "sim_mean", "dimers", "actual"]


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", s)
    s = s.replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def read_dimers(path: Path) -> dict:
    """(team, normalized name) -> row. Later rows win on a repeat."""
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            out[(r["Team"], norm(r["First Name"] + " " + r["Last Name"]))] = r
    return out


def _check_db(db: Path) -> None:
    if "ledger" in db.resolve().parts:
        raise SystemExit("refusing to open anything under data/ledger: the scorecard is separate from prospective validation")


def build_rows(db: Path, date: str, dimers: dict | None) -> tuple[list[dict], dict]:
    """One row per player-game-market that has a stored line. Explicit column list: `result` is never selected."""
    _check_db(db)
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tips = {g: parse_ts(t) for g, t in c.execute("SELECT game_id, tipoff_utc FROM games WHERE date_et=?", (date,))}
        sims = {(p, g, m): (mean, parse_ts(at), status) for p, g, m, mean, at, status in
                c.execute("SELECT player_id, game_id, market, mean, computed_at, status FROM sims WHERE date_et=?", (date,))}
        props = c.execute("SELECT player_id, game_id, market, name, team, line, actual FROM props_current WHERE date_et=?", (date,)).fetchall()
    finally:
        c.close()
    by_pg: dict = defaultdict(list)
    for p, g, m, name, team, line, actual in props:
        if m in MARKETS:
            by_pg[(p, g)].append((m, actual))
    excluded = {"no_final_stat": 0, "did_not_play": 0, "dimers_after_tip": 0, "sim_after_tip": 0}
    dnp = set()
    for (p, g), ms in by_pg.items():
        vals = [a for _, a in ms if a is not None]
        if vals and all(a == 0 for a in vals):
            dnp.add((p, g))
    rows = []
    for p, g, m, name, team, line, actual in props:
        if m not in MARKETS:
            continue
        if actual is None:
            excluded["no_final_stat"] += 1
            continue
        if (p, g) in dnp:
            excluded["did_not_play"] += 1
            continue
        tip = tips.get(g)
        sm = sims.get((p, g, m))
        sim_mean = None
        if sm and sm[2] == "ok" and sm[0] is not None:
            if tip and sm[1] and sm[1] < tip:
                sim_mean = sm[0]
            else:
                excluded["sim_after_tip"] += 1
        d = None
        if dimers is not None:
            r = dimers.get((team, norm(name)))
            if r:
                upd = parse_ts(r.get("Last Updated"))
                if tip and upd and upd < tip:
                    d = float(r[MARKETS[m]])
                else:
                    excluded["dimers_after_tip"] += 1
        rows.append({"date": date, "market": m, "player_id": p, "name": name, "team": team, "game_id": g,
                     "line": line, "sim_mean": sim_mean, "dimers": d, "actual": actual})
    return rows, excluded


def write_rows(rows: list[dict], date: str, out: Path = OUT) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"rows_{date}.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ROW_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r[k] is None else r[k]) for k in ROW_FIELDS})
    return p


def read_all_rows(out: Path = OUT) -> list[dict]:
    rows = []
    for p in sorted(out.glob("rows_*.csv")):
        with open(p, newline="") as f:
            for r in csv.DictReader(f):
                rows.append({k: (float(r[k]) if k in ("line", "sim_mean", "dimers", "actual") and r[k] != "" else (None if k in ("line", "sim_mean", "dimers", "actual") else r[k])) for k in ROW_FIELDS})
    return rows


def _err(rows, key):
    e = [r[key] - r["actual"] for r in rows]
    return {"bias": st.mean(e), "mae": st.mean(abs(x) for x in e), "rmse": (st.mean(x * x for x in e)) ** 0.5}


def score(rows: list[dict]) -> dict:
    """Per market: error of each source on the rows where every compared source is present. Projection error only."""
    out = {}
    for m in MARKETS:
        mr = [r for r in rows if r["market"] == m and r["line"] is not None]
        if not mr:
            continue
        entry = {"rows": len(mr)}
        for srcs in (("sim_mean", "line"), ("dimers", "line"), ("sim_mean", "dimers", "line")):
            common = [r for r in mr if all(r[s] is not None for s in srcs)]
            if common:
                entry["+".join(srcs)] = {"n": len(common), **{s: _err(common, s) for s in srcs}}
        out[m] = entry
    return out


def summary_text(rows: list[dict]) -> str:
    days = sorted({r["date"] for r in rows})
    sc = score(rows)
    L = ["# Projection scorecard (descriptive)", "",
         "Separate from prospective validation. Projection error only: no hit rates, returns or bets. Does not change the model.",
         f"Days covered: {len(days)} ({days[0] if days else '-'} to {days[-1] if days else '-'}). Player-market rows: {len(rows)}.",
         "Bias = mean(projection - actual); MAE = mean absolute error; RMSE = root mean squared error. All three sources are compared only on rows where each is present.",
         "A short run is noisy: players in one game share outcomes and minutes, so rows are not independent. Read this weekly, not daily, and treat gaps smaller than the day-to-day spread as no difference.", ""]
    for combo, title in (("sim_mean+dimers+line", "Simulator vs Dimers vs posted line (common rows)"), ("sim_mean+line", "Simulator vs posted line"), ("dimers+line", "Dimers vs posted line")):
        L += [f"## {title}", "", "| market | rows | " + " | ".join(f"{s} bias | {s} MAE | {s} RMSE" for s in combo.split("+")) + " |",
              "|---|---|" + "---|" * (3 * len(combo.split("+")))]
        for m, e in sc.items():
            if combo in e:
                c = e[combo]
                L.append(f"| {m} | {c['n']} | " + " | ".join(f"{c[s]['bias']:+.2f} | {c[s]['mae']:.2f} | {c[s]['rmse']:.2f}" for s in combo.split("+")) + " |")
        L.append("")
    return "\n".join(L) + "\n"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="board date (ET) to score, YYYY-MM-DD")
    ap.add_argument("--dimers", type=Path, help="Dimers projections CSV captured before tip-off")
    ap.add_argument("--retrieved", help="when and where the CSV was downloaded, for the provenance note")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "props.db")
    ap.add_argument("--summary", action="store_true", help="aggregate every day collected so far")
    a = ap.parse_args(argv)
    if a.summary:
        rows = read_all_rows()
        if not rows:
            print("no scored days yet")
            return 0
        txt = summary_text(rows)
        (OUT / "scorecard.md").write_text(txt)
        print(txt)
        return 0
    if not a.date:
        ap.error("--date is required unless --summary is used")
    dimers = None
    if a.dimers:
        if not a.retrieved:
            ap.error("--retrieved is required with --dimers (when and where the file was downloaded)")
        OUT.mkdir(parents=True, exist_ok=True)
        keep = OUT / "dimers" / f"{a.date}_{a.dimers.name}"
        keep.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(a.dimers, keep)
        (OUT / "dimers" / f"{a.date}_provenance.txt").write_text(f"file: {a.dimers.name}\nsha256: {sha256(keep)}\nretrieved: {a.retrieved}\n")
        dimers = read_dimers(keep)
    rows, excluded = build_rows(a.db, a.date, dimers)
    p = write_rows(rows, a.date)
    print(f"{a.date}: {len(rows)} rows -> {p}")
    print("excluded:", ", ".join(f"{k} {v}" for k, v in excluded.items()))
    print("with sim:", sum(r["sim_mean"] is not None for r in rows), "with dimers:", sum(r["dimers"] is not None for r in rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
