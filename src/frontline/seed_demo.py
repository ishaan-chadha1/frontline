"""Generate a realistic simulated network so the demo shows the product.

Nine notes from one rep demonstrates extraction. It does not demonstrate an
intelligence system -- no trends, no concentration, and every confidence grade
correctly stuck at Low. This builds enough volume for the thresholds in the
spec to actually be met.

Everything it writes is flagged is_simulated. The patterns below are invented,
so the demo must be framed as "this is the shape of what you would see", never
as market insight.

    python -m frontline.seed_demo build --notes 1800
"""
from __future__ import annotations

import argparse
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from . import notes as N
from . import taxonomy as tax_mod
from .db import connect, init_schema
from .extract import get_extractor
from .resolve import record_unresolved, resolve

# --------------------------------------------------------------- the network

STATES = [
    ("Uttar Pradesh", [("Lucknow Hazratganj", 1.0), ("Kanpur Mall Road", 0.95)]),
    ("Bihar", [("Patna Boring Road", 0.35)]),          # deliberately under-reporting
    ("Maharashtra", [("Pune Baner", 1.0), ("Nashik CBS", 0.9)]),
]
REPS_PER_STORE = 4
FIRST_NAMES = ["Rahul", "Amit", "Priya", "Suresh", "Neha", "Vikas", "Anjali", "Manoj",
               "Deepak", "Kavita", "Rohit", "Sneha", "Arjun", "Pooja", "Nitin",
               "Ritu", "Sandeep", "Meena", "Gaurav", "Shalini"]
LAST_NAMES = ["Verma", "Sharma", "Singh", "Gupta", "Yadav", "Patil", "Joshi", "Kumar"]

# Patterns the demo needs something to find. Invented, not observed.
PRICE_HEAVY_STATES = {"Uttar Pradesh", "Bihar"}
PRICE_WEIGHT_BASE = 0.26
PRICE_WEIGHT_UPLIFT = 0.13      # how much more price-driven UP + Bihar are
PRICE_TREND_UPLIFT = 0.09       # second half of the window runs hotter
ATHER_TREND_UPLIFT = 0.18       # Ather mentions climb through the period
MODEL_X_PRICE_SHARE = 0.67      # price objections skew to Model X
NOISE_SHARE = 0.08
NETWORK_REPORTING = 0.88    # even a diligent store misses roughly an eighth


def _fill(template: str, rng: random.Random, model: str, rival: str) -> str:
    slots = {k: rng.choice(v) for k, v in N.SLOTS.items()}
    slots.update(model=model, rival=rival, city=rng.choice(["Lucknow", "Patna", "Pune", "Nashik"]))
    return template.format(**slots)


def compose(rng: random.Random, *, price_heavy: bool, late: bool) -> str:
    """Build one note from 1-3 fragments plus an outcome."""
    if rng.random() < NOISE_SHARE:
        return rng.choice(N.NOISE)

    price_w = PRICE_WEIGHT_BASE + (PRICE_WEIGHT_UPLIFT if price_heavy else 0)
    if late:
        price_w += PRICE_TREND_UPLIFT

    rival = rng.choice(N.RIVALS)
    if late and rng.random() < ATHER_TREND_UPLIFT:
        rival = "Ather"

    model = "Model X" if rng.random() < MODEL_X_PRICE_SHARE else rng.choice(N.MODELS)

    parts: list[str] = []
    if rng.random() < price_w:
        parts.append(rng.choice(N.PRICE))
    if rng.random() < 0.30:
        parts.append(rng.choice(N.BATTERY))
    if rng.random() < 0.15:
        parts.append(rng.choice(N.SERVICE))
    if rng.random() < 0.18:
        parts.append(rng.choice(N.POSITIVE))
    if not parts:
        parts.append(rng.choice(N.BATTERY + N.SERVICE + N.POSITIVE))
    if rng.random() < 0.38:
        parts.append(rng.choice(N.COMPETITIVE))

    roll = rng.random()
    parts.append(rng.choice(
        N.BOOKED if roll < 0.18 else N.HOT if roll < 0.38
        else N.LOST if roll < 0.48 else N.WARM))

    return " ".join(_fill(p, rng, model, rival) for p in parts)


