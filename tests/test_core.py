"""Tests for the pure logic: booking windows (rules.py), peak-day parsing (peakdays.py), Frontier
response parsing and route-calendar gaps (frontier.py), and scan planning (scanner.py). No network.
Run:  python -m unittest discover -s tests
"""
import json
import unittest
from datetime import date, timedelta
from unittest import mock

import _stub  # noqa: F401  (sets sys.path, stubs curl_cffi)
import frontier
import peakdays
import rules
import scanner


class RulesTests(unittest.TestCase):
    TODAY = date(2026, 12, 1)

    def test_domestic_window_is_today_and_tomorrow(self):
        b = lambda n: rules.bookable_now(self.TODAY + timedelta(days=n), True, self.TODAY)
        self.assertEqual([b(-1), b(0), b(1), b(2)], [False, True, True, False])

    def test_international_window_is_ten_days(self):
        b = lambda n: rules.bookable_now(self.TODAY + timedelta(days=n), False, self.TODAY)
        self.assertEqual([b(0), b(10), b(11)], [True, True, False])

    def test_scan_dates_cover_today_through_ten_days_out(self):
        ds = rules.scan_dates(self.TODAY)
        self.assertEqual((ds[0], ds[-1], len(ds)), (self.TODAY, self.TODAY + timedelta(days=10), 11))


class PeakParseTests(unittest.TestCase):
    def test_ranges_singles_and_year_changes(self):
        out = peakdays.parse("2026 December: 26-28, 31 2027 January: 1-3, 18")
        self.assertEqual(out, {date(2026, 12, 26), date(2026, 12, 27), date(2026, 12, 28), date(2026, 12, 31),
                               date(2027, 1, 1), date(2027, 1, 2), date(2027, 1, 3), date(2027, 1, 18)})

    def test_dash_variants(self):
        self.assertEqual(peakdays.parse("2026 July: 2–4"), {date(2026, 7, d) for d in (2, 3, 4)})

    def test_bad_day_is_skipped_not_fatal(self):
        out = peakdays.parse("2026 February: 30, 3")      # Feb 30 doesn't exist
        self.assertEqual(out, {date(2026, 2, 3)})

    def test_months_before_any_year_are_ignored(self):
        self.assertEqual(peakdays.parse("March: 1-3"), set())


class ParsingHelperTests(unittest.TestCase):
    def test_price(self):
        self.assertEqual(frontier._price("19.499"), 19.5)
        for bad in (None, "", "abc", 0, -3):
            self.assertIsNone(frontier._price(bad))

    def test_seats(self):
        self.assertEqual(frontier._seats("2 Seats Left!"), 2)
        self.assertIsNone(frontier._seats(None))
        self.assertIsNone(frontier._seats("Seats available"))


def page_with(data):
    return "<html>FlightData = '" + json.dumps(data).replace("'", "&#39;") + "';</html>"


def leg(num, dep, arr, a_station="DEN", dep_utc=None):
    return {"carrierCode": "F9", "flightNumber": num, "departureDate": dep, "arrivalDate": arr,
            "departureDateUtc": dep_utc, "arrivalDateUtc": None, "arrivalStation": a_station}


