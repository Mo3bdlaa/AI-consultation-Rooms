# Deploying the POC on Vercel

Everything is already wired for Vercel (see `vercel.json`, `app.py`, `db.py`).
You just connect the repo once.

## 1. Import the repo

1. Go to **https://vercel.com/new**
2. **Import Git Repository** → pick **`Mo3bdlaa/AI-consultation-Rooms`**
   (authorize Vercel for GitHub if it asks).
3. Leave the defaults — Vercel auto-detects the Python app from `vercel.json`.
   No build command or root-directory change needed.

## 2. Deploy the right branch

The code lives on the **`claude/lucid-fermat-suK5a`** branch. Vercel deploys
your *Production Branch* (defaults to `main`). Two options:

- **Easiest:** merge the branch into `main`, then Vercel deploys `main`.
- **Or:** after importing, open **Project → Settings → Git → Production Branch**,
  set it to `claude/lucid-fermat-suK5a`, and redeploy.

## 3. Environment variables (optional, recommended)

Add these under **Settings → Environment Variables**:

| Name         | Example value            | Why |
|--------------|--------------------------|-----|
| `SECRET_KEY` | any long random string   | signs the login cookie (don't keep the default) |
| `APP_USER`   | `admin`                  | change the demo username |
| `APP_PASS`   | `admin`                  | change the demo password |

You do **not** put the OpenRouter key here — it's entered in the app's
Settings panel and only held in memory while a meeting runs.

## 4. Use it

1. Open the deployment URL.
2. Sign in: **admin / admin** (or whatever you set above).
3. **⚙ Settings** → paste your OpenRouter key → Save.
4. **+ New meeting** → enter a topic → Start.
5. Past meetings show in the left sidebar — click one to re-watch the full
   transcript and decision (loaded from the database).

## POC limitations (by design)

- **Serverless time limit:** each function run is capped at 60s (`maxDuration`
  in `vercel.json`, the Hobby ceiling). A long meeting can get cut off — for a
  smooth demo keep **Rounds low (≈4)** and use the fast free model. If it cuts
  off, the partial transcript is still saved.
- **History storage:** meetings are saved in SQLite at `/tmp/rooms.db`. That
  survives while the function instance stays warm, but a cold start wipes it.
  For permanent history, point `DB_PATH` at a durable database (e.g. a
  libSQL/Turso URL via a small adapter) — ask and I'll wire it up.
- **No background running:** a meeting only advances while its stream request is
  open (serverless can't keep a task alive after the response). This was the
  agreed trade-off for hosting it on Vercel.
