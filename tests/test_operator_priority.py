import copy
import dataclasses
import datetime as dt
import logging
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from test_watcher import make_config
from watcher import (
    ALERT_MODE_OPEN_ONLY, ALERT_OPEN, ALERT_REPLY, ALERT_SEATS_SWEET,
    TelegramError, Watcher,
)


class OperatorPriorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(temporary.name)), telegram_chat_id='owner',
            telegram_broadcast_workers=16, seat_alert_sweet_only=True,
        )
        self.watcher = self.make_watcher()
        self.owner = self.config.telegram_chat_id
        for chat_id in ('A', 'B', 'C'):
            self.watcher.state.add_subscriber(chat_id)
        self.original_subscribers = copy.deepcopy(self.watcher.state.data['subscribers'])

    def make_watcher(self):
        logger = logging.getLogger(f'operator-priority-{id(self)}')
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(self.config, logger=logger)
        watcher.telegram.send_message = Mock()
        return watcher

    def enqueue(self, text='opening', category=ALERT_OPEN, **kwargs):
        with patch.object(self.watcher, '_delivery_worker_running', return_value=True):
            return self.watcher._broadcast_message(text, category=category, **kwargs)

    def queue_order(self, text):
        return [r['chat_id'] for _, r in sorted(
            self.watcher.state.pending_deliveries(), key=Watcher._delivery_sort_key
        ) if r['text'] == text]

    def sent_ids(self):
        return [c.kwargs['chat_id'] for c in self.watcher.telegram.send_message.call_args_list]

    def test_owner_is_first_without_consuming_subscriber_rotation(self):
        for index, others in enumerate((['A', 'B', 'C'], ['B', 'C', 'A'], ['C', 'A', 'B'])):
            self.enqueue(str(index))
            self.assertEqual(self.queue_order(str(index)), [self.owner, *others])
        self.assertEqual(self.watcher.state.data['subscribers'], self.original_subscribers)

    def test_duplicate_and_owner_only_batches_do_not_advance_ring(self):
        self.enqueue('first')
        before = copy.deepcopy(self.watcher.state.data)
        self.enqueue('first')
        self.assertEqual(self.watcher.state.data, before)
        self.enqueue('owner-only', recipients=(self.owner, self.owner))
        self.assertEqual(self.queue_order('owner-only'), [self.owner])
        self.assertEqual(self.watcher.state.data['broadcast_rotation'], before['broadcast_rotation'])
        self.enqueue('second')
        self.assertEqual(self.queue_order('second'), [self.owner, 'B', 'C', 'A'])

    def test_restart_preserves_owner_priority_and_other_subscriber_cursor(self):
        self.enqueue('first')
        self.watcher = self.make_watcher()
        self.enqueue('second')
        self.assertEqual(self.queue_order('second'), [self.owner, 'B', 'C', 'A'])
        self.assertEqual(self.watcher.state.data['subscribers'], self.original_subscribers)

    def test_subscription_mode_movie_and_explicit_recipients_are_not_bypassed(self):
        self.watcher.state.set_alert_mode(self.owner, ALERT_MODE_OPEN_ONLY)
        self.enqueue('seats', ALERT_SEATS_SWEET, seats_available=1)
        self.assertNotIn(self.owner, self.queue_order('seats'))
        self.watcher.state.select_movie(self.owner, self.config.movie_no, False)
        self.enqueue('unselected', movie_no=self.config.movie_no)
        self.assertNotIn(self.owner, self.queue_order('unselected'))
        self.enqueue('explicit', recipients=('B', 'A'))
        self.assertEqual(set(self.queue_order('explicit')), {'A', 'B'})
        self.watcher.state.remove_subscriber(self.owner)
        self.enqueue('unsubscribed')
        self.assertNotIn(self.owner, self.queue_order('unsubscribed'))

    def test_filter_change_before_send_can_remove_operator(self):
        self.enqueue('seats', ALERT_SEATS_SWEET, seats_available=1)
        self.watcher.state.set_alert_mode(self.owner, ALERT_MODE_OPEN_ONLY)
        self.watcher._retry_pending_deliveries()
        self.assertEqual(set(self.sent_ids()), {'A', 'B', 'C'})
        self.assertEqual(self.watcher.state.pending_delivery_count(), 0)

    def assert_owner_attempt_finishes_first(self, queued):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def send(_text, *, chat_id, **kwargs):
            calls.append(chat_id)
            if chat_id == self.owner:
                entered.set()
                if not release.wait(timeout=2):
                    raise RuntimeError('test owner timeout')
                finished.set()
            else:
                self.assertTrue(finished.is_set())

        self.watcher.telegram.send_message = send
        if queued:
            self.enqueue()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.watcher._retry_pending_deliveries if queued
                                     else lambda: self.watcher._broadcast_message('opening', category=ALERT_OPEN))
            try:
                self.assertTrue(entered.wait(timeout=1))
                self.assertEqual(calls, [self.owner])
                if queued:
                    # CGV-side enqueue remains non-blocking while the operator
                    # network attempt is still running in the delivery worker.
                    self.enqueue('next-opening')
            finally:
                release.set()
            future.result(timeout=2)
        self.assertEqual(calls[0], self.owner)
        self.assertCountEqual(calls, [self.owner, 'A', 'B', 'C'])

    def test_parallel_synchronous_send_waits_for_owner_attempt(self):
        self.assert_owner_attempt_finishes_first(queued=False)

    def test_parallel_queued_send_waits_for_owner_and_does_not_block_enqueue(self):
        self.assert_owner_attempt_finishes_first(queued=True)

    def test_opening_priority_still_precedes_operator_seat_alert(self):
        self.enqueue('seats', ALERT_SEATS_SWEET, seats_available=1)
        self.enqueue('opening')
        self.watcher._retry_pending_deliveries()
        calls = self.watcher.telegram.send_message.call_args_list
        self.assertEqual([c.args[0] for c in calls], ['opening'] * 4 + ['seats'] * 4)
        self.assertEqual([calls[0].kwargs['chat_id'], calls[4].kwargs['chat_id']], [self.owner] * 2)

    def test_recipient_specific_failure_does_not_stop_others_or_repeat_their_sends(self):
        def send(_text, *, chat_id, **kwargs):
            if chat_id == self.owner:
                raise TelegramError('test timeout')

        self.watcher.telegram.send_message.side_effect = send
        self.enqueue()
        self.watcher._retry_pending_deliveries()
        self.assertEqual(self.sent_ids()[0], self.owner)
        self.assertCountEqual(self.sent_ids(), [self.owner, 'A', 'B', 'C'])
        pending = self.watcher.state.pending_deliveries()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][1]['chat_id'], self.owner)
        self.watcher.telegram.send_message.reset_mock()
        self.watcher._retry_pending_deliveries()
        self.watcher.telegram.send_message.assert_not_called()  # honor retry backoff
        self.watcher.state.data['pending_deliveries'][pending[0][0]]['next_attempt_at'] = ''
        self.watcher.telegram.send_message.side_effect = None
        self.watcher._retry_pending_deliveries()
        self.assertEqual(self.sent_ids(), [self.owner])
        self.assertEqual(self.watcher.state.pending_delivery_count(), 0)

    def test_operator_429_preserves_global_pause(self):
        self.enqueue()
        self.watcher.telegram.send_message.side_effect = TelegramError(
            'test rate limit', status_code=429, retry_after_seconds=60
        )
        self.watcher._retry_pending_deliveries()
        self.assertEqual(self.sent_ids(), [self.owner])
        self.assertEqual(self.watcher.state.pending_delivery_count(), 4)
        self.assertIsNotNone(self.watcher._telegram_pause_until())
        self.assertTrue(all(r['attempts'] == 0 for _, r in self.watcher.state.pending_deliveries()))

    def test_owner_in_retry_backoff_does_not_delay_due_subscribers(self):
        self.enqueue()
        for record in self.watcher.state.data['pending_deliveries'].values():
            if record['chat_id'] == self.owner:
                record['next_attempt_at'] = (dt.datetime.now(dt.timezone.utc)
                                            + dt.timedelta(hours=1)).isoformat()
        self.watcher._retry_pending_deliveries()
        self.assertCountEqual(self.sent_ids(), ['A', 'B', 'C'])
        self.assertEqual(self.watcher.state.pending_delivery_count(), 1)

    def test_priority_does_not_skip_rate_limiter(self):
        self.enqueue()
        with patch.object(self.watcher._telegram_broadcast_limiter, 'wait') as limiter:
            self.watcher._retry_pending_deliveries()
        self.assertEqual(limiter.call_count, 4)
        self.assertEqual(self.sent_ids()[0], self.owner)

    def test_old_queue_promotes_owner_before_batch_cut_without_mutating_records(self):
        # The operator used to be last in a large broadcast; its queue record
        # must not wait behind a 25-message batch selected before promotion.
        for index in range(30):
            chat = f'user-{index}'
            self.watcher.state.add_subscriber(chat)
            self.watcher.state.queue_pending_delivery(chat, 'old-opening', ALERT_OPEN)
        self.watcher.state.queue_pending_delivery(self.owner, 'old-opening', ALERT_OPEN)
        before = copy.deepcopy(self.watcher.state.data)
        ordered = sorted(self.watcher.state.pending_deliveries(),
                         key=self.watcher._prioritized_delivery_sort_key)
        self.assertEqual(ordered[0][1]['chat_id'], self.owner)
        self.assertEqual(self.watcher.state.data, before)
        self.watcher.state.save()
        self.config = dataclasses.replace(self.config, telegram_broadcast_rate_per_second=1)
        self.watcher = self.make_watcher()
        arrived = threading.Event()

        def send(_text, **kwargs):
            self.assertEqual(kwargs['chat_id'], self.owner)
            self.watcher._delivery_stop_event.set()
            arrived.set()

        self.watcher.telegram.send_message = send
        self.watcher.start_delivery_worker()
        try:
            self.assertTrue(arrived.wait(timeout=2))
        finally:
            self.watcher.stop_delivery_worker(timeout=2)
        self.assertEqual(self.watcher.state.pending_delivery_count(), 30)

    def test_reply_order_is_not_promoted_or_rotated(self):
        store = self.watcher.state
        before = copy.deepcopy(store.data['broadcast_rotation'])
        result = store.rotate_broadcast_recipients(
            ALERT_REPLY, ('B', self.owner, 'A'), priority_recipient=self.owner
        )
        self.assertEqual(result, ('B', self.owner, 'A'))
        self.assertEqual(store.data['broadcast_rotation'], before)


if __name__ == '__main__':
    unittest.main()
