"""FastAPI web layer.

Two short, serverless-friendly endpoints stream one unit of work each:
  POST /api/step      -> runs a single round
  POST /api/decision  -> streams the closing decision
The browser holds the transcript and drives the loop until consensus or the
round cap, which keeps every request short enough for Vercel.

The OpenRouter API key is taken from the `X-OpenRouter-Key` header (sent by the
UI) or, as a fallback, the OPENROUTER_API_KEY env var. We never store it.
"""

import json
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse

from room import run_step, run_decision

load_dotenv()

app = FastAPI(title="AI Consultation Rooms")
HERE = os.path.dirname(os.path.abspath(__file__))


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_response(gen_factory, request):
    async def gen():
        try:
            async for event, data in gen_factory():
                if await request.is_disconnected():
                    break
                yield _sse(event, data)
        except Exception as e:
            yield _sse("error", {"message": str(e)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/step")
async def step(request: Request):
    body = await request.json()
    key = request.headers.get("X-OpenRouter-Key")
    return _stream_response(
        lambda: run_step(
            topic=body.get("topic", ""),
            details=body.get("details", ""),
            history=body.get("history", []),
            round_no=int(body.get("round", 1)),
            total=int(body.get("total", 8)),
            key=key,
            model=body.get("model"),
        ),
        request,
    )


@app.post("/api/decision")
async def decision(request: Request):
    body = await request.json()
    key = request.headers.get("X-OpenRouter-Key")
    return _stream_response(
        lambda: run_decision(
            topic=body.get("topic", ""),
            details=body.get("details", ""),
            history=body.get("history", []),
            key=key,
            model=body.get("model"),
        ),
        request,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
