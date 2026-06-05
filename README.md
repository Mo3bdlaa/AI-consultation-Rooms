# 🪑 AI Consultation Rooms

A proof-of-concept where several AI personas sit in a "room", debate a topic you
give them, optionally **search the web**, **challenge each other** instead of just
agreeing, and **stop once they reach consensus**.

Meetings run **server-side in a background task**, so you can start one, close the
tab, and reconnect later to watch — the server keeps the discussion going.

## How it works

Each round:

```
1. Organizer → one call scores how urgently each persona wants to speak (0–10)
               AND judges whether the room has reached consensus
2. Research  → (optional) the chosen speaker uses the web-search tool
3. Speaking  → the chosen speaker's reply is streamed token-by-token
```

The meeting ends when the organizer detects genuine consensus, or when the max
round cap is hit. Then the Facilitator streams a closing decision (agreements /
open disagreements / recommendation).

### Files

| File | Role |
|------|------|
| `personas.py` | The 4 personas + `HOUSE_RULES` + all prompt builders |
| `room.py` | Model I/O (OpenRouter, retry/backoff), the search tool, parsing |
| `meetings.py` | Server-side engine: background meeting loop + in-memory store + event log |
| `app.py` | FastAPI: start / list meetings, and a reconnectable SSE stream per meeting |
| `static/index.html` | UI: sidebar (New meeting, Settings, meeting list) + live room view |
| `render.yaml`, `Procfile` | Deploy config for a persistent host |

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py            # open http://localhost:8000
```

Click **⚙ Settings**, paste your OpenRouter key (free at
<https://openrouter.ai/keys>) and pick a model. The key is sent to your own
server and kept in memory only while meetings run; it's also saved in your
browser for convenience.

## Model & speed

Default is a **fast, free, non-reasoning** model: `meta-llama/llama-3.3-70b-instruct:free`.
You can switch models in Settings (Gemini Flash is fastest; DeepSeek/Qwen/GLM are
alternatives). Reasoning models (like GLM) are slower and sometimes return odd
formatting, which is why the default is a plain instruct model.

Calls per round = **1 organizer + 1 speak** (+1 for research if web search is on).
Calls are sequential and retry with backoff on 429, to stay within free-tier limits.

## Deploy (persistent host required)

Because meetings run in a background task, this needs a host that runs a real
process — **not** Vercel/serverless (a serverless function is killed once it
returns, so the meeting can't keep running). The repo includes `render.yaml`,
so the easy path is [Render](https://render.com):

1. Push this repo to GitHub.
2. On Render: **New → Blueprint**, pick the repo. It reads `render.yaml` and
   creates a free web service (`uvicorn app:app`).
3. Open the service URL, go to **Settings**, paste your OpenRouter key.

> Free hosts sleep after idle time and state is in-memory, so meetings are lost
> if the service restarts. Fine for a POC; add a database for durability.

## Known limitations (it's a POC)

- In-memory state: meetings don't survive a process restart.
- The API key lives in server memory for the meeting's duration.
- Consensus is judged by an LLM, not a formal vote.
- Web search scrapes DuckDuckGo HTML (no key, but can rate-limit).
