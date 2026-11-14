"""Flag-only audit of a finished answer. It never changes the answer; it only reports what it could not check.

What it can catch:
  * decimals and percentages in the answer that appear nowhere in the tool results (or in the earlier conversation),
    after allowing for rounding, percent formats, and simple arithmetic on two numbers shown in the same answer
    (difference, sum, ratio, points per 36)
  * numbers in an answer when no tool was called at all
  * a rank written as a range ("ranks 26/30"); the data gives one rank per group
  * web searches whose results the answer never names an outlet for
  * prop-board table rows (a player's name in the first cell) whose line, over-under records, matchup rank or any
    decimal (averages, allowed, vs avg, or an average minus the line) differ from that player's own row in the
    prop_board results. This catches a right number on the wrong row, which the decimal check cannot. Notes from it
    start with "Row check". Roughly 1 in 15 wrong decimals can still pass, by landing near another figure in the
    same row; garbled cell text is not caught unless it contains a number that doesn't belong.

What it cannot catch: a wrong number that happens to equal some other number in the data (likely on a big board
with hundreds of values), outside those prop rows. Treat a clean report as "no obvious problem", not as proof.
"""
from __future__ import annotations

import bisect
import json
import re
import unicodedata
from typing import Dict, Iterable, List, Optional

_EVIDENCE_NUM = re.compile(r"(?<![A-Za-z_])(?:\d+\.\d+|\.\d+|\d+)(?![A-Za-z_])")
_EVIDENCE_NOISE = [  # ids and timestamps in tool results are not stats
    re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
    re.compile(r"\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?"),
    re.compile(r"\b\d{4}-\d{2}\b"),
]

# Things in an answer that look like numbers but are not stats.
_NOT_STATS = [
    re.compile(r"https?://\S+"),
    re.compile(r"\]\([^)]*\)"),  # markdown link targets
    re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b"),  # 2026-27, 2026-11-12
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b(?:\s*[AaPp][Mm])?"),  # 7:00 PM
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),  # 11/12
    re.compile(r"\bv\d+(?:\.\d+)+\b"),
]
_DECIMAL = re.compile(r"(?<![\w.])(\d*\.\d+)(%?)(?!\w)")
_PERCENT = re.compile(r"(?<![\w.])(\d+)%")
_RANK_RANGE = re.compile(r"\branks?\s+(\d{1,2})\s*/\s*(\d{1,2})\b", re.I)

MAX_OPERANDS = 120
MAX_CANDIDATES = 60


class _Pool:
    """Sorted distinct values from the tool results, with a fast 'is anything within tol of x'."""

    def __init__(self, values: Iterable[float]):
        self.v = sorted({round(x, 6) for x in values})

    def near(self, x: float, tol: float) -> bool:
        i = bisect.bisect_left(self.v, x - tol)
        return i < len(self.v) and self.v[i] <= x + tol


def evidence_numbers(texts: Iterable[str]) -> List[float]:
    out: List[float] = []
    for t in texts:
        t = t or ""
        for pat in _EVIDENCE_NOISE:
            t = pat.sub(" ", t)
        for m in _EVIDENCE_NUM.finditer(t):
            try:
                out.append(float(m.group(0)))
            except ValueError:
                pass
    return out


def _decimals(raw: str) -> int:
    return len(raw.split(".", 1)[1]) if "." in raw else 0


def _direct(a: float, tol: float, pct: bool, pool: _Pool) -> bool:
    if pct:
        return pool.near(a / 100, tol / 100) or pool.near(a, tol)
    if a < 1:
        return pool.near(a, tol) or pool.near(a * 100, tol * 100)
    return pool.near(a, tol) or (a <= 100 and pool.near(a / 100, tol / 100))


def _operands(text: str) -> List[tuple]:
    """Every number shown in the answer as (value, rounding unit), for explaining derived figures."""
    out = []
    for m in re.finditer(r"(?<![\w.])(\d*\.\d+|\d+)(?!\w)", text):
        raw = m.group(1)
        out.append((float(raw), 10.0 ** -_decimals(raw)))
    return out[:MAX_OPERANDS]


def _derived(a: float, ua: float, ops: List[tuple]) -> bool:
    """True if a is x - y, x + y, x / y or x / y * 36 for two numbers x, y shown in the same answer.

    Using only numbers the answer itself shows keeps this honest: "up 6.3" must sit next to the two values it
    comes from. The allowed error is the rounding of a, x and y."""
    base = 0.5 * ua + 1e-9
    n = len(ops)
    for i in range(n):
        x, ux = ops[i]
        for j in range(n):
            if i == j:
                continue
            y, uy = ops[j]
            slack = base + 0.5 * (ux + uy)
            if abs(a - (x - y)) <= slack or (i < j and abs(a - (x + y)) <= slack):
                return True
            if y:
                rel = abs(x / y) * (0.5 * ux / abs(x) if x else 0) + abs(x / y) * 0.5 * uy / abs(y)
                if abs(a - x / y) <= base + rel or abs(a - x / y * 36) <= base + 36 * rel:
                    return True
    return False


def _strip(answer: str) -> str:
    text = answer
    for pat in _NOT_STATS:
        text = pat.sub(" ", text)
    return text


