"""Taxonomy: the controlled vocabulary the extractor is allowed to emit.

Configuration, not code. One YAML file per vertical, and the extractor's output
schema is GENERATED from it -- so an invalid node is impossible by construction
rather than caught by validation.

Without that constraint the extractor writes price_too_high on Monday,
expensive on Tuesday and cost concern on Wednesday. Each is reasonable; together
they silently fragment the aggregate, and nothing errors.
"""
from __future__ import annotations

import json
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import settings
from .db import connect


@dataclass(frozen=True)
class Slot:
    name: str
    entity_type: str
    label: str


@dataclass
class Taxonomy:
    vertical: str
    version: str
    language_hints: list[str]
    slots: dict[str, Slot]
    org_levels: list[str]
    dimensions: dict[str, dict[str, Any]]
    seed_entities: dict[str, list[dict]] = field(default_factory=dict)

    def paths(self) -> list[tuple[str, str, str, int]]:
        """Flatten to (path, dimension, label, depth), parents before children."""
        out: list[tuple[str, str, str, int]] = []

        def walk(dimension: str, prefix: str, nodes: dict[str, Any], depth: int) -> None:
            for key, body in nodes.items():
                body = body or {}
                path = f"{prefix}.{key}" if prefix else f"{dimension}.{key}"
                out.append((path, dimension, body.get("label", key), depth))
                children = body.get("children")
                if children:
                    walk(dimension, path, children, depth + 1)

        for dimension, body in self.dimensions.items():
            walk(dimension, "", body.get("nodes", {}), 1)
        return out

    def leaf_paths(self) -> list[str]:
        """Paths an extractor may emit: leaves only, so counts never double."""
        all_paths = [p for p, _, _, _ in self.paths()]
        prefixes = {p.rsplit(".", 1)[0] for p in all_paths if "." in p}
        return [p for p in all_paths if p not in prefixes]

    def render_tree(self) -> str:
        """Indented tree for the extraction prompt."""
        lines: list[str] = []
        current = None
        for path, dimension, label, depth in self.paths():
            if dimension != current:
                lines.append(f"\n{dimension}:")
                current = dimension
            lines.append(f"{'  ' * depth}{path}  -- {label}")
        return "\n".join(lines).strip()


def load(path: Path | None = None) -> Taxonomy:
    path = Path(path or settings.taxonomy_file)
    raw = yaml.safe_load(path.read_text())
    slots = {
        name: Slot(name=name, entity_type=body["entity_type"], label=body["label"])
        for name, body in (raw.get("slots") or {}).items()
    }
    return Taxonomy(
        vertical=raw["vertical"],
        version=str(raw["version"]),
        language_hints=raw.get("language_hints", []),
        slots=slots,
        org_levels=raw.get("org_levels", []),
        dimensions=raw.get("dimensions", {}),
        seed_entities=raw.get("seed_entities", {}) or {},
    )


def normalise(text: str) -> str:
    """Fold an alias to its lookup form: unaccented, lowercase, collapsed."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


def sync_to_db(conn, tax: Taxonomy) -> dict[str, int]:
    """Upsert nodes and seed entities.

    Integer ids are permanent and never reused. A node that disappears from the
    YAML is marked retired, never deleted -- history stays queryable.
    """
    now = datetime.now(timezone.utc).isoformat()
    ids: dict[str, int] = {}

    for order, (path, dimension, label, depth) in enumerate(tax.paths()):
        parent_path = path.rsplit(".", 1)[0] if depth > 1 else None
        parent_id = ids.get(parent_path) if parent_path else None
        row = conn.execute(
            "SELECT id FROM taxonomy_node WHERE vertical = ? AND path = ?",
            (tax.vertical, path),
        ).fetchone()
        if row:
            node_id = row["id"]
            conn.execute(
                "UPDATE taxonomy_node SET label = ?, parent_id = ?, depth = ?, "
                "sort_order = ?, retired_in = NULL WHERE id = ?",
                (label, parent_id, depth, order, node_id),
            )
        else:
            node_id = conn.insert(
                "INSERT INTO taxonomy_node (vertical, path, dimension, label, "
                "parent_id, depth, sort_order, added_in) VALUES (?,?,?,?,?,?,?,?)",
                (tax.vertical, path, dimension, label, parent_id, depth, order, tax.version),
            )
        ids[path] = node_id

    live = set(ids)
    for row in conn.execute(
        "SELECT id, path FROM taxonomy_node WHERE vertical = ? AND retired_in IS NULL",
        (tax.vertical,),
    ).fetchall():
        if row["path"] not in live:
            conn.execute(
                "UPDATE taxonomy_node SET retired_in = ? WHERE id = ?",
                (tax.version, row["id"]),
            )

    for entity_type, items in tax.seed_entities.items():
        for item in items:
            row = conn.execute(
                "SELECT id FROM entity WHERE entity_type = ? AND name = ?",
                (entity_type, item["name"]),
            ).fetchone()
            entity_id = (
                int(row["id"])
                if row
                else conn.insert(
                    "INSERT INTO entity (entity_type, name) VALUES (?,?)",
                    (entity_type, item["name"]),
                )
            )
            aliases = set(item.get("aliases", [])) | {item["name"]}
            for alias in aliases:
                conn.execute(
                    "INSERT INTO entity_alias (entity_id, alias_norm, source, created_at) "
                    "VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                    (entity_id, normalise(alias), "seed", now),
                )

    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?,?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (f"taxonomy_version:{tax.vertical}", tax.version),
    )
    conn.commit()
    return ids


def extraction_schema(tax: Taxonomy) -> dict[str, Any]:
    """The extractor's output contract, generated from the taxonomy.

    The node enum is the set of valid leaf paths, so the model cannot invent a
    category. Slot keys stay generic (subject/rival/actor) -- the prompt says
    what they mean in this vertical, which is what keeps one schema generator
    serving every industry.
    """
    event_props: dict[str, Any] = {
        "node": {"type": "string", "enum": tax.leaf_paths()},
    }
    for name in tax.slots:
        event_props[name] = {"type": ["string", "null"]}
    event_props.update(
        {
            "polarity": {"type": "integer", "enum": [-1, 0, 1]},
            "intensity": {"type": ["integer", "null"], "enum": [1, 2, 3, None]},
            "span": {"type": "string"},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        }
    )
    return {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": event_props,
                    "required": list(event_props),
                    "additionalProperties": False,
                },
            },
            "interaction_count": {"type": "integer"},
            "unclear": {"type": "boolean"},
        },
        "required": ["events", "interaction_count", "unclear"],
        "additionalProperties": False,
    }


def _cli(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m frontline.taxonomy {load|schema|tree} [file]", file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    tax = load(Path(rest[0]) if rest else None)

    if command == "load":
        conn = connect()
        ids = sync_to_db(conn, tax)
        entities = conn.execute("SELECT COUNT(*) c FROM entity").fetchone()["c"]
        aliases = conn.execute("SELECT COUNT(*) c FROM entity_alias").fetchone()["c"]
        conn.close()
        print(f"{tax.vertical} v{tax.version}")
        print(f"  {len(ids)} taxonomy nodes ({len(tax.leaf_paths())} emittable leaves)")
        print(f"  {entities} entities, {aliases} aliases")
        return 0
    if command == "schema":
        print(json.dumps(extraction_schema(tax), indent=2))
        return 0
    if command == "tree":
        print(tax.render_tree())
        return 0
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
