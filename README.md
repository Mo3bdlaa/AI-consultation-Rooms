# 🪑 AI Consultation Rooms

A proof-of-concept where several AI personas sit in a "room", debate a topic you
give them, **challenge each other** instead of just agreeing, and reach a
decision. You watch it happen live in the browser, including which persona
"raises a hand" to speak each round.

## How it works

```
┌─────────────────────────────────────────────┐
│  Each round:                                 │
│  1. Bidding  → every persona scores how       │
│     urgently it wants to speak (0–10) + why   │
│  2. The highest bidder speaks (its reply is   │
│     streamed token-by-token to the screen)    │
│  3. Repeat for N rounds                        │
│  4. A neutral Facilitator forces a decision   │
└─────────────────────────────────────────────┘
```

- **Personas** live in `personas.py` (Maya, Karim, Lina, Omar). Each has a
  system prompt. Shared `HOUSE_RULES` push them to disagree, raise objections,
  and avoid the usual "AI agrees with everyone" behaviour.
- **Orchestration** lives in `room.py` (the round loop, bidding, streaming).
- **Web layer** is `app.py` (FastAPI + Server-Sent Events) and `static/index.html`.
- **Model access** is via [OpenRouter](https://openrouter.ai) — one HTTP API for
  many models.

## Run it

```bash
# 1. create a virtual env and install deps
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. add your OpenRouter key
cp .env.example .env
#    then edit .env and paste your key (https://openrouter.ai/keys)

# 3. start it
python app.py
```

Open <http://localhost:8000>, type a topic, and press **Start meeting**.

## Cost / your 1000-requests-a-day limit

Per round the app makes **N bid calls + 1 speaking call** (N = number of
personas, currently 4 → 5 calls/round). The final decision is 1 more call.
A 6-round meeting ≈ **31 requests**. With 1000/day you can run ~30 meetings.

To use less: lower the round count, remove personas, or switch to a `:free`
model in `.env` (set `OPENROUTER_MODEL`).

## Tweak it

- **Personas / their personalities** → `personas.py`
- **Number of rounds** → the field in the UI (or the `rounds` query param)
- **Make debate sharper / softer** → edit `HOUSE_RULES` in `personas.py`
- **Model** → `OPENROUTER_MODEL` in `.env`

## Known limitations (it's a POC)

- Termination is just "max rounds", not true convergence detection.
- No tools / web access yet, so personas can hallucinate facts.
- Long meetings grow the context window (no summarization mid-meeting yet).
- Bidding uses the same model; a cheaper model could be used for bids to save cost.
