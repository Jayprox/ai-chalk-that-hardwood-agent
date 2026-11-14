from app.verify import audit

DATA = ['{"pts": 26.04, "fg_pct": 0.488, "usage": 0.234, "min": 28.2, "season_pts": 30.31, "last5_pts": 36.62, "rank": 26}']


def run(answer, texts=DATA, tool_calls=1, **kw):
    return audit(answer, texts, tool_calls=tool_calls, **kw)


def test_numbers_found_in_the_data_pass_including_rounding_and_percent_formats():
    out = run("Scored 26.0 on .488 shooting (48.8%) with 23.4% usage in 28.2 minutes.")
    assert out == {"unverified": [], "notes": []}


def test_a_decimal_that_is_not_in_the_data_is_flagged():
    out = run("He averaged 31.7 points and shot .488.")
    assert out["unverified"] == ["31.7"]
    assert "31.7" in out["notes"][0] and "Not found in the tool data" in out["notes"][0]


def test_arithmetic_is_accepted_when_the_answer_shows_both_inputs():
    # change = last5 - season, points per 36 = pts / min * 36, sum of two shown values
    assert run("Season 30.3, last 5 36.6: up 6.3.", ['{"s": 30.31, "l": 36.62}'])["unverified"] == []
    assert run("He scores 36.6 in 28.2 minutes, 46.7 per 36.", ['{"pts": 36.6, "min": 28.2}'])["unverified"] == []
    assert run("30.3 plus 26.4 is 56.7.", ['{"a": 30.3, "b": 26.4}'])["unverified"] == []


def test_a_derived_number_without_its_inputs_on_the_page_is_flagged():
    assert run("Up 6.3 points over his season average.", ['{"s": 30.31, "l": 36.62}'])["unverified"] == ["6.3"]


def test_wrong_arithmetic_is_not_accepted():
    assert run("Season 30.3, last 5 36.6: up 9.9.", ['{"a": 36.6, "b": 30.3}'])["unverified"] == ["9.9"]


def test_digits_in_field_names_and_ids_are_not_evidence():
    # "last5_pts" must not make 5 available, and a uuid must not supply numbers either
    data = ['{"last5_pts": 36.62, "player_id": "11111111-1111-4111-8111-111111111111", "date": "2026-11-12"}']
    assert run("He scored 31.7.", data)["unverified"] == ["31.7"]


def test_dates_times_seasons_urls_and_integers_are_ignored():
    text = ("Nov 12, 2026-27 season, tip 7:00 PM ET, report 8:30 AM ET, see https://espn.com/story/1.5 and "
            "[link](https://nba.com/a/2.5). He is 7-3 over 10 games, rank 26, prices -111/-115.")
    assert run(text, ["{}"]) == {"unverified": [], "notes": []}


def test_a_rank_written_as_a_range_gets_a_note():
    out = run("Reid and Kalkbrenner face Phoenix's centers (ranks 26/30).")
    assert out["unverified"] == [] and "rank written as a range" in out["notes"][0]
    assert run("Both are rank 26.")["notes"] == []
    assert run("ranks 26/26")["notes"] == []


def test_numbers_with_no_tool_call_are_flagged_unless_a_web_search_ran():
    out = run("He is averaging 27.1 and 10.4.", [], tool_calls=0)
    assert out["unverified"] == [] and "no tool was called" in out["notes"][0]
    assert run("He is averaging 27.1.", [], tool_calls=0, searches=1)["notes"] == []
    assert run("No numbers here.", [], tool_calls=0) == {"unverified": [], "notes": []}


def test_web_search_that_names_no_outlet_gets_a_note():
    hosts = ["www.espn.com", "www.nba.com"]
    out = run("Embiid is questionable.", tool_calls=0, searches=1, source_hosts=hosts)
    assert "doesn't name an outlet" in out["notes"][0]
    assert run("Embiid is questionable (ESPN, Nov 12).", tool_calls=0, searches=1, source_hosts=hosts)["notes"] == []
    # short brand names like nba.com and si.com are too generic to test for
    assert run("Embiid is questionable.", tool_calls=0, searches=1, source_hosts=["www.nba.com", "www.si.com"])["notes"] == []


def test_long_lists_are_capped():
    nums = [f"{i}.{i}7" for i in range(10, 22)]
    out = run("Values " + " ".join(nums), ["{}"], max_report=3)
    assert len(out["unverified"]) == 4 and out["unverified"][-1] == "and 9 more"


