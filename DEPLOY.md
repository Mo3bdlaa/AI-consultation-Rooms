# Deploying on your own VPS (Docker)

These are copy-paste steps you run **on your server over SSH**. Replace
`YOUR_SERVER_IP` and (optionally) the domain.

## 1. Connect to the server

```bash
ssh root@YOUR_SERVER_IP
```

## 2. Install Docker (once)

```bash
curl -fsSL https://get.docker.com | sh
```

That installs Docker Engine + the Compose plugin. Verify:

```bash
docker --version && docker compose version
```

## 3. Open the firewall (if ufw is on)

```bash
ufw allow 80
ufw allow 443
ufw allow OpenSSH
```

## 4. Get the code

```bash
apt-get update && apt-get install -y git
git clone -b claude/lucid-fermat-suK5a https://github.com/Mo3bdlaa/AI-consultation-Rooms.git
cd AI-consultation-Rooms
```

## 5a. Run on the server IP (no domain, plain HTTP)

```bash
docker compose up -d --build
```

Open: `http://YOUR_SERVER_IP` → **⚙ Settings** → paste your OpenRouter key.

## 5b. Run on a domain (automatic HTTPS)

First point an **A record** for your domain to `YOUR_SERVER_IP` in your DNS.
Then:

```bash
echo "SITE_ADDRESS=rooms.example.com" > .env
docker compose up -d --build
```

Caddy fetches a free HTTPS certificate automatically. Open
`https://rooms.example.com`.

## Everyday commands

```bash
docker compose logs -f          # watch logs
docker compose ps               # status
docker compose restart          # restart
docker compose down             # stop

# update after new code is pushed:
git pull
docker compose up -d --build
```

## Notes

- The OpenRouter key is entered in the app's Settings (stored in your browser
  and held in server memory only while a meeting runs). Nothing secret is in
  this repo.
- State is in memory, so meetings reset if the container restarts. Ask me to
  add a database (Postgres/SQLite) when you want meetings to persist.
- This is the only service using ports 80/443. If you already run something
  there, tell me and I'll change the ports or put it behind your existing proxy.