def _candidates(answer: str) -> List[tuple]:
    text = _strip(answer)
    found: List[tuple] = []
    seen = set()
    for m in _DECIMAL.finditer(text):
        raw, pct = m.group(1), bool(m.group(2))
        if (raw, pct) not in seen:
            seen.add((raw, pct))
            found.append((raw, pct))
    for m in _PERCENT.finditer(text):
        key = (m.group(1), True)
        if key not in seen:
            seen.add(key)
            found.append(key)
    return found[:MAX_CANDIDATES]


def _source_labels(hosts: Iterable[str]) -> List[str]:
    labels = []
    for h in hosts:
        parts = (h or "").lower().replace("www.", "").split(".")
        if len(parts) >= 2 and len(parts[-2]) > 3:
            labels.append(parts[-2])
    return labels


_REC = re.compile(r"(?<![\d.\-/+:])(\d{1,3})-(\d{1,3})(?:-(\d{1,3}))?(?![\d\-/.:%])")
_RANK_CELL = re.compile(r"(?<![A-Za-z])[GFC](?![A-Za-z])[^|\d]{0,20}(\d{1,2})(?!\d)")
_LEAD_NUM = re.compile(r"^(\d+(?:\.\d+)?)(?=\s|\(|$)")


def _name_key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z0-9]", "", "".join(ch for ch in s if not unicodedata.combining(ch)).lower())


def _first_name(cell: str) -> str:
    return re.sub(r"[*†‡]", "", re.sub(r"\([^)]*\)", " ", cell)).strip()


def prop_rows(texts: Iterable[str]) -> Dict[str, List[dict]]:
    """Rows from prop_board results (JSON text), keyed by normalized player name."""
    out: Dict[str, List[dict]] = {}
    for t in texts:
        if not isinstance(t, str) or '"props"' not in t:
            continue
        try:
            d = json.loads(t)
        except ValueError:
            continue
        props = d.get("props") if isinstance(d, dict) else None
        if not isinstance(props, list):
            continue
        for r in props:
            if isinstance(r, dict) and isinstance(r.get("name"), str):
                out.setdefault(_name_key(r["name"]), []).append(r)
    return out


def _recs(stat) -> set:
    if not isinstance(stat, dict):
        return set()
    try:
        o, u, p = int(stat["over"]), int(stat["under"]), int(stat.get("push") or 0)
    except (KeyError, TypeError, ValueError):
        return set()
    return {f"{o}-{u}"} | ({f"{o}-{u}-{p}"} if p else set())


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


_ROW_DECIMAL = re.compile(r"(?<![\w.])[+-]?(\d*\.\d+)(?![\w%])(?!\s*%)")


def _row_values(row: dict) -> tuple:
    """The decimals a prop row legitimately shows: the line, the averages, the matchup figures, and the
    differences between an average and the line or another average (what 'avg vs line' would be)."""
    def get(*path):
        cur = row
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        return _num(cur)

    direct = [v for v in (get("line"), get("open_line"), get("last10", "avg"), get("season", "avg"),
                          get("last_season", "avg"), get("matchup", "allowed"), get("matchup", "vs_avg")) if v is not None]
    base = [v for v in (get("line"), get("open_line"), get("last10", "avg"), get("season", "avg"),
                        get("last_season", "avg")) if v is not None]
    derived = [abs(x - y) for i, x in enumerate(base) for y in base[i + 1:]]
    return direct, derived


def _bad_decimals(rest: str, row: dict) -> List[str]:
    direct, derived = _row_values(row)
    if not direct:
        return []
    bad: List[str] = []
    for m in _ROW_DECIMAL.finditer(rest):
        raw = m.group(1)
        a = abs(float(raw))
        tol = 0.5 * 10 ** -_decimals(raw) + 1e-9
        if any(abs(a - abs(v)) <= tol for v in direct) or any(abs(a - d) <= tol + 0.05 for d in derived):
            continue
        if raw not in bad:
            bad.append(raw)
    return bad


def _season_games(row: dict) -> Optional[int]:
    s = row.get("season")
    if not isinstance(s, dict):
        return None
    g = _num(s.get("games"))
    if g is None:
        try:
            g = int(s["over"]) + int(s["under"]) + int(s.get("push") or 0)
        except (KeyError, TypeError, ValueError):
            return None
    return int(g)


def _row_problems(rest: str, shown: List[str], rank_m, row: dict) -> List[str]:
    probs: List[str] = []
    allowed = _recs(row.get("last10")) | _recs(row.get("season")) | _recs(row.get("last_season"))
    if allowed:
        bad = [r for r in shown if r not in allowed]
        if bad:
            def fmt(k):
                s = row.get(k)
                return f"{s.get('over')}-{s.get('under')}" if isinstance(s, dict) and "over" in s else "n/a"
            probs.append(f"shows record {', '.join(bad)} but Hardwood has last 10 {fmt('last10')}, season {fmt('season')}")
    m = row.get("matchup")
    exp_rank = _num(m.get("rank")) if isinstance(m, dict) else None
    if rank_m and exp_rank is not None and int(rank_m.group(1)) != int(exp_rank):
        probs.append(f"shows matchup rank {rank_m.group(1)} but Hardwood has {int(exp_rank)}")
    bad_dec = _bad_decimals(rest, row)
    if bad_dec:
        probs.append(f"shows {', '.join(bad_dec)}, which is not among this player's numbers on the prop board "
                     f"(line, averages, matchup allowed / vs avg)")
    first = _LEAD_NUM.match(rest.split("|", 1)[0].strip())
    exp_line = _num(row.get("line"))
    if first and exp_line is not None and abs(float(first.group(1)) - exp_line) > 1e-6:
        probs.append(f"shows line {first.group(1)} but Hardwood has {exp_line:g}")
    return probs


