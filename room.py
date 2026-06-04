"""Consultation room orchestrator.

Designed to be *stateless per round* so it runs on serverless (Vercel): the
browser holds the transcript and calls `/api/step` once per round, then
`/api/decision` at the end. Each call is short.

Every round:
  1. Bidding   - all personas score how urgently they want to speak (parallel)
  2. Research  - the chosen speaker may use the web-search tool
  3. Speaking  - the chosen speaker's reply is streamed token-by-token
  4. Consensus - the facilitator judges whether the room has converged
"""

import os
import re
import json
import asyncio

import httpx

from personas import (
    PERSONAS,
    build_speak_messages,
    build_moderator_messages,
    build_research_messages,
    build_consensus_messages,
    build_decision_messages,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "z-ai/glm-4.5-air:free"

ENABLE_SEARCH = True
_RETRYABLE = {429, 500, 502, 503, 529}
_MAX_RETRIES = 4

_CONSENSUS_RE = re.compile(r"CONSENSUS:\s*(YES|NO).*?REASON:\s*(.*)", re.IGNORECASE | re.DOTALL)


def resolve_model(model):
    return (model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL).strip()


def resolve_key(key):
    key = (key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "No OpenRouter API key. Paste your key in the field at the top "
            "(get one free at openrouter.ai/keys)."
        )
    return key


def _headers(key):
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": os.environ.get("APP_TITLE", "AI Consultation Rooms"),
    }


def _friendly_error(e):
    msg = str(e)
    if "429" in msg:
        return (
            "OpenRouter rate-limited you (429), even after retries. Free models "
            "have tight per-minute/daily limits. Wait ~30–60s and try again, lower "
            "the max rounds, or add credits / pick a paid model in OPENROUTER_MODEL."
        )
    if "401" in msg or "403" in msg:
        return "OpenRouter rejected the API key (401/403). Check the key you pasted."
    return f"Model call failed: {msg}"


def _backoff(resp, attempt):
    """Seconds to wait before retrying. Honors Retry-After when present."""
    if resp is not None:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return min(20, float(ra))
            except ValueError:
                pass
    return min(16, 2 ** (attempt + 1))  # 2, 4, 8, 16


async def _complete(client, key, model, messages, max_tokens=400, temperature=0.8):
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    for attempt in range(_MAX_RETRIES + 1):
        resp = await client.post(OPENROUTER_URL, headers=_headers(key), json=body, timeout=60)
        if resp.status_code in _RETRYABLE and attempt < _MAX_RETRIES:
            await asyncio.sleep(_backoff(resp, attempt))
            continue
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


async def _stream(client, key, model, messages, max_tokens=400, temperature=0.85):
    body = {
        "model": model, "messages": messages, "max_tokens": max_tokens,
        "temperature": temperature, "stream": True,
    }
    for attempt in range(_MAX_RETRIES + 1):
        async with client.stream(
            "POST", OPENROUTER_URL, headers=_headers(key), json=body, timeout=120
        ) as resp:
            if resp.status_code in _RETRYABLE and attempt < _MAX_RETRIES:
                await resp.aread()
                wait = _backoff(resp, attempt)
            else:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0]["delta"].get("content")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                return
        await asyncio.sleep(wait)


_SNIPPET_RE = re.compile(r'result__snippet[^>]*>(.*?)</a>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SEARCH_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _clean_html(s):
    import html as _html
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


async def web_search(client, query, max_results=4):
    """Free, key-less web search via DuckDuckGo's HTML results endpoint."""
    try:
        r = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=_SEARCH_HEADERS,
            timeout=20,
            follow_redirects=True,
        )
        snippets = [_clean_html(s) for s in _SNIPPET_RE.findall(r.text)]
        return [s for s in snippets if s][:max_results]
    except Exception:
        return []


def _render(history):
    return "\n".join(f"[{m['name']}]: {m['text']}" for m in history)


