"""GoWild pass rules, from flyfrontier.com/deals/gowild-pass (read 2026-10-14).

Domestic (US + Puerto Rico): bookable the day before departure (and same day).
International: bookable from 10 days before departure.
There are no blackout dates any more, only peak days (extra charge, still bookable);
those are scraped from the same page, see peakdays.py.
"""
from datetime import date, timedelta

import peakdays

DOMESTIC_DAYS_AHEAD = 1
INTERNATIONAL_DAYS_AHEAD = 10


def is_peak(d):
    """Peak day: Frontier may add a peak-day charge, but GoWild seats can still be booked (see peakdays.py)."""
    return peakdays.is_peak(d)


def bookable_now(d, domestic, today=None):
    """True if a pass holder could book a GoWild seat on date d right now (ignoring seat inventory)."""
    today = today or date.today()
    ahead = (d - today).days
    if ahead < 0:
        return False
    return ahead <= (DOMESTIC_DAYS_AHEAD if domestic else INTERNATIONAL_DAYS_AHEAD)


def scan_dates(today=None):
    """Dates worth scanning: today .. today+10 (domestic only matters for the first two)."""
    today = today or date.today()
    return [today + timedelta(days=i) for i in range(INTERNATIONAL_DAYS_AHEAD + 1)]