# ------------------------------------------------------------------ building

def build_network(conn) -> list[dict]:
    """Create org units and people. Idempotent on store code."""
    people: list[dict] = []
    rng = random.Random(7)
    india = conn.execute(
        "SELECT id FROM org_unit WHERE level = 'country' AND name = 'India'"
    ).fetchone()
    india_id = india["id"] if india else conn.insert(
        "INSERT INTO org_unit (name, level, parent_id, depth) VALUES ('India','country',NULL,0)"
    )

    name_pool = [f"{f} {l}" for f in FIRST_NAMES for l in LAST_NAMES]
    rng.shuffle(name_pool)
    phone = 919000000000

    for state_name, stores in STATES:
        row = conn.execute(
            "SELECT id FROM org_unit WHERE level = 'state' AND name = ?", (state_name,)
        ).fetchone()
        state_id = row["id"] if row else conn.insert(
            "INSERT INTO org_unit (name, level, parent_id, depth) VALUES (?,'state',?,1)",
            (state_name, india_id),
        )
        for store_name, reporting in stores:
            row = conn.execute(
                "SELECT id FROM org_unit WHERE level = 'store' AND name = ?", (store_name,)
            ).fetchone()
            store_id = row["id"] if row else conn.insert(
                "INSERT INTO org_unit (name, code, level, parent_id, depth) VALUES (?,?,'store',?,2)",
                (store_name, store_name.split()[0].upper()[:6], state_id),
            )
            for _ in range(REPS_PER_STORE):
                phone += 1
                p = f"+{phone}"
                row = conn.execute("SELECT id FROM person WHERE phone_e164 = ?", (p,)).fetchone()
                pid = row["id"] if row else conn.insert(
                    "INSERT INTO person (name, phone_e164, unit_id, role, language_pref) "
                    "VALUES (?,?,?,'sales_exec','hi')",
                    (name_pool.pop(), p, store_id),
                )
                people.append({
                    "id": pid, "unit_id": store_id, "state": state_name,
                    "store": store_name, "reporting": reporting,
                })
    conn.commit()
    return people


def ancestors(conn, unit_id: int):
    chain, cur = [], unit_id
    while cur is not None:
        chain.append(cur)
        row = conn.execute("SELECT parent_id FROM org_unit WHERE id = ?", (cur,)).fetchone()
        cur = row["parent_id"] if row else None
    chain.reverse()
    return (chain + [None, None, None])[:3]


def plan_notes(people: list[dict], total: int, days: int, seed: int) -> list[dict]:
    """Decide who says what, when. Reporting rate drives per-store volume."""
    rng = random.Random(seed)
    today = date.today()
    weights = [p["reporting"] for p in people]
    planned: list[dict] = []
    for i in range(total):
        person = rng.choices(people, weights=weights, k=1)[0]
        day_offset = rng.randrange(days)
        occurred = today - timedelta(days=day_offset)
        late = day_offset < days // 2          # the more recent half of the window
        planned.append({
            "person": person,
            "occurred_on": occurred.isoformat(),
            "text": compose(rng, price_heavy=person["state"] in PRICE_HEAVY_STATES, late=late),
            "external_id": f"sim-{seed}-{i:05d}",
        })
    return planned