async def _moderator_bids(client, key, model, topic, details, transcript):
    """One call that scores every participant. Returns {persona_id: (urgency, reason)}."""
    scores = {}
    try:
        raw = await _complete(
            client, key, model,
            build_moderator_messages(topic, details, transcript),
            max_tokens=160, temperature=0.4,
        )
    except Exception:
        raw = ""
    for p in PERSONAS:
        m = re.search(rf"{re.escape(p['name'])}\s*\|\s*(\d+)\s*\|\s*([^\n|]*)", raw, re.IGNORECASE)
        if m:
            scores[p["id"]] = (max(0, min(10, int(m.group(1)))), m.group(2).strip()[:80])
        else:
            scores[p["id"]] = (5, "")  # neutral fallback so the room keeps moving
    return scores


async def run_step(topic, details, history, round_no, total, key=None, model=None):
    """One round. Yields (event, data). Caller appends turn_end text to history."""
    key = resolve_key(key)
    model = resolve_model(model)
    history = history or []
    last_speaker = history[-1]["name"] if history else None
    transcript = _render(history)

    async with httpx.AsyncClient() as client:
        # 1. Bidding: one organizer call scores everyone (no parallel burst) --
        scores = await _moderator_bids(client, key, model, topic, details, transcript)
        bids = []
        for p in PERSONAS:
            urgency, reason = scores[p["id"]]
            effective = urgency - (2 if p["name"] == last_speaker else 0)
            bids.append({
                "id": p["id"], "name": p["name"], "color": p["color"],
                "urgency": urgency, "effective": effective, "reason": reason,
            })
        yield "bids", {"bids": bids}

        speaker = next(p for p in PERSONAS if p["id"] == max(bids, key=lambda b: b["effective"])["id"])
        yield "turn_start", {"id": speaker["id"], "name": speaker["name"],
                             "role": speaker["role"], "color": speaker["color"]}

        # 2. Research (optional tool use) ---------------------------------
        research = None
        if ENABLE_SEARCH:
            try:
                decision = await _complete(
                    client, key, model,
                    build_research_messages(speaker, topic, details, transcript),
                    max_tokens=40, temperature=0.3,
                )
            except Exception:
                decision = "NONE"
            q = decision.strip()
            if q.upper().startswith("QUERY:"):
                query = q.split(":", 1)[1].strip()[:120]
                yield "searching", {"id": speaker["id"], "query": query}
                hits = await web_search(client, query)
                research = {"query": query, "results": hits}
                yield "search_results", {"id": speaker["id"], "query": query, "results": hits}

        # 3. Speaking (streamed) ------------------------------------------
        messages = build_speak_messages(speaker, topic, details, transcript, round_no, total, research)
        buffer = []
        try:
            async for delta in _stream(client, key, model, messages):
                buffer.append(delta)
                yield "token", {"id": speaker["id"], "text": delta}
        except Exception as e:
            yield "error", {"message": _friendly_error(e)}
            return
        text = "".join(buffer).strip() or "(stayed silent)"
        yield "turn_end", {"id": speaker["id"], "name": speaker["name"], "text": text}

        # 4. Consensus check ----------------------------------------------
        # Don't bother checking until the room has had a real exchange.
        consensus, reason = False, ""
        if len(history) + 1 >= 3:
            new_transcript = transcript + f"\n[{speaker['name']}]: {text}"
            try:
                raw = await _complete(
                    client, key, model,
                    build_consensus_messages(topic, details, new_transcript),
                    max_tokens=40, temperature=0.0,
                )
                m = _CONSENSUS_RE.search(raw)
                if m:
                    consensus = m.group(1).upper() == "YES"
                    reason = m.group(2).strip().split("\n")[0][:100]
            except Exception:
                pass
        yield "verdict", {"consensus": consensus, "reason": reason}


async def run_decision(topic, details, history, key=None, model=None):
    """Stream the facilitator's closing decision."""
    key = resolve_key(key)
    model = resolve_model(model)
    async with httpx.AsyncClient() as client:
        try:
            async for delta in _stream(
                client, key, model,
                build_decision_messages(topic, details, _render(history or [])),
                max_tokens=600, temperature=0.3,
            ):
                yield "decision_token", {"text": delta}
        except Exception as e:
            yield "error", {"message": _friendly_error(e)}
    yield "done", {}
