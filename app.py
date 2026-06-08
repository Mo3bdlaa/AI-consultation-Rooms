"""FastAPI web layer.

A meeting runs *inside* the SSE stream request (see meetings.py) and is persisted
to SQLite as it goes, so it survives the request and can be replayed later. This
fits serverless platforms (e.g. Vercel) where background tasks don't outlive the
response.

  POST /login                       log in (JSON {user, password}) -> sets cookie
  GET  /logout                      clear the session cookie
  POST /api/meetings                start a meeting -> {id}
  GET  /api/meetings                list meetings (from the DB, for the sidebar)
  GET  /api/meetings/{id}/stream    SSE: run it live (first time) or replay it

Auth is a simple signed cookie with dummy credentials (admin/admin by default)
for the POC. The OpenRouter key is sent in the request body (from Settings) and
kept in server memory only for the life of the meeting.
"""

import os
import json
import hmac
import base64
import hashlib

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
)

import meetings

load_dotenv()

app = FastAPI(title="AI Consultation Rooms")
HERE = os.path.dirname(os.path.abspath(__file__))

# --- dummy auth (POC) -------------------------------------------------------
SECRET = os.environ.get("SECRET_KEY", "dev-secret-change-me").encode()
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASS = os.environ.get("APP_PASS", "admin")
_PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico"}


def _sign(value):
    return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()


def _make_token(user):
    return base64.urlsafe_b64encode(f"{user}.{_sign(user)}".encode()).decode()


def _valid_token(token):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user, sig = raw.rsplit(".", 1)
        return hmac.compare_digest(sig, _sign(user))
    except Exception:
        return False


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS:
        return await call_next(request)
    if _valid_token(request.cookies.get("session", "")):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


_INDEX_CANDIDATES = [
    os.path.join(HERE, "static", "index.html"),
    os.path.join(os.path.dirname(HERE), "static", "index.html"),
    os.path.join(os.getcwd(), "static", "index.html"),
    "static/index.html",
]


def _read_index():
    for path in _INDEX_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


@app.get("/")
async def index():
    path = _read_index()
    if path:
        return FileResponse(path)
    return HTMLResponse("<h1>UI file not found</h1>", status_code=500)


# --- auth routes ------------------------------------------------------------
_LOGIN_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Sign in · AI Consultation Rooms</title><style>
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0b1220;
color:#e2e8f0;display:flex;align-items:center;justify-content:center}
.card{width:330px;background:#131c2e;border:1px solid #2a3852;border-radius:16px;padding:28px}
h1{font-size:19px;margin:0 0 4px}p{color:#8aa0bd;font-size:13px;margin:0 0 18px}
label{display:block;font-size:12px;color:#8aa0bd;margin:14px 0 5px}
input{width:100%;background:#0b1220;border:1px solid #2a3852;color:#e2e8f0;
padding:11px 12px;border-radius:9px;font-size:14px}
button{width:100%;margin-top:20px;background:#3b82f6;color:#fff;border:0;
border-radius:9px;padding:12px;font-weight:600;font-size:14px;cursor:pointer}
.err{color:#fca5a5;font-size:13px;margin-top:12px;min-height:18px}
.hint{color:#64748b;font-size:12px;margin-top:14px;text-align:center}
</style></head><body><div class="card">
<h1>AI Consultation Rooms</h1><p>Sign in to continue</p>
<label>Username</label><input id="u" autofocus autocomplete="username"/>
<label>Password</label><input id="p" type="password" autocomplete="current-password"/>
<button id="go">Sign in</button><div class="err" id="err"></div>
<div class="hint">POC demo login &mdash; <b>admin</b> / <b>admin</b></div>
</div><script>
const go=document.getElementById('go');
async function submit(){
  go.disabled=true;document.getElementById('err').textContent='';
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({user:document.getElementById('u').value,
      password:document.getElementById('p').value})});
  if(r.ok){location.href='/';}
  else{document.getElementById('err').textContent='Wrong username or password.';go.disabled=false;}
}
go.onclick=submit;
document.getElementById('p').addEventListener('keydown',e=>{if(e.key==='Enter')submit();});
</script></body></html>"""


@app.get("/login")
async def login_page():
    return HTMLResponse(_LOGIN_PAGE)


@app.post("/login")
async def login(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if data.get("user") == APP_USER and data.get("password") == APP_PASS:
        resp = JSONResponse({"ok": True})
        resp.set_cookie("session", _make_token(APP_USER), httponly=True,
                        max_age=86400, samesite="lax")
        return resp
    return JSONResponse({"error": "invalid credentials"}, status_code=401)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# --- meetings API -----------------------------------------------------------
@app.post("/api/meetings")
async def start_meeting(request: Request):
    body = await request.json()
    try:
        m = meetings.create_meeting(
            topic=(body.get("topic") or "").strip(),
            details=(body.get("details") or "").strip(),
            total=body.get("total", 8),
            key=body.get("key"),
            model=body.get("model"),
            enable_search=bool(body.get("search", False)),
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"id": m.id}


@app.get("/api/meetings")
async def get_meetings():
    return {"meetings": meetings.list_meetings()}


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/meetings/{meeting_id}/stream")
async def stream_meeting(meeting_id: str, request: Request):
    m = meetings.MEETINGS.get(meeting_id)
    if m and not m.started:
        m.started = True                 # run it live, exactly once
        source = meetings.run_and_stream(m)
    else:
        if not meetings.db.get(meeting_id):
            return JSONResponse({"error": "meeting not found"}, status_code=404)
        source = meetings.replay(meeting_id)   # re-watch from the DB

    async def gen():
        async for ev in source:
            yield _sse(ev["event"], ev["data"])

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
