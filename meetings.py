"""Server-side meeting engine.

A meeting runs as a background asyncio task that appends events to an in-memory
log. Browsers subscribe to that log over SSE and can disconnect / reconnect at
any time (we replay from the beginning), so the meeting keeps running on the
server even when no one is watching.

State is in-memory only: meetings are lost if the process restarts. That's fine
for a POC; swap in a DB later for durability.
"""

import time
import uuid
import asyncio

import httpx

from personas import PERSONAS
from room import (
    DEFAULT_MODEL,
    resolve_key,
    resolve_model,
    organizer,
    research_query,
    web_search,
    stream_completion,
    speak_messages,
    decision_messages,
    render_transcript,
    friendly_error,
)

PARTICIPANTS = [
    {"id": p["id"], "name": p["name"], "role": p["role"], "color": p["color"]}
    for p in PERSONAS
]

_MAX_MEETINGS = 50  # keep memory bounded; evict oldest beyond this


class Meeting:
    def __init__(self, topic, details, total, key, model, enable_search):
        self.id = uuid.uuid4().hex[:12]
        self.topic = topic
        self.details = details
        self.total = total
        self.key = key
        self.model = model
        self.enable_search = enable_search
        self.created = time.time()
        self.status = "running"  # running | done | error
        self.events = []          # list of {"event":..., "data":...}
        self._waiters = []        # futures woken on each new event
        self.history = []         # [{name, text}]

    # --- event plumbing ---
    def _emit(self, event, data):
        self.events.append({"event": event, "data": data})
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(None)
        self._waiters = []

    async def wait_for_event(self):
        fut = asyncio.get_event_loop().create_future()
        self._waiters.append(fut)
        await fut

    def summary(self):
        last = self.history[-1]["text"][:60] if self.history else ""
        return {
            "id": self.id,
            "topic": self.topic,
            "status": self.status,
            "created": self.created,
            "rounds_done": len(self.history),
            "last": last,
        }


MEETINGS = {}  # id -> Meeting


def _evict_if_needed():
    if len(MEETINGS) <= _MAX_MEETINGS:
        return
    # drop the oldest finished meetings first
    finished = sorted(
        (m for m in MEETINGS.values() if m.status != "running"),
        key=lambda m: m.created,
    )
    for m in finished[: len(MEETINGS) - _MAX_MEETINGS]:
        MEETINGS.pop(m.id, None)


def create_meeting(topic, details, total, key, model, enable_search):
    key = resolve_key(key)                 # raises if missing
    model = resolve_model(model)
    total = max(2, min(20, int(total)))
    m = Meeting(topic, details, total, key, model, enable_search)
    MEETINGS[m.id] = m
    _evict_if_needed()
    asyncio.create_task(_run(m))
    return m


def list_meetings():
    return sorted((m.summary() for m in MEETINGS.values()),
                  key=lambda s: s["created"], reverse=True)


async def _run(m):
    """The background meeting loop."""
    try:
        m._emit("meeting_started", {
            "id": m.id, "topic": m.topic, "details": m.details,
            "total": m.total, "participants": PARTICIPANTS,
        })
        async with httpx.AsyncClient() as client:
            last_speaker = None
            for round_no in range(1, m.total + 1):
                m._emit("round", {"n": round_no, "total": m.total})
                transcript = render_transcript(m.history)

                # 1. Organizer: scores + consensus (single call) -------------
                scores, matched, consensus, creason = await organizer(
                    client, m.key, m.model, m.topic, m.details, transcript
                )
                # Stop early if the room genuinely agreed.
                if consensus and len(m.history) >= 2:
                    m._emit("consensus", {"reason": creason})
                    break

                bids, ordered = [], []
                for p in PERSONAS:
                    urgency, reason = scores[p["id"]]
                    effective = urgency - (2 if p["name"] == last_speaker else 0)
                    bids.append({"id": p["id"], "name": p["name"], "color": p["color"],
                                 "urgency": urgency, "effective": effective, "reason": reason})
                    ordered.append((effective, p))
                m._emit("bids", {"bids": bids})

                # Fallback to round-robin if the model gave us nothing usable.
                if matched == 0:
                    idx = (round_no - 1) % len(PERSONAS)
                    speaker = PERSONAS[idx]
                    if speaker["name"] == last_speaker:
                        speaker = PERSONAS[(idx + 1) % len(PERSONAS)]
                else:
                    speaker = max(ordered, key=lambda t: t[0])[1]

                m._emit("turn_start", {"id": speaker["id"], "name": speaker["name"],
                                       "role": speaker["role"], "color": speaker["color"]})

                # 2. Research (optional) ------------------------------------
                research = None
                if m.enable_search:
                    q = await research_query(client, m.key, m.model, speaker,
                                             m.topic, m.details, transcript)
                    if q:
                        m._emit("searching", {"id": speaker["id"], "query": q})
                        hits = await web_search(client, q)
                        research = {"query": q, "results": hits}
                        m._emit("search_results", {"id": speaker["id"], "query": q, "results": hits})

                # 3. Speaking (streamed) ------------------------------------
                buffer = []
                async for delta in stream_completion(
                    client, m.key, m.model,
                    speak_messages(speaker, m.topic, m.details, transcript, round_no, m.total, research),
                ):
                    buffer.append(delta)
                    m._emit("token", {"id": speaker["id"], "text": delta})
                text = "".join(buffer).strip() or "(stayed silent)"
                m.history.append({"name": speaker["name"], "text": text})
                last_speaker = speaker["name"]
                m._emit("turn_end", {"id": speaker["id"], "name": speaker["name"], "text": text})

            # Closing decision ---------------------------------------------
            m._emit("deciding", {})
            async for delta in stream_completion(
                client, m.key, m.model,
                decision_messages(m.topic, m.details, m.history),
                max_tokens=600, temperature=0.3,
            ):
                m._emit("decision_token", {"text": delta})

        m.status = "done"
        m._emit("done", {"status": "done"})
    except Exception as e:
        m.status = "error"
        m._emit("error", {"message": friendly_error(e)})
        m._emit("done", {"status": "error"})


async def event_stream(meeting):
    """Async generator that replays past events then follows live ones."""
    idx = 0
    while True:
        while idx < len(meeting.events):
            yield meeting.events[idx]
            idx += 1
        if meeting.status != "running" and idx >= len(meeting.events):
            return
        await meeting.wait_for_event()
