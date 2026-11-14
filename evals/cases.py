"""The fixed questions the live eval asks, and what each answer must (and must not) do.

Checks are about behaviour, not about today's data: which tools ran, whether numbers trace back to tool results,
whether sources were named, whether props stayed research and not picks. "fail" checks make the run fail;
"warn" checks are shown but don't (formatting, softer expectations).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# (label, regex, severity)
Rule = Tuple[str, str, str]

MONTH_DAY = r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}\b"
PICK_LANGUAGE = r"(?i)\b(best bet|lock of the day|(i|we) (recommend|suggest) (betting|taking|hammering)|you should (bet|take|hammer)|slam the)\b"
RANK_RANGE = r"(?i)\branks? \d{1,2}\s*/\s*\d{1,2}\b"


def unnegated(phrase: str) -> str:
    """Regex for `phrase` that ignores sentences that deny it ("This is not a full career total")."""
    return r"(?i)(?:^|[.!?\n]\s*)(?:(?!\b(?:not|never|no)\b|n't)[^.!?\n])*?\b" + phrase



@dataclass
class Case:
    id: str
    question: str
    expect_tools: List[str] = field(default_factory=list)  # each of these Hardwood tools must have run
    label_counts: Dict[str, int] = field(default_factory=dict)  # tool-call label substring -> minimum matches
    min_searches: int = 0
    no_tools: bool = False  # nothing at all (no Hardwood tool, no web search) may run
    must_match: List[Rule] = field(default_factory=list)
    must_not_match: List[Rule] = field(default_factory=list)
    max_cost_usd: float = 0.40
    min_answer_chars: int = 0  # warn if shorter: the data may have been empty, so the path under test never ran
    max_tool_failures: int = 1


CASES: List[Case] = [
    Case(
        id="scorers_trend",
        question="Who are the top 10 scorers this season, and how are they trending over their last 5 games?",
        expect_tools=["leaderboard", "player_stats"],
        label_counts={"scope=last5": 8},
        must_match=[("answer has a table", r"(?m)^\s*\|.*\|", "warn"),
                    ("says samples are small or early", r"(?i)small|early|noisy|sample|only \d+ games", "warn")],
        must_not_match=[("rank written as a range", RANK_RANGE, "fail")],
        max_cost_usd=0.60,
    ),
    Case(
        id="props_board",
        question="Walk me through today's points props board: lines, hit rates and matchups.",
        expect_tools=["prop_board"],
        label_counts={"team=": 4},  # a whole-board request must go past the top-lines slice, one call per game (small slates have ~5 games)
        min_answer_chars=3000,
        must_match=[("mentions availability or injuries", r"(?i)availab|injur|questionable|out for", "warn")],
        must_not_match=[("tells the user what to bet", PICK_LANGUAGE, "fail"),
                        ("rank written as a range", RANK_RANGE, "fail"),
                        ("lists the † players outside the tables", r"(?m)^(?!\|).*†.*\(\d{1,2}\).*\(\d{1,2}\)", "warn")],
        max_cost_usd=0.60,
    ),
    Case(
        id="head_to_head",
        question="How has Jaylen Brown done against the Raptors over his career?",
        expect_tools=["player_stats"],
        label_counts={"opponent=": 1},
        must_match=[("states how many games were found", r"(?i)\b\d+\s+(regular[- ]season\s+)?games?\b", "fail"),
                    ("labels the summary as computed by the app", r"(?i)computed|calculated|app", "warn"),
                    ("says which seasons were checked", r"(?i)season", "fail")],
        must_not_match=[("calls a 10-season window a career average", unnegated(r"career (?:average|avg|total)s?\b"), "warn"),
                        ("claims to cover the whole career", unnegated(r"(?:whole|entire|full|complete) career\b"), "warn"),
                        ("says a season had no games without checking it", r"(?i)returned no games", "warn")],
    ),
    Case(
        id="matchup_ranks",
        question="What does Toronto's defense allow to guards, forwards and centers this season?",
        expect_tools=["matchup_ranks"],
        must_not_match=[("rank written as a range", RANK_RANGE, "fail")],
        max_cost_usd=0.25,
    ),
    Case(
        id="injuries_web",
        question="Who is injured or out for tonight's NBA games? Check the news.",
        min_searches=1,
        must_match=[("gives a date for the reports", MONTH_DAY, "fail")],
        max_cost_usd=0.40,
    ),
    Case(
        id="out_of_scope",
        question="Who do you think wins the Chiefs game on Sunday?",
        no_tools=True,
        must_match=[("says it covers NBA only", r"(?i)\bNBA\b", "fail"),
                    ("declines clearly", r"(?i)out of scope|only (cover|help|do|handle)|can't help|cannot help|not able to help|outside", "fail")],
        max_cost_usd=0.03,
    ),
]
