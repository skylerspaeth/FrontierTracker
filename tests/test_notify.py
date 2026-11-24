"""Tests for the Telegram deal alerts (notify.py) and the store helpers they use.

No network: curl_cffi is stubbed (so frontier.py imports without it), the database is a temp file,
and Telegram's send() is replaced by a recorder.   Run:  python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta

import _stub  # noqa: F401  (sets sys.path, stubs curl_cffi)

import frontier  # noqa: E402
import notify  # noqa: E402
import store  # noqa: E402


def st(code, city, state, country, country_name, domestic):
    return {"code": code, "city": city, "state": state, "country": country,
            "countryName": country_name, "domestic": domestic}


US = "United States of America"
STATIONS = {
    "AUS": st("AUS", "Austin", "TX", "US", US, True),
    "ATL": st("ATL", "Atlanta", "GA", "US", US, True),
    "BOI": st("BOI", "Boise", "ID", "US", US, True),
    "LAX": st("LAX", "Los Angeles", "CA", "US", US, True),
    "LAS": st("LAS", "Las Vegas", "NV", "US", US, True),
    "TUL": st("TUL", "Tulsa, OK", "OK", "US", US, True),
    "ORD": st("ORD", "Chicago (O'Hare)", "IL", "US", US, True),
    "SJU": st("SJU", "San Juan", "", "PR", "Puerto Rico", True),
    "BOG": st("BOG", "Bogota", "", "CO", "Colombia, Republic of", False),
    "MEX": st("MEX", "Mexico City (AICM)", "", "MX", "Mexico, United Mexican States", False),
    "SJO": st("SJO", "San Jose", "", "CR", "Costa Rica", False),
}
CFG = dict(notify.DEFAULTS)


def flight(key, price, via=(), seats=None, gowild=True):
    key = key + "+F9 9" if via else key      # a connecting itinerary's key joins its segments with '+'
    return {"key": key, "gowild": gowild, "gowild_price": price if gowild else None, "gowild_seats": seats,
            "gowild_seats_text": None, "standard_price": 200.0, "discount_den_price": None,
            "via": list(via), "stops": len(via), "flight_numbers": [key], "depart": "2026-12-01T06:00:00",
            "arrive": "2026-12-01T09:00:00"}


class Job:
    def __init__(self, origin="AUS", day=None, notify=True):
        self.origin, self.day, self.notify = origin, day or date(2026, 12, 1), notify
        self.state = "done"


class PlaceTests(unittest.TestCase):
    def test_domestic_shows_state(self):
        self.assertEqual(notify.place(STATIONS["LAX"]), "Los Angeles, CA")
        self.assertEqual(notify.place(STATIONS["LAS"]), "Las Vegas, NV")

    def test_puerto_rico_and_international_show_country(self):
        self.assertEqual(notify.place(STATIONS["SJU"]), "San Juan, Puerto Rico")
        self.assertEqual(notify.place(STATIONS["BOG"]), "Bogota, Colombia")
        self.assertEqual(notify.place(STATIONS["MEX"]), "Mexico City (AICM), Mexico")
        self.assertEqual(notify.place(STATIONS["SJO"]), "San Jose, Costa Rica")

    def test_state_not_repeated_and_airport_qualifier_kept(self):
        self.assertEqual(notify.place(STATIONS["TUL"]), "Tulsa, OK")
        self.assertEqual(notify.place(STATIONS["ORD"]), "Chicago (O'Hare), IL")


class ClassifyTests(unittest.TestCase):
    def c(self, code, price, nonstop=()):
        return notify.classify(flight("k", price), STATIONS[code], set(nonstop), CFG)

    def test_under_50_connection_destination(self):
        self.assertEqual(self.c("BOI", 47.0), "under")
        self.assertIsNone(self.c("BOI", 50.0))          # the cap is exclusive
        self.assertIsNone(self.c("BOI", 60.0))

    def test_nonstop_destinations_are_excluded_from_rule_one(self):
        self.assertIsNone(self.c("ATL", 15.41, nonstop={"ATL"}))
        self.assertEqual(self.c("ATL", 15.41), "under")  # same fare, but no nonstop known

    def test_international_under_100(self):
        self.assertEqual(self.c("BOG", 93.53), "intl")
        self.assertIsNone(self.c("BOG", 100.0))
        self.assertIsNone(self.c("BOG", 140.0))

    def test_puerto_rico_has_its_own_lower_cap(self):
        self.assertEqual(self.c("SJU", 79.0), "intl")
        self.assertIsNone(self.c("SJU", 91.01))         # normal in-window PR one-stop: not a deal
        self.assertEqual(self.c("SJU", 40.0, nonstop={"SJU"}), "intl")

    def test_nonstop_internationals_still_alert_even_when_cheap(self):
        # the nonstop exclusion only covers the "under $50" rule (cheap domestic nonstops); internationals always count
        for code in ("BOG", "SJU"):
            self.assertEqual(notify.classify(flight("k", 30.0), STATIONS[code], {code}, CFG), "intl")
        self.assertIsNone(notify.classify(flight("k", 30.0), STATIONS["BOI"], {"BOI"}, CFG))

    def test_ignores_flights_without_a_gowild_fare(self):
        f = flight("k", None, gowild=False)
        self.assertIsNone(notify.classify(f, STATIONS["BOI"], set(), CFG))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store.DB_PATH = os.path.join(self.tmp.name, "t.db")
        store.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_nonstop_dests_from_single_segment_keys(self):
        day = "2026-12-01"
        store.record("AUS", "ATL", day, "ok", {"flights": [flight("F9 100@2026-12-01T06:00:00", 15.4)]})
        store.record("AUS", "BOI", day, "ok", {"flights": [flight("F9 1+F9 2@2026-12-01T06:00:00", 47.0, via=["DEN"])]})
        self.assertEqual(store.nonstop_dests("AUS"), {"ATL"})
        self.assertEqual(store.nonstop_dests("DEN"), set())

    def test_alert_rows_close_and_reopen(self):
        store.alerts_set("AUS", "BOI", "2026-12-01", "k1", 47.0)
        self.assertTrue(store.alerts_for("AUS", "BOI", "2026-12-01")["k1"]["open"])
        store.alerts_close_missing("AUS", "BOI", "2026-12-01", [])
        self.assertFalse(store.alerts_for("AUS", "BOI", "2026-12-01")["k1"]["open"])
        store.alerts_set("AUS", "BOI", "2026-12-01", "k1", 44.0)
        row = store.alerts_for("AUS", "BOI", "2026-12-01")["k1"]
        self.assertTrue(row["open"])
        self.assertEqual(row["last_price"], 44.0)

    def test_meta(self):
        self.assertIsNone(store.meta_get("x"))
        store.meta_set("x", 5)
        store.meta_set("x", 6)
        self.assertEqual(store.meta_get("x"), "6")


class FlowTests(unittest.TestCase):
    """on_check -> flush, with a recorder instead of Telegram."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store.DB_PATH = os.path.join(self.tmp.name, "t.db")
        store.init()
        notify.WATCH_PATH = os.path.join(self.tmp.name, "watch.json")
        notify._pending.clear()
        notify._nonstop_cache.clear()
        os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "tok", "chat"
        self.sent, self.send_ok = [], True
        self._orig = (notify.send, frontier.get_markets)
        notify.send = lambda text: (self.sent.append(text), self.send_ok)[1]
        frontier.get_markets = lambda force=False: {"stations": STATIONS, "routes": {"AUS": list(STATIONS)}}
        store.meta_set("notify_started", 0)      # long past: not in the silent baseline

    def tearDown(self):
        notify.send, frontier.get_markets = self._orig
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        self.tmp.cleanup()

    def check(self, dest, flights, job=None):
        job = job or Job()
        store.record(job.origin, dest, job.day.isoformat(), "ok", {"flights": flights})
        notify.on_check(job, dest, {"flights": flights})

    def test_new_deal_alerts_once_then_stays_quiet(self):
        self.check("BOI", [flight("a", 47.0, via=["DEN"])])
        self.assertEqual(notify.flush("AUS"), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Boise, ID", self.sent[0])
        self.assertIn("$47", self.sent[0])
        self.check("BOI", [flight("a", 47.0, via=["DEN"])])
        self.assertEqual(notify.flush("AUS"), 0)
        self.assertEqual(len(self.sent), 1)

    def test_price_drop_of_ten_alerts_but_small_drop_does_not(self):
        self.check("BOG", [flight("a", 99.0, via=["MCO"])])
        notify.flush("AUS")
        self.check("BOG", [flight("a", 95.0, via=["MCO"])])
        self.assertEqual(notify.flush("AUS"), 0)
        self.check("BOG", [flight("a", 88.0, via=["MCO"])])
        self.assertEqual(notify.flush("AUS"), 1)
        self.assertIn("$88", self.sent[-1])

    def test_reopened_seat_alerts_again(self):
        self.check("BOI", [flight("a", 47.0, via=["DEN"])])
        notify.flush("AUS")
        self.check("BOI", [])                                   # seat closed
        self.check("BOI", [flight("a", 47.0, via=["DEN"])])     # and came back
        self.assertEqual(notify.flush("AUS"), 1)
        self.assertIn("reopened", self.sent[-1])

    def test_nonstop_destination_is_not_alerted_but_connection_elsewhere_is(self):
        self.check("ATL", [flight("n", 15.41), flight("c", 30.0, via=["DEN"])])
        self.assertEqual(notify.flush("AUS"), 0)
        self.check("LAX", [flight("c", 40.0, via=["DEN"])])
        self.assertEqual(notify.flush("AUS"), 1)

    def test_manual_checks_never_alert_but_still_track_closures(self):
        self.check("BOI", [flight("a", 47.0, via=["DEN"])], job=Job(notify=False))
        self.assertEqual(notify.flush("AUS"), 0)
        self.check("BOI", [flight("a", 47.0, via=["DEN"])])
        notify.flush("AUS")
        self.check("BOI", [], job=Job(notify=False))            # a manual check sees it closed
        self.assertFalse(store.alerts_for("AUS", "BOI", "2026-12-01")["a+F9 9"]["open"])

    def test_baseline_period_records_silently(self):
        store.meta_set("notify_started", __import__("time").time())     # just switched on
        self.check("BOI", [flight("a", 47.0, via=["DEN"])])
        self.assertEqual(notify.flush("AUS"), 0)
        self.assertEqual(self.sent, [])
        self.assertIn("a+F9 9", store.alerts_for("AUS", "BOI", "2026-12-01"))
        store.meta_set("notify_started", 0)                              # baseline over
        self.check("BOI", [flight("a", 47.0, via=["DEN"])])
        self.assertEqual(notify.flush("AUS"), 0)                         # already known: still quiet

    def test_failed_send_is_retried_next_cycle(self):
        self.send_ok = False
        self.check("BOI", [flight("a", 47.0, via=["DEN"])])
        self.assertEqual(notify.flush("AUS"), 0)
        self.assertEqual(store.alerts_for("AUS", "BOI", "2026-12-01"), {})
        self.send_ok = True
        self.check("BOI", [flight("a", 47.0, via=["DEN"])])
        self.assertEqual(notify.flush("AUS"), 1)

    def test_disabled_without_env_vars(self):
        os.environ.pop("TELEGRAM_BOT_TOKEN")
        self.check("BOI", [flight("a", 47.0, via=["DEN"])])
        self.assertEqual(notify.flush("AUS"), 0)
        self.assertEqual(self.sent, [])


class DigestTests(unittest.TestCase):
    def alert(self, dest, price, rule, **kw):
        a = {"dest": dest, "day": "2026-12-01", "key": "k", "price": price, "reason": "new", "was": None,
             "rule": rule, "via": ["DEN"], "seats": None}
        a.update(kw)
        return a

    def test_grouped_by_rule_with_place_names(self):
        msg = "\n".join(notify.build_digest("AUS", [
            self.alert("BOI", 47.0, "under"), self.alert("BOG", 93.53, "intl", seats=3),
            self.alert("SJU", 79.0, "intl", reason="drop", was=96.61)], STATIONS, CFG))
        self.assertIn("🛫 New deals from Austin, TX (AUS)", msg)
        self.assertLess(msg.index("Under $50 (connections)"), msg.index("International / Offshore"))
        for text in ("Boise, ID (BOI)", "Bogota, Colombia (BOG)", "San Juan, Puerto Rico (SJU)"):
            self.assertIn(text, msg)
        for text in ("seats left", "was $", "📉"):
            self.assertNotIn(text, msg)
        self.assertTrue(msg.endswith("Tap a deal and switch to the GoWild tab to book."))

    def test_prices_round_to_whole_dollars(self):
        self.assertEqual([notify._money(p) for p in (46.5, 46.49, 47.0, 93.53, 0.4)], ["$47", "$46", "$47", "$94", "$0"])

    def test_layover_times_follow_each_airport(self):
        one = self.alert("BOI", 47.0, "under", layovers=[{"station": "DEN", "minutes": 200}])
        two = self.alert("BOG", 94.0, "intl", via=["DEN", "MCO"],
                         layovers=[{"station": "DEN", "minutes": 849}, {"station": "MCO", "minutes": 50}])
        msg = "\n".join(notify.build_digest("AUS", [one, two], STATIONS, CFG))
        self.assertIn("via DEN (3h20m)", msg)
        self.assertIn("via DEN (14h9m) - MCO (0h50m)", msg)

    def test_flights_saved_without_layover_times_still_list_airports(self):
        msg = "\n".join(notify.build_digest("AUS", [self.alert("BOG", 94.0, "intl", via=["DEN", "MCO"])], STATIONS, CFG))
        self.assertIn("via DEN - MCO", msg)
        self.assertNotIn("(0h", msg)

    def test_one_line_per_destination_and_date(self):
        msg = "\n".join(notify.build_digest("AUS", [
            self.alert("BOI", 47.0, "under", key="a"), self.alert("BOI", 44.0, "under", key="b")], STATIONS, CFG))
        self.assertEqual(msg.count("Boise, ID"), 1)
        self.assertIn("$44", msg)
        self.assertIn("+1 more", msg)

    def test_html_is_escaped_and_links_point_at_frontier(self):
        stations = {**STATIONS, "BOI": st("BOI", "Boise <&>", "ID", "US", US, True)}
        msg = "\n".join(notify.build_digest("AUS", [self.alert("BOI", 47.0, "under")], stations, CFG))
        self.assertIn("Boise &lt;&amp;&gt;, ID", msg)
        self.assertIn("booking.flyfrontier.com/Flight/InternalSelect?o1=AUS&amp;d1=BOI", msg)

    def test_long_digests_are_split_under_the_limit(self):
        alerts = [self.alert("BOI", 40.0 + i / 100, "under", day=(date(2026, 12, 1) + timedelta(days=i)).isoformat())
                  for i in range(120)]
        msgs = notify.build_digest("AUS", alerts, STATIONS, CFG)
        self.assertGreater(len(msgs), 1)
        self.assertTrue(all(len(m) <= 4096 for m in msgs))
        self.assertEqual(sum(m.count("Boise, ID") for m in msgs), 120)


if __name__ == "__main__":
    unittest.main()
