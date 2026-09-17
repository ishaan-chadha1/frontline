# frontline

Frontline retail intelligence: WhatsApp voice notes from salespeople become
structured, evidence-backed signals for leadership.

The moat is reliability. Every number this system reports carries a denominator,
a confidence grade, a delta, and a complete chain back to the voice notes that
produced it.

Full spec: see the technical specification document (Parts I-V).

## Core idea

```
raw_capture  ->  transcript  ->  extraction_run  ->  event  ->  rollup_daily
  (audio)        (the words)     (one model pass)   (claims)    (counts)
```

Each row points back at the one above it, so any percentage in a report drills
all the way down to the audio behind it.

The `event` table holds only integers. The words live in lookup tables, joined
at render time and never inside a count.

## Vertical-agnostic by design

The fact table's dimension columns (`subject_id`, `rival_id`, `actor_id`) carry
no industry meaning. A YAML file per vertical declares what they mean:

```yaml
slots:
  subject: { entity_type: product,    label: Model }
  rival:   { entity_type: competitor, label: Competitor }
  actor:   { entity_type: lead,       label: Customer }
```

EV two-wheelers today, mattresses or pharma tomorrow -- a new file, never an
`ALTER TABLE`.

## Status

Sprint 1, in progress. Schema, taxonomy loader, and extraction schema generator.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m frontline.db init
python -m frontline.taxonomy load taxonomy/ev_two_wheeler.yaml
python -m frontline.taxonomy schema
```
