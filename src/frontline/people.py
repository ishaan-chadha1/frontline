"""Rep management: who is capturing, and the link that identifies them."""
from __future__ import annotations

import secrets
from datetime import date


def new_token() -> str:
    return secrets.token_urlsafe(12)


def list_people(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT p.id, p.name, p.phone_e164, p.role, p.capture_token, p.is_active,
                  o.name AS store,
                  (SELECT COUNT(*) FROM raw_capture rc
                   WHERE rc.person_id = p.id AND rc.is_simulated = 0) AS notes
           FROM person p LEFT JOIN org_unit o ON o.id = p.unit_id
           WHERE p.capture_token IS NOT NULL
           ORDER BY p.is_active DESC, p.name"""
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "phone": r["phone_e164"],
             "role": r["role"], "store": r["store"], "token": r["capture_token"],
             "notes": r["notes"], "active": bool(r["is_active"])} for r in rows]


def stores(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name FROM org_unit WHERE level = 'store' AND is_active = 1 ORDER BY name"
    ).fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def add_person(conn, *, name: str, unit_id: int, phone: str | None = None,
               role: str = "sales_exec", language: str = "hi") -> dict:
    """Create a rep and mint their capture link."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    phone = (phone or "").strip() or f"pending-{secrets.token_hex(4)}"
    if conn.execute("SELECT 1 FROM person WHERE phone_e164 = ?", (phone,)).fetchone():
        raise ValueError(f"{phone} is already registered")

    token = new_token()
    pid = conn.insert(
        "INSERT INTO person (name, phone_e164, unit_id, role, language_pref, "
        "joined_on, capture_token) VALUES (?,?,?,?,?,?,?)",
        (name, phone, unit_id, role, language, date.today().isoformat(), token),
    )
    conn.commit()
    return {"id": pid, "name": name, "token": token}


def by_token(conn, token: str) -> dict | None:
    r = conn.execute(
        """SELECT p.id, p.name, p.unit_id, p.language_pref, o.name AS store
           FROM person p LEFT JOIN org_unit o ON o.id = p.unit_id
           WHERE p.capture_token = ? AND p.is_active = 1""",
        (token,),
    ).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "name": r["name"], "unit_id": r["unit_id"],
            "store": r["store"], "language": r["language_pref"]}


def deactivate(conn, person_id: int) -> None:
    conn.execute("UPDATE person SET is_active = 0 WHERE id = ?", (person_id,))
    conn.commit()
