"""Enrichment: work out what an unknown name actually is.

Extraction is deliberately locked to the taxonomy -- it cannot invent a
category, which is what keeps aggregates from fragmenting. That same constraint
means a brand nobody seeded lands in the review queue as a bare string.

This layer picks those up and asks Gemini, grounded in Google Search, what they
are. It runs as a batch over the queue, not per note.

The hard boundary: **enrichment never writes to the fact table.** A grounded
lookup can be confidently wrong, and a hallucinated brand becoming a real row
would quietly corrupt every competitive number. It writes a proposal; a human
approves it; only then does an entity exist.

    python -m frontline.enrich run
    python -m frontline.enrich list
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import taxonomy as tax_mod
from .config import settings
from .db import connect
from .taxonomy import normalise

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

LOOKUP_PROMPT = """You are helping identify names that salespeople mentioned in voice notes.

Context: {vertical} retail in India. Salespeople describe customer conversations,
and sometimes name a brand, model, or company the system does not recognise.

The name heard was: "{surface}"
It appeared in the "{slot}" slot, which in this vertical means: {slot_meaning}.
Heard {occurrences} time(s).

Search and answer briefly:
1. Is this a real company, brand or product in this market? Or is it a
   mishearing, a generic word, or a person's name?
2. If real: its correct spelling, who makes it, and what it is.
3. Other ways Indian salespeople might say or misspell it.

Be direct. If you cannot identify it with reasonable confidence, say so plainly
rather than guessing."""

STRUCTURE_PROMPT = """Turn this research note into structured data.

Research note:
{research}

The name as heard was "{surface}".

is_relevant is false when the name is a mishearing, a generic word, a person's
name, or anything that is not a company, brand or product in this market.

confidence is how certain the research is that this identification is correct:
90+ only when the name is unambiguous and well documented."""

STRUCTURE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_relevant": {"type": "BOOLEAN"},
        "proposed_name": {"type": "STRING", "nullable": True},
        "entity_type": {"type": "STRING", "nullable": True,
                        "enum": ["competitor", "product", "lead", "other"]},
        "description": {"type": "STRING"},
        "aliases": {"type": "ARRAY", "items": {"type": "STRING"}},
        "confidence": {"type": "INTEGER"},
    },
    "required": ["is_relevant", "proposed_name", "entity_type",
                 "description", "aliases", "confidence"],
}


def _ssl_ctx() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _call(body: dict, model: str) -> dict:
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    req = urllib.request.Request(
        ENDPOINT.format(model=model) + f"?key={key}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120, context=_ssl_ctx()) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Gemini {exc.code}: {exc.read().decode()[:300]}") from exc


def _text(payload: dict) -> str:
    parts = payload["candidates"][0]["content"].get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def _sources(payload: dict) -> list[dict]:
    """Pull citations out of the grounding metadata, so a proposal can be checked.

    Often empty: the model decides per query whether to search, and answers
    well-known names from its own knowledge. An empty list is therefore a real
    signal to whoever approves -- unverified, not merely uncited.
    """
    meta = payload["candidates"][0].get("groundingMetadata", {}) or {}
    out = []
    for chunk in meta.get("groundingChunks", []) or []:
        web = chunk.get("web") or {}
        if web.get("uri"):
            out.append({"title": web.get("title", ""), "uri": web["uri"]})
    return out[:4]


def research(surface: str, slot: str, slot_meaning: str, occurrences: int,
             vertical: str, model: str) -> tuple[str, list[dict]]:
    """Grounded lookup. Search and structured output cannot be combined in one
    call, so this returns prose plus citations and the next step structures it."""
    payload = _call({
        "contents": [{"role": "user", "parts": [{"text": LOOKUP_PROMPT.format(
            surface=surface, slot=slot, slot_meaning=slot_meaning,
            occurrences=occurrences, vertical=vertical.replace("_", " "))}]}],
        "tools": [{"google_search": {}}],
    }, model)
    return _text(payload), _sources(payload)


def structure(researched: str, surface: str, model: str) -> dict:
    payload = _call({
        "contents": [{"role": "user", "parts": [{"text": STRUCTURE_PROMPT.format(
            research=researched, surface=surface)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": STRUCTURE_SCHEMA,
            "temperature": 0,
        },
    }, model)
    return json.loads(_text(payload))


def pending_surfaces(conn, limit: int) -> list[dict]:
    """Unknown names not already proposed, most frequent first."""
    rows = conn.execute(
        """SELECT u.surface_form, u.slot, SUM(u.occurrences) AS n
           FROM unresolved_mention u
           WHERE u.reviewed = 0
             AND NOT EXISTS (SELECT 1 FROM entity_proposal p
                             WHERE p.surface_form = u.surface_form)
           GROUP BY u.surface_form, u.slot
           ORDER BY n DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [{"surface": r["surface_form"], "slot": r["slot"], "n": r["n"]} for r in rows]


