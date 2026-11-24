# GoWild Tracker

Map of every Frontier destination from an airport, with live GoWild seat availability,
pass prices, flight times, and seat history. All data comes from flyfrontier.com.

## Run

Double-click `start.bat`, or:

    python server.py        # then open http://localhost:8777

Needs Python 3.11+ and `pip install -r requirements.txt`.

## How it works

| Frontier source | What we use it for |
|---|---|
| `booking.flyfrontier.com/Resource/GetMarkets` | All 123 stations and every city pair Frontier sells (cached 24 h) |
| `booking.flyfrontier.com/Flight/InternalSelect` | The flight-select page. Its embedded `FlightData` JSON has each itinerary's GoWild fare, whether GoWild is open, and the "N seats left" text |

- **Price shown** is Frontier's own GoWild price for that itinerary: the $0.01 fare plus taxes
  and fees (about $15.41 nonstop domestic, more with connections, $85+ international).
- **Windows**: domestic GoWild opens the day before departure; international 10 days before.
  Dates later than that show **early-booking** fares
  (Frontier sells some flights ahead of the window for an extra charge), so you can check any
  date up to 10 days out; the picker labels them "early booking". There are no blackout dates any more, only
  **peak days** (Frontier may add a peak-day charge; seats are still bookable). The picker marks
  them, and `peakdays.py` re-reads the list from Frontier's GoWild page once a day (a copy is
  built in as a fallback).
- **Which routes are real**: Frontier's route list (`GetMarkets`) names every pair it has ever
  loaded, far more than it flies. The source of truth here is Frontier's own booking calendar for
  each route (the greyed-out dates when you pick a date on flyfrontier.com). The background tracker
  reads those calendars for every route from a watched airport (about 80 requests every 3 days,
  paced) and the app uses them to skip routes that don't fly a given date ("no service until
  Mar 4") and to hide routes with no flying dates left at all.
- **Tracking**: every check is saved in `data/gowild.db`. The flight list shows when each
  GoWild seat was first seen open, the price range seen, and seats that have since closed.
  Turn on **Auto-track** for an airport to re-check today's and tomorrow's flights every 2 hours while
  the app runs.
- **Return trips**: open a destination and pick a return date; it checks that single route.

## Telegram deal alerts

The background tracker can message you when a new deal shows up. It sends one digest per scan
cycle (nothing if there's nothing new), grouped by rule:

- **Under $50** to any destination the origin has no nonstop flight to (nonstop destinations are
  always cheap, so they'd flood the chat; the list comes from your own scan history)
- **International under $100**, or **Puerto Rico under $85**

A flight alerts when it is new, when its price drops $10 or more below what you were last told,
or when a seat that sold out reopens. The first watch interval after you switch alerts on is a
silent baseline, so what's already open when you start doesn't produce a flood. Manual "Check"
clicks never send alerts; only the background tracker does.

Setup:

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the bot token.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id`.
3. Give the app two environment variables (never commit them): `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID`. Without them alerts are off.
4. Check it with `python notify.py --test` (sends a sample digest).

The rules can be changed in `data/watch.json` without a redeploy (all optional, defaults shown):

```json
"notify": {"under": 50, "international": 100, "puerto_rico": 85, "drop": 10}
```

## Tests

None of the tests touch the network or your real data (`data/gowild.db` is never used).

```bash
python -m unittest discover -s tests     # Python: alerts, parsing, scan planning, booking rules, history queries
node --test tests/departed.test.js       # JavaScript: hiding flights that have already departed
```

The Python tests need only the standard library (`curl_cffi` is stubbed). The JavaScript test needs
Node 18+ and no packages.

## Rate limits

Frontier's site is behind DataDome bot protection. Bursts of requests get the IP blocked
(HTTP 406) for a while. The tracker therefore sends one request at a time, 4-7 seconds
apart, from one persistent session, so a full airport (~90 routes) takes 6-10 minutes.
If Frontier refuses a request, the scan stops and waits 30 minutes before allowing another.

## Files

- `frontier.py` reads Frontier's site
- `scanner.py` runs scans, pacing and block handling
- `store.py` SQLite history
- `notify.py` Telegram deal alerts
- `tests/` unit tests, see [Tests](#tests)
- `rules.py` GoWild windows
- `peakdays.py` peak-day dates, scraped from Frontier's GoWild page
- `server.py` local web server and API
- `static/index.html` the map UI
- `static/departed.js` timezone-aware "has this flight left" check (shared by the UI and its test)
- `data/airports.json` airport coordinates (OurAirports)