class GetFlightsTests(unittest.TestCase):
    def run_with(self, data):
        with mock.patch.object(frontier, "_get", return_value=(200, page_with(data))):
            return frontier.get_flights("AUS", "DEN", date(2026, 12, 1))

    def test_normalizes_gowild_and_connection_flights(self):
        data = {"lowestGWAmount": 29, "journeys": [{"flights": [
            {"legs": [leg("101", "2026-12-01T06:00:00", "2026-12-01T07:30:00", dep_utc="2026-12-01T12:00:00")],
             "isGoWildFareEnabled": True, "goWildFare": 29.0, "goWildFareSeatsRemaining": "3 Seats Left!",
             "standardFare": 120.0, "duration": "1h 30m"},
            {"legs": [leg("202", "2026-12-01T05:00:00", "2026-12-01T06:00:00", "DFW"),
                      leg("303", "2026-12-01T07:00:00", "2026-12-01T09:00:00")],
             "isGoWildFareEnabled": False, "standardFare": 90.0}]}]}
        out = self.run_with(data)
        self.assertEqual(out["lowest_gowild"], 29.0)
        conn, nonstop = out["flights"]            # sorted by departure: the 05:00 connection comes first
        self.assertEqual((conn["stops"], conn["via"], conn["gowild"]), (1, ["DFW"], False))
        self.assertEqual(conn["key"], "F9 202+F9 303@2026-12-01T05:00:00")
        self.assertEqual((nonstop["gowild"], nonstop["gowild_price"], nonstop["gowild_seats"]), (True, 29.0, 3))
        self.assertEqual(nonstop["depart_utc"], "2026-12-01T12:00:00")

    def test_gowild_flag_without_a_price_is_not_gowild(self):
        data = {"journeys": [{"flights": [{"legs": [leg("1", "2026-12-01T06:00:00", "2026-12-01T07:00:00")],
                                           "isGoWildFareEnabled": True, "goWildFare": None}]}]}
        self.assertFalse(self.run_with(data)["flights"][0]["gowild"])

    def test_return_trips_are_ignored(self):
        data = {"journeys": [{"isReturnTrip": True, "flights": [
            {"legs": [leg("1", "2026-12-01T06:00:00", "2026-12-01T07:00:00")]}]}]}
        self.assertEqual(self.run_with(data)["flights"], [])

    def test_page_without_flightdata_raises_and_flags_captcha(self):
        with mock.patch.object(frontier, "_get", return_value=(200, "<html>nothing</html>")):
            with self.assertRaises(frontier.FrontierError) as c:
                frontier.get_flights("AUS", "DEN", date(2026, 12, 1))
        self.assertFalse(c.exception.blocked)
        with mock.patch.object(frontier, "_get", return_value=(200, "<html>Please solve the CAPTCHA</html>")):
            with self.assertRaises(frontier.FrontierError) as c:
                frontier.get_flights("AUS", "DEN", date(2026, 12, 1))
        self.assertTrue(c.exception.blocked)


class ScheduleGapTests(unittest.TestCase):
    def gap(self, sched, d):
        with mock.patch.object(frontier, "get_schedule", return_value=sched):
            return frontier.schedule_gap("AUS", "DEN", d, fetch=False)

    def test_flying_date_has_no_gap(self):
        self.assertIsNone(self.gap(({date(2026, 12, 5)}, date(2027, 3, 1)), date(2026, 12, 1)))

    def test_seasonal_route_says_when_it_returns(self):
        off = {date(2026, 12, d) for d in range(1, 10)}
        self.assertEqual(self.gap((off, date(2027, 3, 1)), date(2026, 12, 2)), "no service until Dec 10")

    def test_past_last_date_or_empty_calendar_is_no_service(self):
        self.assertEqual(self.gap((set(), date(2026, 12, 1)), date(2026, 12, 5)), frontier.NO_SERVICE)
        self.assertEqual(self.gap((set(), None), date(2026, 12, 5)), frontier.NO_SERVICE)

    def test_unknown_calendar_never_skips(self):
        self.assertIsNone(self.gap(None, date(2026, 12, 5)))
        with mock.patch.object(frontier, "get_schedule", side_effect=frontier.FrontierError("x")):
            self.assertIsNone(frontier.schedule_gap("AUS", "DEN", date(2026, 12, 5)))


class ScanPlanTests(unittest.TestCase):
    ROUTES = {"routes": {"AUS": ["AUS", "DEN", "LAX", "ORD"]}}

    def plan(self, day, dests=None, gaps=None):
        gaps = gaps or {}
        with mock.patch.object(frontier, "get_markets", return_value=self.ROUTES), \
                mock.patch.object(frontier, "schedule_gap", side_effect=lambda o, d, day, fetch=True: gaps.get(d)):
            return scanner.plan("AUS", day, dests)

    def test_skips_origin_and_routes_that_dont_fly(self):
        todo, skipped = self.plan(date.today(), gaps={"LAX": "no service until Mar 4"})
        self.assertEqual(todo, ["DEN", "ORD"])
        self.assertEqual(skipped, {"LAX": "no service until Mar 4"})

    def test_past_dates_skip_everything_on_full_scans(self):
        todo, skipped = self.plan(date.today() - timedelta(days=1))
        self.assertEqual(todo, [])
        self.assertEqual(set(skipped), {"DEN", "LAX", "ORD"})

    def test_explicit_destinations_ignore_the_calendar(self):
        todo, skipped = self.plan(date.today(), dests=["LAX"], gaps={"LAX": "no service until Mar 4"})
        self.assertEqual((todo, skipped), (["LAX"], {}))


if __name__ == "__main__":
    unittest.main()
