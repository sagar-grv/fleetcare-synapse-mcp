"""Read-only FleetCare viewer + request launcher. Separate process, NOT part of the app.
Reads the app only through its existing GET endpoints and the adapter's JSONL log.
Each request gets its own id and its own folder under RUNS_DIR; results are bound to that id.
Usage: ENDPOINT_BASE_URL=http://127.0.0.1:8000 GUARD_LOG=guard_log.jsonl python viewer.py 8090
Model runs are off unless ALLOW_MODEL_RUN=1 (with MOCK_LLM=1 for the scripted no-model plumbing check).
"""
import json, os, re, sys, tempfile, subprocess, threading, time, uuid, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from intent import resolve

BASE = os.environ.get("ENDPOINT_BASE_URL", "http://127.0.0.1:8000")
LOG = os.environ.get("GUARD_LOG", "guard_log.jsonl")
LABEL = os.environ.get("VIEWER_LABEL", "")
RUNS = os.environ.get("RUNS_DIR") or tempfile.mkdtemp(prefix="fc_runs_")
ID_RE = re.compile(r"^[0-9a-f]{12}$")
NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%S")

PAGE = r"""<!doctype html><meta charset=utf-8><title>FleetCare viewer</title><style>
body{font:14px system-ui;margin:0;background:#0f1419;color:#e6e6e6}header{padding:12px 20px;background:#161c24;display:flex;gap:16px;align-items:center}
h1{font-size:16px;margin:0}.tag{background:#33414f;border-radius:10px;padding:2px 9px;font-size:12px}main{display:grid;grid-template-columns:2.6fr 1.25fr 1.15fr;gap:14px;padding:14px}
section{background:#161c24;border-radius:8px;padding:12px;overflow:auto;max-height:80vh}h2{font-size:13px;margin:0 0 8px;color:#9fb3c8;text-transform:uppercase}
table{width:100%;border-collapse:collapse}td,th{padding:4px 6px;text-align:left;border-bottom:1px solid #26303b;font-size:13px}
.wo{min-width:190px;max-width:290px}.wo .rev{display:block;font-size:12px;margin-top:2px}td{vertical-align:top}.crit{color:#ff6b6b}.high{color:#ffa94d}.rev{color:#ffd43b}.rej{color:#ff8787}.ok{color:#69db7c}small{color:#8a97a6}</style>
<header><h1>FleetCare viewer</h1><span class=tag>read-only</span><span class=tag id=lab></span><small>reads the app's GET endpoints and the adapter log</small></header>
<main><section><h2>Alerts and work orders</h2><div id=a></div></section><section><h2>Parts and purchase orders</h2><div id=p></div></section>
<section><h2>Adapter activity</h2><div id=l></div></section>
<section style="grid-column:1/4"><h2>Result</h2><div id=rs><small>No request yet.</small></div></section>
<section style="grid-column:1/4"><h2>New request (a person chooses what it may do; the text never grants anything)</h2>
<textarea id=txt rows=2 style="width:60%;background:#0f1419;color:#e6e6e6;border:1px solid #33414f;padding:6px" placeholder="e.g. What should we restock and what would it cost?"></textarea><br>
<label><input type=radio name=m value=read checked> Ask only (read-only, default)</label>
<label><input type=radio name=m value=draft_work_orders> Allow draft work orders</label>
<label><input type=radio name=m value=draft_purchase_order> Allow a draft purchase order</label>
<label><input type=checkbox id=cf> I confirm drafts may be created</label>
<button id=run>Run</button> <span id=res></span></section></main><script>
// Every string from the backend, the log, or the server is escaped. Cells are text unless built with raw().
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const raw=h=>({h});const cell=y=>(y&&typeof y=='object'&&'h' in y)?y.h:esc(y);
const t=(h,r)=>'<table><tr>'+h.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr>'+r.map(x=>'<tr>'+x.map(y=>'<td>'+cell(y)+'</td>').join('')+'</tr>').join('')+'</table>';
const sp=(c,s)=>raw('<span class="'+esc(c)+'">'+esc(s)+'</span>');
let cur=null;
async function go(){const d=await (await fetch('/data')).json();document.getElementById('lab').textContent=d.label;
const wo={};d.work_orders.forEach(w=>wo[w.alert_id]=w);const sc={};d.centers.forEach(c=>sc[c.id]=c);const v={};d.vehicles.forEach(x=>v[x.id]=x);
const sev=x=>['crit','high','low','medium'].includes(x)?x:'';
a.innerHTML=t(['#','Vehicle','Alert','Sev','Status','Needs','Work order'],d.alerts.map(x=>{const w=wo[x.id];
 const wcell=w?raw('<div class=wo>#'+esc(w.id)+' '+esc((sc[w.service_center_id]||{}).name)+(w.needs_human_review?' <span class=rev>review: '+esc(w.review_reason||'')+'</span>':' <span class=ok>ok</span>')+'</div>'):raw('<small>none</small>');
 return [x.id,(v[x.vehicle_id]||{}).plate,x.description,sp(sev(x.severity),x.severity),x.status,x.needed_specialty,wcell]}));
p.innerHTML=t(['Part','Stock','Reorder'],d.parts.map(x=>[x.name,x.stock<=x.reorder_point?sp('crit',x.stock):x.stock,x.reorder_point]))+'<br>'+t(['PO','Lines','Total Rs','Status'],d.purchase_orders.map(x=>[x.id,x.lines,x.total,x.status]));
l.innerHTML=t(['Time','Event'],d.log.slice().reverse().map(x=>[x.ts,x.kind=='rejected'?raw('<span class=rej>rejected '+esc(x.code)+'</span><br><small>'+esc(x.reason)+'</small>'):x.kind=='skipped'?raw('<span class=rev>skipped parts '+esc((x.parts||[]).join(','))+'</span><br><small>'+esc(x.reason)+'</small>'):raw('<span class=ok>created '+esc(x.what)+' #'+esc(x.id)+'</span>')]))}
async function rsl(){if(!cur){return}
const d=await (await fetch('/result?id='+encodeURIComponent(cur))).json();
if(d.request_id!==cur){return}
const e=d.envelope;let h='<small>request '+esc(d.request_id)+' | created '+esc(d.created)+' | state: <b>'+esc(d.state)+'</b>'+(d.finished?' | finished '+esc(d.finished):'')+(e&&e.run_kind?' | '+esc(e.run_kind):'')+'</small>';
if(!e||e.request_id!==cur){h+='<p>'+(d.state=='not_started'?esc(d.reason||'not started'):d.state=='failed'?'This request failed. No verified answer. '+esc(d.reason||''):'Running or queued. No result yet.')+'</p>';rs.innerHTML=h;return}
const cls={rendered:'ok',refused:'rej',evidence_only:'rev',no_supported_answer:'rej'}[e.status]||'';
h+='<p><span class=tag>status: <b class="'+cls+'">'+esc(e.status)+'</b></span> <small>allowed: '+esc(e.capabilities)+' | tools used: '+esc((e.basis||[]).join(', ')||'none')+'</small></p>';
h+='<p class=rev><b>Overall completion: '+esc(e.overall_completion)+'</b> (a status of rendered means only that the figures below came from tools, not that the request was fully done)</p>';if((e.verified_results||[]).length){h+='<h2>Verified tool results (each checked separately)</h2><p style="white-space:pre-wrap">'+esc(e.answer_text)+'</p>'}else{h+='<p style="white-space:pre-wrap">'+esc(e.answer_text)+'</p>'}
if(e.evidence_disclosure&&e.status!='evidence_only')h+='<p class=rev>'+esc(e.evidence_disclosure)+'</p>';
(e.evidence||[]).forEach(q=>{h+='<small>'+esc(q.tool)+': '+esc(q.rows_returned)+' rows returned'+(q.more_rows_not_shown?' ('+esc(q.more_rows_not_shown)+' more not shown, limit '+esc(e.row_limit)+')':'')+'</small>';
const ks=q.rows.length?Object.keys(q.rows[0]):[];h+=t(ks,q.rows.map(r=>ks.map(k=>typeof r[k]=='object'?JSON.stringify(r[k]):r[k])))});
h+='<p><small>'+esc(e.request_coverage)+'</small></p>';rs.innerHTML=h}
run.onclick=async()=>{const m=document.querySelector('input[name=m]:checked').value;
const r=await (await fetch('/request',{method:'POST',body:JSON.stringify({text:txt.value,mode:m,confirmed:cf.checked})})).json();
cur=r.request_id;res.textContent='allowed: '+r.capabilities+' ('+r.note+')';rs.innerHTML='<small>Request '+esc(r.request_id)+' submitted.</small>'}
go();setInterval(go,2000);setInterval(rsl,1000)</script>"""


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.load(r)


