"""Settings and time helpers."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent

# Deployed Hardwood backend-api (from the Hardwood README). Override with HARDWOOD_BASE_URL.
DEFAULT_HARDWOOD_URL = "https://backend-api-production-f05a.up.railway.app"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_WEB_SEARCH_TOOL = "web_search_20250305"


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = ""
    hardwood_api_key: str = ""
    hardwood_base_url: str = DEFAULT_HARDWOOD_URL
    model: str = DEFAULT_MODEL
    max_tokens: int = 16000
    web_search_max_uses: int = 4  # per question, across the whole tool loop; 0 turns web search off
    web_search_tool_type: str = DEFAULT_WEB_SEARCH_TOOL
    max_cost_usd: float = 1.0  # stop a question once its estimated cost passes this
    max_seconds: float = 300.0  # stop starting new model calls after this long
    effort: str = ""  # "" = the API default (high on Sonnet 5.5); low | medium | high | xhigh | max
    thinking: str = "adaptive"  # adaptive = the model's default reasoning; off = no up-front reasoning (progress notes between tools only)
    prompt_cache: bool = True  # cache the tools, system prompt and conversation so repeated tool rounds cost far less
    log_path: Optional[Path] = None
    db_path: Path = ROOT / "data" / "props.db"  # SQLite file for the props dashboard (DB_PATH overrides)
    # Collection schedule (config only). Early in the day the board is refreshed every COLLECT_EARLY_MIN minutes; from
    # COLLECT_WINDOW_HOURS before a game's tip-off every COLLECT_ACTIVE_MIN minutes, until tip-off; then every
    # COLLECT_SETTLE_MIN minutes only to pick up final scores and results.
    collect_early_min: int = 120
    collect_active_min: int = 30
    collect_window_hours: float = 3.0
    collect_settle_min: int = 60
    collect_retry_min: int = 10
    sim_enabled: bool = True  # run the simulation enrichment after each collection (SIM_ENABLED=false turns it off)
    # Prospective validation ledger (off by default). When on, every scheduler tick also runs the pregame recorder (app/record.py).
    ledger_enabled: bool = False
    ledger_path: Path = ROOT / "data" / "ledger" / "ledger.db"
    ledger_mirror_dir: Path = ROOT / "data" / "ledger" / "mirror"


def _int(env: Mapping[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None
    return max(lo, min(hi, value))


def _float(env: Mapping[str, str], key: str, default: float, lo: float, hi: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{key} must be a number, got {raw!r}") from None
    return max(lo, min(hi, value))


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _choice(env: Mapping[str, str], key: str, allowed: tuple, default: str) -> str:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw not in allowed:
        raise ValueError(f"{key} must be one of {', '.join(allowed)}, got {raw!r}")
    return raw


def _db_path(env: Mapping[str, str]) -> Path:
    raw = (env.get("DB_PATH") or "").strip()
    if not raw:
        return ROOT / "data" / "props.db"
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _path(env: Mapping[str, str], key: str, default: Path) -> Path:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Read settings. With no argument, load ./.env (it wins over the shell environment)."""
    if env is None:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=True)
        env = os.environ

    log_raw = env.get("CHAT_LOG")
    if log_raw is None:
        log_raw = "logs/chat.jsonl"
    log_path: Optional[Path] = None
    if log_raw.strip():
        p = Path(log_raw.strip())
        log_path = p if p.is_absolute() else ROOT / p

    settings = Settings(
        anthropic_api_key=(env.get("ANTHROPIC_API_KEY") or "").strip(),
        hardwood_api_key=(env.get("HARDWOOD_API_KEY") or "").strip(),
        hardwood_base_url=(env.get("HARDWOOD_BASE_URL") or DEFAULT_HARDWOOD_URL).strip().rstrip("/"),
        model=(env.get("MODEL") or DEFAULT_MODEL).strip(),
        max_tokens=_int(env, "MAX_TOKENS", 16000, 256, 32000),
        web_search_max_uses=_int(env, "WEB_SEARCH_MAX_USES", 4, 0, 10),
        web_search_tool_type=(env.get("WEB_SEARCH_TOOL_TYPE") or DEFAULT_WEB_SEARCH_TOOL).strip(),
        max_cost_usd=_float(env, "MAX_COST_USD", 1.0, 0.01, 50.0),
        max_seconds=_float(env, "MAX_SECONDS", 300.0, 10.0, 1800.0),
        effort=_choice(env, "EFFORT", EFFORT_LEVELS, ""),
        thinking=_choice(env, "THINKING", ("adaptive", "off"), "adaptive"),
        prompt_cache=(env.get("PROMPT_CACHE") or "true").strip().lower() not in ("0", "false", "no", "off"),
        log_path=log_path,
        db_path=_db_path(env),
        collect_early_min=_int(env, "COLLECT_EARLY_MIN", 120, 1, 1440),
        collect_active_min=_int(env, "COLLECT_ACTIVE_MIN", 30, 1, 720),
        collect_window_hours=_float(env, "COLLECT_WINDOW_HOURS", 3.0, 0.1, 24.0),
        collect_settle_min=_int(env, "COLLECT_SETTLE_MIN", 60, 1, 1440),
        collect_retry_min=_int(env, "COLLECT_RETRY_MIN", 10, 1, 240),
        sim_enabled=(env.get("SIM_ENABLED") or "true").strip().lower() not in ("0", "false", "no", "off"),
        ledger_enabled=(env.get("LEDGER_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "on"),
        ledger_path=_path(env, "LEDGER_PATH", ROOT / "data" / "ledger" / "ledger.db"),
        ledger_mirror_dir=_path(env, "LEDGER_MIRROR_DIR", ROOT / "data" / "ledger" / "mirror"),
    )
    if settings.thinking == "off" and settings.effort in ("xhigh", "max"):
        raise ValueError("THINKING=off only works with EFFORT low, medium or high (the API rejects it at xhigh and max)")
    return settings


def now_et() -> datetime:
    return datetime.now(ET)


def current_season(now: datetime) -> str:
    """NBA season label for a date, e.g. 2026-27. The season rolls over in September."""
    start = now.year if now.month >= 9 else now.year - 1
    return f"{start}-{(start + 1) % 100:02d}"