def insert_captures(conn, planned: list[dict]) -> list[tuple[int, int, str, dict]]:
    """Write captures and transcripts. Returns work items for extraction."""
    now = datetime.now(timezone.utc).isoformat()
    work = []
    for item in planned:
        row = conn.execute(
            "SELECT id FROM raw_capture WHERE external_id = ?", (item["external_id"],)
        ).fetchone()
        if row:
            continue
        person = item["person"]
        capture_id = conn.insert(
            "INSERT INTO raw_capture (external_id, person_id, unit_id, audio_sha256, "
            "audio_uri, captured_at, captured_on, received_at, status, is_simulated) "
            "VALUES (?,?,?,?,?,?,?,?,?,1)",
            (item["external_id"], person["id"], person["unit_id"], "simulated",
             "simulated://none", now, item["occurred_on"], now, "transcribed"),
        )
        tid = conn.insert(
            "INSERT INTO transcript (capture_id, engine, engine_version, text, created_at) "
            "VALUES (?,?,?,?,?)",
            (capture_id, "simulated", "1", item["text"], now),
        )
        work.append((capture_id, tid, item["text"], person))
    conn.commit()
    return work


def seed_shifts(conn, people: list[dict], days: int) -> None:
    """Expected captures per rep per day.

    Set from the store's reporting rate, so coverage is a real computed number
    rather than a decoration -- and Patna genuinely fails the threshold.
    """
    today = date.today()
    for person in people:
        for d in range(days):
            occurred = (today - timedelta(days=d)).isoformat()
            actual = conn.execute(
                "SELECT COUNT(*) AS c FROM raw_capture WHERE person_id = ? AND captured_on = ?",
                (person["id"], occurred),
            ).fetchone()["c"]
            if not actual:
                continue
            # A network-wide under-reporting factor on top of the store's own
            # rate: nobody logs every conversation, and coverage that reads 100%
            # everywhere would quietly defeat the point of measuring it.
            rate = max(person["reporting"] * NETWORK_REPORTING, 0.15)
            expected = max(actual, round(actual / rate))
            conn.execute(
                "INSERT INTO shift (person_id, occurred_on, expected_captures, count_source) "
                "VALUES (?,?,?,'manager_count') ON CONFLICT DO NOTHING",
                (person["id"], occurred, expected),
            )
    conn.commit()


def extract_all(conn, work, tax, workers: int = 8) -> int:
    """Run the real extractor over every note, then write serially.

    Network calls fan out; database writes stay on one thread because the
    connection is not safe to share. Resumable: anything already extracted is
    skipped, so a failure halfway does not mean paying for it twice.
    """
    extractor = get_extractor()
    node_ids = {
        r["path"]: r["id"]
        for r in conn.execute(
            "SELECT id, path FROM taxonomy_node WHERE vertical = ?", (tax.vertical,)
        ).fetchall()
    }

    pending = [w for w in work if not conn.execute(
        "SELECT 1 FROM extraction_run r JOIN transcript t ON t.id = r.transcript_id "
        "WHERE t.id = ? AND r.is_active = 1", (w[1],)
    ).fetchone()]
    if not pending:
        return 0

    def run(item):
        capture_id, tid, text, person = item
        try:
            return item, extractor.extract(text, tax), None
        except Exception as exc:  # noqa: BLE001
            return item, None, exc

    written = 0
    failures = 0
    now = datetime.now(timezone.utc).isoformat()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, (item, result, err) in enumerate(pool.map(run, pending), start=1):
            if n % 100 == 0 or n == len(pending):
                print(f"  extracted {n}/{len(pending)}", flush=True)
            if err is not None:
                failures += 1
                continue
            capture_id, tid, text, person = item
            l1, l2, l3 = ancestors(conn, person["unit_id"])
            occurred = conn.execute(
                "SELECT captured_on FROM raw_capture WHERE id = ?", (capture_id,)
            ).fetchone()["captured_on"]

            run_id = conn.insert(
                "INSERT INTO extraction_run (transcript_id, model, prompt_version, "
                "taxonomy_version, input_tokens, output_tokens, latency_ms, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (tid, result.model, result.prompt_version, tax.version,
                 result.input_tokens, result.output_tokens, result.latency_ms, now),
            )
            for ev in result.events:
                node_id = node_ids.get(ev.node)
                if node_id is None:
                    continue
                slot_ids, unmatched = {}, []
                for slot_name, slot in tax.slots.items():
                    res = resolve(conn, slot.entity_type, ev.slots.get(slot_name))
                    slot_ids[slot_name] = res.entity_id
                    if res.surface_form and not res.matched:
                        unmatched.append((slot_name, res.surface_form))
                start = text.find(ev.span) if ev.span else -1
                event_id = conn.insert(
                    "INSERT INTO event (run_id, capture_id, person_id, unit_id, unit_l1, "
                    "unit_l2, unit_l3, occurred_on, taxonomy_node_id, subject_id, rival_id, "
                    "actor_id, polarity, intensity, span_start, span_end, confidence) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, capture_id, person["id"], person["unit_id"], l1, l2, l3,
                     occurred, node_id, slot_ids.get("subject"), slot_ids.get("rival"),
                     slot_ids.get("actor"), ev.polarity, ev.intensity,
                     start if start >= 0 else None,
                     start + len(ev.span) if start >= 0 else None, ev.confidence),
                )
                for slot_name, surface in unmatched:
                    record_unresolved(conn, event_id, slot_name, surface)
            conn.execute(
                "UPDATE raw_capture SET status = 'extracted' WHERE id = ?", (capture_id,)
            )
            written += 1
            if written % 50 == 0:
                conn.commit()
    conn.commit()
    if failures:
        print(f"  {failures} extraction failures (re-run to retry those)")
    return written


