"""GoWild peak days: travel dates where Frontier may add a peak-day charge.

These are NOT blackout dates (the 2027 pass has none): GoWild fares can still be searched and
booked on them. Frontier lists the dates on flyfrontier.com/deals/gowild-pass, and they change
every year, so we scrape that page (at most once a day, cached in data/peak_days.json) and fall
back to the cache, then to the copy below, if the page can't be read.
"""
import json
import os
import re
import threading
import time
from datetime import date, datetime

import frontier

URL = "https://www.flyfrontier.com/deals/gowild-pass"
CACHE_PATH = os.path.join(frontier.DATA_DIR, "peak_days.json")
MAX_AGE = 24 * 3600
RETRY_AFTER = 3600          # after a failed refresh, wait this long before trying again
TRUST_DAYS = 14             # a list we last read from Frontier more than this long ago is "possible", not certain
MIN_DATES = 20              # a parse yielding fewer than this is treated as a layout change

# Copy of Frontier's list as read 2026-11-23, in the page's own format. Only used until the
# first successful scrape (or if the page becomes unreadable and there's no cache).
FALLBACK = """
2026 November: 24-25, 28-30 December: 19-31
2027 January: 1-3, 14-15, 18 February: 11-12, 15 March: 12-14, 19-21, 26-29 April: 2-4
May: 27-28, 31 July: 1-5 September: 2-3, 6 October: 7-8, 11 November: 23-24, 27-29
December: 18-24, 26-31
2028 January: 1-3, 13-14, 17 February: 17-18, 21 March: 10-12, 17-19, 24-26, 31 April: 1-2, 14-17
May: 25-26, 29 June: 29-30 July: 1-5, 8-9 August: 31 September: 1, 4 October: 5-6, 9
November: 21-22, 25-27 December: 20-24, 26-31
"""

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], 1)}
_RANGE = r"\d+(?:\s*[-\u2013\u2014]\s*\d+)?"
_TOKEN = re.compile(r"\b(20\d\d)\b|\b(" + "|".join(_MONTHS) + r"):\s*(" + _RANGE + r"(?:\s*,\s*" + _RANGE + r")*)")

_lock = threading.Lock()
_mem = {"dates": None, "fetched": 0.0, "failed": 0.0}


def parse(text):
    """Turn '2027 January: 1-3, 14-15, 18 February: ...' (HTML tags already stripped) into a set of dates."""
    out, year = set(), None
    for m in _TOKEN.finditer(text):
        if m.group(1):
            year = int(m.group(1))
            continue
        if year is None:
            continue
        month = _MONTHS[m.group(2)]
        for part in m.group(3).replace("–", "-").replace("—", "-").split(","):
            part = part.strip()
            if not part:
                continue
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b or a)
                out.update(date(year, month, d) for d in range(lo, hi + 1))
            except ValueError:
                continue    # a bad day number: skip it rather than drop the whole list
    return out


def _from_page(html):
    i = html.lower().find("what are the peak-day dates")
    if i < 0:
        return set()
    end = html.find("</article>", i)
    section = html[i:end if end > 0 else i + 6000]
    return parse(re.sub(r"<[^>]+>", " ", section))


def _read_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            c = json.load(f)
        return {date.fromisoformat(d) for d in c["dates"]}, c["fetched_at"]
    except (OSError, ValueError, KeyError):
        return None, None


def _write_cache(dates):
    os.makedirs(frontier.DATA_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "dates": sorted(d.isoformat() for d in dates)}, f)
    os.replace(tmp, CACHE_PATH)


def get():
    """The current set of peak dates. Refreshes from Frontier when the cache is over a day old."""
    with _lock:
        now = time.time()
        if _mem["dates"] is None:
            cached, _ = _read_cache()
            _mem["dates"] = cached or parse(FALLBACK)
            try:
                _mem["fetched"] = os.path.getmtime(CACHE_PATH) if cached else 0.0
            except OSError:
                _mem["fetched"] = 0.0
        if now - _mem["fetched"] > MAX_AGE and now - _mem["failed"] > RETRY_AFTER:
            try:
                _, html = frontier._get(URL)
                dates = _from_page(html)
                if len(dates) < MIN_DATES:
                    raise ValueError("peak-day list not found on page")
                _mem["dates"], _mem["fetched"] = dates, now
                _write_cache(dates)
            except (frontier.FrontierError, ValueError, OSError):
                _mem["failed"] = now
        return _mem["dates"]


def reliable():
    """True if the list in use was read from Frontier within TRUST_DAYS (not just the built-in copy)."""
    get()
    return bool(_mem["fetched"]) and time.time() - _mem["fetched"] <= TRUST_DAYS * 86400


def is_peak(d):
    return d in get()


def fetched_at():
    """ISO timestamp of the last successful scrape, or None if we're still on the built-in copy."""
    return _read_cache()[1]
