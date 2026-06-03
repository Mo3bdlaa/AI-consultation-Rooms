"""The consultation room orchestrator.

Runs the whole meeting as an async generator that yields events. The web layer
(app.py) just forwards those events to the browser over SSE. Keeping the logic
here makes it easy to read and to reuse outside the web UI later.
"""

import os
import re
import json
import asyncio

import httpx

from personas import (
    PERSONAS,
    build_speak_messages,
    build_bid_messages,
    build_decision_messages,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_BID_RE = re.compile(r"URGENCY:\s*(\d+).*?REASON:\s*(.*)", re.IGNORECASE | re.DOTALL)


def _headers():
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Copy .env.example to .env first.")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": os.environ.get("APP_TITLE", "AI Consultation Rooms"),
    }


def _model():
    return os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free").strip()


async def _complete(client, messages, max_tokens=400, temperature=0.8):
    """Single non-streaming completion. Used for bids and the final decision."""
    body = {
        "model": _model(),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    resp = await client.post(OPENROUTER_URL, headers=_headers(), json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


async def _stream(client, messages, max_tokens=400, temperature=0.85):
    """Streaming completion. Yields text chunks as they arrive."""
    body = {
        "model": _model(),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    async with client.stream(
        "POST", OPENROUTER_URL, headers=_headers(), json=body, timeout=120
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
                delta = chunk["choices"][0]["delta"].get("content")
                if delta:
                    yield delta
            except (json.JSONDecodeError, KeyError, IndexError):
                continue


def _render_transcript(history):
    return "\n".join(f"[{name}]: {text}" for name, text in history)


async def _get_bid(client, persona, topic, details, transcript):
    """Ask one persona how badly it wants to speak. Returns (urgency, reason)."""
    try:
        raw = await _complete(
            client,
            build_bid_messages(persona, topic, details, transcript),
            max_tokens=40,
            temperature=0.4,
        )
    except Exception:
        return 0, "(no response)"
    m = _BID_RE.search(raw)
    if not m:
        return 1, raw[:60]
    urgency = max(0, min(10, int(m.group(1))))
    reason = m.group(2).strip().split("\n")[0][:80]
    return urgency, reason


async def run_room(topic, details, rounds=8):
    """Main loop. Yields (event_name, data_dict) tuples."""
    yield "start", {
        "topic": topic,
        "details": details,
        "rounds": rounds,
        "participants": [
            {"id": p["id"], "name": p["name"], "role": p["role"], "color": p["color"]}
            for p in PERSONAS
        ],
    }

    history = []          # list of (name, text)
    last_speaker = None

    async with httpx.AsyncClient() as client:
        for n in range(1, rounds + 1):
            yield "round", {"n": n, "total": rounds}
            transcript = _render_transcript(history)

            # --- Bidding phase: everyone raises a hand in parallel ---
            results = await asyncio.gather(
                *[_get_bid(client, p, topic, details, transcript) for p in PERSONAS]
            )
            bids = []
            for p, (urgency, reason) in zip(PERSONAS, results):
                # Light penalty so the same person doesn't dominate the room.
                effective = urgency - (2 if p["name"] == last_speaker else 0)
                bids.append(
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "color": p["color"],
                        "urgency": urgency,
                        "effective": effective,
                        "reason": reason,
                    }
                )
            yield "bids", {"bids": bids}

            winner_bid = max(bids, key=lambda b: b["effective"])
            speaker = next(p for p in PERSONAS if p["id"] == winner_bid["id"])
            yield "turn_start", {
                "id": speaker["id"],
                "name": speaker["name"],
                "role": speaker["role"],
                "color": speaker["color"],
            }

            # --- Speaking phase: stream the chosen persona's reply live ---
            messages = build_speak_messages(speaker, topic, details, transcript)
            buffer = []
            try:
                async for delta in _stream(client, messages):
                    buffer.append(delta)
                    yield "token", {"id": speaker["id"], "text": delta}
            except Exception as e:
                yield "error", {"message": f"Stream failed: {e}"}
                return

            text = "".join(buffer).strip()
            if not text:
                text = "(stayed silent)"
            history.append((speaker["name"], text))
            last_speaker = speaker["name"]
            yield "turn_end", {"id": speaker["id"], "name": speaker["name"], "text": text}

        # --- Closing: facilitator forces a decision ---
        yield "deciding", {}
        try:
            decision = await _complete(
                client,
                build_decision_messages(topic, details, _render_transcript(history)),
                max_tokens=600,
                temperature=0.3,
            )
        except Exception as e:
            decision = f"(Could not generate decision: {e})"
        yield "decision", {"text": decision}

    yield "done", {}
