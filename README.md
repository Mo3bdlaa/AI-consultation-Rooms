# 🪑 AI Consultation Rooms

A proof-of-concept where several AI personas sit in a "room", debate a topic you
give them, **search the web**, **challenge each other** instead of just agreeing,
and **stop once they reach consensus**. You watch it happen live in the browser,
including which persona "raises a hand" to speak each round.

## How it works

Each round:

```
1. Bidding   → every persona scores how urgently it wants to speak (0–10) + why
2. Research  → the chosen speaker may use the web-search tool (DuckDuckGo)
3. Speaking  → the chosen speaker's reply is streamed token-by-token
4. Consensus → a neutral Facilitator judges whether the room has converged
```

The meeting ends as soon as the Facilitator detects genuine consensus, or when
the max-round cap is hit (whichever comes first). Then the Facilitator writes a
closing decision (agreements / open disagreements / recommendation).

The loop is driven **from the browser**: it holds the transcript and calls the
server once per round. That keeps every request short, which is what lets it run
on serverless (Vercel).

### Files

| File | Role |
|------|------|
| `personas.py` | The 4 personas + `HOUSE_RULES` (force disagreement, then converge) + all prompt builders |
| `room.py` | One round of orchestration: bidding, the search tool, streamed speaking, consensus check |
| `app.py` | FastAPI: `POST /api/step` (one round) and `POST /api/decision` (closing) |
| `static/index.html` | Live chat UI: raised-hands panel, streamed messages, "🔎 searched the web" notes |
| `api/index.py`, `vercel.json` | Vercel serverless entrypoint + config |

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py            # open http://localhost:8000
```

Paste your OpenRouter key in the field at the top of the page (get one free at
<https://openrouter.ai/keys>). The key is sent per-request and never stored
server-side. You can also set `OPENROUTER_API_KEY` in a `.env` file instead.

## Model & cost

Defaults to a **free** model: `meta-llama/llama-3.3-70b-instruct:free`. Change it
with `OPENROUTER_MODEL` in `.env`, or list more at <https://openrouter.ai/models>.

Calls per round = **1 organizer (scores everyone) + 1 research + 1 speak + 1
consensus = ~4**, plus 1 for the final decision. A meeting that converges in ~5
rounds ≈ **21 requests**. Calls are made **sequentially** (no parallel bursts)
and **retry with backoff on 429**, which keeps it within free-tier limits. To
use even less: lower the round cap, or set `ENABLE_SEARCH = False` in `room.py`.

> Free OpenRouter models are rate-limited per minute and per day. If you still
> see a 429, wait ~30–60s, lower the round count, or switch `OPENROUTER_MODEL`
> to a paid model.

## Deploy to Vercel

The repo is Vercel-ready (`api/index.py` + `vercel.json`). Push it and import the
repo on Vercel, or deploy from the CLI. No server secrets are required because
the API key is entered in the UI.

> Note: on Vercel's serverless runtime, a round's tokens may arrive in a batch at
> the end of that round rather than truly character-by-character; locally
> (`python app.py`) it streams live.

## Known limitations (it's a POC)

- Consensus is judged by an LLM, not a formal vote — it can be optimistic.
- Search uses DuckDuckGo HTML scraping (no key, but can rate-limit).
- Long meetings grow the context window (no mid-meeting summarization yet).
