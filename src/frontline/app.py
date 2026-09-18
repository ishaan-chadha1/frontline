"""The demo web app.

One page: speak or type a note, watch it become structured events with a
confidence, a confirmation card, and a link back to the exact words.

Gated by a shared access code. It runs LLM calls on a real billing account, so
it must never sit open on a public URL.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse

from . import taxonomy as tax_mod
from .asr import get_asr
from .confirm import card_items, render_card
from .db import connect, init_schema
from .demo import seed
from .people import add_person, by_token, deactivate, list_people, stores
from .pipeline import apply_correction, extract_capture, ingest_text, record_confirmation
from .storage import store as store_audio
from .enrich import approve as approve_proposal, reject as reject_proposal
from .resolve import prune_unresolved

ACCESS_CODE = os.getenv("ACCESS_CODE", "")

app = FastAPI(title="Frontline", docs_url=None, redoc_url=None)


def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def require_code(request: Request) -> None:
    if not ACCESS_CODE:
        return
    supplied = request.headers.get("x-access-code") or request.query_params.get("code")
    if not supplied or not secrets.compare_digest(supplied, ACCESS_CODE):
        raise HTTPException(status_code=401, detail="Invalid access code")


@app.on_event("startup")
def startup() -> None:
    conn = connect()
    init_schema(conn)
    tax = tax_mod.load()
    tax_mod.sync_to_db(conn, tax)
    prune_unresolved(conn, tax)
    seed(conn)
    conn.close()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/process")
async def process_note(
    request: Request,
    text: str = Form(default=""),
    token: str = Form(default=""),
    audio: UploadFile | None = File(default=None),
    conn=Depends(db),
):
    # A rep's token is their identity and their authorisation: asking a
    # salesperson to also remember a shared code is one more thing that gets
    # them to stop sending notes.
    person = by_token(conn, token) if token else None
    if person is None:
        require_code(request)
    transcript, engine, engine_version = text.strip(), "typed", "1"
    audio_uri, audio_sha, audio_len, mime = "typed://none", None, None, None

    if audio is not None and not transcript:
        raw = await audio.read()
        if not raw:
            raise HTTPException(400, "Empty recording")
        mime = (audio.content_type or "audio/webm").split(";")[0]
        # Persist BEFORE transcribing. The bytes are the bottom of the evidence
        # chain and the only thing that makes re-transcription possible later.
        audio_sha, audio_uri = store_audio(raw, mime)
        audio_len = len(raw)
        tax = tax_mod.load()
        vocab = [i["name"] for items in tax.seed_entities.values() for i in items]
        vocab += [r["name"] for r in conn.execute(
            "SELECT name FROM entity WHERE is_active = 1 LIMIT 200").fetchall()]
        try:
            result = get_asr().transcribe(raw, mime, vocab)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Transcription failed: {exc}") from exc
        transcript, engine, engine_version = result.text, result.engine, result.engine_version
        if not transcript:
            raise HTTPException(
                400, "Nothing audible in that recording. Try again, or type it.")

    if not transcript:
        raise HTTPException(400, "Nothing to process")

    person_id = person["id"] if person else seed(conn)

    external_id = f"web-{datetime.now(timezone.utc).isoformat()}-{secrets.token_hex(4)}"
    capture_id = ingest_text(
        conn, person_id=person_id, text=transcript, external_id=external_id,
        audio_uri=audio_uri, audio_sha256=audio_sha, audio_bytes=audio_len,
        mime_type=mime, engine=engine, engine_version=engine_version,
    )
    extract_capture(conn, capture_id)

    events = [
        {
            "path": r["path"], "label": r["label"], "dimension": r["dimension"],
            "confidence": r["confidence"], "subject": r["subject"], "rival": r["rival"],
            "span": transcript[r["span_start"]:r["span_end"]]
            if r["span_start"] is not None else None,
        }
        for r in conn.execute(
            """SELECT t.path, t.label, t.dimension, e.confidence, e.span_start, e.span_end,
                      se.name AS subject, re.name AS rival
               FROM event e JOIN taxonomy_node t ON t.id = e.taxonomy_node_id
               LEFT JOIN entity se ON se.id = e.subject_id
               LEFT JOIN entity re ON re.id = e.rival_id
               WHERE e.capture_id = ? AND e.is_active = 1 ORDER BY e.id""",
            (capture_id,),
        ).fetchall()
    ]
    return JSONResponse({
        "capture_id": capture_id, "transcript": transcript, "engine": engine,
        # Only cloud storage is durable. A local write on Cloud Run disappears
        # with the instance, and claiming it was saved would be a lie the first
        # time someone went looking for the recording.
        "audio_stored": audio_uri.startswith("gs://"),
        "rep": person["name"] if person else None,
        "events": events, "card": card_items(conn, capture_id),
    })


@app.post("/api/capture/{capture_id}/confirm")
def confirm(capture_id: int, request: Request, token: str = Form(default=""),
            conn=Depends(db)):
    if not (token and by_token(conn, token)):
        require_code(request)
    record_confirmation(conn, capture_id)
    return {"ok": True}


@app.post("/api/capture/{capture_id}/correct")
def correct(capture_id: int, request: Request, correction: str = Form(...),
            token: str = Form(default=""), conn=Depends(db)):
    """A correction is not an edit -- it is a labelled example.

    The prior extraction is superseded, not overwritten, so the wrong answer
    survives next to the right one and the eval set grows from ordinary use.
    """
    if not (token and by_token(conn, token)):
        require_code(request)
    if not correction.strip():
        raise HTTPException(400, "Tell me what to fix")
    before = card_items(conn, capture_id)
    apply_correction(conn, capture_id, correction.strip())
    after = card_items(conn, capture_id)

    # A rep who cannot see what their correction changed has no reason to
    # believe it landed, and stops bothering.
    before_set = {(i["kind"], i["text"]) for i in before}
    after_set = {(i["kind"], i["text"]) for i in after}
    return {
        "ok": True,
        "before": before,
        "after": after,
        "removed": [{"kind": k, "text": v} for k, v in before_set - after_set],
        "added": [{"kind": k, "text": v} for k, v in after_set - before_set],
    }


@app.get("/api/aggregate")
def aggregate(request: Request, days: int = 14, conn=Depends(db)):
    """Findings for a trailing window, against the window before it.

    Two things here are correctness, not presentation. Findings roll up to the
    parent category -- "Price objection", not five separate leaves that each
    look small. And the numerator counts distinct CAPTURES, not events: a note
    mentioning EMI twice is one interaction with a price objection, and
    counting events would let a single talkative note inflate a percentage.
    """
    require_code(request)
    today = date.today()
    cur_from = (today - timedelta(days=days - 1)).isoformat()
    prev_from = (today - timedelta(days=2 * days - 1)).isoformat()
    prev_to = (today - timedelta(days=days)).isoformat()

    def denominator(frm, to):
        return conn.execute(
            "SELECT COUNT(*) AS c FROM raw_capture WHERE status = 'extracted' "
            "AND captured_on BETWEEN ? AND ?", (frm, to),
        ).fetchone()["c"]

    total = denominator(cur_from, today.isoformat())
    prev_total = denominator(prev_from, prev_to)

    # Roll a leaf up to its category: depth-3 nodes report under their parent,
    # depth-2 nodes are already the category.
    GROUPED = """
        SELECT g.id AS gid, g.label AS label, g.path AS path,
               COUNT(DISTINCT e.capture_id) AS n,
               COUNT(DISTINCT e.person_id) AS people,
               AVG(e.confidence) AS conf
        FROM event e
        JOIN taxonomy_node t ON t.id = e.taxonomy_node_id
        JOIN taxonomy_node g ON g.id = (CASE WHEN t.parent_id IS NOT NULL THEN t.parent_id ELSE t.id END)
        WHERE e.is_active = 1 AND t.dimension IN ('objection', 'competitive')
          AND e.occurred_on BETWEEN ? AND ?
        GROUP BY g.id, g.label, g.path
    """
    rows = conn.execute(GROUPED + " ORDER BY n DESC LIMIT 8",
                        (cur_from, today.isoformat())).fetchall()
    prior = {r["gid"]: r["n"] for r in
             conn.execute(GROUPED, (prev_from, prev_to)).fetchall()}

    # Coverage is what makes a confidence grade mean anything. Without an
    # independent expected-capture count, High is unreachable by design.
    cov_row = conn.execute(
        "SELECT SUM(expected_captures) AS exp FROM shift WHERE occurred_on BETWEEN ? AND ?",
        (cur_from, today.isoformat()),
    ).fetchone()
    expected = cov_row["exp"] or 0
    coverage = round(100 * total / expected) if expected else 0

    def grade(n, people, conf):
        if n >= 100 and coverage >= 70 and people >= 10 and (conf or 0) >= 75:
            return "High"
        if n >= 30 and coverage >= 50 and people >= 5:
            return "Medium"
        return "Low" if n >= 10 else "Very low"

    findings = []
    for r in rows:
        pct = round(100 * r["n"] / total) if total else 0
        prev_pct = round(100 * prior.get(r["gid"], 0) / prev_total) if prev_total else None
        findings.append({
            "label": r["label"], "path": r["path"], "n": r["n"], "people": r["people"],
            "pct": pct,
            "delta_pp": (pct - prev_pct) if prev_pct is not None else None,
            "confidence": grade(r["n"], r["people"], r["conf"]),
        })

    top_units = {}
    for f in findings[:3]:
        top = conn.execute(
            """SELECT o.name AS name, COUNT(DISTINCT e.capture_id) AS n
               FROM event e
               JOIN taxonomy_node t ON t.id = e.taxonomy_node_id
               JOIN taxonomy_node g ON g.id = (CASE WHEN t.parent_id IS NOT NULL THEN t.parent_id ELSE t.id END)
               JOIN org_unit o ON o.id = e.unit_l2
               WHERE e.is_active = 1 AND g.path = ? AND e.occurred_on BETWEEN ? AND ?
               GROUP BY o.name ORDER BY n DESC LIMIT 2""",
            (f["path"], cur_from, today.isoformat()),
        ).fetchall()
        top_units[f["path"]] = [r["name"] for r in top]

    simulated = conn.execute(
        "SELECT COUNT(*) AS c FROM raw_capture WHERE is_simulated = 1"
    ).fetchone()["c"]

    return {
        "denominator": total, "prev_denominator": prev_total,
        "window_days": days, "coverage_pct": coverage,
        "simulated_count": simulated, "real_count": max(total - simulated, 0),
        "findings": findings, "top_units": top_units,
    }


@app.get("/api/evidence/{path:path}")
def evidence(path: str, request: Request, conn=Depends(db)):
    require_code(request)
    rows = conn.execute(
        """SELECT e.id, e.confidence, e.occurred_on, p.name AS rep, o.name AS store,
                  e.span_start, e.span_end, tr.text, t.label AS leaf
           FROM event e
           JOIN raw_capture rc ON rc.id = e.capture_id
           JOIN transcript tr ON tr.capture_id = rc.id
           JOIN person p ON p.id = e.person_id
           JOIN org_unit o ON o.id = e.unit_id
           JOIN taxonomy_node t ON t.id = e.taxonomy_node_id
           JOIN taxonomy_node g ON g.id = (CASE WHEN t.parent_id IS NOT NULL
                                           THEN t.parent_id ELSE t.id END)
           -- Findings roll up to a category, so evidence has to match either the
           -- category or a leaf directly: clicking "Price" must reach every EMI,
           -- down payment and exchange-value note beneath it.
           WHERE (g.path = ? OR t.path = ?) AND e.is_active = 1
           ORDER BY e.id DESC LIMIT 25""",
        (path, path),
    ).fetchall()
    return {
        "path": path,
        "evidence": [
            {
                "event_id": r["id"], "date": r["occurred_on"], "rep": r["rep"],
                "store": r["store"], "confidence": r["confidence"], "leaf": r["leaf"],
                "said": r["text"][r["span_start"]:r["span_end"]].strip()
                if r["span_start"] is not None else r["text"][:160],
            }
            for r in rows
        ],
    }


@app.get("/api/discoveries")
def discoveries(request: Request, conn=Depends(db)):
    """Names the reps used that resolve to nothing we know.

    Sorted by frequency this is the weekly review queue -- and a brand entering
    the market shows up here before it shows up anywhere else."""
    require_code(request)
    rows = conn.execute(
        "SELECT slot, surface_form, occurrences FROM unresolved_mention "
        "WHERE reviewed = 0 ORDER BY occurrences DESC LIMIT 10"
    ).fetchall()
    return {"discoveries": [
        {"slot": r["slot"], "name": r["surface_form"], "seen": r["occurrences"]}
        for r in rows
    ]}


@app.get("/api/proposals")
def proposals(request: Request, conn=Depends(db)):
    """What the enricher worked out, awaiting a human decision.

    These are proposals, never facts: a grounded lookup can be confidently
    wrong, and a hallucinated brand becoming a real entity would corrupt every
    competitive number. Nothing here touches the fact table until approved.
    """
    require_code(request)
    rows = conn.execute(
        "SELECT id, surface_form, proposed_name, entity_type, description, confidence, "
        "is_relevant, occurrences, sources_json FROM entity_proposal "
        "WHERE status = 'pending' ORDER BY is_relevant DESC, occurrences DESC LIMIT 12"
    ).fetchall()
    return {"proposals": [
        {
            "id": r["id"], "surface": r["surface_form"], "name": r["proposed_name"],
            "entity_type": r["entity_type"], "description": r["description"],
            "confidence": r["confidence"], "relevant": bool(r["is_relevant"]),
            "seen": r["occurrences"],
            "sources": json.loads(r["sources_json"] or "[]"),
        } for r in rows
    ]}


@app.post("/api/proposals/{proposal_id}/{decision}")
def decide(proposal_id: int, decision: str, request: Request, conn=Depends(db)):
    require_code(request)
    if decision == "approve":
        try:
            return approve_proposal(conn, proposal_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
    if decision == "reject":
        reject_proposal(conn, proposal_id)
        return {"ok": True}
    raise HTTPException(400, "decision must be approve or reject")


@app.get("/api/team")
def team(request: Request, conn=Depends(db)):
    require_code(request)
    return {"people": list_people(conn), "stores": stores(conn)}


@app.post("/api/team")
def team_add(request: Request, name: str = Form(...), unit_id: int = Form(...),
             phone: str = Form(default=""), conn=Depends(db)):
    require_code(request)
    try:
        return add_person(conn, name=name, unit_id=unit_id, phone=phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/team/{person_id}/remove")
def team_remove(person_id: int, request: Request, conn=Depends(db)):
    require_code(request)
    deactivate(conn, person_id)
    return {"ok": True}


@app.get("/r/{token}", response_class=HTMLResponse)
def capture_page(token: str, conn=Depends(db)):
    """A rep's own capture link. No access code: the token is the identity, and
    a code on top would be one more thing to forget on a showroom floor."""
    person = by_token(conn, token)
    if person is None:
        return HTMLResponse("<p style='font:16px sans-serif;padding:40px'>"
                            "This link is not active. Ask for a new one.</p>", status_code=404)
    return INDEX_HTML


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Frontline</title>
<style>
:root{--bg:#faf9f7;--fg:#1a1a18;--mut:#6b6b66;--line:#e0ded8;--card:#fff;--ac:#0f6e56;--acbg:#e1f5ee;--warn:#854f0b;--warnbg:#faeeda}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:28px 18px 80px}
h1{font-size:22px;font-weight:500;margin:0 0 4px}
h2{font-size:16px;font-weight:500;margin:0 0 12px}
.sub{color:var(--mut);font-size:14px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px}
textarea{width:100%;min-height:92px;padding:12px;border:1px solid var(--line);border-radius:8px;font:inherit;font-size:15px;resize:vertical;background:var(--bg)}
input{padding:9px 12px;border:1px solid var(--line);border-radius:8px;font:inherit;background:var(--bg)}
select{padding:9px 12px;border:1px solid var(--line);border-radius:8px;font:inherit;background:var(--bg)}
button{font:inherit;font-size:15px;padding:10px 18px;border-radius:8px;border:1px solid var(--line);background:var(--card);cursor:pointer}
button.primary{background:var(--ac);color:#fff;border-color:var(--ac)}
button.chip{font-size:13px;padding:6px 12px;border-radius:99px;color:var(--mut)}
button.rec{background:#a32d2d;color:#fff;border-color:#a32d2d}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
.tabs{display:flex;gap:4px;margin-bottom:18px;border-bottom:1px solid var(--line)}
.tabs button{border:0;background:none;border-bottom:2px solid transparent;border-radius:0;color:var(--mut);padding:10px 14px}
.tabs button.on{color:var(--fg);border-bottom-color:var(--ac)}
.ev{border-left:3px solid var(--ac);padding:10px 14px;margin:10px 0;background:var(--bg);border-radius:0 6px 6px 0}
.meta{color:var(--mut);font-size:13px}
.quote{font-style:italic;color:var(--mut);font-size:14px;margin-top:4px}
pre{white-space:pre-wrap;font:inherit;background:var(--bg);padding:14px;border-radius:8px;margin:0}
.pill{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;background:var(--acbg);color:var(--ac);margin-left:6px}
.pill.w{background:var(--warnbg);color:var(--warn)}
.err{color:#a32d2d;font-size:14px;margin-top:10px}
.ok{color:var(--ac);font-size:14px;margin-top:10px}
.hide{display:none}
label{font-size:13px;color:var(--mut);display:block;margin-bottom:6px}
.find{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:10px 0;border-bottom:1px solid var(--line);cursor:pointer}
.find:last-child{border-bottom:0}
.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.prow{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
.prow:last-child{border-bottom:0}
code{background:var(--bg);padding:2px 6px;border-radius:4px;font-size:13px}
.kv{display:flex;gap:10px;padding:7px 0;border-bottom:1px solid var(--line)}
.kv:last-child{border-bottom:0}
.kv .k{color:var(--mut);font-size:13px;min-width:130px}
.kv .v{font-weight:500}
.gone .v{text-decoration:line-through;color:var(--mut);font-weight:400}
.new .v{color:var(--ac)}
.banner{display:flex;gap:8px;align-items:center;padding:10px 14px;border-radius:8px;
  background:var(--acbg);color:var(--ac);font-size:14px}
</style></head><body><div class="wrap">
<h1 id="title">Frontline</h1>
<div class="sub" id="subtitle">A salesperson's voice note becomes a structured, evidence-backed signal.</div>

<div class="card" id="gate">
  <label for="code">Access code</label>
  <div class="row"><input id="code" type="password" autocomplete="off">
  <button class="primary" onclick="unlock()">Enter</button></div>
  <div class="err hide" id="gateErr">Wrong code.</div>
</div>

<div id="main" class="hide">
  <div class="tabs" id="tabs">
    <button class="on" onclick="tab('capture')">Capture</button>
    <button onclick="tab('signals')">Signals</button>
    <button onclick="tab('team')">Team</button>
  </div>

  <div id="pane-capture">
    <div class="card">
      <h2 id="captureHead">Send a note</h2>
      <textarea id="text" placeholder="Type what happened, in Hindi, English, or both."></textarea>
      <div class="row">
        <button class="primary" onclick="send()">Process</button>
        <button id="rec" onclick="toggleRec()">Record</button>
        <span class="meta" id="status"></span>
      </div>
      <div class="row" id="samples"></div>
      <div class="err hide" id="err"></div>
    </div>
    <div id="out"></div>
  </div>

  <div id="pane-signals" class="hide"><div id="agg"></div></div>

  <div id="pane-team" class="hide">
    <div class="card">
      <h2>Add a salesperson</h2>
      <div class="meta" style="margin-bottom:10px">Each person gets their own link. Opening it identifies them, so notes attach to the right rep and store.</div>
      <div class="row">
        <input id="pname" placeholder="Full name" style="min-width:180px">
        <select id="pstore"></select>
        <input id="pphone" placeholder="Phone (optional)" style="width:150px">
        <button class="primary" onclick="addPerson()">Add</button>
      </div>
      <div class="err hide" id="teamErr"></div>
    </div>
    <div class="card"><h2>Team</h2><div id="teamList"></div></div>
  </div>
</div>

<script>
let code="",rec=null,chunks=[],repToken="",lastCapture=null,curTab="capture";
const SAMPLES=[
 ["Price + rival","Sir ko Model X pasand aayi thi but EMI 4,500 bola toh bole 4,000 tak hi. Exchange mein purani Activa ka 22,000 de rahe hain. Ather bhi dekh ke aaye hain."],
 ["Range doubt","Customer Model X ke liye aaya tha, range ko lekar doubt tha, 150 km claim karte ho par actual kitna chalegi. Ola wale ne 180 bola tha."],
 ["Unknown brand","Sir BGauss aur Kinetic Green dekh ke aaye hain, unka price kam laga."],
 ["Nothing to extract","Haan toh main nikal raha hoon ab, kal milte hain."]
];
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function hdr(){return code?{'x-access-code':code}:{}}
function tab(n){
  curTab=n;
  ['capture','signals','team'].forEach(x=>
    document.getElementById('pane-'+x).classList.toggle('hide',x!==n));
  [...document.querySelectorAll('.tabs button')].forEach((b,i)=>
    b.classList.toggle('on',['capture','signals','team'][i]===n));
  if(n==='signals')loadAgg();
  if(n==='team')loadTeam();
}
function unlock(){
  code=document.getElementById('code').value;
  fetch('/api/aggregate',{headers:hdr()}).then(r=>{
    if(!r.ok){document.getElementById('gateErr').classList.remove('hide');return;}
    document.getElementById('gate').classList.add('hide');
    document.getElementById('main').classList.remove('hide');
    document.getElementById('samples').innerHTML=SAMPLES.map((s,i)=>
      '<button class="chip" onclick="useSample('+i+')">'+esc(s[0])+'</button>').join('');
  });
}
function useSample(i){document.getElementById('text').value=SAMPLES[i][1];}
async function post(fd){
  document.getElementById('err').classList.add('hide');
  document.getElementById('status').textContent='working...';
  if(repToken)fd.append('token',repToken);
  const r=await fetch('/api/process',{method:'POST',headers:hdr(),body:fd});
  document.getElementById('status').textContent='';
  if(!r.ok){const e=document.getElementById('err');
    e.textContent=((await r.json()).detail)||'Failed';e.classList.remove('hide');return;}
  render(await r.json());
}
function send(){
  const t=document.getElementById('text').value.trim();
  if(!t){const e=document.getElementById('err');e.textContent='Type something first.';e.classList.remove('hide');return;}
  const fd=new FormData();fd.append('text',t);post(fd);
}
async function toggleRec(){
  const b=document.getElementById('rec');
  if(rec&&rec.state==='recording'){rec.stop();b.textContent='Record';b.classList.remove('rec');return;}
  try{
    const s=await navigator.mediaDevices.getUserMedia({audio:true});
    rec=new MediaRecorder(s);chunks=[];
    rec.ondataavailable=e=>chunks.push(e.data);
    rec.onstop=()=>{const fd=new FormData();
      fd.append('audio',new Blob(chunks,{type:'audio/webm'}),'note.webm');
      fd.append('text','');post(fd);s.getTracks().forEach(t=>t.stop());};
    rec.start();b.textContent='Stop';b.classList.add('rec');
  }catch(e){const el=document.getElementById('err');
    el.textContent='Microphone unavailable \\u2014 type instead.';el.classList.remove('hide');}
}
function render(d){
  lastCapture=d.capture_id;
  let h='<div class="card"><h2>Transcript <span class="pill">'+esc(d.engine)+'</span>';
  if(d.audio_stored)h+='<span class="pill">audio saved</span>';
  h+='</h2><pre>'+esc(d.transcript)+'</pre></div>';
  h+='<div class="card"><h2>Extracted</h2>';
  if(!d.events.length)h+='<div class="meta">Nothing extractable. Flagged unclear rather than guessed \\u2014 the system will not invent an objection that was never said.</div>';
  d.events.forEach(e=>{
    h+='<div class="ev"><b>'+esc(e.label)+'</b><span class="pill">conf '+e.confidence+'</span>';
    h+='<div class="meta">'+esc(e.path);
    if(e.subject)h+=' \\u00b7 '+esc(e.subject);
    if(e.rival)h+=' \\u00b7 vs '+esc(e.rival);
    h+='</div>';
    if(e.span)h+='<div class="quote">"'+esc(e.span)+'"</div>';
    h+='</div>';
  });
  h+='</div><div class="card" id="cardbox"><h2>Is this right?</h2>';
  h+='<div id="cardrows">'+rows(d.card)+'</div>';
  h+='<div class="row" id="cardbtns"><button class="primary" onclick="confirmIt()">Yes, that\'s right</button>';
  h+='<button onclick="showFix()">Fix it</button></div>';
  h+='<div id="fixbox" class="hide" style="margin-top:12px">';
  h+='<label>What did I get wrong?</label>';
  h+='<textarea id="fixtext" placeholder="e.g. nahi, EMI nahi tha, exchange value ka issue tha"></textarea>';
  h+='<div class="row"><button class="primary" id="fixbtn" onclick="sendFix()">Send correction</button>';
  h+='<button onclick="hideFix()">Cancel</button></div></div>';
  h+='<div id="result"></div></div>';
  document.getElementById('out').innerHTML=h;
  window.scrollTo({top:document.getElementById('out').offsetTop-20,behavior:'smooth'});
}
function rows(items,cls){
  if(!items||!items.length)return '<div class="meta">Nothing recorded from this note.</div>';
  return items.map(i=>'<div class="kv '+(cls||'')+'"><div class="k">'+esc(i.kind)+
    '</div><div class="v">'+esc(i.text)+'</div></div>').join('');
}
function showFix(){
  document.getElementById('fixbox').classList.remove('hide');
  document.getElementById('fixtext').focus();
}
function hideFix(){document.getElementById('fixbox').classList.add('hide');}
async function confirmIt(){
  const fd=new FormData(); if(repToken)fd.append('token',repToken);
  await fetch('/api/capture/'+lastCapture+'/confirm',{method:'POST',headers:hdr(),body:fd});
  document.getElementById('cardbtns').classList.add('hide');
  document.getElementById('fixbox').classList.add('hide');
  document.getElementById('result').innerHTML=
    '<div class="banner" style="margin-top:12px">Confirmed. Thanks \\u2014 that helps the numbers.</div>';
}
async function sendFix(){
  const v=document.getElementById('fixtext').value.trim();
  if(!v)return;
  const btn=document.getElementById('fixbtn');
  btn.disabled=true;btn.textContent='Re-reading\\u2026';
  const fd=new FormData();fd.append('correction',v); if(repToken)fd.append('token',repToken);
  const r=await fetch('/api/capture/'+lastCapture+'/correct',{method:'POST',headers:hdr(),body:fd});
  btn.disabled=false;btn.textContent='Send correction';
  if(!r.ok){document.getElementById('result').innerHTML=
    '<div class="err">Could not apply that correction.</div>';return;}
  const d=await r.json();
  document.getElementById('cardrows').innerHTML=rows(d.after);
  document.getElementById('cardbtns').classList.add('hide');
  document.getElementById('fixbox').classList.add('hide');
  let s='<div class="banner" style="margin-top:12px">Updated \\u2014 thanks.</div>';
  if(d.removed.length||d.added.length){
    s+='<div style="margin-top:12px"><div class="meta" style="margin-bottom:4px">What changed</div>';
    s+=rows(d.removed,'gone')+rows(d.added,'new')+'</div>';
  }
  document.getElementById('result').innerHTML=s;
}
async function loadAgg(){
  const h=hdr();
  const [a,dsc,prop]=await Promise.all([
    fetch('/api/aggregate',{headers:h}).then(r=>r.json()),
    fetch('/api/discoveries',{headers:h}).then(r=>r.json()),
    fetch('/api/proposals',{headers:h}).then(r=>r.json())]);
  let s='';
  if(a.simulated_count){
    s+='<div class="card" style="border-color:#EF9F27;background:#FAEEDA"><b>Simulated dataset</b>';
    s+='<div class="meta" style="color:#854F0B">'+a.simulated_count+' generated notes across 20 reps, 5 stores, 3 states, 30 days. ';
    s+='Patterns here are invented to show the shape of the output \\u2014 not market insight.</div></div>';
  }
  if(a.findings.length){
    s+='<div class="card"><h2>Last '+a.window_days+' days</h2>';
    s+='<div class="meta" style="margin-bottom:12px">Based on <b>'+a.denominator+'</b> reported interactions';
    if(a.coverage_pct)s+=' at <b>'+a.coverage_pct+'% coverage</b>';
    s+='. Click any line for the evidence behind it.</div>';
    a.findings.forEach(f=>{
      const low=(f.confidence==='Low'||f.confidence==='Very low');
      s+='<div class="find" onclick="eviden(\\''+f.path+'\\')"><div><b>'+esc(f.label)+'</b>';
      s+='<span class="pill'+(low?' w':'')+'">'+f.confidence+'</span>';
      if(f.delta_pp!==null&&f.delta_pp!==0){const up=f.delta_pp>0;
        s+='<span class="pill'+(up?' w':'')+'">'+(up?'\\u2191':'\\u2193')+Math.abs(f.delta_pp)+'pp</span>';}
      const tu=a.top_units[f.path];
      if(tu&&tu.length)s+='<div class="meta">Highest in '+tu.map(esc).join(' + ')+'</div>';
      s+='</div><div class="num meta">'+f.pct+'% of '+a.denominator+' \\u00b7 n='+f.n+' \\u00b7 '+f.people+' '+(f.people===1?'person':'people')+'</div></div>';
    });
    s+='<div class="meta" style="margin-top:14px">Confidence is computed, not chosen. High requires 100+ interactions, 10+ people, and 70%+ coverage \\u2014 a thin or biased sample is labelled honestly rather than flattered.</div>';
    s+='</div><div id="evbox"></div>';
  }
  if(prop.proposals.length){
    const real=prop.proposals.filter(x=>x.relevant),junk=prop.proposals.filter(x=>!x.relevant);
    s+='<div class="card"><h2>Names the system worked out</h2>';
    s+='<div class="meta" style="margin-bottom:12px">Reps said these; nothing in the taxonomy matched. Gemini searched the web to identify them. Nothing is added until you approve it.</div>';
    real.forEach(x=>{
      s+='<div class="ev"><b>'+esc(x.name||x.surface)+'</b><span class="pill">'+esc(x.entity_type||'?')+'</span>';
      s+='<span class="pill">conf '+x.confidence+'</span>';
      s+='<div class="meta">heard as \\u201c'+esc(x.surface)+'\\u201d, '+x.seen+'\\u00d7</div>';
      s+='<div class="quote" style="font-style:normal">'+esc(x.description||'')+'</div>';
      if(x.sources.length){s+='<div class="meta">Checked against: '+x.sources.map(src=>'<a href="'+esc(src.uri)+'">'+esc((src.title||'source').slice(0,30))+'</a>').join(' \\u00b7 ')+'</div>';}
      else s+='<div class="meta">From model knowledge \\u2014 no web sources cited. Worth a check.</div>';
      s+='<div class="row"><button class="primary" onclick="decide('+x.id+',\\'approve\\')">Add as '+esc(x.entity_type||'entity')+'</button>';
      s+='<button onclick="decide('+x.id+',\\'reject\\')">Not relevant</button></div></div>';
    });
    if(junk.length)s+='<div class="meta" style="margin-top:14px">Dismissed as not a brand: '+junk.map(x=>'\\u201c'+esc(x.surface)+'\\u201d').join(', ')+'</div>';
    s+='</div>';
  }else if(dsc.discoveries.length){
    s+='<div class="card"><h2>Names we did not recognise</h2>';
    s+='<div class="meta">Nothing in the taxonomy matches these. Run enrichment to identify them.</div>';
    dsc.discoveries.forEach(d=>{s+='<div class="prow"><b>'+esc(d.name)+'</b><span class="meta">seen '+d.seen+'\\u00d7</span></div>';});
    s+='</div>';
  }
  document.getElementById('agg').innerHTML=s;
}
async function decide(id,what){
  await fetch('/api/proposals/'+id+'/'+what,{method:'POST',headers:hdr()});
  loadAgg();
}
async function eviden(path){
  const r=await fetch('/api/evidence/'+encodeURIComponent(path),{headers:hdr()});
  const d=await r.json();
  let s='<div class="card"><h2>Evidence behind that number</h2>';
  d.evidence.forEach(e=>{
    s+='<div class="ev"><div class="meta">'+esc(e.date)+' \\u00b7 '+esc(e.rep)+' \\u00b7 '+esc(e.store);
    if(e.leaf)s+=' \\u00b7 '+esc(e.leaf);
    s+=' \\u00b7 conf '+e.confidence+'</div><div class="quote">"'+esc(e.said)+'"</div></div>';
  });
  if(!d.evidence.length)s+='<div class="meta">No active events.</div>';
  document.getElementById('evbox').innerHTML=s+'</div>';
  document.getElementById('evbox').scrollIntoView({behavior:'smooth',block:'nearest'});
}
async function loadTeam(){
  const d=await fetch('/api/team',{headers:hdr()}).then(r=>r.json());
  document.getElementById('pstore').innerHTML=d.stores.map(s=>
    '<option value="'+s.id+'">'+esc(s.name)+'</option>').join('');
  const base=location.origin+'/r/';
  let h='';
  if(!d.people.length)h='<div class="meta">Nobody added yet. Add a salesperson above and send them their link.</div>';
  d.people.forEach(p=>{
    h+='<div class="prow"><div><b>'+esc(p.name)+'</b>';
    if(!p.active)h+='<span class="pill w">removed</span>';
    h+='<div class="meta">'+esc(p.store||'no store')+' \\u00b7 '+p.notes+' notes sent</div></div>';
    h+='<div class="row" style="margin:0"><button class="chip" onclick="copyLink(\\''+p.token+'\\')">Copy link</button>';
    if(p.active)h+='<button class="chip" onclick="removePerson('+p.id+')">Remove</button>';
    h+='</div></div>';
  });
  document.getElementById('teamList').innerHTML=h;
}
function copyLink(tok){
  const url=location.origin+'/r/'+tok;
  navigator.clipboard.writeText(url).then(()=>alert('Link copied:\\n'+url),()=>prompt('Copy this link:',url));
}
async function addPerson(){
  const name=document.getElementById('pname').value.trim();
  const e=document.getElementById('teamErr');e.classList.add('hide');
  if(!name){e.textContent='Name is required.';e.classList.remove('hide');return;}
  const fd=new FormData();
  fd.append('name',name);
  fd.append('unit_id',document.getElementById('pstore').value);
  fd.append('phone',document.getElementById('pphone').value.trim());
  const r=await fetch('/api/team',{method:'POST',headers:hdr(),body:fd});
  if(!r.ok){e.textContent=(await r.json()).detail||'Could not add.';e.classList.remove('hide');return;}
  document.getElementById('pname').value='';document.getElementById('pphone').value='';
  loadTeam();
}
async function removePerson(id){
  await fetch('/api/team/'+id+'/remove',{method:'POST',headers:hdr()});
  loadTeam();
}
(function init(){
  const m=location.pathname.match(/^\\/r\\/(.+)$/);
  if(m){
    repToken=m[1];
    document.getElementById('gate').classList.add('hide');
    document.getElementById('tabs').classList.add('hide');
    document.getElementById('main').classList.remove('hide');
    document.getElementById('subtitle').textContent='Send a quick note about the conversation you just had.';
    document.getElementById('samples').innerHTML='';
  }else{
    document.getElementById('code').addEventListener('keydown',e=>{if(e.key==='Enter')unlock()});
  }
})();
</script></div></body></html>"""
