"""The demo web app.

One page: speak or type a note, watch it become structured events with a
confidence, a confirmation card, and a link back to the exact words.

Gated by a shared access code. It runs LLM calls on a real billing account, so
it must never sit open on a public URL.
"""
from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse

from . import taxonomy as tax_mod
from .asr import get_asr
from .confirm import render_card
from .db import connect, init_schema
from .demo import seed
from .pipeline import extract_capture, ingest_text

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
    tax_mod.sync_to_db(conn, tax_mod.load())
    seed(conn)
    conn.close()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/process")
async def process_note(
    request: Request,
    text: str = Form(default=""),
    audio: UploadFile | None = File(default=None),
    conn=Depends(db),
):
    require_code(request)
    transcript, engine = text.strip(), "typed"

    if audio is not None and not transcript:
        raw = await audio.read()
        if not raw:
            raise HTTPException(400, "Empty recording")
        tax = tax_mod.load()
        vocab = [i["name"] for items in tax.seed_entities.values() for i in items]
        result = get_asr().transcribe(raw, audio.content_type or "audio/webm", vocab)
        transcript, engine = result.text, result.engine

    if not transcript:
        raise HTTPException(400, "Nothing to process")

    person_id = seed(conn)
    external_id = f"web-{datetime.now(timezone.utc).isoformat()}-{secrets.token_hex(4)}"
    capture_id = ingest_text(
        conn, person_id=person_id, text=transcript,
        external_id=external_id, engine=engine,
    )
    extract_capture(conn, capture_id)

    events = [
        {
            "path": r["path"],
            "label": r["label"],
            "dimension": r["dimension"],
            "confidence": r["confidence"],
            "subject": r["subject"],
            "rival": r["rival"],
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
        "transcript": transcript,
        "engine": engine,
        "events": events,
        "card": render_card(conn, capture_id),
    })


@app.get("/api/aggregate")
def aggregate(request: Request, conn=Depends(db)):
    require_code(request)
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM raw_capture WHERE status = 'extracted'"
    ).fetchone()["c"]
    rows = conn.execute(
        """SELECT t.id, t.label, t.path, COUNT(*) AS n,
                  COUNT(DISTINCT e.person_id) AS people
           FROM event e JOIN taxonomy_node t ON t.id = e.taxonomy_node_id
           WHERE e.is_active = 1 AND t.dimension = 'objection'
           GROUP BY t.id, t.label, t.path ORDER BY n DESC LIMIT 8""",
    ).fetchall()
    return {
        "denominator": total,
        "findings": [
            {
                "label": r["label"], "path": r["path"], "n": r["n"],
                "people": r["people"],
                "pct": round(100 * r["n"] / total) if total else 0,
                # Confidence is computed, never chosen. Demo data is tiny, so
                # almost everything here is correctly Low -- that is the point.
                "confidence": "High" if r["n"] >= 100 and r["people"] >= 10
                else "Medium" if r["n"] >= 30 and r["people"] >= 5
                else "Low",
            }
            for r in rows
        ],
    }


