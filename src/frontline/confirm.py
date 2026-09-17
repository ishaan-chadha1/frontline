"""The confirmation card.

Three jobs at once: catches extraction errors while the rep still remembers the
conversation, shows the rep exactly what was recorded about them, and turns
every correction into a labelled training example.

Plain language always -- the rep sees "Price objection (EMI amount or tenure)",
never objection.PRICE.EMI.
"""
from __future__ import annotations

import sqlite3

DIMENSION_LABEL = {
    "objection": "objection",
    "brand_attribute": "brand",
    "outcome": "status",
}


def render_card(conn: sqlite3.Connection, capture_id: int) -> str:
    rows = conn.execute(
        """SELECT t.label, t.dimension, t.path,
                  se.name AS subject, re.name AS rival, e.confidence
           FROM event e
           JOIN taxonomy_node t ON t.id = e.taxonomy_node_id
           LEFT JOIN entity se ON se.id = e.subject_id
           LEFT JOIN entity re ON re.id = e.rival_id
           WHERE e.capture_id = ? AND e.is_active = 1
           ORDER BY e.id""",
        (capture_id,),
    ).fetchall()

    if not rows:
        return (
            "I couldn't make out anything from that one.\n"
            "Mind sending it again, or typing what happened?"
        )

    lines = ["Got it — here's what I recorded:", ""]
    seen_subject = None
    for r in rows:
        kind = DIMENSION_LABEL.get(r["dimension"], r["dimension"])
        if r["dimension"] == "outcome":
            lines.append(f"• Status: {r['label'].lower()}")
        else:
            lines.append(f"• {kind.capitalize()}: {r['label']}")
        if r["subject"] and r["subject"] != seen_subject:
            seen_subject = r["subject"]
        if r["rival"]:
            lines.append(f"• Also considering {r['rival']}")
    if seen_subject:
        lines.insert(2, f"• {seen_subject}")

    lines += ["", "Reply 1 if that's right,", "or just tell me what to fix."]
    return "\n".join(lines)
