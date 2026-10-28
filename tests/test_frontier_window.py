"""Bounded scan policy: no startup, periodic or retry escape beyond frontier+N."""
import dataclasses
import datetime as dt
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from movie_features import public_guide
from watcher import Config, SCAN_MODE_FRONTIER, Watcher
from test_watcher import make_config, _kst


class FrontierWindowTests(unittest.TestCase):
    TODAY = dt.date(2026, 10, 28)
    FRONTIER = dt.date(2026, 11, 1)
    END = dt.date(2026, 11, 6)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = dataclasses.replace(
            make_config(self.root), dynamic_date_window=True,
            scan_mode=SCAN_MODE_FRONTIER, cursor_probe_days=5,
            multi_movie_enabled=True, open_only_mode=True,
        )
        clock = patch.object(Config, 'local_now', return_value=_kst(self.TODAY))
        clock.start()
        self.addCleanup(clock.stop)
        self.logger = logging.getLogger(f'frontier-window-{id(self)}')
        self.logger.handlers = [logging.NullHandler()]
        self.watcher = Watcher(self.config, logger=self.logger)
        self.watcher.telegram = Mock()
        self.watcher.cgv = Mock()

    def seed_frontier(self):
        self.watcher.state.advance_frontier(self.FRONTIER)

    def expected_dates(self, end=None):
        end = end or self.END
        return {self.TODAY + dt.timedelta(days=i)
                for i in range((end-self.TODAY).days+1)}

    def test_frontier_mode_is_accepted_from_environment(self):
        with patch.dict(os.environ, {'SCAN_MODE': 'frontier', 'CURSOR_PROBE_DAYS': '5'}):
            parsed = make_config(self.root)
        self.assertEqual(parsed.scan_mode, SCAN_MODE_FRONTIER)
        self.assertEqual(parsed.cursor_probe_days, 5)

    def test_every_cycle_including_periodic_covers_only_the_bounded_window(self):
        self.seed_frontier()
        for cycle in (0, 1, 9, 10, 20, 100):
            self.watcher._cycle_index = cycle
            plan = self.watcher._plan_scan(self.TODAY)
            self.assertEqual(set(plan.dates), self.expected_dates())
            self.assertEqual(len(plan.dates), 10)
            self.assertEqual(plan.dates[:5], tuple(
                self.FRONTIER + dt.timedelta(days=i) for i in range(1, 6)))
            self.assertEqual(plan.dates[5], self.TODAY)

    def test_failed_date_outside_window_stays_recorded_but_is_not_requested(self):
        self.seed_frontier()
        far = dt.date(2026, 11, 20)
        self.watcher.state.note_schedule_failure(far)
        self.assertNotIn(far, self.watcher._plan_scan(self.TODAY).dates)
        self.assertIn(far, self.watcher.state.failed_schedule_dates())

    def test_unknown_or_expired_frontier_bootstraps_today_through_today_plus_five(self):
        expected = self.expected_dates(self.TODAY + dt.timedelta(days=5))
        self.assertEqual(set(self.watcher._plan_scan(self.TODAY).dates), expected)
        self.watcher.state.advance_frontier(self.TODAY-dt.timedelta(days=10))
        self.assertEqual(set(self.watcher._plan_scan(self.TODAY).dates), expected)

    def test_frontier_limit_replaces_the_legacy_28_day_horizon(self):
        future = self.TODAY + dt.timedelta(days=27)
        start, end = self.config.target_range(today=self.TODAY, frontier=future)
        self.assertEqual(start, self.TODAY)
        self.assertEqual(end, future+dt.timedelta(days=5))

    def test_explicit_fixed_end_is_not_extended(self):
        fixed = dataclasses.replace(self.config, dynamic_date_window=False,
                                    target_start=self.TODAY, target_end=self.FRONTIER)
        self.assertEqual(fixed.target_range(frontier=self.FRONTIER), (self.TODAY, self.FRONTIER))

    def test_boundary_and_subscriptions_survive_restart(self):
        self.seed_frontier()
        self.watcher.state.save()
        previous = dict(self.watcher.state.data['subscribers'])
        restarted = Watcher(self.config, logger=self.logger)
        self.assertEqual(set(restarted._plan_scan(self.TODAY).dates), self.expected_dates())
        self.assertEqual(previous, restarted.state.data['subscribers'])

    def test_real_cycle_never_walks_past_limit_and_next_cycle_advances(self):
        self.seed_frontier()
        self.watcher._cycle_index = 1
        self.watcher.state.finish_movie_baseline()

        def payload(day):
            return {'data': [{'siteNo': self.config.site_no, 'movkndDsplNm': 'IMAX LASER 2D',
                              'scnsNm': 'IMAX관', 'scnYmd': day.strftime('%Y%m%d'),
                              'scnsrtTm': '1430', 'scnsNo': '018', 'scnSseq': '1',
                              'movNo': self.config.movie_no, 'movNm': self.config.movie_label,
                              'frSeatCnt': 20, 'stcnt': 624}]}

        self.watcher.cgv.fetch_date.side_effect = payload
        with patch.object(self.watcher, '_walk_new_range', side_effect=AssertionError('out-of-window walk')):
            result = self.watcher.run_cycle()
        requested = [call.args[0] for call in self.watcher.cgv.fetch_date.call_args_list]
        self.assertEqual(set(requested), self.expected_dates())
        self.assertEqual(len(requested), 10)
        self.assertEqual(result.successful_dates, 10)
        self.assertEqual(self.watcher.state.frontier_date, self.END)
        self.assertEqual(max(self.watcher._plan_scan(self.TODAY).dates), self.END+dt.timedelta(days=5))

    def test_movie_initialization_cannot_restore_28_day_sweep(self):
        self.seed_frontier()
        self.assertFalse(self.watcher.state.movie_catalog_ready())
        self.watcher.cgv.fetch_date.return_value = {'data': []}
        self.watcher.run_cycle()
        requested = [call.args[0] for call in self.watcher.cgv.fetch_date.call_args_list]
        self.assertEqual(set(requested), self.expected_dates())
        self.assertTrue(self.watcher.state.movie_catalog_ready())

    def test_guide_explains_active_range_in_both_languages(self):
        for language, phrase in (('ko', '상영일의 5일 뒤까지'),
                                 ('en', '5 days after the latest observed IMAX show date')):
            text = public_guide(language, self.config)
            self.assertIn(phrase, text)
            self.assertNotIn('28일', text)
            self.assertNotIn('28 days', text)


if __name__ == '__main__':
    unittest.main()