def check_prop_rows(answer: str, rows: Dict[str, List[dict]], max_report: int = 6) -> List[str]:
    """Compare table rows that start with a player on the board against that player's prop_board row."""
    if not rows or "|" not in answer:
        return []
    issues: List[str] = []
    checked = 0
    unmarked: List[tuple] = []  # (name, season games) for rows under 10 games that carry no dagger
    for line in answer.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3:
            continue
        cands = rows.get(_name_key(_first_name(cells[0])))
        if not cands:
            continue
        rest = " | ".join(cells[1:])
        shown = []
        for m in _REC.finditer(rest):
            third = m.group(3)
            shown.append(f"{int(m.group(1))}-{int(m.group(2))}" + (f"-{int(third)}" if third and int(third) else ""))
        rank_m = _RANK_CELL.search(rest)
        if not shown and not rank_m:
            continue  # not a prop row (some other table that happens to start with this player)
        best: Optional[List[str]] = None
        chosen = cands[0]
        for r in cands:
            p = _row_problems(rest, shown, rank_m, r)
            if best is None or len(p) < len(best):
                best, chosen = p, r
            if not p:
                break
        checked += 1
        games = _season_games(chosen)
        if games is not None and games < 10 and "†" not in cells[0]:
            unmarked.append((_first_name(cells[0]), games))
        if best:
            issues.append(f"Row check: {_first_name(cells[0])} " + "; ".join(best) + ".")
    if len(issues) > max_report:
        issues = issues[:max_report] + [f"Row check: and {len(issues) - max_report} more rows differ from the prop board."]
    # A board-style answer must mark every row whose last-10 window spans two seasons (under 10 season games) with †.
    if checked >= 5 and unmarked:
        if "†" in answer:
            for name, g in unmarked[:max_report]:
                issues.append(f"Row check: {name} has only {g} season games but is not marked † (the last-10 window spans two seasons).")
            if len(unmarked) > max_report:
                issues.append(f"Row check: and {len(unmarked) - max_report} more rows under 10 season games are not marked †.")
        else:
            issues.append(f"Row check: {len(unmarked)} rows have under 10 season games but none is marked † (the last-10 window spans two seasons).")
    return issues


def audit(answer: str, evidence_texts: Iterable[str], *, tool_calls: int, searches: int = 0,
          source_hosts: Optional[Iterable[str]] = None, max_report: int = 8) -> Dict[str, List[str]]:
    """Return {"unverified": [numbers...], "notes": [sentences...]}; both empty means nothing to flag."""
    unverified: List[str] = []
    notes: List[str] = []
    evidence_texts = list(evidence_texts)
    notes.extend(check_prop_rows(answer, prop_rows(evidence_texts)))

    for m in _RANK_RANGE.finditer(answer):
        if m.group(1) != m.group(2):
            notes.append(f'"{m.group(0)}" is a rank written as a range. Each rank in the data belongs to one group, so check it.')
            break

    cands = _candidates(answer)
    if cands:
        if tool_calls == 0:
            if not searches:  # with a web search the numbers may legitimately come from the web, which can't be checked here
                shown = ", ".join(("%s%%" % r if p else r) for r, p in cands[:5])
                notes.append(f"This answer has numbers ({shown}) but no tool was called, so they are not from Hardwood or the web.")
        else:
            pool = _Pool(evidence_numbers(evidence_texts))
            ops = _operands(_strip(answer))
            for raw, pct in cands:
                a = float(raw)
                tol = 0.5 * 10 ** -_decimals(raw) + 1e-9
                if _direct(a, tol, pct, pool):
                    continue
                if not pct and _derived(a, 10.0 ** -_decimals(raw), [o for o in ops if o[0] != a]):
                    continue
                unverified.append(raw + ("%" if pct else ""))
        if unverified:
            if len(unverified) > max_report:
                extra = len(unverified) - max_report
                unverified = unverified[:max_report] + [f"and {extra} more"]
            notes.insert(0, "Not found in the tool data (they may be derived from it or be typos): "
                            + ", ".join(unverified) + ". Check these before relying on them.")

    if searches:
        labels = _source_labels(source_hosts or [])
        low = answer.lower()
        if labels and not any(lb in low for lb in labels):
            notes.append("Web search ran, but the answer doesn't name an outlet that was found "
                         f"({', '.join(sorted(set(labels))[:4])}). Check where the news claims came from.")

    return {"unverified": unverified, "notes": notes}
