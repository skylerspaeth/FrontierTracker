"""Tests for the history queries the UI reads (store.latest / history / summary). Temp database, no network.
Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from unittest import mock

import _stub  # noqa: F401
import store

DAY = "2026-12-01"


def fl(key, price=None, seats=None):
    return {"key": key, "gowild": price is not None, "gowild_price": price, "gowild_seats": seats,
            "gowild_seats_text": None, "standard_price": 150.0, "discount_den_price": None,
            "via": [], "stops": 0, "flight_numbers": [key.split("@")[0]], "depart": key.split("@")[1],
            "arrive": key.split("@")[1]}


class StoreQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store.DB_PATH = os.path.join(self.tmp.name, "t.db")
        store.init()
        self.clock = iter(f"2026-12-01T{h:02d}:00:00-07:00" for h in range(6, 24))

    def rec(self, dest, status, flights=None, day=DAY, origin="AUS", error=None):
        with mock.patch.object(store, "now_iso", lambda: next(self.clock)):
            store.record(origin, dest, day, status, {"flights": flights or []} if status != "error" else None, error)

    def test_latest_tracks_first_open_price_range_and_closed_seats(self):
        a, b = "F9 1@2026-12-02T06:00:00", "F9 2@2026-12-02T09:00:00"
        self.rec("DEN", "ok", [fl(a, 30.0), fl(b, 40.0)])      # 06:00 both open
        self.rec("DEN", "ok", [fl(a, 25.0), fl(b)])             # 07:00 a cheaper, b sold out
        r = store.latest("AUS", DAY)["DEN"]
        by = {f["key"]: f for f in r["flights"]}
        self.assertEqual(r["checked_at"], "2026-12-01T07:00:00-07:00")
        self.assertEqual((by[a]["first_open"], by[a]["price_lo"], by[a]["price_hi"]), ("2026-12-01T06:00:00-07:00", 25.0, 30.0))
        self.assertFalse(by[b]["gowild"])
        self.assertEqual([c["key"] for c in r["closed_since"]], [b])
        self.assertEqual(r["closed_since"][0]["last_open"], "2026-12-01T06:00:00-07:00")

    def test_a_reopened_seat_is_no_longer_listed_as_closed(self):
        k = "F9 1@2026-12-02T06:00:00"
        self.rec("DEN", "ok", [fl(k, 30.0)])
        self.rec("DEN", "ok", [fl(k)])
        self.rec("DEN", "ok", [fl(k, 35.0)])
        self.assertEqual(store.latest("AUS", DAY)["DEN"]["closed_since"], [])

    def test_an_error_does_not_replace_the_last_good_result_but_is_reported(self):
        k = "F9 1@2026-12-02T06:00:00"
        self.rec("DEN", "ok", [fl(k, 30.0)])
        self.rec("DEN", "error", error="blocked")
        r = store.latest("AUS", DAY)["DEN"]
        self.assertEqual(len(r["flights"]), 1)
        self.assertEqual(r["last_error"]["error"], "blocked")

    def test_a_later_good_check_clears_the_error(self):
        self.rec("DEN", "error", error="blocked")
        self.rec("DEN", "ok", [])
        self.assertIsNone(store.latest("AUS", DAY)["DEN"]["last_error"])

    def test_error_only_route_is_still_returned(self):
        self.rec("DEN", "error", error="blocked")
        r = store.latest("AUS", DAY)["DEN"]
        self.assertEqual((r["status"], r["flights"]), ("error", []))

    def test_results_are_scoped_by_origin_and_day(self):
        self.rec("DEN", "ok", [fl("F9 1@x", 30.0)])
        self.rec("DEN", "ok", [], day="2026-12-02")
        self.rec("DEN", "ok", [], origin="ATL")
        self.assertEqual(list(store.latest("AUS", DAY)), ["DEN"])
        self.assertEqual(len(store.latest("AUS", DAY)["DEN"]["flights"]), 1)

    def test_history_is_oldest_first_and_summary_uses_latest_check_per_route(self):
        self.rec("DEN", "ok", [fl("F9 1@2026-12-01T06:00:00", 30.0)])
        self.rec("DEN", "ok", [])
        self.rec("LAX", "ok", [fl("F9 2@2026-12-01T06:00:00", 20.0)])
        self.assertEqual([h["n_gowild"] for h in store.history("AUS", "DEN", DAY)], [1, 0])
        s = store.summary("AUS")
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]["day"], s[0]["checked"], s[0]["open_dests"], s[0]["cheapest"]), (DAY, 2, 1, 20.0))


if __name__ == "__main__":
    unittest.main()
