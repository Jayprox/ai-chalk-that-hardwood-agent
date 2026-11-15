"""SQLite store for the props dashboard. Standard library only.

Tables
  games          one row per game (state is derived by the collector from status and tip-off)
  props_current  latest known row per (player, game, market); `active` = present in the most recent successful collection
  prop_history   one row each time a prop's line or prices changed (a step history: no row means unchanged)
  runs           one row per collection, with per-market counts and errors
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from app.signal import add_percentiles

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  game_id TEXT PRIMARY KEY, date_et TEXT, home TEXT, away TEXT, tipoff_utc TEXT, status TEXT, state TEXT,
  props_open_at TEXT, props_close_at TEXT, home_score INTEGER, away_score INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS props_current (
  player_id TEXT, game_id TEXT, market TEXT, date_et TEXT, name TEXT, team TEXT, opponent TEXT, venue TEXT,
  line REAL, open_line REAL, over_price REAL, under_price REAL, snapshot_kind TEXT,
  p_over REAL, side TEXT, strength REAL, line_move REAL, actual REAL, result TEXT, extra TEXT,
  first_seen_at TEXT, last_seen_at TEXT, active INTEGER DEFAULT 1,
  PRIMARY KEY (player_id, game_id, market)
);
CREATE INDEX IF NOT EXISTS idx_props_market ON props_current (market, date_et, active);
CREATE TABLE IF NOT EXISTS prop_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, player_id TEXT, game_id TEXT, market TEXT, collected_at TEXT,
  line REAL, over_price REAL, under_price REAL, snapshot_kind TEXT, p_over REAL, strength REAL
);
CREATE INDEX IF NOT EXISTS idx_hist_key ON prop_history (player_id, game_id, market, collected_at);
CREATE TABLE IF NOT EXISTS sims (
  player_id TEXT, game_id TEXT, market TEXT, date_et TEXT, status TEXT, mean REAL, sd REAL, n_prior INTEGER, pmf TEXT, computed_at TEXT,
  PRIMARY KEY (player_id, game_id, market)
);
CREATE INDEX IF NOT EXISTS idx_sims_date ON sims (date_et);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, date_et TEXT, rows INTEGER, changed INTEGER,
  skipped INTEGER, ok INTEGER, detail TEXT
);
"""

PROP_FIELDS = ["date_et", "name", "team", "opponent", "venue", "line", "open_line", "over_price", "under_price", "snapshot_kind",
               "p_over", "side", "strength", "line_move", "actual", "result", "extra"]
# A change in any of these writes a history row.
TRACKED = ("line", "over_price", "under_price", "snapshot_kind")


