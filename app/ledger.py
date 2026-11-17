"""Append-only pregame forecast ledger for the prospective test (separate from the dashboard's recomputable store).

A separate SQLite file. Every table is insert-only: database triggers reject UPDATE and DELETE, each forecast row carries the hash of
the previous row (a chain), and a JSON-lines mirror gets a second copy of every row. Forecasts are written before tip-off and are
never reconstructed afterwards. Later source edits, simulation-cache invalidation or a restart cannot change a recorded row.
Standard library only (the forecasts themselves are computed in app/record.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

GENESIS = "0" * 64

LEDGER_FIELDS = [
    "game_id", "player_id", "market", "date_et", "name", "team", "line", "over_price", "under_price", "snapshot", "fetched_at",
    "observed_at", "tipoff_utc", "market_p_over", "market_side", "sim_pmf", "sim_mean", "sim_sd", "sim_p_over_raw", "sim_p_over_cal",
    "sim_side", "agree", "base_pmf", "base_mean", "n_prior", "input_hash", "league_hash", "inputs_retrieved_at", "league_created_at",
    "forecast_computed_at", "recorded_at", "sim_version", "fit_sha256", "params_json", "slope", "code_sha", "git_commit",
    "protocol_version", "freeze_id",
]
HASHED = LEDGER_FIELDS + ["prev_hash"]
COLUMN_TYPES = {"line": "REAL", "over_price": "REAL", "under_price": "REAL", "market_p_over": "REAL", "sim_mean": "REAL", "sim_sd": "REAL",
                "sim_p_over_raw": "REAL", "sim_p_over_cal": "REAL", "agree": "INTEGER", "base_mean": "REAL", "n_prior": "INTEGER",
                "slope": "REAL", "freeze_id": "INTEGER"}

_LEDGER_COLS = ", ".join(f"{f} {COLUMN_TYPES.get(f, 'TEXT')}" for f in LEDGER_FIELDS)
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_info (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS inputs (input_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, blob BLOB NOT NULL, n_bytes INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS input_index (date_et TEXT NOT NULL, player_id TEXT NOT NULL, input_hash TEXT NOT NULL, retrieved_at TEXT NOT NULL,
  n_games INTEGER, PRIMARY KEY (date_et, player_id));
CREATE TABLE IF NOT EXISTS league_snapshots (date_et TEXT PRIMARY KEY, input_hash TEXT NOT NULL, created_at TEXT NOT NULL, n_players INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS expected_games (game_id TEXT PRIMARY KEY, date_et TEXT NOT NULL, tipoff_utc TEXT, season_type TEXT, status_first TEXT,
  home TEXT, away TEXT, first_seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manifest_events (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL, observed_at TEXT NOT NULL, kind TEXT NOT NULL,
  old_value TEXT, new_value TEXT, UNIQUE (game_id, kind, new_value));
CREATE TABLE IF NOT EXISTS universe (game_id TEXT NOT NULL, player_id TEXT NOT NULL, market TEXT NOT NULL, date_et TEXT NOT NULL,
  first_observed_at TEXT NOT NULL, phase TEXT NOT NULL, PRIMARY KEY (game_id, player_id, market));
CREATE TABLE IF NOT EXISTS ledger (seq INTEGER PRIMARY KEY AUTOINCREMENT, {_LEDGER_COLS}, prev_hash TEXT NOT NULL, row_hash TEXT NOT NULL,
  UNIQUE (game_id, player_id, market));
CREATE TABLE IF NOT EXISTS rejections (id INTEGER PRIMARY KEY AUTOINCREMENT, first_seen_at TEXT NOT NULL, game_id TEXT NOT NULL DEFAULT '',
  player_id TEXT NOT NULL DEFAULT '', market TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL, detail TEXT, UNIQUE (game_id, player_id, market, reason));
CREATE TABLE IF NOT EXISTS source_diffs (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL, game_id TEXT NOT NULL, player_id TEXT NOT NULL,
  market TEXT NOT NULL, kind TEXT NOT NULL, recorded_value TEXT, observed_value TEXT NOT NULL, UNIQUE (game_id, player_id, market, kind, observed_value));
CREATE TABLE IF NOT EXISTS ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, ok INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS settlements (id INTEGER PRIMARY KEY AUTOINCREMENT, ledger_seq INTEGER NOT NULL, settled_at TEXT NOT NULL, version INTEGER NOT NULL,
  status TEXT NOT NULL, actual REAL, outcome TEXT, source TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS verifications (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL, player_id TEXT NOT NULL, status TEXT NOT NULL,
  source TEXT NOT NULL, checked_at TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS freeze (id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL, first_counted_date TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL, manifest_json TEXT NOT NULL, git_commit TEXT, git_clean INTEGER);
CREATE TABLE IF NOT EXISTS analysis_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL, freeze_id INTEGER, games_sha256 TEXT NOT NULL,
  result_sha256 TEXT NOT NULL, result_json TEXT NOT NULL, code_sha TEXT);
CREATE TABLE IF NOT EXISTS settle_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL, games INTEGER, settled INTEGER, changed INTEGER,
  undecided INTEGER, errors INTEGER NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_ledger_game ON ledger (game_id);
CREATE INDEX IF NOT EXISTS idx_settle_seq ON settlements (ledger_seq, id);
"""
APPEND_ONLY = ["inputs", "input_index", "league_snapshots", "expected_games", "manifest_events", "universe", "ledger", "rejections", "source_diffs",
               "ticks", "settlements", "verifications", "freeze", "analysis_runs", "settle_runs"]


def canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def ts(dt: datetime) -> str:
    """Timestamp with microseconds, UTC. Ledger timing rules compare these, so the resolution matters."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _coerce(field: str, value: Any) -> Any:
    """Store numbers exactly as SQLite will return them (REAL as float, INTEGER as int), so the hash computed at write time equals the one recomputed on read."""
    if value is None:
        return None
    kind = COLUMN_TYPES.get(field, "TEXT")
    if kind == "REAL":
        return float(value)
    if kind == "INTEGER":
        return int(value)
    return value


def row_hash(rec: Dict[str, Any]) -> str:
    return sha256_text(canon({k: rec.get(k) for k in HASHED}))


class Ledger:
    def __init__(self, path: Any, mirror_dir: Any = None):
        self.path = str(path)
        self.mirror_dir = Path(mirror_dir) if mirror_dir else None
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if self.mirror_dir:
            self.mirror_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)       # explicit transactions
        self.conn.row_factory = sqlite3.Row
        for pragma in ("journal_mode=WAL", "synchronous=FULL", "busy_timeout=15000"):
            self.conn.execute(f"PRAGMA {pragma}")
        self.conn.executescript(SCHEMA)
        for t in APPEND_ONLY:
            self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT, 'ledger tables are append-only'); END")
            self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT, 'ledger tables are append-only'); END")
        self.conn.execute("INSERT OR IGNORE INTO schema_info (key, value) VALUES ('created', ?)", (ts(datetime.now(timezone.utc)),))

    def close(self) -> None:
        self.conn.close()

    def _all(self, sql: str, args: Iterable = ()) -> List[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def _one(self, sql: str, args: Iterable = ()) -> Optional[dict]:
        r = self.conn.execute(sql, tuple(args)).fetchone()
        return dict(r) if r else None

    # ---- content-addressed inputs

    def add_input(self, kind: str, obj: Any, created_at: str) -> str:
        text = canon(obj)
        h = sha256_text(text)
        blob = zlib.compress(text.encode("utf-8"), 6)
        self.conn.execute("INSERT OR IGNORE INTO inputs (input_hash, kind, blob, n_bytes, created_at) VALUES (?,?,?,?,?)", (h, kind, blob, len(text), created_at))
        return h

    def get_input(self, h: str) -> Any:
        r = self._one("SELECT blob FROM inputs WHERE input_hash=?", (h,))
        if r is None:
            raise KeyError(h)
        text = zlib.decompress(r["blob"]).decode("utf-8")
        if sha256_text(text) != h:
            raise ValueError(f"input {h} does not match its hash")
        return json.loads(text)

    def index_input(self, date_et: str, player_id: str, h: str, retrieved_at: str, n_games: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO input_index (date_et, player_id, input_hash, retrieved_at, n_games) VALUES (?,?,?,?,?)",
                          (date_et, player_id, h, retrieved_at, n_games))

    def player_input(self, date_et: str, player_id: str) -> Optional[dict]:
        return self._one("SELECT * FROM input_index WHERE date_et=? AND player_id=?", (date_et, player_id))

    def league_snapshot(self, date_et: str) -> Optional[dict]:
        return self._one("SELECT * FROM league_snapshots WHERE date_et=?", (date_et,))

    def add_league_snapshot(self, date_et: str, h: str, created_at: str, n_players: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO league_snapshots (date_et, input_hash, created_at, n_players) VALUES (?,?,?,?)", (date_et, h, created_at, n_players))

    # ---- expected-game manifest and observed contract universe

    def register_game(self, g: dict, observed_at: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO expected_games (game_id, date_et, tipoff_utc, season_type, status_first, home, away, first_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (g["game_id"], g["date_et"], g.get("tipoff_utc"), g.get("season_type"), g.get("status"), g.get("home"), g.get("away"), observed_at))
        new = cur.rowcount == 1
        if not new:
            first = self._one("SELECT tipoff_utc, status_first FROM expected_games WHERE game_id=?", (g["game_id"],))
            if first and g.get("tipoff_utc") and first["tipoff_utc"] != g.get("tipoff_utc"):
                self.conn.execute("INSERT OR IGNORE INTO manifest_events (game_id, observed_at, kind, old_value, new_value) VALUES (?,?,?,?,?)",
                                  (g["game_id"], observed_at, "tipoff", first["tipoff_utc"], g.get("tipoff_utc")))
        if g.get("status"):
            last = self._one("SELECT new_value FROM manifest_events WHERE game_id=? AND kind='status' ORDER BY id DESC LIMIT 1", (g["game_id"],))
            prev = last["new_value"] if last else self._one("SELECT status_first FROM expected_games WHERE game_id=?", (g["game_id"],))["status_first"]
            if prev != g["status"]:
                self.conn.execute("INSERT OR IGNORE INTO manifest_events (game_id, observed_at, kind, old_value, new_value) VALUES (?,?,?,?,?)",
                                  (g["game_id"], observed_at, "status", prev, g["status"]))
        return new

    def expected_game(self, game_id: str) -> Optional[dict]:
        return self._one("SELECT * FROM expected_games WHERE game_id=?", (game_id,))

    def latest_status(self, game_id: str) -> Optional[str]:
        r = self._one("SELECT new_value FROM manifest_events WHERE game_id=? AND kind='status' ORDER BY id DESC LIMIT 1", (game_id,))
        if r:
            return r["new_value"]
        e = self.expected_game(game_id)
        return e["status_first"] if e else None

    def add_universe(self, game_id: str, player_id: str, market: str, date_et: str, observed_at: str, phase: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO universe (game_id, player_id, market, date_et, first_observed_at, phase) VALUES (?,?,?,?,?,?)",
                          (game_id, player_id, market, date_et, observed_at, phase))

    # ---- the forecast chain

    def has_record(self, game_id: str, player_id: str, market: str) -> bool:
        return self._one("SELECT 1 AS x FROM ledger WHERE game_id=? AND player_id=? AND market=?", (game_id, player_id, market)) is not None

    def record(self, game_id: str, player_id: str, market: str) -> Optional[dict]:
        return self._one("SELECT * FROM ledger WHERE game_id=? AND player_id=? AND market=?", (game_id, player_id, market))

    def _freeze_for(self, date_et: str, recorded_at: str) -> Optional[int]:
        r = self._one("SELECT id FROM freeze WHERE first_counted_date <= ? AND recorded_at <= ? ORDER BY id DESC LIMIT 1", (date_et, recorded_at))
        return r["id"] if r else None

    def append_record(self, rec: Dict[str, Any]) -> Optional[int]:
        """Write one forecast row. Returns its sequence number, or None if that contract already has a primary record."""
        rec = {k: _coerce(k, rec.get(k)) for k in LEDGER_FIELDS}
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.has_record(rec["game_id"], rec["player_id"], rec["market"]):
                self.conn.execute("ROLLBACK")
                return None
            last = self._one("SELECT row_hash FROM ledger ORDER BY seq DESC LIMIT 1")
            rec["freeze_id"] = self._freeze_for(rec["date_et"], rec["recorded_at"])
            rec["prev_hash"] = last["row_hash"] if last else GENESIS
            rec["row_hash"] = row_hash(rec)
            cols = LEDGER_FIELDS + ["prev_hash", "row_hash"]
            cur = self.conn.execute(f"INSERT INTO ledger ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})", [rec[c] for c in cols])
            seq = cur.lastrowid
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        self._mirror({"seq": seq, **rec})
        return seq

    def _mirror(self, rec: dict) -> None:
        if not self.mirror_dir:
            return
        try:
            with open(self.mirror_dir / f"ledger-{rec['date_et']}.jsonl", "a", encoding="utf-8") as f:
                f.write(canon(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass                                                           # the integrity check reports a missing mirror line

    def records(self, where: str = "1=1", args: Iterable = ()) -> List[dict]:
        return self._all(f"SELECT * FROM ledger WHERE {where} ORDER BY seq", args)

    def reject(self, first_seen_at: str, reason: str, game_id: str = "", player_id: str = "", market: str = "", detail: Any = None) -> None:
        self.conn.execute("INSERT OR IGNORE INTO rejections (first_seen_at, game_id, player_id, market, reason, detail) VALUES (?,?,?,?,?,?)",
                          (first_seen_at, game_id, player_id, market, reason, canon(detail) if detail is not None else None))

    def rejected_reasons(self, game_id: str, player_id: str, market: str) -> List[str]:
        return [r["reason"] for r in self._all("SELECT reason FROM rejections WHERE game_id=? AND player_id=? AND market=?", (game_id, player_id, market))]

    def source_diff(self, observed_at: str, game_id: str, player_id: str, market: str, kind: str, recorded: Any, observed: Any) -> None:
        self.conn.execute("INSERT OR IGNORE INTO source_diffs (observed_at, game_id, player_id, market, kind, recorded_value, observed_value) VALUES (?,?,?,?,?,?,?)",
                          (observed_at, game_id, player_id, market, kind, str(recorded), str(observed)))

    def add_tick(self, started_at: str, finished_at: str, ok: bool, detail: Any) -> None:
        self.conn.execute("INSERT INTO ticks (started_at, finished_at, ok, detail) VALUES (?,?,?,?)", (started_at, finished_at, 1 if ok else 0, canon(detail)))

    # ---- settlement and verification

    def latest_settlement(self, seq: int) -> Optional[dict]:
        return self._one("SELECT * FROM settlements WHERE ledger_seq=? ORDER BY id DESC LIMIT 1", (seq,))

    def add_settlement(self, seq: int, settled_at: str, status: str, actual: Optional[float], outcome: Optional[str], source: str, detail: Any) -> None:
        v = (self._one("SELECT COUNT(*) AS n FROM settlements WHERE ledger_seq=?", (seq,)) or {"n": 0})["n"] + 1
        self.conn.execute("INSERT INTO settlements (ledger_seq, settled_at, version, status, actual, outcome, source, detail) VALUES (?,?,?,?,?,?,?,?)",
                          (seq, settled_at, v, status, actual, outcome, source, canon(detail)))

    def settlement_history(self, seq: int) -> List[dict]:
        return self._all("SELECT * FROM settlements WHERE ledger_seq=? ORDER BY id", (seq,))

    def add_verification(self, game_id: str, player_id: str, status: str, source: str, checked_at: str, note: str = "") -> None:
        self.conn.execute("INSERT INTO verifications (game_id, player_id, status, source, checked_at, note) VALUES (?,?,?,?,?,?)",
                          (game_id, player_id, status, source, checked_at, note))

    def latest_verification(self, game_id: str, player_id: str) -> Optional[dict]:
        return self._one("SELECT * FROM verifications WHERE game_id=? AND player_id=? ORDER BY id DESC LIMIT 1", (game_id, player_id))

    # ---- freeze

    def add_freeze(self, recorded_at: str, first_counted_date: str, manifest: dict, git_commit: Optional[str], git_clean: Optional[bool]) -> int:
        text = canon(manifest)
        cur = self.conn.execute("INSERT INTO freeze (recorded_at, first_counted_date, manifest_sha256, manifest_json, git_commit, git_clean) VALUES (?,?,?,?,?,?)",
                                (recorded_at, first_counted_date, sha256_text(text), text, git_commit, None if git_clean is None else int(git_clean)))
        return cur.lastrowid

    def latest_freeze(self) -> Optional[dict]:
        return self._one("SELECT * FROM freeze ORDER BY id DESC LIMIT 1")

    def add_analysis_run(self, run_at: str, freeze_id: Optional[int], games_sha: str, result: dict, code_sha: Optional[str]) -> int:
        text = canon(result)
        cur = self.conn.execute("INSERT INTO analysis_runs (run_at, freeze_id, games_sha256, result_sha256, result_json, code_sha) VALUES (?,?,?,?,?,?)",
                                (run_at, freeze_id, games_sha, sha256_text(text), text, code_sha))
        return cur.lastrowid

    def add_settle_run(self, run_at: str, summary: dict) -> None:
        self.conn.execute("INSERT INTO settle_runs (run_at, games, settled, changed, undecided, errors, detail) VALUES (?,?,?,?,?,?,?)",
                          (run_at, summary.get("games"), summary.get("settled"), summary.get("changed"), summary.get("undecided"),
                           len(summary.get("errors") or []), canon(summary)))

    def settle_runs(self) -> List[dict]:
        return self._all("SELECT * FROM settle_runs ORDER BY id")

    def analysis_runs(self) -> List[dict]:
        return self._all("SELECT * FROM analysis_runs ORDER BY id")

    # ---- integrity

    def triggers_ok(self) -> List[str]:
        have = {r["name"] for r in self._all("SELECT name FROM sqlite_master WHERE type='trigger'")}
        return [f"{t}_{k}" for t in APPEND_ONLY for k in ("no_update", "no_delete") if f"{t}_{k}" not in have]

    def verify_chain(self) -> dict:
        problems: List[str] = []
        prev = GENESIS
        n = 0
        mirrored: Dict[str, set] = {}
        for r in self.conn.execute("SELECT * FROM ledger ORDER BY seq"):
            rec = dict(r)
            n += 1
            if rec["prev_hash"] != prev:
                problems.append(f"seq {rec['seq']}: chain broken (prev_hash does not match the previous row)")
            if row_hash(rec) != rec["row_hash"]:
                problems.append(f"seq {rec['seq']}: row content does not match its hash")
            prev = rec["row_hash"]
            if self.mirror_dir:
                d = rec["date_et"]
                if d not in mirrored:
                    path = self.mirror_dir / f"ledger-{d}.jsonl"
                    hashes = set()
                    if path.exists():
                        for line in path.read_text(encoding="utf-8").splitlines():
                            try:
                                hashes.add(json.loads(line).get("row_hash"))
                            except ValueError:
                                problems.append(f"mirror {path.name}: unreadable line")
                    mirrored[d] = hashes
                if rec["row_hash"] not in mirrored[d]:
                    problems.append(f"seq {rec['seq']}: not found in the mirror file")
        missing = self.triggers_ok()
        if missing:
            problems.append("missing append-only triggers: " + ", ".join(missing))
        return {"ok": not problems, "rows": n, "head": prev, "problems": problems}
