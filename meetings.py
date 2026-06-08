"""Meeting engine (serverless-friendly).

Unlike the original background-task design, a meeting here runs *inside* the SSE
request that streams it (`run_and_stream`). This fits platforms like Vercel where
functions are short-lived and can't keep a task alive after the response ends.

As the meeting runs we persist a compact event log to SQLite (see db.py), so a
finished meeting can be replayed from the sidebar later (`replay`). State that is
held in memory (MEETINGS, the API key) only lives for the request that runs the
meeting; everything needed to re-watch it afterwards is in the database.
"""

import time
import uuid
import asyncio

import httpx

import db
from personas import PERSONAS
from room import (
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

_MAX_MEETINGS = 50  # keep the in-memory map bounded


class Meeting:
    def __init__(self, topic, details, total, key, model, enable_search):
        self.id = uuid.uuid4().hex[:12]
        self.topic = topic
        self.details = details
        self.total = total
        self.key = key                 # in memory only, never persisted
        self.model = model
        self.enable_search = enable_search
        self.created = time.time()
        self.status = "pending"        # pending | running | done | error
        self.started = False           # set true once a stream begins running it
        self.history = []              # [{name, text}]
        self.log = []                  # compact, persisted events


MEETINGS = {}  # id -> Meeting (only meaningful on the instance that runs it)


def _evict_if_needed():
    if len(MEETINGS) <= _MAX_MEETINGS:
        return
    finished = sorted(
        (m for m in MEETINGS.values() if m.status not in ("pending", "running")),
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
    db.create(m)                           # persist a "pending" row
    return m


def list_meetings():
    # The database is the source of truth for the sidebar so history shows up
    # even for meetings this instance never ran.
    return db.list_all()


def _ev(m, event, data, persist=True):
    """Build an SSE event dict and (optionally) add it to the persisted log.

    Token-level events are streamed to the browser but not persisted (there can
    be thousands); on replay we reconstruct them from the stored turn text.
    """
    if persist:
        m.log.append({"event": event, "data": data})
    return {"event": event, "data": data}


async def run_and_stream(m):
    """Run the whole meeting, yielding events and persisting as we go."""
    m.status = "running"
    decision_text = ""
    try:
        yield _ev(m, "meeting_started", {
            "id": m.id, "topic": m.topic, "details": m.details,
            "total": m.total, "participants": PARTICIPANTS,
        })
        async with httpx.AsyncClient() as client:
            last_speaker = None
            for round_no in range(1, m.total + 1):
                yield _ev(m, "round", {"n": round_no, "total": m.total})
                transcript = render_transcript(m.history)

                # 1. Organizer: scores + consensus (single call)
                scores, matched, consensus, creason = await organizer(
                    client, m.key, m.model, m.topic, m.details, transcript
                )
                if consensus and len(m.history) >= 2:
                    yield _ev(m, "consensus", {"reason": creason})
                    break

                bids, ordered = [], []
                for p in PERSONAS:
                    urgency, reason = scores[p["id"]]
                    effective = urgency - (2 if p["name"] == last_speaker else 0)
                    bids.append({"id": p["id"], "name": p["name"], "color": p["color"],
                                 "urgency": urgency, "effective": effective, "reason": reason})
                    ordered.append((effective, p))
                yield _ev(m, "bids", {"bids": bids})

                if matched == 0:
                    idx = (round_no - 1) % len(PERSONAS)
                    speaker = PERSONAS[idx]
                    if speaker["name"] == last_speaker:
                        speaker = PERSONAS[(idx + 1) % len(PERSONAS)]
                else:
                    speaker = max(ordered, key=lambda t: t[0])[1]

                yield _ev(m, "turn_start", {"id": speaker["id"], "name": speaker["name"],
                                            "role": speaker["role"], "color": speaker["color"]})

                # 2. Research (optional)
                research = None
                if m.enable_search:
                    q = await research_query(client, m.key, m.model, speaker,
                                             m.topic, m.details, transcript)
                    if q:
                        yield _ev(m, "searching", {"id": speaker["id"], "query": q})
                        hits = await web_search(client, q)
                        research = {"query": q, "results": hits}
                        yield _ev(m, "search_results",
                                  {"id": speaker["id"], "query": q, "results": hits})

                # 3. Speaking (streamed; tokens not persisted individually)
                buffer = []
                async for delta in stream_completion(
                    client, m.key, m.model,
                    speak_messages(speaker, m.topic, m.details, transcript,
                                   round_no, m.total, research),
                ):
                    buffer.append(delta)
                    yield _ev(m, "token", {"id": speaker["id"], "text": delta}, persist=False)
                text = "".join(buffer).strip() or "(stayed silent)"
                m.history.append({"name": speaker["name"], "text": text})
                last_speaker = speaker["name"]
                yield _ev(m, "turn_end", {"id": speaker["id"], "name": speaker["name"], "text": text})

                # Partial durability: save after each completed turn.
                db.save(m.id, m.log, "running", "")

            # Closing decision
            yield _ev(m, "deciding", {})
            dbuf = []
            async for delta in stream_completion(
                client, m.key, m.model,
                decision_messages(m.topic, m.details, m.history),
                max_tokens=600, temperature=0.3,
            ):
                dbuf.append(delta)
                yield _ev(m, "decision_token", {"text": delta}, persist=False)
            decision_text = "".join(dbuf).strip()
            m.log.append({"event": "decision", "data": {"text": decision_text}})

        m.status = "done"
        yield _ev(m, "done", {"status": "done"})
        db.save(m.id, m.log, "done", decision_text)
    except Exception as e:
        m.status = "error"
        yield _ev(m, "error", {"message": friendly_error(e)})
        yield _ev(m, "done", {"status": "error"})
        db.save(m.id, m.log, "error", decision_text)


async def replay(meeting_id):
    """Re-emit a finished meeting's stored log as live-looking events."""
    row = db.get(meeting_id)
    if not row:
        return
    import json
    log = json.loads(row["log"] or "[]")
    for e in log:
        ev, data = e["event"], e["data"]
        if ev == "turn_end":
            # reconstruct the streamed bubble from the stored full text
            yield {"event": "token", "data": {"id": data.get("id"), "text": data.get("text", "")}}
            yield {"event": "turn_end", "data": data}
        elif ev == "decision":
            yield {"event": "decision_token", "data": {"text": data.get("text", "")}}
        else:
            yield {"event": ev, "data": data}
    # make sure the UI marks it finished even on older partial logs
    if not any(e["event"] == "done" for e in log):
        yield {"event": "done", "data": {"status": row["status"]}}
        await asyncio.sleep(0)
