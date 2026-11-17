"""Record a manual confirmation that a player did not participate (authoritative source required). Settlement then voids the contract.

    python scripts/confirm_dnp.py --game <game_id> --player <player_id> --source "official NBA box score" --note "listed inactive: ankle"

Only use this when an authoritative source (official NBA box score, league inactive list) states he did not play. Missing data is not a
confirmation. The entry is append-only and keeps the source, time and note.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from _ledger_cli import open_ledger, settings

from app.ledger import ts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--game", required=True)
    ap.add_argument("--player", required=True)
    ap.add_argument("--source", required=True, help="the authoritative source you checked")
    ap.add_argument("--note", default="")
    a = ap.parse_args()
    L = open_ledger(settings())
    try:
        if not L.records("game_id = ? AND player_id = ?", (a.game, a.player)):
            print("No recorded contract for that game and player.")
            return 2
        L.add_verification(a.game, a.player, "confirmed_dnp", a.source, ts(datetime.now(timezone.utc)), a.note)
    finally:
        L.close()
    print("saved; the next settlement run will void his contracts in that game")
    return 0


if __name__ == "__main__":
    sys.exit(main())
