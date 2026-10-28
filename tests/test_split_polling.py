import copy
import dataclasses
import datetime as dt
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from movie_features import public_guide
from watcher import CgvClient, Config, FetchError, SeatSnapshot, Watcher
from test_watcher import make_config, _kst


class SplitPollingTests(unittest.TestCase):
    DAY = dt.date(2026, 10, 28)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = dataclasses.replace(
            make_config(self.root), target_start=self.DAY,
            target_end=self.DAY+dt.timedelta(days=1),
            poll_interval_seconds=60, seat_poll_interval_seconds=120,
            cgv_request_spacing_seconds=2, scan_mode='frontier', cursor_probe_days=5,
            multi_movie_enabled=True, subscriptions_enabled=True,
        )
        self.now = 1000.0
        self.schedule_cost = 1.0
        self.seat_cost = 2.0
        self.requests = []
        self.rows = {self.DAY: [self.row(self.DAY)], self.DAY+dt.timedelta(days=1): []}
        self.fail_dates = {}
        self.seat_error = None
        self.snapshot = SeatSnapshot(total=1, usable=1, mapped_total=1,
                                     available_rows=('J',), available_seats=(('J', '20'),))
        for patcher in (
            patch('watcher.time.monotonic', side_effect=lambda: self.now),
            patch('watcher.time.sleep', side_effect=self.sleep),
            patch.object(Config, 'local_now', side_effect=lambda: _kst(self.DAY)+dt.timedelta(seconds=self.now-1000)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.logger = logging.getLogger(f'split-poll-{id(self)}')
        self.logger.handlers = [logging.NullHandler()]
        self.restart()
        self.watcher.state.advance_frontier(self.DAY)
        self.watcher.state.finish_movie_baseline()
        self.mark_existing(self.rows[self.DAY][0])

    def sleep(self, seconds):
        self.assertGreaterEqual(seconds, 0)
        self.now += seconds

    def row(self, day, start='1430', remaining=1):
        return {'siteNo': self.config.site_no, 'movNo': self.config.movie_no,
                'movNm': '오디세이', 'scnsNm': 'IMAX관', 'movkndDsplNm': 'IMAX LASER 2D',
                'scnYmd': day.strftime('%Y%m%d'), 'scnsrtTm': start,
                'scnsNo': '018', 'scnSseq': start, 'frSeatCnt': remaining, 'stcnt': 624}

    def restart(self):
        self.watcher = Watcher(self.config, logger=self.logger)
        self.watcher.telegram = Mock()
        self.watcher.cgv = Mock()
        self.watcher.cgv.fetch_date.side_effect = self.fetch_date
        self.watcher.cgv.fetch_seat_snapshot.side_effect = self.fetch_seat

    def fetch_date(self, day):
        started = self.now
        self.sleep(self.schedule_cost)
        self.requests.append(('schedule', str(day), started, self.now))
        if day in self.fail_dates:
            raise FetchError(self.fail_dates[day])
        return {'data': self.rows.get(day, [])}

    def fetch_seat(self, session):
        started = self.now
        self.sleep(self.seat_cost)
        self.requests.append(('seat', session.start_time, started, self.now))
        if self.seat_error:
            raise FetchError(self.seat_error)
        return self.snapshot

    def mark_existing(self, row):
        day = dt.datetime.strptime(row['scnYmd'], '%Y%m%d').date()
        session = next(iter(self.watcher._extract_sessions({day: {'data': [row]}}).values()))
        key = session.notification_key(site_no=self.config.site_no, movie_no=self.config.movie_no)
        self.watcher.state.mark_notified(key, session)
        self.watcher.state.set_seat_snapshot(key, self.snapshot)
        self.watcher.state.save()
        return key

    def cycle(self, offset):
        self.assertLessEqual(self.now, 1000+offset)
        self.now = 1000+offset
        with patch.object(self.watcher, '_delivery_worker_running', return_value=True):
            return self.watcher.run_cycle()

    def seat_requests(self):
        return [r for r in self.requests if r[0] == 'seat']

    def test_all_dates_and_new_open_alert_precede_any_seat_request(self):
        tomorrow = self.DAY+dt.timedelta(days=1)
        self.rows[tomorrow] = [self.row(tomorrow)]
        original = self.fetch_seat

        def fetch(session):
            opens = [v for _,v in self.watcher.state.pending_deliveries() if v['category'] == 'open']
            self.assertEqual(len(opens), 1)
            self.assertEqual(opens[0]['show_date'], str(tomorrow))
            return original(session)

        self.watcher.cgv.fetch_seat_snapshot.side_effect = fetch
        self.assertEqual(self.cycle(0).new_sessions, 1)
        self.assertEqual([r[0] for r in self.requests], ['schedule', 'schedule', 'seat'])

    def test_schedules_each_minute_seats_at_least_120_seconds_apart(self):
        self.cycle(0)
        snapshots = copy.deepcopy(self.watcher.state.data['seat_counts'])
        self.cycle(60)
        self.assertEqual(len(self.seat_requests()), 1)
        self.assertEqual(self.watcher.state.data['seat_counts'], snapshots)
        self.cycle(120)
        self.assertEqual(len([r for r in self.requests if r[0] == 'schedule']), 6)
        seats = self.seat_requests()
        self.assertEqual(len(seats), 2)
        self.assertGreaterEqual(seats[1][2]-seats[0][2], 120)
        self.assertLess(seats[1][2]-seats[0][2], 125)

    def test_slightly_faster_scan_waits_until_due_instead_of_slipping_a_minute(self):
        self.cycle(0)
        first = self.seat_requests()[0][2]
        self.schedule_cost = 0.1
        self.cycle(120)
        self.assertEqual(len(self.seat_requests()), 2)
        self.assertGreaterEqual(self.seat_requests()[1][2]-first, 120)
        self.assertLess(self.seat_requests()[1][2]-first, 121)

    def test_budget_exhaustion_keeps_fifo_jobs_and_refreshes_before_resuming(self):
        self.seat_cost = 10
        self.rows[self.DAY] = [self.row(self.DAY, f'14{i:02d}') for i in range(7)]
        for row in self.rows[self.DAY]:
            self.mark_existing(row)
        self.cycle(0)
        carried = list(self.watcher._seat_pending)
        self.assertTrue(carried)
        self.assertLess(self.now, 1060)
        first_waiting = self.watcher._seat_pending[carried[0]].start_time
        before = len(self.requests)
        self.cycle(60)
        recent = self.requests[before:]
        self.assertEqual([r[0] for r in recent[:2]], ['schedule', 'schedule'])
        self.assertEqual(next(r[1] for r in recent if r[0]=='seat'), first_waiting)
        self.cycle(120)
        self.assertEqual(len({r[1] for r in self.seat_requests()}), 7)

    def test_long_schedule_does_not_start_seat_requests_near_deadline(self):
        self.schedule_cost = 23
        self.cycle(0)
        self.assertFalse(self.seat_requests())
        self.assertTrue(self.watcher._seat_pending)

    def test_failed_date_retains_pending_job_but_never_uses_stale_schedule(self):
        self.schedule_cost = 23
        self.cycle(0)
        pending = list(self.watcher._seat_pending)
        self.schedule_cost = 1
        self.fail_dates[self.DAY] = 'timeout'
        self.cycle(60)
        self.assertEqual(list(self.watcher._seat_pending), pending)
        self.assertFalse(self.seat_requests())
        self.fail_dates.clear()
        self.cycle(120)
        self.assertEqual(len(self.seat_requests()), 1)

    def test_schedule_403_or_429_never_runs_seat_work(self):
        for code in (403, 429):
            with self.subTest(code=code):
                self.fail_dates[self.DAY] = f'HTTP {code}'
                self.cycle(0 if code==403 else 60)
                self.assertFalse(self.seat_requests())

    def test_seat_block_stops_seats_but_not_next_minute_opening_detection(self):
        self.seat_error = 'HTTP 429'
        self.cycle(0)
        tomorrow = self.DAY+dt.timedelta(days=1)
        self.rows[tomorrow] = [self.row(tomorrow, '1800')]
        self.assertEqual(self.cycle(60).new_sessions, 1)
        self.assertEqual(len(self.seat_requests()), 1)
        self.assertTrue(self.watcher._seat_lookup_paused())

    def test_rotation_uses_seat_interval_not_schedule_cycle_index(self):
        self.watcher._seat_epoch = self.now
        day = self.DAY+dt.timedelta(days=3)
        self.watcher._cycle_index = 0
        first = self.watcher._forces_seat_recheck(day, self.DAY)
        self.now += 60
        self.watcher._cycle_index = 1
        self.assertEqual(self.watcher._forces_seat_recheck(day, self.DAY), first)
        self.now += 60
        self.assertEqual(self.watcher._forces_seat_recheck(day, self.DAY), day.toordinal()%5 == 1)

    def test_unreadable_seat_deferral_is_not_consumed_on_schedule_only_minute(self):
        with patch.object(self.watcher.state, 'take_deferred_skip', return_value=True) as skip:
            self.cycle(0)
            self.cycle(60)
            self.assertEqual(skip.call_count, 1)
            self.cycle(120)
            self.assertEqual(skip.call_count, 2)

    def test_same_total_seat_identity_change_is_seen_on_next_seat_check(self):
        self.cycle(0)
        key = next(iter(self.watcher.state.data['seat_counts']))
        self.snapshot = dataclasses.replace(self.snapshot, available_rows=('K',), available_seats=(('K', '20'),))
        self.cycle(60)
        self.assertEqual(self.watcher.state.seat_snapshot(key).available_seats, (('J','20'),))
        self.cycle(120)
        self.assertEqual(self.watcher.state.seat_snapshot(key).available_seats, (('K','20'),))

    def test_all_cgv_calls_share_existing_two_second_spacing(self):
        self.cycle(0)
        for previous, current in zip(self.requests, self.requests[1:]):
            self.assertGreaterEqual(current[2]-previous[3], 2)

    def test_restart_rebuilds_jobs_without_resetting_notified_history(self):
        self.schedule_cost = 23
        self.cycle(0)
        history = copy.deepcopy(self.watcher.state.data['notified'])
        self.restart()
        self.schedule_cost = 1
        self.assertEqual(self.cycle(60).new_sessions, 0)
        self.assertEqual(self.watcher.state.data['notified'], history)
        self.assertEqual(len(self.seat_requests()), 1)

    def test_disappeared_or_unselected_showings_do_not_keep_pending_jobs(self):
        self.schedule_cost = 23
        self.cycle(0)
        self.assertTrue(self.watcher._seat_pending)
        self.rows[self.DAY] = []
        self.schedule_cost = 1
        self.cycle(60)
        self.assertFalse(self.watcher._seat_pending)
        self.assertFalse(self.seat_requests())

    def test_open_only_operation_never_queues_or_processes_seats(self):
        self.watcher.config = dataclasses.replace(self.config, open_only_mode=True)
        self.cycle(0)
        self.cycle(60)
        self.assertFalse(self.watcher._seat_pending)
        self.assertFalse(self.seat_requests())

    def test_cadence_is_localized_and_env_setting_parses(self):
        self.assertIn('60초', public_guide('ko', self.config))
        self.assertIn('120초', public_guide('ko', self.config))
        self.assertIn('120s apart', public_guide('en', self.config))
        with patch.dict(os.environ, {'SEAT_POLL_INTERVAL_SECONDS':'120'}):
            self.assertEqual(make_config(self.root).seat_poll_interval_seconds, 120)

    def test_split_seat_request_has_no_hidden_transport_retry(self):
        session = next(iter(self.watcher._extract_sessions({self.DAY: {'data': self.rows[self.DAY]}}).values()))
        for interval in (0, 120):
            with self.subTest(interval=interval):
                client = CgvClient(dataclasses.replace(self.config, seat_poll_interval_seconds=interval))
                with patch.object(client, '_get_json', return_value={'data': []}) as get:
                    client.fetch_seat_snapshot(session)
                self.assertEqual(get.call_args.kwargs['single_attempt'], bool(interval))


if __name__ == '__main__':
    unittest.main()
