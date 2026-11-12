import dataclasses
import datetime as dt
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_watcher import make_config
from watcher import BookingSession, Config, FetchError, SeatSnapshot, Watcher, _CycleTally


def snapshot(*seats):
    return SeatSnapshot(total=len(seats), usable=sum(row != 'A' for row, _ in seats),
                        mapped_total=len(seats), available_rows=tuple(sorted({r for r, _ in seats})),
                        available_seats=tuple(seats))


class NewSweetSeatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(make_config(Path(temporary.name)),
                                         seat_alert_sweet_only=True, seat_alert_repeat_minutes=0,
                                         deferred_recheck_cycles=1)
        self.watcher = self.make_watcher()
        self.session = BookingSession(date='2026-11-12', start_time='24:45',
                                      screen_no='018', screen_sequence='1', total_seats=624,
                                      movie_no=self.config.movie_no, movie_label='테스트 영화')
        self.key = self.session.notification_key(site_no=self.config.site_no, movie_no=self.config.movie_no)
        self.watcher.state.mark_notified(self.key, self.session)
        self.watcher.state.set_seat_snapshot(self.key, snapshot(('J', '20'), ('J', '21')))
        clock = patch.object(Config, 'local_now', return_value=dt.datetime(2026, 11, 12, 15, 0,
                                       tzinfo=dt.timezone(dt.timedelta(hours=9))))
        clock.start()
        self.addCleanup(clock.stop)

    def make_watcher(self):
        watcher = Watcher(self.config, logger=logging.getLogger('new-sweet-seat-test'))
        watcher.cgv = Mock()
        watcher.telegram = Mock()
        watcher._forces_seat_recheck = Mock(return_value=True)
        return watcher

    def check(self, current, error=None):
        before = self.watcher.telegram.send_message.call_count
        self.watcher.cgv.fetch_seat_snapshot.side_effect = error
        self.watcher.cgv.fetch_seat_snapshot.return_value = current
        tally = _CycleTally()
        self.watcher._alert_for_sessions([dataclasses.replace(self.session, remaining_seats=current.total)], tally)
        return self.watcher.telegram.send_message.call_count - before, tally

    def test_unchanged_seats_and_reordered_response_never_repeat(self):
        for seats in ((('J', '20'), ('J', '21')), (('J', '21'), ('J', '20')), (('J', '20'), ('J', '21'))):
            count, tally = self.check(snapshot(*seats))
            self.assertEqual(count, 0)
            self.assertEqual(tally.seat_changes, 0)
        self.assertEqual(self.watcher.cgv.fetch_seat_snapshot.call_count, 3)

    def test_removals_update_baseline_and_a_returning_seat_alerts_again(self):
        self.assertEqual(self.check(snapshot(('J', '20')))[0], 0)
        self.assertEqual(self.watcher.state.seat_snapshot(self.key).available_seats, (('J', '20'),))
        self.assertEqual(self.check(snapshot(('J', '20'), ('J', '21')))[0], 1)
        self.assertEqual(self.check(snapshot(('J', '20'), ('J', '21')))[0], 0)

    def test_same_count_with_a_new_seat_alerts_once(self):
        current = snapshot(('J', '20'), ('J', '22'))
        self.assertEqual(self.check(current)[0], 1)
        self.assertIn('J20, 22', self.watcher.telegram.send_message.call_args.args[0])
        self.assertEqual(self.check(current)[0], 0)

    def test_total_can_decrease_while_new_sweet_seat_appears(self):
        self.assertEqual(self.check(snapshot(('K', '20')))[0], 1)

    def test_changes_outside_sweet_spots_do_not_alert(self):
        for ordinary in (('B', '5'), ('A', '1'), ('J', '35')):
            self.assertEqual(self.check(snapshot(('J', '20'), ('J', '21'), ordinary))[0], 0)

    def test_sold_out_or_no_sweet_then_reappearance_alerts(self):
        for empty in (snapshot(), snapshot(('A', '1')), snapshot(('B', '1'))):
            with self.subTest(empty=empty.available_seats):
                self.assertEqual(self.check(empty)[0], 0)
                self.assertEqual(self.check(snapshot(('J', '20')))[0], 1)

    def test_first_complete_map_is_announced_once(self):
        for old in (None, SeatSnapshot(total=2)):
            with self.subTest(previous=old):
                if old is None:
                    self.watcher.state.data['seat_counts'].pop(self.key, None)
                else:
                    self.watcher.state.set_seat_snapshot(self.key, old)
                current = snapshot(('J', '20'), ('J', '21'))
                self.assertEqual(self.check(current)[0], 1)
                self.assertEqual(self.check(current)[0], 0)

    def test_failed_or_incomplete_map_keeps_verified_baseline(self):
        previous = self.watcher.state.seat_snapshot(self.key)
        incomplete = [SeatSnapshot(total=8),
                      SeatSnapshot(total=2, usable=2, mapped_total=2, available_rows=('J',))]
        for current, error in ((incomplete[0], FetchError('timeout')),
                               (incomplete[0], RuntimeError('test')),
                               (incomplete[0], None), (incomplete[1], None)):
            with self.subTest(current=current, error=error):
                count, tally = self.check(current, error)
                self.assertEqual(count, 0)
                self.assertIn(self.key, tally.deferred_keys)
                self.assertEqual(self.watcher.state.seat_snapshot(self.key), previous)
                self.assertEqual(self.check(previous)[0], 0)
        self.assertEqual(self.check(snapshot(('J', '20'), ('J', '22')))[0], 1)

    def test_minimum_counts_current_sweet_seats_not_number_of_new_seats(self):
        self.watcher.state.set_min_seats(self.config.telegram_chat_id, 2)
        self.assertEqual(self.check(snapshot(('J', '22')))[0], 0)
        self.assertEqual(self.check(snapshot(('J', '22'), ('J', '23')))[0], 1)

    def test_saved_baseline_survives_restart_without_realert(self):
        self.check(snapshot(('J', '20')))
        self.watcher.state.save()
        self.watcher = self.make_watcher()
        self.assertEqual(self.check(snapshot(('J', '20')))[0], 0)
        self.assertEqual(self.check(snapshot(('J', '20'), ('J', '21')))[0], 1)

    def test_unchanged_scan_does_not_requeue_after_delivery(self):
        with patch.object(self.watcher, '_delivery_worker_running', return_value=True):
            self.check(snapshot(('J', '20'), ('J', '22')))
            pending = self.watcher.state.pending_deliveries()
            self.assertEqual(len(pending), 1)
            self.check(snapshot(('J', '20'), ('J', '22')))
            self.assertEqual(len(self.watcher.state.pending_deliveries()), 1)
            self.watcher._retry_pending_deliveries(pending)
            self.assertEqual(self.watcher.telegram.send_message.call_count, 1)
            self.assertEqual(self.watcher.state.pending_deliveries(), ())
            self.check(snapshot(('J', '20'), ('J', '22')))
            self.assertEqual(self.watcher.state.pending_deliveries(), ())

    def test_opening_still_sends_without_a_seat_lookup(self):
        for remaining in (0, 2, 624):
            with self.subTest(remaining=remaining):
                self.watcher = self.make_watcher()
                self.watcher.state.set_min_seats(self.config.telegram_chat_id, 2)
                self.watcher.state.data['notified'].pop(self.key, None)
                self.assertEqual(self.check(SeatSnapshot(total=remaining))[0], 1)
                self.assertIn('예매 오픈 감지', self.watcher.telegram.send_message.call_args.args[0])
                self.watcher.cgv.fetch_seat_snapshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
