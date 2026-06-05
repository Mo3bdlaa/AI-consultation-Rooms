"""FastAPI web layer.

Meetings run server-side in background tasks (see meetings.py), so they keep
going whether or not a browser is connected. Endpoints:

  POST   /api/meetings              start a meeting -> {id}
  GET    /api/meetings              list meetings (for the sidebar)
  GET    /api/meetings/{id}/stream  SSE: replay past events + follow live ones

The OpenRouter API key is sent in the request body (from Settings) and kept in
server memory only for the life of the meeting.
"""

import os
import json

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

import meetings

load_dotenv()

app = FastAPI(title="AI Consultation Rooms")
HERE = os.path.dirname(os.path.abspath(__file__))

_INDEX_CANDIDATES = [
    os.path.join(HERE, "static", "index.html"),
    os.path.join(os.path.dirname(HERE), "static", "index.html"),
    os.path.join(os.getcwd(), "static", "index.html"),
    "static/index.html",
]


@app.get("/")
async def index():
    for path in _INDEX_CANDIDATES:
        if os.path.exists(path):
            return FileResponse(path)
    return HTMLResponse("<h1>UI file not found</h1>", status_code=500)


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
    meeting = meetings.MEETINGS.get(meeting_id)
    if not meeting:
        return JSONResponse({"error": "meeting not found"}, status_code=404)

    async def gen():
        async for ev in meetings.event_stream(meeting):
            if await request.is_disconnected():
                break
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
