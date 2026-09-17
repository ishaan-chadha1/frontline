"""The taxonomy layer is where a silent mistake is most expensive.

A fragmented enum or a reshuffled node id corrupts every aggregate downstream
without raising anything, so these are the invariants worth guarding.
"""
from pathlib import Path

import pytest

from frontline.db import connect, init_schema, table_names
from frontline.taxonomy import extraction_schema, load, normalise, sync_to_db

REPO = Path(__file__).resolve().parents[1]
EV = REPO / "taxonomy" / "ev_two_wheeler.yaml"
MATTRESS = REPO / "taxonomy" / "mattress.yaml"


@pytest.fixture
def conn(tmp_path):
    c = connect(path=tmp_path / "test.db")
    init_schema(c)
    yield c
    c.close()


def test_schema_creates_every_table(conn):
    names = table_names(conn)
    for expected in ("event", "raw_capture", "transcript", "extraction_run",
                     "rollup_daily", "entity_alias", "taxonomy_node", "shift"):
        assert expected in names


def test_node_ids_are_stable_across_reload(conn):
    tax = load(EV)
    first = sync_to_db(conn, tax)
    second = sync_to_db(conn, tax)
    assert first == second, "node ids must never be reused or reshuffled"


def test_only_leaves_are_emittable(conn):
    tax = load(EV)
    leaves = set(tax.leaf_paths())
    assert "objection.PRICE.EMI" in leaves
    # A parent must not be emittable, or counts would double against its children.
    assert "objection.PRICE" not in leaves


def test_generated_schema_constrains_the_node_field():
    schema = extraction_schema(load(EV))
    node = schema["properties"]["events"]["items"]["properties"]["node"]
    assert node["enum"] == load(EV).leaf_paths()
    assert schema["properties"]["events"]["items"]["additionalProperties"] is False


def test_schema_exposes_generic_slots_not_industry_names():
    props = extraction_schema(load(EV))["properties"]["events"]["items"]["properties"]
    for slot in ("subject", "rival", "actor"):
        assert slot in props
    # The fact table must never learn what industry it is in.
    assert "product" not in props
    assert "competitor" not in props


def test_a_second_vertical_needs_no_code_change():
    ev, mattress = load(EV), load(MATTRESS)
    assert ev.leaf_paths() != mattress.leaf_paths()
    assert mattress.slots["subject"].label == "Mattress"
    assert extraction_schema(mattress)["properties"]["events"]["items"]["properties"]["node"]["enum"]


def test_retired_nodes_are_marked_not_deleted(conn, tmp_path):
    tax = load(EV)
    sync_to_db(conn, tax)
    removed = tax.dimensions["objection"]["nodes"]["PRICE"]["children"].pop("EMI")
    assert removed
    tax.version = "1.1.0"
    sync_to_db(conn, tax)
    row = conn.execute(
        "SELECT retired_in FROM taxonomy_node WHERE path = 'objection.PRICE.EMI'"
    ).fetchone()
    assert row is not None, "history must stay queryable"
    assert row["retired_in"] == "1.1.0"


@pytest.mark.parametrize(
    "surface,expected",
    [("Ather", "Ather"), ("athar", "Ather"), ("AT", "Ather"),
     ("  OLAA  ", "Ola"), ("TVS iQube", "TVS")],
)
def test_aliases_collapse_to_one_entity(conn, surface, expected):
    sync_to_db(conn, load(EV))
    row = conn.execute(
        "SELECT e.name FROM entity_alias a JOIN entity e ON e.id = a.entity_id "
        "WHERE a.alias_norm = ?",
        (normalise(surface),),
    ).fetchone()
    assert row is not None, f"{surface!r} should resolve, not fragment the count"
    assert row["name"] == expected


def test_unknown_mention_does_not_resolve(conn):
    sync_to_db(conn, load(EV))
    row = conn.execute(
        "SELECT 1 FROM entity_alias WHERE alias_norm = ?", (normalise("Sleepwell"),)
    ).fetchone()
    assert row is None, "an unknown name must queue for review, not silently bind"


def test_gemini_schema_dialect_conversion():
    """Gemini takes a subset of JSON Schema. Keeping the canonical schema
    provider-neutral and translating at the edge is what lets a second provider
    land without touching the taxonomy layer."""
    from frontline.extract import to_gemini_schema

    g = to_gemini_schema(extraction_schema(load(EV)))
    item = g["properties"]["events"]["items"]
    assert item["type"] == "OBJECT"
    assert item["properties"]["node"]["enum"], "the node enum must survive translation"
    assert item["properties"]["subject"]["nullable"] is True
    assert "additionalProperties" not in item, "Gemini rejects it"
