"""Alias resolution, at write time.

"Ather", "athar", "AT" must become one id before the row lands, or every
competitive number is quietly understated and nothing errors to tell you.

The surface form is kept on record even after a match, so an auditor can check
that "AT" really did mean Ather in that note.
"""
from __future__ import annotations

from dataclasses import dataclass

from .taxonomy import normalise


@dataclass
class Resolution:
    entity_id: int | None
    surface_form: str
    matched: bool


def resolve(conn, entity_type: str, surface: str | None) -> Resolution:
    if not surface or not surface.strip():
        return Resolution(None, surface or "", False)
    row = conn.execute(
        "SELECT a.entity_id FROM entity_alias a JOIN entity e ON e.id = a.entity_id "
        "WHERE a.alias_norm = ? AND e.entity_type = ?",
        (normalise(surface), entity_type),
    ).fetchone()
    if row:
        return Resolution(int(row["entity_id"]), surface, True)
    return Resolution(None, surface, False)


def record_unresolved(conn, event_id: int, slot: str, surface: str) -> None:
    """Queue for weekly review. Sorted by frequency, this is how the alias table
    grows -- and a new competitor entering the market shows up here first."""
    row = conn.execute(
        "SELECT id, occurrences FROM unresolved_mention "
        "WHERE slot = ? AND surface_form = ? AND reviewed = 0",
        (slot, surface),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE unresolved_mention SET occurrences = ? WHERE id = ?",
            (row["occurrences"] + 1, row["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO unresolved_mention (event_id, slot, surface_form) VALUES (?,?,?)",
            (event_id, slot, surface),
        )


def prune_unresolved(conn, tax) -> int:
    """Close queue entries that are no longer open questions.

    Two things retire a mention: the slot left the taxonomy, or someone added
    the alias so it now resolves. Leaving either in the queue makes the review
    list look busy while saying nothing.
    """
    live_slots = set(tax.slots)
    closed = 0
    for row in conn.execute(
        "SELECT id, slot, surface_form FROM unresolved_mention WHERE reviewed = 0"
    ).fetchall():
        slot = row["slot"]
        if slot not in live_slots:
            stale = True
        else:
            entity_type = tax.slots[slot].entity_type
            stale = resolve(conn, entity_type, row["surface_form"]).matched
        if stale:
            conn.execute(
                "UPDATE unresolved_mention SET reviewed = 1 WHERE id = ?", (row["id"],)
            )
            closed += 1
    conn.commit()
    return closed