def enrich_one(conn, item: dict, tax, model: str) -> dict:
    slot = tax.slots.get(item["slot"])
    meaning = f"{slot.label} ({slot.entity_type})" if slot else item["slot"]
    prose, sources = research(item["surface"], item["slot"], meaning,
                              item["n"], tax.vertical, model)
    data = structure(prose, item["surface"], model)
    conn.execute(
        "INSERT INTO entity_proposal (surface_form, slot, proposed_name, entity_type, "
        "description, aliases_json, confidence, is_relevant, sources_json, model, "
        "occurrences, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT DO NOTHING",
        (item["surface"], item["slot"], data.get("proposed_name"),
         data.get("entity_type"), data.get("description", ""),
         json.dumps(data.get("aliases", [])), int(data.get("confidence", 0)),
         1 if data.get("is_relevant") else 0, json.dumps(sources), model,
         item["n"], datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return data


def approve(conn, proposal_id: int) -> dict:
    """Create the entity, seed its aliases, and retroactively link the events
    that mentioned it. Approving is what turns a proposal into data."""
    p = conn.execute(
        "SELECT * FROM entity_proposal WHERE id = ? AND status = 'pending'", (proposal_id,)
    ).fetchone()
    if p is None:
        raise ValueError("no such pending proposal")

    name = p["proposed_name"] or p["surface_form"]
    etype = p["entity_type"] or "competitor"
    now = datetime.now(timezone.utc).isoformat()

    row = conn.execute(
        "SELECT id FROM entity WHERE entity_type = ? AND name = ?", (etype, name)
    ).fetchone()
    entity_id = int(row["id"]) if row else conn.insert(
        "INSERT INTO entity (entity_type, name) VALUES (?,?)", (etype, name)
    )

    aliases = set(json.loads(p["aliases_json"] or "[]")) | {name, p["surface_form"]}
    for alias in aliases:
        if alias and alias.strip():
            conn.execute(
                "INSERT INTO entity_alias (entity_id, alias_norm, source, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                (entity_id, normalise(alias), "human_confirmed", now),
            )

    # Backfill: the events that mentioned this name were written with a null
    # slot. Approving should fix the past, not only the future.
    column = {"subject": "subject_id", "rival": "rival_id", "actor": "actor_id"}.get(p["slot"])
    backfilled = 0
    if column:
        ids = [r["event_id"] for r in conn.execute(
            "SELECT event_id FROM unresolved_mention WHERE surface_form = ? AND event_id IS NOT NULL",
            (p["surface_form"],),
        ).fetchall()]
        for eid in ids:
            conn.execute(f"UPDATE event SET {column} = ? WHERE id = ?", (entity_id, eid))
            backfilled += 1

    conn.execute(
        "UPDATE unresolved_mention SET reviewed = 1 WHERE surface_form = ?", (p["surface_form"],))
    conn.execute(
        "UPDATE entity_proposal SET status = 'approved', decided_at = ?, entity_id = ? WHERE id = ?",
        (now, entity_id, proposal_id))
    conn.commit()
    return {"entity_id": entity_id, "name": name, "backfilled_events": backfilled}


def reject(conn, proposal_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT surface_form FROM entity_proposal WHERE id = ?", (proposal_id,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE unresolved_mention SET reviewed = 1 WHERE surface_form = ?",
            (row["surface_form"],))
    conn.execute(
        "UPDATE entity_proposal SET status = 'rejected', decided_at = ? WHERE id = ?",
        (now, proposal_id))
    conn.commit()


def cmd_run(args) -> int:
    conn = connect()
    tax = tax_mod.load()
    model = os.getenv("ENRICH_MODEL", settings.gemini_model)
    items = pending_surfaces(conn, args.limit)
    if not items:
        print("nothing unknown in the queue")
        return 0
    print(f"researching {len(items)} unknown names with {model} + Google Search\n")
    for item in items:
        try:
            d = enrich_one(conn, item, tax, model)
        except Exception as exc:  # noqa: BLE001
            print(f"  {item['surface']!r}: failed - {exc}")
            continue
        if d.get("is_relevant"):
            print(f"  {item['surface']!r} -> {d.get('proposed_name')} "
                  f"({d.get('entity_type')}, conf {d.get('confidence')})")
            print(f"      {d.get('description','')[:110]}")
        else:
            print(f"  {item['surface']!r} -> not a brand ({d.get('description','')[:70]})")
    conn.close()
    return 0


def cmd_list(args) -> int:
    conn = connect()
    rows = conn.execute(
        "SELECT id, surface_form, proposed_name, entity_type, confidence, is_relevant, "
        "occurrences, description FROM entity_proposal WHERE status = 'pending' "
        "ORDER BY is_relevant DESC, occurrences DESC"
    ).fetchall()
    if not rows:
        print("no pending proposals")
    for r in rows:
        tag = "" if r["is_relevant"] else "  [not a brand]"
        print(f"[{r['id']:3}] {r['surface_form']!r} -> {r['proposed_name']} "
              f"({r['entity_type']}, conf {r['confidence']}, seen {r['occurrences']}x){tag}")
        print(f"      {(r['description'] or '')[:110]}")
    conn.close()
    return 0


def cmd_approve(args) -> int:
    conn = connect()
    print(approve(conn, args.id))
    conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="frontline.enrich")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--limit", type=int, default=10)
    r.set_defaults(func=cmd_run)
    sub.add_parser("list").set_defaults(func=cmd_list)
    a = sub.add_parser("approve"); a.add_argument("id", type=int)
    a.set_defaults(func=cmd_approve)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