@app.get("/api/evidence/{path:path}")
def evidence(path: str, request: Request, conn=Depends(db)):
    require_code(request)
    rows = conn.execute(
        """SELECT e.id, e.confidence, e.occurred_on, p.name AS rep, o.name AS store,
                  e.span_start, e.span_end, tr.text
           FROM event e
           JOIN raw_capture rc ON rc.id = e.capture_id
           JOIN transcript tr ON tr.capture_id = rc.id
           JOIN person p ON p.id = e.person_id
           JOIN org_unit o ON o.id = e.unit_id
           JOIN taxonomy_node t ON t.id = e.taxonomy_node_id
           WHERE t.path = ? AND e.is_active = 1 ORDER BY e.id DESC LIMIT 25""",
        (path,),
    ).fetchall()
    return {
        "path": path,
        "evidence": [
            {
                "event_id": r["id"], "date": r["occurred_on"], "rep": r["rep"],
                "store": r["store"], "confidence": r["confidence"],
                "said": r["text"][r["span_start"]:r["span_end"]].strip()
                if r["span_start"] is not None else r["text"][:160],
            }
            for r in rows
        ],
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Frontline</title>
<style>
:root{--bg:#faf9f7;--fg:#1a1a18;--mut:#6b6b66;--line:#e0ded8;--card:#fff;--ac:#0f6e56}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:22px;font-weight:500;margin:0 0 4px}
.sub{color:var(--mut);font-size:14px;margin-bottom:28px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px}
textarea{width:100%;min-height:96px;padding:12px;border:1px solid var(--line);border-radius:8px;font:inherit;font-size:15px;resize:vertical;background:var(--bg)}
button{font:inherit;font-size:15px;padding:10px 18px;border-radius:8px;border:1px solid var(--line);background:var(--card);cursor:pointer}
button.primary{background:var(--ac);color:#fff;border-color:var(--ac)}
button:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
.ev{border-left:3px solid var(--ac);padding:10px 14px;margin:10px 0;background:var(--bg);border-radius:0 6px 6px 0}
.ev b{font-weight:500}
.meta{color:var(--mut);font-size:13px}
.quote{font-style:italic;color:var(--mut);font-size:14px;margin-top:4px}
pre{white-space:pre-wrap;font:inherit;background:var(--bg);padding:14px;border-radius:8px;margin:0}
.pill{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;background:#e1f5ee;color:#0f6e56;margin-left:6px}
.err{color:#a32d2d;font-size:14px;margin-top:10px}
.hide{display:none}
label{font-size:13px;color:var(--mut);display:block;margin-bottom:6px}
input[type=password]{padding:9px 12px;border:1px solid var(--line);border-radius:8px;font:inherit;width:200px}
h2{font-size:16px;font-weight:500;margin:0 0 12px}
</style></head><body><div class="wrap">
<h1>Frontline</h1>
<div class="sub">A salesperson's voice note becomes a structured, evidence-backed signal.</div>

<div class="card" id="gate">
  <label for="code">Access code</label>
  <div class="row"><input id="code" type="password" autocomplete="off">
  <button class="primary" onclick="unlock()">Enter</button></div>
  <div class="err hide" id="gateErr">Wrong code.</div>
</div>

<div id="main" class="hide">
  <div class="card">
    <h2>Send a note</h2>
    <textarea id="text" placeholder="Type what happened, in Hindi, English, or both.

e.g. Sir ko Model X pasand aayi thi but EMI thoda zyada lag raha tha. Ather bhi dekh ke aaye hain."></textarea>
    <div class="row">
      <button class="primary" onclick="send()">Process</button>
      <button id="rec" onclick="toggleRec()">Record</button>
      <span class="meta" id="status"></span>
    </div>
    <div class="err hide" id="err"></div>
  </div>
  <div id="out"></div>
</div>

<script>
let code="",rec=null,chunks=[];
function unlock(){
  code=document.getElementById('code').value;
  fetch('/api/aggregate',{headers:{'x-access-code':code}}).then(r=>{
    if(!r.ok){document.getElementById('gateErr').classList.remove('hide');return;}
    document.getElementById('gate').classList.add('hide');
    document.getElementById('main').classList.remove('hide');
  });
}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function post(fd){
  document.getElementById('err').classList.add('hide');
  document.getElementById('status').textContent='working...';
  const r=await fetch('/api/process',{method:'POST',headers:{'x-access-code':code},body:fd});
  document.getElementById('status').textContent='';
  if(!r.ok){const e=document.getElementById('err');e.textContent=(await r.json()).detail||'Failed';e.classList.remove('hide');return;}
  render(await r.json());
}
function send(){
  const t=document.getElementById('text').value.trim();
  if(!t){document.getElementById('err').textContent='Type something first.';document.getElementById('err').classList.remove('hide');return;}
  const fd=new FormData();fd.append('text',t);post(fd);
}
async function toggleRec(){
  const b=document.getElementById('rec');
  if(rec&&rec.state==='recording'){rec.stop();b.textContent='Record';return;}
  try{
    const s=await navigator.mediaDevices.getUserMedia({audio:true});
    rec=new MediaRecorder(s);chunks=[];
    rec.ondataavailable=e=>chunks.push(e.data);
    rec.onstop=()=>{const fd=new FormData();
      fd.append('audio',new Blob(chunks,{type:'audio/webm'}),'note.webm');
      fd.append('text','');post(fd);s.getTracks().forEach(t=>t.stop());};
    rec.start();b.textContent='Stop';
  }catch(e){document.getElementById('err').textContent='Microphone unavailable — type instead.';
    document.getElementById('err').classList.remove('hide');}
}
function render(d){
  let h='<div class="card"><h2>Transcript <span class="pill">'+esc(d.engine)+'</span></h2><pre>'+esc(d.transcript)+'</pre></div>';
  h+='<div class="card"><h2>Extracted</h2>';
  if(!d.events.length){h+='<div class="meta">Nothing extractable. Flagged unclear rather than guessed — the system does not invent an objection that was never said.</div>';}
  d.events.forEach(e=>{
    h+='<div class="ev"><b>'+esc(e.label)+'</b><span class="pill">conf '+e.confidence+'</span>';
    h+='<div class="meta">'+esc(e.path);
    if(e.subject)h+=' · '+esc(e.subject);
    if(e.rival)h+=' · vs '+esc(e.rival);
    h+='</div>';
    if(e.span)h+='<div class="quote">"'+esc(e.span)+'"</div>';
    h+='</div>';
  });
  h+='</div><div class="card"><h2>Card sent back to the rep</h2><pre>'+esc(d.card)+'</pre></div>';
  document.getElementById('out').innerHTML=h;
  window.scrollTo({top:document.getElementById('out').offsetTop-20,behavior:'smooth'});
}
document.getElementById('code').addEventListener('keydown',e=>{if(e.key==='Enter')unlock()});
</script></div></body></html>"""
