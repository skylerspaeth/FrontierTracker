"""Telegram deal alerts.

After each scan result is stored, `on_check` looks at the GoWild flights in it and decides
which are new deals under the configured rules:

  1. under $50 to a destination the origin has no nonstop flight to (nonstop destinations are
     routinely $15-$31, so they would flood the chat; the set is derived from our own data),
  2. any international destination under $100, or Puerto Rico under a lower cap ($85, because
     ordinary in-window one-stops there cost ~$91-$97).

Matches are collected, and once every scan job of a watcher cycle has finished, one digest message
per origin is sent (`flush_when_done`). A flight alerts when it is new, when its price falls by at
least `drop` dollars below the price we last alerted at, or when a seat that closed comes back.
The first watch interval after notifications are switched on is a silent baseline, so a fresh or
empty database doesn't send everything that's already open.

Setup: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment (see README). Without them
this module does nothing. Rules live in data/watch.json under "notify" (all optional):
    {"under": 50, "international": 100, "puerto_rico": 85, "drop": 10}
`python notify.py --test` sends a sample digest to check the setup.
"""
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date

import frontier
import store

DEFAULTS = {"under": 50, "international": 100, "puerto_rico": 85, "drop": 10}
WATCH_PATH = os.path.join(frontier.DATA_DIR, "watch.json")
MAX_MESSAGE = 3800          # Telegram's hard limit is 4096 characters
BASELINE_MIN_MINUTES = 60
FLUSH_POLL_SECONDS = 5
FLUSH_GIVE_UP_SECONDS = 6 * 3600

_lock = threading.Lock()
_pending = {}               # origin -> {(dest, day, key): alert dict}
_nonstop_cache = {}         # origin -> (fetched_at, set)


# ---------------------------------------------------------------- config

def _token():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _chat():
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def enabled():
    return bool(_token() and _chat())


def _watch():
    try:
        with open(WATCH_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def config():
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (_watch().get("notify") or {}).items() if k in DEFAULTS})
    return cfg


# ---------------------------------------------------------------- names and matching

def place(st):
    """'Los Angeles, CA' for domestic; 'San Juan, Puerto Rico' / 'Bogota, Colombia' for Puerto Rico
    and international. Frontier's country names carry suffixes ("Colombia, Republic of"): keep the
    part before the first comma."""
    city = (st.get("city") or st.get("code") or "").strip()
    if st.get("country") in ("PR", "VI") or not st.get("domestic", True):
        country = (st.get("countryName") or st.get("country") or "").split(",")[0].strip()
        return f"{city}, {country}" if country else city
    state = (st.get("state") or "").strip()
    if state and not re.search(r",\s*" + re.escape(state) + r"$", city):
        return f"{city}, {state}"
    return city


def classify(flight, dest_station, nonstop_dests, cfg):
    """Which rule a GoWild flight matches: 'under', 'intl', or None."""
    price = flight.get("gowild_price")
    if not flight.get("gowild") or price is None:
        return None
    code = dest_station.get("code")
    if price < cfg["under"] and code not in nonstop_dests:
        return "under"
    if dest_station.get("country") == "PR":
        return "intl" if price < cfg["puerto_rico"] else None
    if not dest_station.get("domestic", True):
        return "intl" if price < cfg["international"] else None
    return None


def _nonstop(origin):
    now = time.time()
    hit = _nonstop_cache.get(origin)
    if hit and now - hit[0] < 300:
        return hit[1]
    s = store.nonstop_dests(origin)
    _nonstop_cache[origin] = (now, s)
    return s


# ---------------------------------------------------------------- collecting