def wipe(conn) -> None:
    """Remove simulated data only. Anything a human typed into the demo stays."""
    # Children before parents: unresolved_mention points at event, so clearing
    # events first violates the foreign key.
    conn.execute("DELETE FROM unresolved_mention")
    conn.execute(
        "DELETE FROM event WHERE capture_id IN (SELECT id FROM raw_capture WHERE is_simulated = 1)")
    conn.execute(
        "DELETE FROM extraction_run WHERE transcript_id IN (SELECT t.id FROM transcript t "
        "JOIN raw_capture rc ON rc.id = t.capture_id WHERE rc.is_simulated = 1)")
    conn.execute(
        "DELETE FROM transcript WHERE capture_id IN (SELECT id FROM raw_capture WHERE is_simulated = 1)")
    conn.execute("DELETE FROM raw_capture WHERE is_simulated = 1")
    conn.commit()


def cmd_build(args) -> int:
    conn = connect()
    init_schema(conn)
    tax = tax_mod.load()
    tax_mod.sync_to_db(conn, tax)

    if args.reset:
        print("clearing previous simulated data...")
        wipe(conn)

    print(f"building network: {len(STATES)} states, "
          f"{sum(len(s) for _, s in STATES)} stores, "
          f"{sum(len(s) for _, s in STATES) * REPS_PER_STORE} reps")
    people = build_network(conn)

    print(f"planning {args.notes} notes over {args.days} days...")
    planned = plan_notes(people, args.notes, args.days, args.seed)
    work = insert_captures(conn, planned)
    print(f"  {len(work)} new captures written")

    print(f"extracting with {get_extractor().model} ({args.workers} workers)...")
    extract_all(conn, work, tax, args.workers)

    print("seeding shift expectations...")
    seed_shifts(conn, people, args.days)

    total = conn.execute(
        "SELECT COUNT(*) AS c FROM raw_capture WHERE is_simulated = 1").fetchone()["c"]
    events = conn.execute(
        "SELECT COUNT(*) AS c FROM event WHERE is_active = 1").fetchone()["c"]
    print(f"\ndone: {total} simulated captures, {events} events")
    conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="frontline.seed_demo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--notes", type=int, default=1800)
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--seed", type=int, default=42)
    b.add_argument("--workers", type=int, default=8)
    b.add_argument("--reset", action="store_true", help="clear previous simulated data first")
    b.set_defaults(func=cmd_build)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