def test_the_audit_cannot_catch_a_wrong_number_that_exists_elsewhere_in_the_data():
    # Documented limit: 24.5 is a real value in the data, just not this player's.
    assert run("Maxey scored 24.5.", ['{"maxey": 26.0, "booker": 24.5}'])["unverified"] == []


def test_a_big_data_set_does_not_make_everything_pass():
    # 2,000 values, none near 31.7: a fabricated 31.7 must still be flagged (arithmetic only counts numbers on the page)
    vals = [round(i * 0.0137, 2) for i in range(1, 2000) if abs(i * 0.0137 - 31.7) > 0.2]
    data = ['{"v": [' + ", ".join(map(str, vals)) + "]}"]
    assert run("He averaged 31.7 points.", data)["unverified"] == ["31.7"]


# ---------------------------------------------------------------- prop-board row check

import json  # noqa: E402


def _board(*rows):
    return json.dumps({"props": list(rows), "shown": len(rows)})


def _row(name, line, l10=(5, 5, 0), season=(5, 7, 0), rank=9, avg10=10.1, avg_season=9.4):
    stat = lambda t, avg: {"over": t[0], "under": t[1], "push": t[2], "games": sum(t), "avg": avg}  # noqa: E731
    return {"name": name, "team": "OKC", "line": line, "last10": stat(l10, avg10), "season": stat(season, avg_season),
            "matchup": {"position": "G", "rank": rank, "of": 30, "allowed": 41.5, "vs_avg": -1.7}}


KENRICH = _board(_row("Kenrich Williams", 7.5), _row("Kobe Sanders", 7.5, (6, 4, 0), (6, 4, 0), 9))
HEAD = "| Player | Line | L10 | Season | Matchup |\n|---|---|---|---|---|\n"


def rows_note(table, data=KENRICH):
    out = run(HEAD + table, [data], tool_calls=1)
    return [n for n in out["notes"] if n.startswith("Row check")]


def test_correct_prop_rows_pass_in_several_layouts():
    assert rows_note("| Kenrich Williams (OKC) | 7.5 (-107/-119) | 5-5 | 5-7 | G rank 9 |") == []
    assert rows_note("| Kenrich Williams (OKC) | 7.5 | 5-5 | 5-7 | G vs LAC: rank 9, 41.5 allowed |") == []
    assert rows_note("| **Kobe Sanders** (LAC) † | 7.5 | 6-4* | 6-4 | G 9 |") == []


def test_a_wrong_record_on_one_row_is_flagged_with_the_players_name():
    notes = rows_note("| Kenrich Williams (OKC) | 7.5 | 6-4 | 5-7 | G rank 9 |")
    assert len(notes) == 1 and "Kenrich Williams" in notes[0] and "6-4" in notes[0] and "5-5" in notes[0]


def test_a_wrong_matchup_rank_is_flagged():
    notes = rows_note("| Kobe Sanders (LAC) | 7.5 | 6-4 | 6-4 | G rank 14 |")
    assert len(notes) == 1 and "rank 14" in notes[0] and "has 9" in notes[0]


def test_a_wrong_line_is_flagged():
    notes = rows_note("| Kenrich Williams (OKC) | 8.5 | 5-5 | 5-7 | G rank 9 |")
    assert len(notes) == 1 and "line 8.5" in notes[0]


def test_names_match_without_accents_and_other_tables_are_left_alone():
    data = _board(_row("Nikola Jokić", 26.5))
    assert rows_note("| Nikola Jokic (DEN) | 26.5 | 5-5 | 5-7 | G rank 9 |", data) == []
    # a table about the same player that has no record or rank cells is not a prop row
    out = run("| Player | PPG |\n|---|---|\n| Nikola Jokić (DEN) | 27.1 (10 games) | 12.0 |", [data])
    assert [n for n in out["notes"] if n.startswith("Row check")] == []


def test_pushes_may_be_written_either_way_and_bad_json_is_ignored():
    data = _board(_row("Pat Push", 20.5, (5, 4, 1), (5, 4, 1)))
    assert rows_note("| Pat Push (BOS) | 20.5 | 5-4 | 5-4-1 | G rank 9 |", data) == []
    out = run(HEAD + "| Pat Push (BOS) | 20.5 | 9-0 | 5-4 | G rank 9 |", ['{"props": [{"name": "Pat Push", "line": ', data])
    assert len([n for n in out["notes"] if n.startswith("Row check")]) == 1