def on_check(job, dest, result):
    """Called by the scanner after a route's check is stored (never raises into the scan).
    `result` is frontier.get_flights() output, or {'flights': []} for a route with no service."""
    if not enabled():
        return
    origin, day = job.origin, job.day.isoformat()
    flights = [f for f in (result or {}).get("flights", []) if f.get("gowild") and f.get("gowild_price") is not None]
    open_keys = [f["key"] for f in flights]
    store.alerts_close_missing(origin, dest, day, open_keys)
    if not getattr(job, "notify", False) or not flights:
        return
    cfg = config()
    stations = frontier.get_markets()["stations"]
    st = stations.get(dest)
    if not st:
        return
    started = store.meta_get("notify_started")
    if started is None:
        started = time.time()
        store.meta_set("notify_started", started)
    baseline = time.time() - float(started) < max(BASELINE_MIN_MINUTES, int(_watch().get("interval_minutes", 120))) * 60
    nonstop = _nonstop(origin)
    known = store.alerts_for(origin, dest, day)
    for f in flights:
        if not classify(f, {**st, "code": dest}, nonstop, cfg):
            continue
        price, row = f["gowild_price"], known.get(f["key"])
        if row is None:
            reason, was = "new", None
        elif not row["open"]:
            reason, was = "reopened", None
        elif row["last_price"] is not None and price <= row["last_price"] - cfg["drop"]:
            reason, was = "drop", row["last_price"]
        else:
            continue
        if baseline:
            store.alerts_set(origin, dest, day, f["key"], price)    # note it, don't send it
            continue
        with _lock:
            _pending.setdefault(origin, {})[(dest, day, f["key"])] = {
                "dest": dest, "day": day, "key": f["key"], "price": price, "reason": reason, "was": was,
                "rule": classify(f, {**st, "code": dest}, nonstop, cfg), "via": f.get("via") or [],
                "layovers": f.get("layovers"),
                "seats": f.get("gowild_seats")}


# ---------------------------------------------------------------- message

def _day_label(day):
    d = date.fromisoformat(day)
    return f"{d:%b} {d.day}"


def _money(p):
    """Whole dollars, rounded to the nearest (halves up): 46.5 -> $47."""
    return f"${int(p + 0.5)}"


def _duration(minutes):
    return f"{minutes // 60}h{minutes % 60}m"


def _via(alert):
    """'via DEN (14h9m) - MCO (0h50m)': connection airports with the time spent at each. Flights saved
    before layover times were recorded show the airports alone."""
    stops = alert.get("layovers")
    if not stops or len(stops) != len(alert["via"]):
        stops = [{"station": v, "minutes": None} for v in alert["via"]]
    return "via " + " - ".join(
        html.escape(s["station"]) + (f" ({_duration(s['minutes'])})" if s.get("minutes") is not None else "")
        for s in stops)


def build_digest(origin, alerts, stations, cfg=None):
    """Telegram HTML messages (a list: chunked under the size limit) for one origin's new deals."""
    cfg = cfg or config()
    # one line per (destination, date): the cheapest flight, noting any others
    lines = {}
    for a in alerts:
        k = (a["rule"], a["dest"], a["day"])
        lines.setdefault(k, []).append(a)
    sections = {"under": [], "intl": []}
    for (rule, dest, day), group in lines.items():
        best = min(group, key=lambda a: a["price"])
        st = stations.get(dest, {"city": dest})
        url = frontier.search_url(origin, dest, date.fromisoformat(day))
        name = place({**st, "code": dest}) + f" ({dest})"
        bits = [f'<a href="{html.escape(url, quote=True)}">{html.escape(name)}</a>',
                _money(best["price"]), _day_label(day)]
        if best["via"]:
            bits.append(_via(best))
        elif rule == "intl":
            bits.append("nonstop")
        if best["reason"] == "reopened":
            bits.append("🔁 reopened")
        if len(group) > 1:
            bits.append(f"+{len(group) - 1} more")
        sections[rule].append((best["price"], day, "• " + " · ".join(bits)))
    titles = {"under": f"🔥 Under {_money(cfg['under'])} (connections)", "intl": "🌎 International / Offshore"}
    origin_name = place({**stations.get(origin, {"city": origin}), "code": origin})
    head = f"🛫 New deals from {html.escape(origin_name)} ({html.escape(origin)})"
    foot = "Tap a deal and switch to the GoWild tab to book."
    body = []
    for rule in ("under", "intl"):
        if sections[rule]:
            body.append("")
            body.append(titles[rule])
            body.extend(t for _, _, t in sorted(sections[rule]))
    messages, cur = [], [head]
    for line in body:
        if sum(len(x) + 1 for x in cur) + len(line) + len(foot) > MAX_MESSAGE and len(cur) > 1:
            messages.append("\n".join(cur))
            cur = [head + " (cont.)", line] if line else [head + " (cont.)"]
        else:
            cur.append(line)
    cur += ["", foot]
    messages.append("\n".join(cur))
    return messages


