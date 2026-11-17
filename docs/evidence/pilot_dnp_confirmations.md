# Pilot nonparticipation confirmations (preserved evidence)

Historical pilot game: 2025-11-12, CHI at DET. These two cases were confirmed against the official NBA box score before this ledger existed. They concern the historical replay pilot, not prospective-ledger contracts, and are kept here unchanged as the evidence behind the void policy.

| Player | Resolution | Evidence |
|---|---|---|
| Cade Cunningham | Confirmed nonparticipant; contract void | Official NBA box score lists him only in the Detroit inactive line: "Cunningham (Injury/Illness - Left Hip; Contusion)" |
| Jalen Duren | Confirmed nonparticipant; contract void | Official NBA box score: "DNP - Injury/Illness - Right Ankle; Sprain" |

Sources: official NBA box score PDF for 2025-11-12 CHI at DET (statsdmz.nba.com, no publication timestamp); AP recap carried by CBS Sports dated Nov 13, 2025 ("missing their entire usual starting lineup of Cade Cunningham (hip), Jalen Duren (ankle)"). Side finding from the same box score: Isaac Jones "DNP - Coach's Decision" is also absent from Hardwood's box, so a missing Hardwood row is consistent with non-participation but is **not proof**; only an explicit DNP or inactive flag, or a manual confirmation against an authoritative source, voids a contract.

For prospective contracts, confirmations are saved with `python scripts/confirm_dnp.py` into the ledger's `verifications` table (source, time and note kept).
