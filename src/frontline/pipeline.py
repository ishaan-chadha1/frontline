"""The capture pipeline: transcript in, evidence-backed rows out.

  raw_capture -> transcript -> extraction_run -> event

Every row points back at the one above it. That chain is the only reason a
percentage in a report can reach the audio behind it.

Nothing is ever edited: re-extraction writes a NEW run and supersedes the old,
so a bad prompt deploy is a rollback rather than data loss.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import taxonomy as tax_mod
from .config import settings
from .confirm import render_card
from .db import connect, init_schema
from .extract import get_extractor
from .resolve import record_unresolved, resolve

UTC_NOW = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731


def ancestors(conn, unit_id: int) -> tuple[int, int | None, int | None]:
    """Flatten the org chain onto the event row so 'group by state' stays a
    join-free integer GROUP BY at any level of the hierarchy."""
    chain: list[int] = []
    current: int | None = unit_id
    while current is not None:
        chain.append(current)
        row = conn.execute("SELECT parent_id FROM org_unit WHERE id = ?", (current,)).fetchone()
        current = row["parent_id"] if row else None
    chain.reverse()
    padded = (chain + [None, None, None])[:3]
    return padded[0], padded[1], padded[2]


def ingest_text(
    conn,
    *,
    person_id: int,
    text: str,
    external_id: str,
    captured_on: str | None = None,
    audio_uri: str = "stub://none",
    engine: str = "manual",
) -> int:
    """Create a capture and its transcript. Idempotent on external_id, because
    the WhatsApp webhook will redeliver and duplicates would corrupt counts."""
    existing = conn.execute(
        "SELECT id FROM raw_capture WHERE external_id = ?", (external_id,)
    ).fetchone()
    if existing:
        return int(existing["id"])

    person = conn.execute("SELECT unit_id FROM person WHERE id = ?", (person_id,)).fetchone()
    if person is None:
        raise ValueError(f"unknown person_id {person_id}")

    now = UTC_NOW()
    capture_id = conn.insert(
        "INSERT INTO raw_capture (external_id, person_id, unit_id, audio_sha256, "
        "audio_uri, captured_at, captured_on, received_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            external_id,
            person_id,
            person["unit_id"],
            hashlib.sha256(text.encode()).hexdigest(),
            audio_uri,
            now,
            captured_on or date.today().isoformat(),
            now,
            "transcribed",
        ),
    )
    conn.execute(
        "INSERT INTO transcript (capture_id, engine, engine_version, text, created_at) "
        "VALUES (?,?,?,?,?)",
        (capture_id, engine, "1", text, now),
    )
    conn.commit()
    return capture_id


def extract_capture(conn, capture_id: int, provider: str | None = None) -> int:
    """Run extraction and write the events. Returns the new run id."""
    tax = tax_mod.load()
    transcript = conn.execute(
        "SELECT id, text FROM transcript WHERE capture_id = ? AND is_active = 1 "
        "ORDER BY id DESC LIMIT 1",
        (capture_id,),
    ).fetchone()
    if transcript is None:
        raise ValueError(f"capture {capture_id} has no active transcript")

    capture = conn.execute(
        "SELECT person_id, unit_id, captured_on FROM raw_capture WHERE id = ?", (capture_id,)
    ).fetchone()
    l1, l2, l3 = ancestors(conn, capture["unit_id"])

    result = get_extractor(provider).extract(transcript["text"], tax)

    # Supersede any previous run rather than editing it.
    prior = conn.execute(
        "SELECT id FROM extraction_run WHERE transcript_id = ? AND is_active = 1",
        (transcript["id"],),
    ).fetchall()

    run_id = conn.insert(
        "INSERT INTO extraction_run (transcript_id, model, prompt_version, "
        "taxonomy_version, input_tokens, output_tokens, latency_ms, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            transcript["id"],
            result.model,
            result.prompt_version,
            tax.version,
            result.input_tokens,
            result.output_tokens,
            result.latency_ms,
            UTC_NOW(),
        ),
    )

    for old in prior:
        conn.execute(
            "UPDATE extraction_run SET is_active = 0, superseded_by = ? WHERE id = ?",
            (run_id, old["id"]),
        )
        conn.execute("UPDATE event SET is_active = 0 WHERE run_id = ?", (old["id"],))

    node_ids = {
        r["path"]: r["id"]
        for r in conn.execute(
            "SELECT id, path FROM taxonomy_node WHERE vertical = ?", (tax.vertical,)
        ).fetchall()
    }

    for ev in result.events:
        node_id = node_ids.get(ev.node)
        if node_id is None:
            continue
        slot_ids: dict[str, int | None] = {}
        unmatched: list[tuple[str, str]] = []
        for slot_name, slot in tax.slots.items():
            res = resolve(conn, slot.entity_type, ev.slots.get(slot_name))
            slot_ids[slot_name] = res.entity_id
            if res.surface_form and not res.matched:
                unmatched.append((slot_name, res.surface_form))

        start = transcript["text"].find(ev.span) if ev.span else -1
        end = start + len(ev.span) if start >= 0 else None

        event_id = conn.insert(
            "INSERT INTO event (run_id, capture_id, person_id, unit_id, unit_l1, "
            "unit_l2, unit_l3, occurred_on, taxonomy_node_id, subject_id, rival_id, "
            "actor_id, polarity, intensity, span_start, span_end, confidence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, capture_id, capture["person_id"], capture["unit_id"], l1, l2, l3,
                capture["captured_on"], node_id,
                slot_ids.get("subject"), slot_ids.get("rival"), slot_ids.get("actor"),
                ev.polarity, ev.intensity,
                start if start >= 0 else None, end, ev.confidence,
            ),
        )
        for slot_name, surface in unmatched:
            record_unresolved(conn, event_id, slot_name, surface)

    conn.execute("UPDATE raw_capture SET status = 'extracted' WHERE id = ?", (capture_id,))
    conn.commit()
    return run_id


def process(conn, *, person_id: int, text: str, external_id: str, provider=None) -> dict:
    capture_id = ingest_text(conn, person_id=person_id, text=text, external_id=external_id)
    run_id = extract_capture(conn, capture_id, provider)
    return {"capture_id": capture_id, "run_id": run_id, "card": render_card(conn, capture_id)}
