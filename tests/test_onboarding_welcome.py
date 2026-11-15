import dataclasses
import datetime as dt
import logging
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock

from movie_features import public_guide, public_welcome
from test_watcher import make_config
from watcher import Config, TelegramError, Watcher


class OnboardingWelcomeTests(unittest.TestCase):
    CHAT = '12345'
    FILM = '30009999'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(make_config(Path(temporary.name)),
            multi_movie_enabled=True, subscriptions_enabled=True, seat_alert_sweet_only=True)
        self.watcher = self.restart()
        self.watcher.state.data['movie_catalog'].update({
            self.FILM: {'title': '선택한 영화'}, '30008888': {'title': '다른 영화'}})
        self.watcher.state.data['movie_catalog_initialized'] = True

    def restart(self):
        watcher = Watcher(self.config, logger=logging.getLogger('welcome-tests'))
        watcher.telegram = Mock()
        watcher.cgv = Mock()
        return watcher

    def command(self, command='/start', chat=CHAT, chat_type='private'):
        self.watcher.telegram.send_message.reset_mock()
        self.watcher.telegram.get_updates.return_value = [{
            'update_id': self.watcher.state.telegram_update_offset,
            'message': {'text': command, 'chat': {'id': chat, 'type': chat_type}}}]
        self.watcher.sync_subscribers()

    def callback(self, data=None, chat=CHAT):
        self.watcher._handle_movie_callback({'id': 'test',
            'data': data or f'mv:{self.FILM}:1:{self.watcher.state.movie_revision(chat)}',
            'message': {'chat': {'id': chat, 'type': 'private'}},
            'from': {'id': chat, 'is_bot': False}})

    def pending(self, category=None, chat=CHAT):
        return [(key, r) for key, r in sorted(self.watcher.state.pending_deliveries(),
            key=Watcher._delivery_sort_key) if r['chat_id'] == chat
            and (category is None or r['category'] == category)]

    def drain(self):
        for _ in range(30):
            if not self.watcher.state.pending_delivery_count():
                return
            self.watcher._retry_pending_deliveries()
        self.fail('Queue did not drain')

    def test_new_subscription_gets_showings_status_then_short_welcome_in_both_languages(self):
        for language, chat in [('ko', self.CHAT), ('en', '12346')]:
            with self.subTest(language=language):
                self.watcher.state.set_language(chat, language)
                self.command(chat=chat)
                self.assertEqual(self.pending(chat=chat), [])
                self.assertTrue(self.watcher.state.data['subscribers'][chat]['welcome_pending'])
                self.callback(chat=chat)
                messages = [r for _, r in self.pending(chat=chat)]
                self.assertEqual([r['category'] for r in messages],
                                 ['movie_summary', 'movie_summary', 'movie_welcome'])
                self.assertIn('최근 조회한 열린 회차' if language == 'ko'
                              else 'Last observed upcoming showings', messages[0]['text'])
                self.assertEqual(messages[1]['text'], self.watcher._status_reply(chat))
                self.assertEqual(messages[2]['text'], public_welcome(language, self.config))
                self.assertNotEqual(messages[2]['text'], public_guide(language, self.config))
                self.watcher.telegram.send_message.reset_mock()
                self.drain()
                self.assertEqual([c.args[0] for c in self.watcher.telegram.send_message.call_args_list],
                                 [r['text'] for r in messages])
        self.assertEqual(self.watcher.cgv.mock_calls, [])

    def test_existing_subscriber_with_no_movies_does_not_get_welcome(self):
        self.watcher.state.add_subscriber(self.CHAT, initial_movies=())
        self.command()
        self.callback()
        self.assertNotIn('welcome_pending', self.watcher.state.data['subscribers'][self.CHAT])
        self.assertEqual([r['category'] for _, r in self.pending()], ['movie_summary'] * 2)

    def test_legacy_subscriber_is_not_marked_new_on_start(self):
        chat = self.config.telegram_chat_id
        before = self.watcher.state.selected_movies(chat)
        self.command(chat=chat)
        self.callback(chat=chat)
        self.assertNotIn('welcome_pending', self.watcher.state.data['subscribers'][chat])
        self.assertEqual(self.pending('movie_welcome', chat=chat), [])
        self.assertTrue(set(before).issubset(self.watcher.state.selected_movies(chat)))

    def test_start_alias_and_restart_keep_intro_pending_until_first_movie(self):
        self.command('/subscribe')
        self.command('/start@YongamBot')
        self.callback('mp:0')
        self.assertEqual(self.pending('movie_welcome'), [])
        self.watcher = self.restart()
        self.command('/language en')
        self.callback()
        self.assertEqual(self.pending('movie_welcome')[0][1]['text'], public_welcome('en', self.config))
        self.assertFalse(self.watcher.state.data['subscribers'][self.CHAT]['welcome_pending'])

    def test_duplicate_buttons_more_movies_and_start_do_not_repeat_intro(self):
        self.command()
        self.callback(f'mv:{self.FILM}:1:0')
        self.callback(f'mv:{self.FILM}:1:0')
        self.callback('mv:30008888:1')
        self.command()
        self.assertEqual(len(self.pending('movie_welcome')), 1)
        self.drain()
        self.watcher = self.restart()
        self.command()
        self.callback(f'mv:{self.FILM}:0')
        self.callback(f'mv:{self.FILM}:1')
        self.assertEqual(self.pending('movie_welcome'), [])

    def test_queued_intro_survives_restart_without_duplication(self):
        self.command()
        self.callback()
        expected = [r['text'] for _, r in self.pending()]
        self.watcher = self.restart()
        self.drain()
        self.assertEqual([c.args[0] for c in self.watcher.telegram.send_message.call_args_list], expected)
        self.command()
        self.callback('mv:30008888:1')
        self.assertEqual(self.pending('movie_welcome'), [])

    def test_intro_waits_for_failed_status_and_keeps_retry_order(self):
        self.command()
        self.callback()
        self.watcher._retry_pending_deliveries()  # Showings.
        status, welcome = self.pending()
        self.watcher.telegram.send_message.reset_mock()
        self.watcher.telegram.send_message.side_effect = TelegramError('test failure')
        self.watcher._retry_pending_deliveries()  # Status fails.
        self.watcher.telegram.send_message.reset_mock(side_effect=True)
        self.watcher._retry_pending_deliveries()
        self.watcher.telegram.send_message.assert_not_called()
        candidates = self.watcher._ordered_delivery_candidates(self.watcher.state.pending_deliveries())
        self.assertNotIn(welcome[0], [key for key, _ in candidates])
        self.watcher.state.defer_pending_delivery(status[0],
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))
        self.drain()
        self.assertEqual([c.args[0] for c in self.watcher.telegram.send_message.call_args_list],
                         [status[1]['text'], welcome[1]['text']])

    def test_stop_cancels_welcome_and_resubscription_queues_only_one_new_intro(self):
        self.command()
        self.callback()
        self.command('/stop')
        self.assertEqual(self.pending('movie_welcome'), [])
        self.drain()
        self.command()
        self.callback()
        self.assertEqual(len(self.pending('movie_welcome')), 1)

    def test_unselecting_film_does_not_duplicate_or_discard_generic_intro(self):
        self.command()
        self.callback()
        self.callback(f'mv:{self.FILM}:0')
        self.assertEqual(self.pending('movie_summary'), [])
        self.assertEqual(len(self.pending('movie_welcome')), 1)
        self.callback('mv:30008888:1')
        self.assertEqual(len(self.pending('movie_welcome')), 1)

    def test_closed_signup_and_group_start_do_not_create_intro(self):
        self.config = dataclasses.replace(self.config, new_subscriptions_enabled=False)
        self.watcher = self.restart()
        self.command()
        self.assertFalse(self.watcher.state.is_subscribed(self.CHAT))
        self.config = dataclasses.replace(self.config, new_subscriptions_enabled=True)
        self.watcher = self.restart()
        self.command(chat='-12345', chat_type='group')
        self.assertNotIn('welcome_pending', self.watcher.state.data['subscribers']['-12345'])

    def test_single_movie_mode_keeps_its_existing_start_reply(self):
        self.config = dataclasses.replace(self.config, multi_movie_enabled=False)
        self.watcher = self.restart()
        self.command()
        self.assertNotIn('welcome_pending', self.watcher.state.data['subscribers'][self.CHAT])
        self.assertEqual(self.pending('movie_welcome'), [])

    def test_intro_commands_are_clickable_localized_and_fit_telegram(self):
        for language in ('ko', 'en'):
            for open_only in (False, True):
                text = public_welcome(language, dataclasses.replace(self.config, open_only_mode=open_only))
                self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)
                self.assertIsNone(re.search(r'/[a-z_]+[가-힣ㄱ-ㅎㅏ-ㅣ]', text))
                self.assertIn('/language', text)
                self.assertIn('/desc', text)
                self.assertEqual('/count' in text, not open_only)


if __name__ == '__main__':
    unittest.main()
