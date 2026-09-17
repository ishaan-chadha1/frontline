"""Runnable demo: sample voice notes in, evidence-backed rows out.

    python -m frontline.demo run            # full pipeline, Gemini
    python -m frontline.demo run --stub     # no network, no key
    python -m frontline.demo evidence       # drill a number back to its source
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

from . import taxonomy as tax_mod
from .confirm import render_card
from .db import connect, init_schema
from .pipeline import process

SAMPLES = [
    (
        "Sir ko Model X pasand aayi thi but EMI thoda zyada lag raha tha, around 4,500 "
        "bol rahe the woh 4,000 tak hi karna chahte the. Exchange mein bhi unko lag raha "
        "tha ki kam de rahe hain. Ather bhi dekh ke aaye hain. Sochenge bol ke gaye hain."
    ),
    (
        "Customer aaya tha Model X ke liye, range ko lekar doubt tha, bola 150 km claim "
        "karte ho par actual mein kitna chalegi. Charging station ka bhi pooch raha tha "
        "ghar ke paas. Ola wale ne 180 bola tha usko."
    ),
    (
        "Ek family aayi thi, Model Y dekha, down payment jo hai 25,000 wo zyada lag raha "
        "tha unhe. Baaki sab theek tha, design bahut pasand aaya. Booking kar di."
    ),
    ("Haan toh main nikal raha hoon ab, kal milte hain."),
]


def seed(conn) -> int:
    """A minimal org: one country, one state, one store, one rep."""
    if conn.execute("SELECT 1 FROM person LIMIT 1").fetchone():
        return int(conn.execute("SELECT id FROM person LIMIT 1").fetchone()["id"])

    def unit(name, level, parent, depth):
        return conn.insert(
            "INSERT INTO org_unit (name, level, parent_id, depth) VALUES (?,?,?,?)",
            (name, level, parent, depth),
        )

    india = unit("India", "country", None, 0)
    up = unit("Uttar Pradesh", "state", india, 1)
    store = unit("Lucknow Hazratganj", "store", up, 2)

    for name in ("Model X", "Model Y"):
        eid = conn.insert(
            "INSERT INTO entity (entity_type, name) VALUES ('product', ?)", (name,)
        )
        conn.execute(
            "INSERT INTO entity_alias (entity_id, alias_norm, source, created_at) "
            "VALUES (?,?,'seed',?) ON CONFLICT DO NOTHING",
            (eid, tax_mod.normalise(name), datetime.now(timezone.utc).isoformat()),
        )

    person_id = conn.insert(
        "INSERT INTO person (name, phone_e164, unit_id, role, language_pref) "
        "VALUES ('Rahul Verma','+919999000001',?,'sales_exec','hi')",
        (store,),
    )
    conn.commit()
    return person_id


def cmd_run(args) -> int:
    conn = connect()
    init_schema(conn)
    tax_mod.sync_to_db(conn, tax_mod.load())
    person_id = seed(conn)
    provider = "stub" if args.stub else None

    for i, text in enumerate(SAMPLES, start=1):
        print("=" * 72)
        print(f"VOICE NOTE {i}")
        print("-" * 72)
        print(text)
        print()
        out = process(
            conn, person_id=person_id, text=text,
            external_id=f"demo-{date.today()}-{i}", provider=provider,
        )
        print("EXTRACTED")
        print("-" * 72)
        rows = conn.execute(
            """SELECT t.path, t.label, e.confidence, e.polarity,
                      se.name AS subject, re.name AS rival, e.span_start, e.span_end
               FROM event e JOIN taxonomy_node t ON t.id = e.taxonomy_node_id
               LEFT JOIN entity se ON se.id = e.subject_id
               LEFT JOIN entity re ON re.id = e.rival_id
               WHERE e.capture_id = ? AND e.is_active = 1 ORDER BY e.id""",
            (out["capture_id"],),
        ).fetchall()
        if not rows:
            print("  (nothing extractable — flagged unclear, routed to the gap list)")
        for r in rows:
            bits = [f"{r['path']:34}", f"conf {r['confidence']:3}"]
            if r["subject"]:
                bits.append(f"subject={r['subject']}")
            if r["rival"]:
                bits.append(f"rival={r['rival']}")
            if r["span_start"] is not None:
                bits.append(f"chars {r['span_start']}-{r['span_end']}")
            print("  " + "  ".join(bits))
        print()
        print("CONFIRMATION CARD SENT TO REP")
        print("-" * 72)
        print(out["card"])
        print()

    print("=" * 72)
    print("AGGREGATE")
    print("-" * 72)
    total = conn.execute(
        "SELECT COUNT(*) c FROM raw_capture WHERE status = 'extracted'"
    ).fetchone()["c"]
    for r in conn.execute(
        """SELECT t.label, COUNT(*) n FROM event e
           JOIN taxonomy_node t ON t.id = e.taxonomy_node_id
           WHERE e.is_active = 1 AND t.dimension = 'objection'
           GROUP BY t.id ORDER BY n DESC"""
    ).fetchall():
        pct = round(100 * r["n"] / total)
        print(f"  {r['label']:34} {pct:3}% of {total} reported interactions  (n={r['n']})")

    unresolved = conn.execute(
        "SELECT slot, surface_form, occurrences FROM unresolved_mention WHERE reviewed = 0 "
        "ORDER BY occurrences DESC"
    ).fetchall()
    if unresolved:
        print()
        print("UNRESOLVED MENTIONS (weekly review queue)")
        print("-" * 72)
        for u in unresolved:
            print(f"  {u['slot']:10} {u['surface_form']!r}  seen {u['occurrences']}x")
    conn.close()
    return 0


def cmd_evidence(args) -> int:
    """The move that sells this product: a number back to its source."""
    conn = connect()
    row = conn.execute(
        "SELECT id, path, label FROM taxonomy_node WHERE path = ?", (args.node,)
    ).fetchone()
    if row is None:
        print(f"no such node: {args.node}", file=sys.stderr)
        return 1
    print(f"{row['label']}  ({row['path']})")
    print("-" * 72)
    for e in conn.execute(
        """SELECT e.id, e.confidence, e.occurred_on, p.name AS rep, o.name AS store,
                  e.span_start, e.span_end, tr.text, rc.audio_uri
           FROM event e
           JOIN raw_capture rc ON rc.id = e.capture_id
           JOIN transcript tr ON tr.capture_id = rc.id
           JOIN person p ON p.id = e.person_id
           JOIN org_unit o ON o.id = e.unit_id
           WHERE e.taxonomy_node_id = ? AND e.is_active = 1""",
        (row["id"],),
    ).fetchall():
        span = (
            e["text"][e["span_start"] : e["span_end"]]
            if e["span_start"] is not None
            else "(span not located)"
        )
        print(f"  event {e['id']} · {e['occurred_on']} · {e['rep']} · {e['store']} · conf {e['confidence']}")
        print(f"    audio: {e['audio_uri']}")
        print(f"    said:  \"{span.strip()}\"")
        print()
    conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="frontline.demo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="process sample voice notes end to end")
    r.add_argument("--stub", action="store_true", help="offline, no API key needed")
    r.set_defaults(func=cmd_run)
    e = sub.add_parser("evidence", help="drill a taxonomy node back to its voice notes")
    e.add_argument("node", nargs="?", default="objection.PRICE.EMI")
    e.set_defaults(func=cmd_evidence)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