def test_many_bad_rows_are_capped():
    names = [f"Player {c}" for c in "ABCDEFGHIJ"]
    data = _board(*[_row(n, 10.5) for n in names])
    table = "".join(f"| {n} (BOS) | 10.5 | 9-1 | 5-7 | G rank 9 |\n" for n in names)
    notes = rows_note(table, data)
    assert len(notes) == 7 and "more rows differ" in notes[-1]


def test_a_wrong_average_on_a_prop_row_is_flagged_but_right_figures_and_differences_pass():
    ok = "| Kenrich Williams (OKC) | 7.5 | 5-5 | 5-7 | 10.1 | G vs LAC: rank 9, 41.5 allowed (-1.7 vs avg) |"
    assert rows_note(ok) == []
    assert rows_note("| Kenrich Williams (OKC) | 7.5 | 5-5 | 5-7 | 9.4 | G rank 9 |") == []  # season average
    assert rows_note("| Kenrich Williams (OKC) | 7.5 | 5-5 | 5-7 | 10.1 (+2.6 vs line) | G rank 9 |") == []  # 10.1 - 7.5
    notes = rows_note("| Kenrich Williams (OKC) | 7.5 | 5-5 | 5-7 | 6.8 | G rank 9 |")
    assert len(notes) == 1 and "6.8" in notes[0] and "Kenrich Williams" in notes[0]


def test_garbled_cells_and_percent_figures_do_not_crash_or_false_flag():
    notes = rows_note("| Kenrich Williams (OKC) | 7.5 | 5-5 (50%) | 5-7 (41.7%) | 10.1 | G rank 9 |")
    assert notes == []
    assert len(rows_note("| Kenrich Williams (OKC) | 7.5 | 5-5 | 5-7 | 9.0 avg is not shown; last-10 avg 8.0 | G rank 9 |")) == 1


# ---------------------------------------------------------------- dagger marks for two-season windows

def _board_with_games(*specs):
    rows = []
    for i, (name, games) in enumerate(specs):
        o = games // 2
        rows.append(_row(name, 10.5 + i, (min(o, 10), min(games - o, 10), 0), (o, games - o, 0)))
    for r, (_, g) in zip(rows, specs):
        r["season"]["games"] = g
    return _board(*rows)


SPECS = [("Ann A", 12), ("Bob B", 8), ("Cy C", 9), ("Di D", 11), ("Ed E", 10), ("Flo F", 7)]


def _table(marks, board=None):
    rows = ""
    for i, (name, g) in enumerate(SPECS):
        o = g // 2
        rec10 = f"{min(o, 10)}-{min(g - o, 10)}"
        dag = " †" if name in marks else ""
        rows += f"| {name} (BOS){dag} | {10.5 + i} | {rec10} | {o}-{g - o} | G rank 9 |\n"
    return rows


def test_every_row_under_ten_season_games_must_carry_a_dagger_on_a_board_answer():
    data = _board_with_games(*SPECS)
    ok = rows_note(_table({"Bob B", "Cy C", "Flo F"}), data)
    assert ok == []
    missed = rows_note(_table({"Bob B", "Flo F"}), data)
    assert len(missed) == 1 and "Cy C" in missed[0] and "9 season games" in missed[0] and "†" in missed[0]


def test_a_board_answer_that_uses_no_dagger_at_all_gets_one_summary_note():
    notes = rows_note(_table(set()), _board_with_games(*SPECS))
    assert len(notes) == 1 and "3 rows have under 10 season games" in notes[0]


def test_dagger_check_needs_a_board_sized_answer_and_ignores_full_seasons():
    data = _board_with_games(*SPECS)
    small = "".join(_table(set()).splitlines(True)[:3])  # 3 rows: not a board walk-through
    assert rows_note(small, data) == []
    specs = [("Ann A", 12), ("Di D", 11), ("Ed E", 10), ("Gus G", 15), ("Hal H", 20)]
    table = ""
    for i, (n, g) in enumerate(specs):
        o = g // 2
        table += f"| {n} (BOS) | {10.5 + i} | {min(o, 10)}-{min(g - o, 10)} | {o}-{g - o} | G rank 9 |\n"
    assert rows_note(table, _board_with_games(*specs)) == []
