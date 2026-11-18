# Joshy

Joshy is a personal reservation sniper. Tell it which restaurants, dates and times you want. It checks Resy and SevenRooms around the clock. When a matching table opens it books it for you on Resy, or sends an urgent link for SevenRooms, and pings your phone.

## What it does

- **Watches**: a restaurant link, party size, dates (one day, a range, or "any Fri/Sat in December"), and a time window with an optional ideal time.
- **Resy**: connects with your Resy email and password. When a slot in your window opens, Joshy books the one closest to your ideal time using the card on your Resy account. If someone grabs it first, Joshy tries the next best slot.
- **SevenRooms**: checks availability and sends an urgent alert that opens the booking page on the right date and party size. It doesn't auto-book, because SevenRooms has no login for diners and its checkout usually puts a hold on your card.
- **Notifications** through [ntfy](https://ntfy.sh) (easiest), Pushover, or email.
- **Dashboard** that works on your phone. You can add, pause, resume and delete watches, run a check on demand, and see an activity log.
- Credentials are encrypted at rest. The dashboard is behind a password.

## Quick start

```bash
cp .env.example .env        # set JOSHY_PASSWORD and JOSHY_NTFY_TOPIC at minimum
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
joshy serve                 # http://127.0.0.1:8000  (user: joshy, password: JOSHY_PASSWORD)
```

Then:

1. Open **Connections** and sign in to Resy. If you only use Google or Apple sign-in, set a Resy password first in the Resy app.
2. Install the **ntfy** app on your phone. Subscribe to the topic you put in `JOSHY_NTFY_TOPIC`, then press **Send a test**.
3. On **Watches**, paste a restaurant link (for example `https://resy.com/cities/new-york-ny/venues/carbone` or `https://www.sevenrooms.com/reservations/<venue>`), pick dates and times, and press **Start watching**.

Joshy only works while it's running. To keep it running all the time, put it on an always-on machine: a small cloud VM, a Raspberry Pi, or a home server.

```bash
docker compose up -d --build   # uses .env, stores data in ./data
```

If it's reachable from the internet, put it behind HTTPS (Caddy, Cloudflare Tunnel, Tailscale) because it holds your Resy login.

## Commands

| Command | What it does |
| --- | --- |
| `joshy serve [--host 0.0.0.0] [--port 8000]` | Dashboard and background watcher |
| `joshy check` | Check every active watch once and exit |
| `joshy test-notify` | Send a test notification |

## How it works

```
joshy/
  engine.py          watcher loop: poll -> match window -> book or alert -> mark done
  providers/resy.py  Resy web API: login, venue lookup, /4/find, /3/details, /3/book
  providers/sevenrooms.py  SevenRooms widget availability feed
  notify.py          ntfy / Pushover / email
  db.py              SQLite + Fernet-encrypted credentials (data/joshy.db, data/secret.key)
  web.py, templates/ FastAPI dashboard
```

Each watch is checked on its own interval (default 60s, with ±15% jitter so requests don't look robotic). Dates that have passed are dropped, and a watch with no dates left expires. Once a watch books, it stops.

## Caveats

- Resy and SevenRooms don't publish these APIs. Joshy uses the same requests their websites make. If they change them, the matching provider file needs updating.
- Automated booking may break Resy's or SevenRooms' terms of service. Keep polling gentle (60s or slower is sensible), use it only for yourself, and accept that an account could be flagged.
- Resy charges no-show and cancellation fees on some venues. Joshy shows the cancellation policy with each booking it makes.

## Tests

```bash
pip install -e '.[dev]' && pytest
```
