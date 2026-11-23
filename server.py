"""GoWild tracker: local web app.  Run:  python server.py   then open http://localhost:8777"""
import json
import os
import sys
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import frontier
import notify
import peakdays
import rules
import scanner
import store

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
WATCH_PATH = os.path.join(ROOT, "data", "watch.json")
PORT = int(os.environ.get("GOWILD_PORT", "8777"))
HOST = os.environ.get("GOWILD_HOST", "127.0.0.1")   # the Docker image sets 0.0.0.0; publish the port on loopback only

with open(os.path.join(ROOT, "data", "airports.json"), encoding="utf-8") as f:
    AIRPORTS = json.load(f)


# ---------------------------------------------------------------- watchlist / auto-refresh

def load_watch():
    try:
        with open(WATCH_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"origins": [], "interval_minutes": 120}


def save_watch(w):
    with open(WATCH_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(w, f, indent=1)
    os.replace(WATCH_PATH + ".tmp", WATCH_PATH)


def _intl_dests(origin):
    """International destinations Frontier sells from `origin` (their GoWild window is 10 days)."""
    m = frontier.get_markets()
    return [d for d in m["routes"].get(origin, []) if not m["stations"].get(d, {}).get("domestic", True)]


def watcher():
    """Re-scan watched airports on an interval, so seat history builds up: tomorrow for every
    destination (domestic GoWild opens the day before), today as well (seats keep opening and
    selling out on the day itself), plus every date through 10 days out for the international
    destinations only (their window opens 10 days ahead)."""
    last, jobs = {}, {}
    while True:
        w = load_watch()
        every = max(30, int(w.get("interval_minutes", 120))) * 60
        for o in w.get("origins", []):
            j = jobs.get(o)
            if j and j.state == "stopped":   # blocked: retry once the cooldown ends, not in 2 h
                last.pop(o, None)
                jobs.pop(o)
            if time.time() - last.get(o, 0) >= every and not scanner.cooldown_left():
                last[o] = time.time()
                try:
                    frontier.refresh_schedules(o)   # Frontier's own calendar is our "is this route really flown" source
                except (frontier.FrontierError, OSError):
                    pass
                t = date.today()
                cycle = [scanner.start(o, t, label=f"auto {o} {t:%a %b %d} (today)", notify=True)[0]]
                d = t + timedelta(days=1)
                jobs[o], _ = scanner.start(o, d, label=f"auto {o} {d:%a %b %d}", notify=True)
                cycle.append(jobs[o])
                try:
                    intl = _intl_dests(o)
                    if intl:
                        # Days 0-1 are covered by the full scans above. Cover the rest of the window,
                        # skipping routes Frontier's calendar says don't fly that day (seasonal routes).
                        for n in range(2, rules.INTERNATIONAL_DAYS_AHEAD + 1):
                            di = t + timedelta(days=n)
                            dests = [x for x in intl if not frontier.schedule_gap(o, x, di, fetch=False)]
                            if dests:
                                cycle.append(scanner.start(o, di, dests, f"auto intl {o} {di:%a %b %d}", notify=True)[0])
                except frontier.FrontierError:
                    pass   # route map unavailable right now; the tomorrow scan above still runs
                notify.flush_when_done(o, cycle)   # one Telegram digest once this cycle's scans finish
        time.sleep(60)


# ---------------------------------------------------------------- API

def meta():
    m = frontier.get_markets()
    stations = []
    for code, s in m["stations"].items():
        a = AIRPORTS.get(code)
        if not a:
            continue
        stations.append({**s, "lat": a["lat"], "lon": a["lon"], "airport": a["name"],
                         "routes": m["routes"].get(code, [])})
    today = date.today()
    days = []
    peaks = peakdays.get()
    for d in rules.scan_dates(today):
        days.append({"date": d.isoformat(), "ahead": (d - today).days,
                     "peak": d in peaks,
                     "domestic_open": rules.bookable_now(d, True, today),   # standard window; later dates are early-booking
                     "international_open": rules.bookable_now(d, False, today)})
    return {"stations": sorted(stations, key=lambda s: s["label"]), "days": days,
            "markets_fetched_at": m["fetched_at"], "watch": load_watch(),
            "peak_days_fetched_at": peakdays.fetched_at(), "peak_reliable": peakdays.reliable()}


def results(origin, day):
    d = datetime.strptime(day, "%Y-%m-%d").date()
    m = frontier.get_markets()
    _, skipped = scanner.plan(origin, d, fetch=False)
    return {"origin": origin, "day": day, "routes": m["routes"].get(origin, []),
            "unserved": [c for c, why in skipped.items() if why == frontier.NO_SERVICE],
            "results": store.latest(origin, day), "skipped": skipped,
            "peak": rules.is_peak(d)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name, ctype):
        path = os.path.join(STATIC, name)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if u.path == "/api/meta":
                return self._json(meta())
            if u.path == "/api/results":
                return self._json(results(q["origin"].upper(), q["day"]))
            if u.path == "/api/history":
                return self._json(store.history(q["origin"].upper(), q["dest"].upper(), q["day"]))
            if u.path == "/api/summary":
                return self._json(store.summary(q["origin"].upper()))
            if u.path == "/api/jobs":
                return self._json({"jobs": scanner.jobs(), "cooldown": scanner.cooldown_left()})
            self.send_error(404)
        except (KeyError, ValueError) as e:
            self._json({"error": f"bad request: {e}"}, 400)
        except frontier.FrontierError as e:
            self._json({"error": str(e)}, 502)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/api/scan":
                day = datetime.strptime(body["day"], "%Y-%m-%d").date()
                dests = [d.upper() for d in body.get("dests") or []] or None
                job, skipped = scanner.start(body["origin"], day, dests, body.get("label", ""))
                return self._json({"job": job.public(), "skipped": skipped})
            if self.path == "/api/watch":
                w = load_watch()
                o = body["origin"].upper()
                if body.get("on"):
                    if o not in w["origins"]:
                        w["origins"].append(o)
                else:
                    w["origins"] = [x for x in w["origins"] if x != o]
                if body.get("interval_minutes"):
                    w["interval_minutes"] = int(body["interval_minutes"])
                save_watch(w)
                return self._json(w)
            self.send_error(404)
        except (KeyError, ValueError) as e:
            self._json({"error": f"bad request: {e}"}, 400)
        except frontier.FrontierError as e:
            self._json({"error": str(e)}, 502)


def main():
    store.init()
    frontier.get_markets()
    threading.Thread(target=watcher, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"GoWild tracker on http://{HOST}:{PORT}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
