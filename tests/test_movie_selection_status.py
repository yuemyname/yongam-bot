import copy
import dataclasses
import datetime as dt
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_watcher import make_config
from watcher import ALERT_MODE_ALL, ALERT_MODE_OPEN_ONLY, BookingSession, Config, TelegramError, Watcher


class MovieSelectionStatusTests(unittest.TestCase):
    CHAT = '12345'
    FILM = '30009999'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(make_config(Path(temporary.name)),
            multi_movie_enabled=True, subscriptions_enabled=True, seat_alert_sweet_only=True)
        self.watcher = self.restart()
        self.watcher.state.data['movie_catalog'][self.FILM] = {'title': '선택 영화'}
        self.watcher.state.data['movie_catalog_initialized'] = True
        self.watcher.state.add_subscriber(self.CHAT, initial_movies=(),
                                          initial_alert_mode=ALERT_MODE_OPEN_ONLY)
        clock = patch.object(Config, 'local_now', return_value=dt.datetime(
            2026, 11, 15, 12, tzinfo=dt.timezone(dt.timedelta(hours=9))))
        clock.start()
        self.addCleanup(clock.stop)

    def restart(self):
        watcher = Watcher(self.config, logger=logging.getLogger('movie-selection-status'))
        watcher.telegram = Mock()
        watcher.cgv = Mock()
        return watcher

    def callback(self, data=None, chat=CHAT):
        self.watcher._handle_movie_callback({'id': 'callback',
            'data': data or f'mv:{self.FILM}:1:{self.watcher.state.movie_revision(chat)}',
            'message': {'chat': {'id': chat, 'type': 'private'}},
            'from': {'id': chat, 'is_bot': False}})

    def pending(self, chat=CHAT):
        return [(key, record) for key, record in sorted(
            self.watcher.state.pending_deliveries(), key=Watcher._delivery_sort_key)
            if record['chat_id'] == chat and record['category'] == 'movie_summary']

    def cache(self, days=1):
        for index in range(days):
            date = (dt.date(2026, 11, 16) + dt.timedelta(days=index)).isoformat()
            self.watcher.state.data['movie_sessions'][date] = {
                'checked_at': '2026-11-15T12:00:00+09:00',
                'sessions': [dataclasses.asdict(BookingSession(date=date, start_time=time,
                    movie_no=self.FILM, remaining_seats=count, total_seats=624))
                    for time, count in [('14:30', 20), ('18:00', 7)]]}

    def drain(self):
        for _ in range(50):
            if not self.watcher.state.pending_delivery_count():
                return
            self.watcher._retry_pending_deliveries()
        self.fail('Queue did not drain')

    def test_selection_sends_actual_status_after_grouped_showings_in_both_languages(self):
        self.cache()
        for language, chat in [('ko', self.CHAT), ('en', '12346')]:
            with self.subTest(language=language):
                self.watcher.state.add_subscriber(chat, initial_movies=(),
                                                  initial_alert_mode=ALERT_MODE_OPEN_ONLY)
                self.watcher.state.set_language(chat, language)
                self.callback(chat=chat)
                messages = [record for _, record in self.pending(chat)]
                self.assertEqual(len(messages), 2)
                self.assertIn('2026-11-16\n14:30 - 20', messages[0]['text'])
                self.assertEqual(messages[1]['text'], self.watcher._status_reply(chat))
                self.assertIn('취소표 알림도 받고 싶다면 /mode ' if language == 'ko'
                              else 'Want cancellation-seat alerts too? Use /mode ', messages[1]['text'])
                self.assertIsNone(messages[0]['reply_markup'])
                self.assertTrue(any(b['text'].startswith('✅')
                    for row in messages[1]['reply_markup']['inline_keyboard'] for b in row))
                self.assertFalse(any(r['category'] == 'reply' for _, r
                                     in self.watcher.state.pending_deliveries()))
        self.assertEqual(self.watcher.cgv.mock_calls, [])

    def test_existing_subscriber_filters_and_language_are_preserved(self):
        state = self.watcher.state
        state.set_alert_mode(self.CHAT, ALERT_MODE_ALL)
        state.set_show_day_selection(self.CHAT, 'weekend')
        state.set_min_seats(self.CHAT, 2)
        state.set_seat_date_range(self.CHAT, ('2026-11-20', None))
        state.set_seat_time_range(self.CHAT, ('12:00', '23:00'))
        state.set_language(self.CHAT, 'en')
        before = copy.deepcopy(state.data['subscribers'][self.CHAT])
        self.callback()
        after = state.data['subscribers'][self.CHAT]
        for key in before.keys() - {'movies', 'movie_revision'}:
            self.assertEqual(before[key], after[key])
        self.assertEqual(self.pending()[-1][1]['text'], self.watcher._status_reply(self.CHAT))
        self.assertEqual(state.language(self.CHAT), 'en')

    def test_no_cached_showings_still_sends_status(self):
        self.callback()
        messages = [r['text'] for _, r in self.pending()]
        self.assertEqual(len(messages), 2)
        self.assertIn('아직 조회된 예정 회차가 없습니다', messages[0])
        self.assertEqual(messages[1], self.watcher._status_reply(self.CHAT))

    def test_multi_part_showings_then_status_survive_restart_and_parallel_sender(self):
        self.cache(days=30)
        self.callback()
        expected = [r['text'] for _, r in self.pending()]
        self.assertGreater(len(expected), 2)
        self.assertEqual(expected[-1], self.watcher._status_reply(self.CHAT))
        self.watcher = self.restart()
        self.drain()
        sent = [c.args[0] for c in self.watcher.telegram.send_message.call_args_list]
        self.assertEqual(sent, expected)
        self.assertEqual(self.watcher.cgv.mock_calls, [])

    def test_failed_showing_blocks_its_status_but_not_other_chats_or_openings(self):
        self.callback()
        first, status = self.pending()
        self.watcher.telegram.send_message.side_effect = TelegramError('temporary failure')
        self.watcher._retry_pending_deliveries()
        self.assertEqual(self.watcher.telegram.send_message.call_count, 1)
        self.assertEqual(self.watcher.telegram.send_message.call_args.args[0], first[1]['text'])
        self.watcher.telegram.send_message.reset_mock(side_effect=True)
        state = self.watcher.state
        state.add_subscriber('12346', initial_movies=(self.FILM,), initial_alert_mode=ALERT_MODE_ALL)
        state.queue_pending_delivery('12346', 'other chat summary', 'movie_summary', movie_no=self.FILM)
        state.queue_pending_delivery(self.CHAT, 'opening', 'open', movie_no=self.FILM)
        candidates = self.watcher._ordered_delivery_candidates(state.pending_deliveries())
        self.assertNotIn(status[0], [key for key, _ in candidates])
        self.watcher._retry_pending_deliveries()
        sent = [c.args[0] for c in self.watcher.telegram.send_message.call_args_list]
        self.assertEqual(sent, ['opening', 'other chat summary'])
        self.assertIn(status[0], dict(state.pending_deliveries()))
        state.defer_pending_delivery(first[0], dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))
        self.watcher.telegram.send_message.reset_mock()
        self.drain()
        self.assertEqual([c.args[0] for c in self.watcher.telegram.send_message.call_args_list],
                         [first[1]['text'], status[1]['text']])

    def test_deselection_cancels_pending_status_too(self):
        self.callback()
        self.callback(f'mv:{self.FILM}:0:1')
        self.assertEqual(self.pending(), [])
        self.drain()
        self.assertFalse(any('알림 구독 중' in c.args[0]
                             for c in self.watcher.telegram.send_message.call_args_list))

    def test_repeated_or_stale_button_and_page_do_not_repeat_status(self):
        self.callback(f'mv:{self.FILM}:1:0')
        expected = self.pending()
        self.callback(f'mv:{self.FILM}:1:0')
        self.callback(f'mv:{self.FILM}:1')  # An old discovery button.
        self.callback('mp:0')
        self.assertEqual(self.pending(), expected)

    def test_unsubscribed_user_does_not_get_pending_status(self):
        self.callback()
        self.watcher.state.remove_subscriber(self.CHAT)
        self.drain()
        self.watcher.telegram.send_message.assert_not_called()

    def test_long_status_uses_localized_headings_and_keeps_all_text(self):
        self.watcher.state.set_language(self.CHAT, 'en')
        title = '🎬' * 3000
        self.watcher.state.data['movie_catalog'][self.FILM]['title'] = title
        self.callback()
        parts = [r for _, r in self.pending()][1:]
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(r['text'].split('\n', 1)[1] for r in parts),
                         self.watcher._status_reply(self.CHAT))
        for index, record in enumerate(parts, 1):
            self.assertTrue(record['text'].startswith(f'⚙️ Current settings {index}/{len(parts)}\n'))
            self.assertLessEqual(len(record['text'].encode('utf-16-le')) // 2, 4096)
        self.assertTrue(all(r['reply_markup'] is None for r in parts[:-1]))
        self.assertIsNotNone(parts[-1]['reply_markup'])


if __name__ == '__main__':
    unittest.main()
