"""Shared fixtures for the prospective-ledger tests: an injectable clock, a fake Hardwood client and a quick record factory."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.ledger import canon, ts
from tests.test_backtest import make_players

DATE = "2026-11-20"                                        # board date (ET); every synthetic game log is dated 2026-11-01 .. 14
TIP = datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc)    # 19:00 ET on DATE
NOW = TIP - timedelta(minutes=30)                          # inside the close window
GAME = "g-1"
SECRET = "ctnba_SECRETCANARY1234567890"


class Clock:
    """Deterministic clock: each call advances by `step` so timestamps are strictly increasing."""
    def __init__(self, start=NOW, step=timedelta(milliseconds=20)):
        self.t, self.step = start, step

    def __call__(self):
        self.t += self.step
        return self.t

    def set(self, t):
        self.t = t


def close_row(pid, market="pts", line=15.5, over=-110, under=-110, game=GAME, fetched=None, snapshot="close", **kw):
    r = {"player_id": pid, "game_id": game, "market": market, "name": f"Player {pid}", "team": "OKC", "line": line, "over_price": over,
         "under_price": under, "open_line": line, "snapshot": snapshot, "fetched_at": ts(fetched or (TIP - timedelta(minutes=33))), "actual": None, "result": None}
    r.update(kw)
    return r


class FakeHW:
    """Serves /games, /props (by market) and /query game logs, plus /games/{id} box scores."""
    def __init__(self, players=None, games=None, rows=None, fail=(), boxes=None, secret=SECRET):
        self.players = players if players is not None else make_players(n_players=45)
        self.games = games if games is not None else [{"id": GAME, "home": "OKC", "away": "DEN", "status": "scheduled",
                                                       "tipoff_utc": "2026-11-21T00:00:00.000Z", "season_type": "regular"}]
        self.rows = rows if rows is not None else {m: [close_row(p, m) for p in self.players] for m in ("pts", "reb", "ast")}
        self.fail, self.boxes, self.calls = set(fail), boxes or {}, []
        self.api_key = secret                                       # a canary: it must never reach any ledger file

    async def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/games":
            if "games" in self.fail:
                raise RuntimeError("boom")
            return {"data": [g for g in self.games if params.get("date") == g.get("date_et", DATE)]}
        if path.startswith("/games/"):
            gid = path.split("/")[-1]
            if gid in self.fail:
                raise RuntimeError("boom")
            return self.boxes.get(gid, {"data": {"game": {"status": "final"}, "teams": []}})
        assert path == "/props"
        if params["market"] in self.fail or "props" in self.fail:
            raise RuntimeError("boom")
        return {"data": self.rows.get(params["market"], [])}

    async def post(self, path, body):
        self.calls.append((path, body["id"]))
        if body["id"] in self.fail:
            raise RuntimeError("boom")
        return {"data": self.players[body["id"]][1] if body["id"] in self.players else []}


def box(final=True, n=8, dnp=(), played=None):
    """Box score: n players with minutes on each team (ids t0-0.., t1-0..), plus explicit DNP rows for `dnp` ids and stat rows for `played` {id: stats}."""
    team = lambda k: {"players": [{"player_id": f"t{k}-{i}", "minutes": 20, "pts": 5, "dnp": False} for i in range(n)]}
    teams = [team(0), team(1)]
    for pid in dnp:
        teams[0]["players"].append({"player_id": pid, "minutes": None, "dnp": True, "dnp_reason": "Coach's decision"})
    for pid, stats in (played or {}).items():
        teams[0]["players"].append({"player_id": pid, "dnp": False, **stats})
    return {"data": {"game": {"status": "final" if final else "live"}, "teams": teams}}


_PMF = [0.05, 0.1, 0.2, 0.3, 0.2, 0.1, 0.05]


def make_rec(i, game=GAME, pid=None, market="pts", date=DATE, tip=TIP, line=2.5, recorded=None, agree=1, side="over", **kw):
    """A complete ledger row without running the simulator (for coverage, settlement and analysis tests)."""
    rec = {"game_id": game, "player_id": pid or f"p{i}", "market": market, "date_et": date, "name": f"P{i}", "team": "OKC", "line": line,
           "over_price": -110, "under_price": -110, "snapshot": "close", "fetched_at": ts(tip - timedelta(minutes=34)), "observed_at": ts(tip - timedelta(minutes=29)),
           "tipoff_utc": ts(tip), "market_p_over": 0.55 if side == "over" else 0.45, "market_side": side, "sim_pmf": canon(_PMF), "sim_mean": 3.0, "sim_sd": 1.5,
           "sim_p_over_raw": 0.5, "sim_p_over_cal": 0.5, "sim_side": side, "agree": agree, "base_pmf": canon(_PMF), "base_mean": 3.0, "n_prior": 10,
           "input_hash": "x", "league_hash": "y", "inputs_retrieved_at": ts(tip - timedelta(hours=3)), "league_created_at": ts(tip - timedelta(hours=3)),
           "forecast_computed_at": ts(tip - timedelta(minutes=29)), "recorded_at": ts(recorded or (tip - timedelta(minutes=28))), "sim_version": "v1",
           "fit_sha256": "f", "params_json": "{}", "slope": 0.2, "code_sha": "c", "git_commit": None, "protocol_version": "prospective-v1"}
    rec.update(kw)
    return rec


def seed_game(ledger, game=GAME, tip=TIP, date=DATE, n_players=10, markets=("pts", "reb", "ast"), status="final", season_type="regular", universe=True, **kw):
    """Register a game and write n_players x markets records for it (a covered game by default)."""
    ledger.register_game({"game_id": game, "date_et": date, "tipoff_utc": ts(tip), "status": status, "season_type": season_type, "home": "A", "away": "B"},
                         ts(tip - timedelta(hours=20)))
    n = 0
    for i in range(n_players):
        for m in markets:
            if universe:
                ledger.add_universe(game, f"{game}-p{i}", m, date, ts(tip - timedelta(minutes=30)), "pregame")
            if ledger.append_record(make_rec(i, game=game, pid=f"{game}-p{i}", market=m, date=date, tip=tip, **kw)):
                n += 1
    return n
