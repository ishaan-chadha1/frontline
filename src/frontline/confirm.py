"""The confirmation card.

Three jobs at once: catches extraction errors while the rep still remembers the
conversation, shows the rep exactly what was recorded about them, and turns
every correction into a labelled training example.

Plain language always -- the rep sees "Price objection (EMI amount or tenure)",
never objection.PRICE.EMI.
"""
from __future__ import annotations


DIMENSION_LABEL = {
    "objection": "objection",
    "brand_attribute": "brand",
    "outcome": "status",
}


def render_card(conn, capture_id: int, channel: str = "whatsapp") -> str:
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
    subject = next((r["subject"] for r in rows if r["subject"]), None)
    if subject:
        lines.append(f"• {subject}")

    # One line per idea. A rival named three times is still one fact to confirm.
    rivals: list[str] = []
    status: str | None = None
    for r in rows:
        if r["rival"] and r["rival"] not in rivals:
            rivals.append(r["rival"])
        if r["dimension"] == "outcome":
            status = r["label"].lower()
        elif r["dimension"] == "objection":
            lines.append(f"• Objection: {r['label']}")
        elif r["dimension"] == "brand_attribute":
            lines.append(f"• Brand: {r['label']}")

    if rivals:
        lines.append("• Also considering " + ", ".join(rivals))
    if status:
        lines.append(f"• Status: {status}")

    # "Reply 1" is WhatsApp copy. In a browser there are buttons, and showing
    # instructions for a channel the reader is not using reads as broken.
    if channel == "whatsapp":
        lines += ["", "Reply 1 if that's right,", "or just tell me what to fix."]
    return "\n".join(lines)


def card_items(conn, capture_id: int) -> list[dict]:
    """The card as structured rows, so a UI can render it rather than print it."""
    rows = conn.execute(
        """SELECT t.label, t.dimension, se.name AS subject, re.name AS rival
           FROM event e
           JOIN taxonomy_node t ON t.id = e.taxonomy_node_id
           LEFT JOIN entity se ON se.id = e.subject_id
           LEFT JOIN entity re ON re.id = e.rival_id
           WHERE e.capture_id = ? AND e.is_active = 1
           ORDER BY e.id""",
        (capture_id,),
    ).fetchall()
    items: list[dict] = []
    subject = next((r["subject"] for r in rows if r["subject"]), None)
    if subject:
        items.append({"kind": "Model", "text": subject})
    rivals: list[str] = []
    status = None
    for r in rows:
        if r["rival"] and r["rival"] not in rivals:
            rivals.append(r["rival"])
        if r["dimension"] == "outcome":
            status = r["label"]
        elif r["dimension"] == "objection":
            items.append({"kind": "Objection", "text": r["label"]})
        elif r["dimension"] == "brand_attribute":
            items.append({"kind": "Brand", "text": r["label"]})
    if rivals:
        items.append({"kind": "Also considering", "text": ", ".join(rivals)})
    if status:
        items.append({"kind": "Status", "text": status})
    return items