def rpath(rid, name):
    return os.path.join(RUNS, rid, name)


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def set_state(rid, **kw):
    p = rpath(rid, "state.json")
    try: st = json.load(open(p))
    except Exception: st = {}
    st.update(kw); write_json_atomic(p, st)


def watch(rid, proc):
    proc.wait()
    done = os.path.exists(rpath(rid, "result.json"))
    set_state(rid, state="done" if done else "failed", finished=NOW(),
              reason=None if done else f"runner exited with code {proc.returncode} and wrote no result")


def launch(rid, q, caps):
    d = os.path.join(RUNS, rid); os.makedirs(d)
    req = {"request_id": rid, "created": NOW(), "mode": q.get("mode"), "confirmed": bool(q.get("confirmed")), "capabilities": caps, "text": q.get("text") or ""}
    write_json_atomic(rpath(rid, "request.json"), req)
    if os.environ.get("ALLOW_MODEL_RUN") != "1" or not req["text"].strip():
        set_state(rid, state="not_started", reason="model runs are disabled in this build (ALLOW_MODEL_RUN=1 enables them)" if os.environ.get("ALLOW_MODEL_RUN") != "1" else "empty request text")
        return "not_started"
    case = {"id": rid, "task": "request", "instruction": req["text"], "mode": q.get("mode"), "confirmed": bool(q.get("confirmed"))}
    json.dump(case, open(rpath(rid, "case.json"), "w"))
    env = {**os.environ, "RESULT_OUT": rpath(rid, "result.json"), "REQUEST_ID": rid, "ENDPOINT_BASE_URL_EXTERNAL": BASE, "GUARD_LOG": LOG}
    with open(rpath(rid, "runner.log"), "w") as lg:
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "run_case_llm.py"), rpath(rid, "case.json")], env=env, stdout=lg, stderr=subprocess.STDOUT)
    set_state(rid, state="running", started=NOW())
    threading.Thread(target=watch, args=(rid, proc), daemon=True).start()
    return "running"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ct="application/json"):
        self.send_response(code); self.send_header("Content-Type", ct); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        if self.path != "/request":
            return self.send(404, b"{}")
        n = int(self.headers.get("Content-Length") or 0)
        try: q = json.loads(self.rfile.read(n) or b"{}")
        except Exception: q = {}
        caps, note = resolve(mode=q.get("mode"), confirmed=bool(q.get("confirmed")))   # text is ignored for permissions
        rid = uuid.uuid4().hex[:12]
        state = launch(rid, q, caps)
        self.send(200, json.dumps({"request_id": rid, "capabilities": caps, "note": note, "state": state}).encode())

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/data":
            log = [json.loads(l) for l in open(LOG)] if os.path.exists(LOG) else []
            return self.send(200, json.dumps({"label": LABEL, "alerts": get("/alerts"), "vehicles": get("/vehicles"), "centers": get("/service-centers"),
                             "work_orders": get("/work-orders"), "parts": get("/parts"), "purchase_orders": get("/purchase-orders"), "log": log}).encode())
        if path == "/result":
            rid = dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("id", "")
            if not ID_RE.match(rid) or not os.path.isdir(os.path.join(RUNS, rid)):
                return self.send(404, b"{}")
            st = json.load(open(rpath(rid, "state.json"))) if os.path.exists(rpath(rid, "state.json")) else {"state": "queued"}
            req = json.load(open(rpath(rid, "request.json")))
            env = None
            if os.path.exists(rpath(rid, "result.json")):
                env = json.load(open(rpath(rid, "result.json")))
                if env.get("request_id") != rid:      # a result that is not bound to this request is never shown
                    env = None
            return self.send(200, json.dumps({"request_id": rid, "created": req["created"], **{k: st.get(k) for k in ("state", "reason", "started", "finished")}, "envelope": env}).encode())
        self.send(200, PAGE.encode(), "text/html")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8090), H).serve_forever()
