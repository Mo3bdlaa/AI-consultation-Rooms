"""FastAPI web layer: serves the UI and streams the meeting over SSE."""

import json
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse

from room import run_room

load_dotenv()

app = FastAPI(title="AI Consultation Rooms")

HERE = os.path.dirname(os.path.abspath(__file__))


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/stream")
async def stream(request: Request, topic: str, details: str = "", rounds: int = 8):
    rounds = max(1, min(20, rounds))

    async def gen():
        try:
            async for event, data in run_room(topic, details, rounds):
                if await request.is_disconnected():
                    break
                yield _sse(event, data)
        except Exception as e:  # surface setup errors (e.g. missing API key) to the UI
            yield _sse("error", {"message": str(e)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
