"""Coverage and alert delivery without CGV or Telegram network access."""
import dataclasses
import datetime as dt
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_movies import row
from test_watcher import make_config, _kst
from watcher import BookingSession, Config, FetchError, Watcher, _CycleTally


class MonthlyWindowTests(unittest.TestCase):
    today = dt.date(2026, 11, 24)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = dataclasses.replace(make_config(Path(temp.name)), dynamic_date_window=True,
                                       scan_mode="monthly", multi_movie_enabled=True,
                                       subscriptions_enabled=True, open_only_mode=True)
        clock = patch.object(Config, "local_now", return_value=_kst(self.today))
        clock.start()
        self.addCleanup(clock.stop)
        self.restart()
        self.w.state.finish_movie_baseline()

    def restart(self):
        self.w = Watcher(self.cfg, logger=logging.getLogger("monthly-test"))
        self.w.cgv = Mock()
        self.w.telegram = Mock()
        self.w._delivery_worker_running = Mock(return_value=True)
        self.w.cgv.fetch_date.return_value = {"data": []}

    def test_calendar_month_inclusive(self):
        for start, end in [("2026-11-24", "2026-12-24"), ("2026-12-31", "2027-01-31"),
                           ("2027-01-31", "2027-02-28"), ("2028-01-31", "2028-02-29")]:
            start = dt.date.fromisoformat(start)
            self.assertEqual(self.cfg.target_range(today=start, frontier=dt.date(2030, 1, 1)),
                             (start, dt.date.fromisoformat(end)))
        fixed = dataclasses.replace(self.cfg, dynamic_date_window=False)
        self.assertEqual(fixed.target_range(), (fixed.target_start, fixed.target_end))

    def test_near_every_cycle_far_all_within_three_even_after_restart(self):
        seen = set()
        self.w.state.advance_frontier(self.today + dt.timedelta(days=5))
        near = [self.today + dt.timedelta(days=i) for i in range(7)]
        for _ in range(3):
            self.w.run_cycle()
            dates = [c.args[0] for c in self.w.cgv.fetch_date.call_args_list]
            self.assertEqual(dates[:7], near)
            self.assertEqual(len(dates), len(set(dates)))
            self.assertLessEqual(len(dates), 16)
            seen.update(dates)
            self.restart()
        self.assertEqual(seen, set(self.cfg.target_dates(today=self.today)))

    def test_dune_discovered_once_across_empty_gap_and_not_auto_selected(self):
        def schedules(day):
            rows = ([row("30001790", "듄: 파트 3", scnYmd=day.strftime("%Y%m%d"))]
                    if dt.date(2026, 12, 16) <= day <= dt.date(2026, 12, 20) else [])
            return {"data": rows}
        self.w.cgv.fetch_date.side_effect = schedules
        for _ in range(4):
            self.w.run_cycle()
        found = {day for day, val in self.w.state.data["movie_sessions"].items()
                 if any(s["movie_no"] == "30001790" for s in val["sessions"])}
        self.assertEqual(found, {f"2026-12-{i}" for i in range(16, 21)})
        alerts = [v for _, v in self.w.state.pending_deliveries() if v["category"] == "movie"]
        self.assertEqual([v["movie_no"] for v in alerts], ["30001790"])
        self.assertFalse(self.w.state.wants_movie(self.cfg.telegram_chat_id, "30001790"))
        self.w.cgv.fetch_seat_snapshot.assert_not_called()

    def test_throttle_does_not_advance_past_unattempted_dates(self):
        far = self.w._plan_scan(self.today).monthly_rotation
        def throttled(day):
            if day == far[1]:
                raise FetchError("HTTP 429")
            return {"data": []}
        self.w.cgv.fetch_date.side_effect = throttled
        self.w.run_cycle()
        self.restart()
        self.assertEqual(self.w._plan_scan(self.today).monthly_rotation[0], far[2])

    def test_seat_jobs_on_unvisited_day_kept_but_not_run(self):
        day = dt.date(2026, 12, 16)
        session = BookingSession(date=str(day), start_time="15:00")
        key = session.notification_key(site_no="0013", movie_no="30001790")
        self.w._seat_pending[key] = session
        self.w._seat_next_due[key] = 100
        def run(days):
            self.assertEqual(self.w._run_due_seat_checks(
                days, {}, _CycleTally(), deadline=0, retained_range=self.cfg.target_range()), 0)
        run([self.today])
        self.assertIn(key, self.w._seat_pending)
        self.assertIn(key, self.w._seat_next_due)
        run([day])  # Fresh successful schedule now has no such showing.
        self.assertNotIn(key, self.w._seat_pending)
        self.assertNotIn(key, self.w._seat_next_due)
        self.w.cgv.fetch_seat_snapshot.assert_not_called()

    def enable_priorities(self):
        self.pin = dt.date(2026, 12, 16)
        self.first = dt.date(2026, 12, 21)
        self.cfg = dataclasses.replace(self.cfg, monthly_priority_date=self.pin,
                                       monthly_priority_from=self.first)
        self.restart()
        self.w.state.finish_movie_baseline()

    def test_priority_request_order_every_cycle_and_no_gaps_in_remaining_month(self):
        self.enable_priorities()
        expected = [self.pin] + [self.first + dt.timedelta(days=n) for n in range(4)]
        seen = set()
        for _ in range(5):
            self.w.run_cycle()
            days = [call.args[0] for call in self.w.cgv.fetch_date.call_args_list]
            self.assertEqual(days[:5], expected)
            self.assertEqual(days[5:12], [self.today + dt.timedelta(days=n) for n in range(7)])
            self.assertEqual(len(days), len(set(days)))
            self.assertLessEqual(len(days), 16)
            seen.update(days)
            self.restart()
        self.assertEqual(seen, set(self.cfg.target_dates()))

    def test_growing_priority_range_rotates_and_recovers_after_restart_or_throttle(self):
        self.enable_priorities()
        day = dt.date(2026, 12, 1)
        with patch.object(Config, "local_now", return_value=_kst(day)):
            def throttle(d):
                if d == self.first:
                    raise FetchError("HTTP 429")
                return {"data": []}
            self.w.cgv.fetch_date.side_effect = throttle
            self.w.run_cycle()
            self.restart()
            self.assertEqual(self.w._plan_scan(day).dates[:2],
                             (self.pin, self.first + dt.timedelta(days=1)))
            self.assertIsNone(self.w.state.monthly_last_date())
            seen, focus = set(), set()
            for _ in range(7):
                plan = self.w._plan_scan(day)
                self.assertEqual(plan.dates[0], self.pin)
                self.assertEqual(len(plan.dates), len(set(plan.dates)))
                self.assertEqual(len(plan.dates), 16)
                self.assertGreaterEqual(len(plan.monthly_rotation), 4)
                seen.update(plan.dates)
                focus.update(plan.priority_rotation)
                self.w.run_cycle()
                self.restart()
            month = set(self.cfg.target_dates())
            self.assertEqual(seen, month)
            self.assertEqual(focus, {d for d in month if d >= self.first})

    def test_new_avengers_and_later_dune_notify_through_existing_filters(self):
        self.enable_priorities()
        dune, avengers = "30001790", "99999001"
        self.w._observe_movies(self.pin, list(self.w._extract_sessions({self.pin: {"data": [
            row(dune, "듄-파트 3", scnYmd="20261216")]}}).values()))
        self.w.state.select_movie(self.cfg.telegram_chat_id, dune, True)
        def schedules(day):
            rows = []
            if day == self.pin:
                rows = [row(avengers, "어벤져스: 둠스데이", scnYmd="20261216")]
            if day == self.first:
                rows = [row(dune, "듄-파트 3", scnYmd="20261221", frSeatCnt=0)]
            return {"data": rows}
        self.w.cgv.fetch_date.side_effect = schedules
        self.w.run_cycle()
        self.w.run_cycle()
        alerts = [(v["category"], v.get("movie_no")) for _, v in self.w.state.pending_deliveries()]
        self.assertEqual(alerts.count(("movie", avengers)), 1)
        self.assertFalse(self.w.state.wants_movie(self.cfg.telegram_chat_id, avengers))
        self.assertEqual(alerts.count(("open", dune)), 1)
        self.w.cgv.fetch_seat_snapshot.assert_not_called()

    def test_focus_does_not_extend_window_duplicate_near_dates_or_pin_in_the_past(self):
        self.enable_priorities()
        plan = self.w._plan_scan(dt.date(2026, 11, 1))
        self.assertFalse(plan.priority_dates)
        plan = self.w._plan_scan(dt.date(2026, 12, 15))
        self.assertEqual(plan.dates[0], self.pin)
        self.assertEqual(len(set(plan.dates)), len(plan.dates))
        plan = self.w._plan_scan(dt.date(2026, 12, 21))
        self.assertNotIn(self.pin, plan.dates)
        self.assertFalse(plan.priority_dates)  # All dates now qualify equally.

    def enable_handoff(self):
        self.enable_priorities()
        self.dune, self.avengers = "30001790", "30001943"
        self.cfg = dataclasses.replace(self.cfg, monthly_pin_movie_no=self.avengers,
                                       monthly_follow_movies=(self.dune, self.avengers))
        self.restart()

    def dec(self, d):
        return dt.date(2026, 12, d)

    def observe(self, day, movies):
        day = self.dec(day)
        rows = [row(n, "test", scnYmd=day.strftime("%Y%m%d"), frSeatCnt=0, **extra)
                for n, extra in movies]
        sessions = self.w._extract_sessions({day: {"data": rows}})
        self.w._observe_movies(day, list(sessions.values()))

    def test_handoff_only_for_exact_pin_movie_date_theater_and_imax(self):
        self.enable_handoff()
        self.observe(16, [(self.dune, {}),
                          (self.avengers, {"siteNo": "0001"}),
                          (self.avengers, {"scnsNm": "1관", "movkndDsplNm": "2D"})])
        self.observe(17, [(self.avengers, {})])
        self.observe(20, [(self.dune, {})])
        self.observe(24, [("777", {})])
        self.assertEqual(self.w._plan_scan(self.today).priority_dates[0], self.pin)
        self.assertFalse(self.w.state.opening_progress()[0])
        self.observe(16, [(self.avengers, {})])  # No follower, no available seats needed.
        self.assertEqual(self.w._plan_scan(self.today).priority_dates,
                         tuple(self.dec(i) for i in [21, 18, 22, 19, 23]))

    def test_next_real_cycle_switches_order_then_keeps_and_moves_each_film_independently(self):
        self.enable_handoff()
        self.observe(20, [(self.dune, {})])
        def schedules(day):
            movies = [self.avengers] if day == self.pin else []
            return {"data": [row(n, "test", scnYmd=day.strftime("%Y%m%d"), frSeatCnt=0) for n in movies]}
        self.w.cgv.fetch_date.side_effect = schedules
        self.w.run_cycle()
        self.assertEqual(self.w.cgv.fetch_date.call_args_list[0].args[0], self.pin)
        self.observe(16, [])  # CGV removes the showing before restart.
        self.restart()
        self.w.run_cycle()
        calls = [c.args[0] for c in self.w.cgv.fetch_date.call_args_list]
        self.assertEqual(calls[:5], [self.dec(d) for d in [21, 17, 22, 18, 23]])
        self.assertEqual(len(set(calls)), len(calls))
        self.assertLessEqual(len(calls), 16)
        self.observe(22, [(self.dune, {})])
        self.assertEqual(self.w._plan_scan(self.today).priority_dates[:2], (self.dec(23), self.dec(17)))
        self.observe(22, [(self.avengers, {})])
        self.assertEqual(self.w._plan_scan(self.today).priority_dates, (self.dec(23), self.dec(24)))
        self.observe(24, [(self.avengers, {}), (self.dune, {})])
        plan = self.w._plan_scan(self.today)
        self.assertFalse(plan.priority_dates)
        self.assertLessEqual(max(plan.dates), self.dec(24))
        self.assertEqual(self.w._plan_scan(self.today + dt.timedelta(days=1)).priority_dates, (self.dec(25),))
        self.w.cgv.fetch_seat_snapshot.assert_not_called()

    def test_legacy_record_migration_is_site_scoped_and_not_a_new_broadcast(self):
        self.enable_priorities()
        for site, movie, day in [("7777", "30001943", 16), ("0013", "30001790", 20)]:
            s = BookingSession(date=str(self.dec(day)), start_time="14:00", movie_no=movie)
            self.w.state.mark_notified(s.notification_key(site_no=site, movie_no=movie), s)
        self.w.state.save()
        self.enable_handoff()
        self.assertEqual(self.w.state.opening_progress(), (False, {self.dune: self.dec(20)}))
        self.assertFalse(self.w.state.pending_deliveries())

    def test_post_handoff_still_sweeps_gaps_and_preserves_near_days_every_cycle(self):
        self.enable_handoff()
        self.observe(20, [(self.dune, {})])
        self.observe(16, [(self.avengers, {})])
        seen = set()
        for _ in range(7):
            self.w.run_cycle()
            days = [c.args[0] for c in self.w.cgv.fetch_date.call_args_list]
            self.assertEqual(days[:2], [self.dec(21), self.dec(17)])
            self.assertLessEqual(len(days), 16)
            self.assertEqual(len(set(days)), len(days))
            self.assertTrue(all(self.today + dt.timedelta(days=i) in days for i in range(7)))
            seen.update(days)
            self.restart()
        self.assertEqual(seen, set(self.cfg.target_dates()))
