"""Coverage and alert delivery without CGV or Telegram network access."""
import dataclasses
import datetime as dt
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from movie_features import public_guide
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
        self.assertIn("3주기", public_guide("ko", self.cfg))
        self.assertIn("three normal cycles", public_guide("en", self.cfg))

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