class Store:
    def __init__(self, path: Any):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(props_current)")}
        if "extra" not in cols:  # database created before the extra column existed
            self.conn.execute("ALTER TABLE props_current ADD COLUMN extra TEXT")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- writes

    def upsert_games(self, games: Iterable[dict], now: str) -> int:
        n = 0
        with self.tx() as c:
            for g in games:
                c.execute(
                    "INSERT INTO games (game_id, date_et, home, away, tipoff_utc, status, state, props_open_at, props_close_at,"
                    " home_score, away_score, updated_at) VALUES (:game_id,:date_et,:home,:away,:tipoff_utc,:status,:state,"
                    ":props_open_at,:props_close_at,:home_score,:away_score,:now) ON CONFLICT(game_id) DO UPDATE SET "
                    "date_et=excluded.date_et, home=excluded.home, away=excluded.away, tipoff_utc=excluded.tipoff_utc,"
                    "status=excluded.status, state=excluded.state, props_open_at=excluded.props_open_at,"
                    "props_close_at=excluded.props_close_at, home_score=excluded.home_score, away_score=excluded.away_score,"
                    "updated_at=excluded.updated_at",
                    {**{k: g.get(k) for k in ("game_id", "date_et", "home", "away", "tipoff_utc", "status", "state", "props_open_at",
                                              "props_close_at", "home_score", "away_score")}, "now": now})
                n += 1
        return n

    def upsert_props(self, rows: Iterable[dict], now: str) -> dict:
        """Insert or update the current row; append a history row on the first sighting and whenever a tracked field changed."""
        changed = inserted = 0
        with self.tx() as c:
            for r in rows:
                key = (r["player_id"], r["game_id"], r["market"])
                old = c.execute("SELECT * FROM props_current WHERE player_id=? AND game_id=? AND market=?", key).fetchone()
                vals = {f: r.get(f) for f in PROP_FIELDS}
                if old is None:
                    c.execute(
                        f"INSERT INTO props_current (player_id, game_id, market, {', '.join(PROP_FIELDS)}, first_seen_at, last_seen_at, active)"
                        f" VALUES (?,?,?,{','.join('?' * len(PROP_FIELDS))},?,?,1)", (*key, *vals.values(), now, now))
                    inserted += 1
                    self._history(c, key, vals, now)
                    continue
                if any(old[k] != vals[k] for k in TRACKED):
                    changed += 1
                    self._history(c, key, vals, now)
                sets = ", ".join(f"{f}=?" for f in PROP_FIELDS)
                c.execute(f"UPDATE props_current SET {sets}, last_seen_at=?, active=1 WHERE player_id=? AND game_id=? AND market=?",
                          (*vals.values(), now, *key))
        return {"inserted": inserted, "changed": changed}

    @staticmethod
    def _history(c: sqlite3.Connection, key: tuple, vals: dict, now: str) -> None:
        c.execute("INSERT INTO prop_history (player_id, game_id, market, collected_at, line, over_price, under_price, snapshot_kind,"
                  " p_over, strength) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (*key, now, vals["line"], vals["over_price"], vals["under_price"], vals["snapshot_kind"], vals["p_over"], vals["strength"]))

    def deactivate_missing(self, date_et: str, market: str, seen_at: str) -> int:
        """Props for this date and market that the latest successful collection did not return are marked inactive."""
        with self.tx() as c:
            cur = c.execute("UPDATE props_current SET active=0 WHERE date_et=? AND market=? AND last_seen_at<? AND active=1",
                            (date_et, market, seen_at))
            return cur.rowcount

    def record_run(self, started_at: str, finished_at: str, date_et: str, rows: int, changed: int, skipped: int, ok: bool, detail: dict) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO runs (started_at, finished_at, date_et, rows, changed, skipped, ok, detail) VALUES (?,?,?,?,?,?,?,?)",
                            (started_at, finished_at, date_et, rows, changed, skipped, int(ok), json.dumps(detail, default=str)))
            return int(cur.lastrowid)

    # ---- reads

    def games_for(self, date_et: str) -> List[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM games WHERE date_et=? ORDER BY tipoff_utc, game_id", (date_et,))]

    def props(self, date_et: str, market: Optional[str] = None, game_id: Optional[str] = None, active_only: bool = True) -> List[dict]:
        q, args = "SELECT * FROM props_current WHERE date_et=?", [date_et]
        if market:
            q += " AND market=?"; args.append(market)
        if game_id:
            q += " AND game_id=?"; args.append(game_id)
        if active_only:
            q += " AND active=1"
        rows = [dict(r) for r in self.conn.execute(q, args)]
        if market is None and game_id is not None:
            # percentiles are always ranked against the whole market for the date, not just one game's slice
            pool = {(r["player_id"], r["game_id"], r["market"]): r for r in add_percentiles(self.props(date_et, None, None, active_only))}
            for r in rows:
                r["pct"] = pool[(r["player_id"], r["game_id"], r["market"])]["pct"]
            rows.sort(key=lambda r: (r["pct"] is None, -(r["pct"] or 0), -abs(r.get("line_move") or 0), r.get("name") or ""))
            return rows
        if game_id is not None and market is not None:
            pool = {(r["player_id"], r["game_id"]): r for r in add_percentiles(self.props(date_et, market, None, active_only))}
            for r in rows:
                r["pct"] = pool[(r["player_id"], r["game_id"])]["pct"]
            rows.sort(key=lambda r: (r["pct"] is None, -(r["pct"] or 0), -abs(r.get("line_move") or 0), r.get("name") or ""))
            return rows
        return add_percentiles(rows)

    def history(self, player_id: str, game_id: str, market: str) -> List[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM prop_history WHERE player_id=? AND game_id=? AND market=? ORDER BY collected_at, id", (player_id, game_id, market))]

    def last_run(self) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def histories(self, date_et: str, market: str) -> Dict[tuple, List[dict]]:
        """Step history for every prop of a market and date: (player_id, game_id) -> rows in time order."""
        out: Dict[tuple, List[dict]] = {}
        q = ("SELECT h.* FROM prop_history h JOIN props_current c ON c.player_id=h.player_id AND c.game_id=h.game_id AND c.market=h.market "
             "WHERE c.date_et=? AND c.market=? ORDER BY h.collected_at, h.id")
        for r in self.conn.execute(q, (date_et, market)):
            out.setdefault((r["player_id"], r["game_id"]), []).append(dict(r))
        return out

    def prop(self, player_id: str, game_id: str, market: str) -> Optional[dict]:
        r = self.conn.execute("SELECT * FROM props_current WHERE player_id=? AND game_id=? AND market=?", (player_id, game_id, market)).fetchone()
        return dict(r) if r else None

    def game(self, game_id: str) -> Optional[dict]:
        r = self.conn.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        return dict(r) if r else None

    def latest_date(self) -> Optional[str]:
        r = self.conn.execute("SELECT MAX(d) FROM (SELECT MAX(date_et) d FROM games UNION ALL SELECT MAX(date_et) FROM props_current)").fetchone()
        return r[0] if r and r[0] else None

    def has_data(self, date_et: str) -> bool:
        a = self.conn.execute("SELECT 1 FROM games WHERE date_et=? LIMIT 1", (date_et,)).fetchone()
        b = self.conn.execute("SELECT 1 FROM props_current WHERE date_et=? LIMIT 1", (date_et,)).fetchone()
        return bool(a or b)

    def last_run_for(self, date_et: str) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM runs WHERE date_et=? ORDER BY id DESC LIMIT 1", (date_et,)).fetchone()
        return dict(r) if r else None

    def recent_game_dates(self, before_date: str, days: int = 3) -> List[str]:
        """Dates before `before_date`, within `days` days, that have games stored (for picking up late results)."""
        rows = self.conn.execute("SELECT DISTINCT date_et FROM games WHERE date_et<? AND date_et>=date(?, ?) ORDER BY date_et",
                                 (before_date, before_date, f"-{int(days)} days"))
        return [r[0] for r in rows]

    # ---- simulation results and small key-value settings

    def upsert_sims(self, rows: Iterable[dict]) -> int:
        n = 0
        with self.tx() as c:
            for r in rows:
                c.execute("INSERT INTO sims (player_id, game_id, market, date_et, status, mean, sd, n_prior, pmf, computed_at) "
                          "VALUES (:player_id,:game_id,:market,:date_et,:status,:mean,:sd,:n_prior,:pmf,:computed_at) "
                          "ON CONFLICT(player_id, game_id, market) DO UPDATE SET date_et=excluded.date_et, status=excluded.status, "
                          "mean=excluded.mean, sd=excluded.sd, n_prior=excluded.n_prior, pmf=excluded.pmf, computed_at=excluded.computed_at", r)
                n += 1
        return n

    def sims_for(self, date_et: str) -> Dict[tuple, dict]:
        return {(r["player_id"], r["game_id"], r["market"]): dict(r)
                for r in self.conn.execute("SELECT * FROM sims WHERE date_et=?", (date_et,))}

    def get_meta(self, key: str) -> Optional[Any]:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def set_meta(self, key: str, value: Any, now: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO meta (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                      (key, json.dumps(value), now))