# ---------------------------------------------------------------- sending

def send(text):
    """Send one message. Returns True on success; failures are logged, never raised."""
    body = json.dumps({"chat_id": _chat(), "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{_token()}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("ok", False)
    except urllib.error.HTTPError as e:
        print(f"telegram: HTTP {e.code}: {e.read()[:200]!r}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"telegram: {e}", file=sys.stderr, flush=True)
    return False


def flush(origin):
    """Send the pending deals for `origin` as one digest and remember them. If sending fails the
    flights aren't remembered, so they are picked up again on the next cycle."""
    with _lock:
        items = list((_pending.pop(origin, None) or {}).values())
    if not items or not enabled():
        return 0
    stations = frontier.get_markets()["stations"]
    ok = True
    for i, msg in enumerate(build_digest(origin, items, stations)):
        if i:
            time.sleep(1.1)     # Telegram allows about one message a second per chat
        ok = send(msg) and ok
    if ok:
        for a in items:
            store.alerts_set(origin, a["dest"], a["day"], a["key"], a["price"])
    return len(items) if ok else 0


def flush_when_done(origin, jobs):
    """Background thread: wait for this cycle's scan jobs to finish, then send one digest."""
    def run():
        deadline = time.time() + FLUSH_GIVE_UP_SECONDS
        while time.time() < deadline and any(j.state not in ("done", "stopped") for j in jobs):
            time.sleep(FLUSH_POLL_SECONDS)
        try:
            flush(origin)
        except Exception as e:
            print(f"notify flush failed: {e}", file=sys.stderr, flush=True)
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- `python notify.py --test`

def _sample():
    stations = {
        "AUS": {"city": "Austin", "state": "TX", "country": "US", "countryName": "United States of America", "domestic": True},
        "BOI": {"city": "Boise", "state": "ID", "country": "US", "countryName": "United States of America", "domestic": True},
        "BOG": {"city": "Bogota", "state": "", "country": "CO", "countryName": "Colombia, Republic of", "domestic": False},
        "SJU": {"city": "San Juan", "state": "", "country": "PR", "countryName": "Puerto Rico", "domestic": True},
    }
    d = date.today().isoformat()
    base = {"day": d, "key": "x", "seats": None, "was": None, "via": ["DEN"],
            "layovers": [{"station": "DEN", "minutes": 200}]}
    alerts = [{**base, "dest": "BOI", "price": 47.2, "reason": "new", "rule": "under"},
              {**base, "dest": "BOG", "price": 93.53, "reason": "new", "rule": "intl", "seats": 3,
               "via": ["DEN", "MCO"], "layovers": [{"station": "DEN", "minutes": 849}, {"station": "MCO", "minutes": 50}]},
              {**base, "dest": "SJU", "price": 79.0, "reason": "drop", "was": 96.61, "rule": "intl", "via": ["MCO"],
               "layovers": [{"station": "MCO", "minutes": 50}]}]
    return stations, alerts


if __name__ == "__main__":
    if "--test" not in sys.argv:
        print(__doc__)
        sys.exit(0)
    if not enabled():
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first (see README).")
    stations, alerts = _sample()
    msgs = build_digest("AUS", alerts, stations, dict(DEFAULTS))
    print("sent" if all(send(m) for m in msgs) else "failed (see message above)")
