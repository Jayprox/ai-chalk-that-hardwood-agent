"""Scheduler: decides when the board is due for collection and runs it.

    python -m app.scheduler --once        one tick: collect whatever is due, then exit (what launchd runs every 5 minutes)
    python -m app.scheduler --status      show what is due and when, without collecting
    python -m app.scheduler --loop        tick every minute in the foreground (handy for testing with short intervals)
    python -m app.scheduler --print-launchd   print a macOS launchd plist for this checkout and Python

Cadence (all in .env, see config.py): every COLLECT_EARLY_MIN minutes early in the day; from COLLECT_WINDOW_HOURS before a
game's tip-off every COLLECT_ACTIVE_MIN minutes until tip-off; after tip-off every COLLECT_SETTLE_MIN minutes, only to pick up
final scores and results. A failed collection is retried after COLLECT_RETRY_MIN minutes. The decision depends only on the clock,
the stored games and the last run, so a missed tick (sleeping computer) simply collects on the next one.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.collector import _iso, _parse_utc, collect_once, game_state
from app.config import ROOT, Settings, current_season, load_settings, now_et
from app.store import Store

MIN_GAP_MIN = 5          # never collect twice within this many minutes
ABANDON_HOURS = 24       # a game not final this long after tip-off is ignored (postponed, abandoned, bad data)


@dataclass(frozen=True)
class Schedule:
    early_min: int = 120
    active_min: int = 30
    window_hours: float = 3.0
    settle_min: int = 60
    retry_min: int = 10

    @property
    def window(self) -> timedelta:
        return timedelta(hours=self.window_hours)

    @classmethod
    def from_settings(cls, s: Settings) -> "Schedule":
        return cls(s.collect_early_min, s.collect_active_min, s.collect_window_hours, s.collect_settle_min, s.collect_retry_min)


@dataclass(frozen=True)
class Decision:
    date: str
    due: bool
    reason: str
    next_due: Optional[datetime]
    interval_min: Optional[int]
    phases: Dict[str, int]


def game_phase(g: dict, now: datetime, sched: Schedule) -> str:
    """upcoming | active | tipped | final | abandoned, recomputed from the stored status and tip-off at `now`."""
    state = game_state(g.get("status"), g.get("tipoff_utc"), now, sched.window)
    tip = _parse_utc(g.get("tipoff_utc"))
    if state == "tipped" and tip is not None and now - tip > timedelta(hours=ABANDON_HOURS):
        return "abandoned"
    return state


def _run_time(run: Optional[dict]) -> Optional[datetime]:
    return _parse_utc(run["started_at"]) if run else None   # the logical collection time (the clock passed to collect_once)


def decide(now: datetime, date: str, games: List[dict], last_run: Optional[dict], sched: Schedule) -> Decision:
    phases: Dict[str, int] = {}
    for g in games:
        ph = game_phase(g, now, sched)
        phases[ph] = phases.get(ph, 0) + 1
    last = _run_time(last_run)
    last_ok = bool(last_run["ok"]) if last_run else True
    live = phases.get("upcoming", 0) + phases.get("active", 0) + phases.get("tipped", 0)
    if games and not live:
        return Decision(date, False, "every game is final", None, None, phases)

    if not games:
        interval = sched.early_min
    elif phases.get("active"):
        interval = sched.active_min
    else:
        options = ([sched.early_min] if phases.get("upcoming") else []) + ([sched.settle_min] if phases.get("tipped") else [])
        interval = min(options)

    if last is None:
        return Decision(date, True, "first collection for this date", now, interval, phases)

    gap = timedelta(minutes=max(MIN_GAP_MIN, sched.retry_min if not last_ok else interval))
    next_due = last + gap
    for g in games:                                       # collect right when a game's pre-tip window opens
        if game_phase(g, now, sched) in ("upcoming", "active"):
            tip = _parse_utc(g.get("tipoff_utc"))
            if tip is None:
                continue
            opens = tip - sched.window
            if last < opens <= now:
                return Decision(date, True, "a game's pre-tip window just opened", now, interval, phases)
            if now < opens < next_due:
                next_due = max(opens, last + timedelta(minutes=MIN_GAP_MIN))
    if now >= next_due:
        return Decision(date, True, "retrying after a failed collection" if not last_ok else f"{interval}-minute interval elapsed", now, interval, phases)
    return Decision(date, False, "not due yet", next_due, interval, phases)


def candidate_dates(store: Store, now: datetime) -> List[str]:
    today = now.astimezone(now_et().tzinfo).strftime("%Y-%m-%d")
    return store.recent_game_dates(today) + [today]


def decisions(store: Store, now: datetime, sched: Schedule) -> List[Decision]:
    out = []
    for d in candidate_dates(store, now):
        out.append(decide(now, d, store.games_for(d), store.last_run_for(d), sched))
    return out


def next_collection(store: Store, now: datetime, sched: Schedule, date: str) -> dict:
    """What the dashboard shows for a date: the next expected collection and whether the scheduler looks stopped."""
    dec = decide(now, date, store.games_for(date), store.last_run_for(date), sched)
    stale = False
    if dec.due and dec.reason != "every game is final":
        last = _run_time(store.last_run_for(date))
        grace = timedelta(minutes=max(10, (dec.interval_min or 30) // 2))
        stale = last is not None and now - last > timedelta(minutes=dec.interval_min or 30) + grace
    return {"next_due": dec.next_due if not dec.due else None, "due_now": dec.due, "stale": stale, "settled": dec.reason == "every game is final",
            "interval_min": dec.interval_min}


async def tick(hw: Any, store: Store, now: datetime, sched: Schedule, season: Optional[str] = None, enrich_sim: bool = False) -> List[dict]:
    results = []
    for dec in decisions(store, now, sched):
        if not dec.due:
            continue
        summary = await collect_once(hw, store, dec.date, now, window=sched.window)
        summary["reason"] = dec.reason
        if enrich_sim and season and summary.get("ok") and not summary.get("errors"):
            try:                                                  # enrichment must never break collection
                from app.sim import enrich
                summary["sim"] = await enrich(hw, store, dec.date, season, now)
            except Exception as e:  # noqa: BLE001
                summary["sim"] = {"error": f"{type(e).__name__}: {e}"}
        results.append(summary)
    return results


def launchd_plist(python: str, workdir: str, log: str, label: str = "com.chalkthat.props-scheduler", every_sec: int = 300) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>{python}</string><string>-m</string><string>app.scheduler</string><string>--once</string></array>
  <key>WorkingDirectory</key><string>{workdir}</string>
  <key>StartInterval</key><integer>{every_sec}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def _fmt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "n/a"
    et = dt.astimezone(now_et().tzinfo)
    return f"{et:%a} {et.hour % 12 or 12}:{et.minute:02d} {'AM' if et.hour < 12 else 'PM'} ET"


def describe(decs: List[Decision]) -> str:
    L = []
    for d in decs:
        ph = ", ".join(f"{n} {k}" for k, n in sorted(d.phases.items())) or "no games stored"
        if d.due:
            L.append(f"{d.date}: DUE ({d.reason}); {ph}; interval {d.interval_min} min")
        else:
            L.append(f"{d.date}: not due ({d.reason}); {ph}" + (f"; next about {_fmt(d.next_due)}" if d.next_due else ""))
    return "\n".join(L)


async def main_async(args: argparse.Namespace) -> int:
    from app.hardwood import HardwoodClient

    s = load_settings()
    sched = Schedule.from_settings(s)
    over = {k: v for k, v in (("early_min", args.early_min), ("active_min", args.active_min), ("window_hours", args.window_hours),
                              ("settle_min", args.settle_min), ("retry_min", args.retry_min)) if v is not None}
    sched = replace(sched, **over)
    store = Store(args.db or s.db_path)
    try:
        if args.status:
            print(describe(decisions(store, datetime.now(timezone.utc), sched)))
            return 0
        if not s.hardwood_api_key:
            print("HARDWOOD_API_KEY is not set in .env")
            return 2
        hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
        try:
            while True:
                now = datetime.now(timezone.utc)
                results = await tick(hw, store, now, sched, current_season(now_et()), s.sim_enabled)
                stamp = _iso(now)
                if not results:
                    print(f"{stamp} nothing due")
                for r in results:
                    print(f"{stamp} collected {r['date']} ({r['reason']}): {r['rows']} props, {r['changed']} changed, {r['games']} games"
                          + (f", ERRORS {r['errors']}" if r["errors"] else "") + (f", {r['warnings']}" if r["warnings"] else ""), flush=True)
                    sm = r.get("sim")
                    if sm:
                        print(f"{stamp}   sim: " + (sm["error"] if "error" in sm else f"{sm['sims']} distributions, {sm['no_sim']} without history, {len(sm['errors'])} errors"
                              + "".join(f", {w}" for w in sm["warnings"])), flush=True)
                ledger_ok = await ledger_step(hw, s, now, stamp)
                if not args.loop:
                    return 0 if all(r["ok"] for r in results) and ledger_ok else 1
                await asyncio.sleep(60)
        finally:
            await hw.aclose()
    finally:
        store.close()


async def ledger_step(hw: Any, s: Settings, now: datetime, stamp: str) -> bool:
    """The pregame recorder for the prospective test. Off unless LEDGER_ENABLED; a failure here never affects collection."""
    if not s.ledger_enabled:
        return True
    try:
        from app.ledger import Ledger
        from app.record import ledger_tick
        ledger = Ledger(s.ledger_path, s.ledger_mirror_dir)
        try:
            out = await ledger_tick(hw, ledger, current_season(now_et()))
        finally:
            ledger.close()
    except Exception as e:  # noqa: BLE001
        print(f"{stamp} ledger: ERROR {type(e).__name__}: {e}", flush=True)
        return False
    if out["recorded"] or out["errors"] or out["window_games"]:
        print(f"{stamp} ledger: {out['recorded']} recorded, {out['already']} already, {sum(out['rejected'].values())} rejected, "
              f"{out['window_games']} games in window" + (f", ERRORS {out['errors']}" if out["errors"] else ""), flush=True)
    return not out["errors"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true", help="one tick, then exit (default)")
    ap.add_argument("--loop", action="store_true", help="tick every minute in the foreground")
    ap.add_argument("--status", action="store_true", help="show what is due without collecting")
    ap.add_argument("--print-launchd", action="store_true", help="print a launchd plist for this checkout and exit")
    ap.add_argument("--db")
    for name, kind in (("early-min", int), ("active-min", int), ("window-hours", float), ("settle-min", int), ("retry-min", int)):
        ap.add_argument("--" + name, type=kind, help="override the config value for this run")
    args = ap.parse_args()
    if args.print_launchd:
        print(launchd_plist(sys.executable, str(ROOT), str(ROOT / "logs" / "scheduler.log")), end="")
        return
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
