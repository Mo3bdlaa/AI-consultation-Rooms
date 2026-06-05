"""Model I/O and per-round logic for the consultation room.

This module only knows how to talk to OpenRouter and turn one round of a meeting
into events. The long-running meeting loop and its state live in `meetings.py`.

Each round:
  1. Organizer - one call scores every persona AND judges consensus
  2. Research  - (optional) the chosen speaker uses the web-search tool
  3. Speaking  - the chosen speaker's reply is streamed token-by-token
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
    build_decision_messages,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Fast, non-reasoning, free model that reliably follows the output format.
DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"

_RETRYABLE = {429, 500, 502, 503, 529}
_MAX_RETRIES = 4


def resolve_model(model):
    return (model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL).strip()


def resolve_key(key):
    key = (key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "No OpenRouter API key. Open Settings and paste your key "
            "(get one free at openrouter.ai/keys)."
        )
    return key


def _headers(key):
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": os.environ.get("APP_TITLE", "AI Consultation Rooms"),
    }


def friendly_error(e):
    msg = str(e)
    if "429" in msg:
        return (
            "OpenRouter rate-limited you (429), even after retries. Free models "
            "have tight per-minute/daily limits. Wait ~30–60s and try again, lower "
            "the rounds, or pick a paid model in Settings."
        )
    if "401" in msg or "403" in msg:
        return "OpenRouter rejected the API key (401/403). Check the key in Settings."
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
    body = {
        "model": model, "messages": messages, "max_tokens": max_tokens,
        "temperature": temperature, "reasoning": {"exclude": True},
    }
    for attempt in range(_MAX_RETRIES + 1):
        resp = await client.post(OPENROUTER_URL, headers=_headers(key), json=body, timeout=60)
        if resp.status_code in _RETRYABLE and attempt < _MAX_RETRIES:
            await asyncio.sleep(_backoff(resp, attempt))
            continue
        resp.raise_for_status()
        return _strip_think(resp.json()["choices"][0]["message"]["content"]).strip()


async def stream_completion(client, key, model, messages, max_tokens=400, temperature=0.85):
    """Yield content chunks from a streaming completion (retries on 429/5xx)."""
    body = {
        "model": model, "messages": messages, "max_tokens": max_tokens,
        "temperature": temperature, "stream": True, "reasoning": {"exclude": True},
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


# --- Web search tool --------------------------------------------------------
_SNIPPET_RE = re.compile(r'result__snippet[^>]*>(.*?)</a>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SEARCH_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _clean_html(s):
    import html as _html
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub("", s))).strip()


async def web_search(client, query, max_results=4):
    """Free, key-less web search via DuckDuckGo's HTML results endpoint."""
    try:
        r = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query}, headers=_SEARCH_HEADERS, timeout=20, follow_redirects=True,
        )
        snippets = [_clean_html(s) for s in _SNIPPET_RE.findall(r.text)]
        return [s for s in snippets if s][:max_results]
    except Exception:
        return []


# --- Parsing helpers --------------------------------------------------------
def render_transcript(history):
    return "\n".join(f"[{m['name']}]: {m['text']}" for m in history)


def _strip_think(text):
    """Drop reasoning blocks some models leak into content (<think>...</think>)."""
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
    return text.strip()


def _score_pattern(name):
    # name, then the first 0-10 near it; tolerates | : - /10 markdown bold etc.
    return re.compile(
        rf"\**{re.escape(name)}\**\s*[\|:\-–>\)]*\s*\(?\s*(10|[0-9])(?!\d)\s*(?:/\s*10)?\s*[\|:\-–\)]*\s*([^\n]*)",
        re.IGNORECASE,
    )


_CONSENSUS_RE = re.compile(r"CONSENSUS\s*[\|:\-–]\s*(YES|NO)\s*[\|:\-–]?\s*([^\n]*)", re.IGNORECASE)


def parse_moderator(raw):
    """Returns (scores: {id:(urgency,reason)}, matched:int, consensus:bool, reason:str)."""
    raw = _strip_think(raw)
    scores, matched = {}, 0
    for p in PERSONAS:
        m = _score_pattern(p["name"]).search(raw)
        if m:
            matched += 1
            urgency = max(0, min(10, int(m.group(1))))
            reason = m.group(2).strip().strip("|:-–) ").strip()[:80]
            scores[p["id"]] = (urgency, reason)
        else:
            scores[p["id"]] = (5, "")
    cm = _CONSENSUS_RE.search(raw)
    consensus = bool(cm and cm.group(1).upper() == "YES")
    creason = cm.group(2).strip()[:100] if cm else ""
    return scores, matched, consensus, creason


async def organizer(client, key, model, topic, details, transcript):
    """One call: scores everyone + consensus verdict."""
    try:
        raw = await _complete(
            client, key, model, build_moderator_messages(topic, details, transcript),
            max_tokens=220, temperature=0.5,
        )
    except Exception:
        raw = ""
    return parse_moderator(raw)


async def research_query(client, key, model, persona, topic, details, transcript):
    """Ask the speaker whether to search; returns a query string or None."""
    try:
        decision = await _complete(
            client, key, model,
            build_research_messages(persona, topic, details, transcript),
            max_tokens=40, temperature=0.3,
        )
    except Exception:
        return None
    if decision.strip().upper().startswith("QUERY:"):
        return decision.split(":", 1)[1].strip()[:120]
    return None


def speak_messages(persona, topic, details, transcript, round_no, total, research):
    return build_speak_messages(persona, topic, details, transcript, round_no, total, research)


def decision_messages(topic, details, history):
    return build_decision_messages(topic, details, render_transcript(history))
