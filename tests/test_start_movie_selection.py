import copy
import dataclasses
import datetime as dt
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_watcher import make_config
from watcher import (ALERT_MODE_ALL, ALERT_MODE_OPEN_ONLY, ALERT_MODE_SEATS_ONLY,
                     ALERT_OPEN, ALERT_SEATS_SWEET, TelegramDeferred, Watcher)


class StartMovieSelectionTests(unittest.TestCase):
    NEW_CHAT = '12345'
    FILM = '30009999'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(make_config(Path(temporary.name)),
            multi_movie_enabled=True, subscriptions_enabled=True, seat_alert_sweet_only=True)
        self.watcher = self.make_watcher()
        self.watcher.state.data['movie_catalog'][self.FILM] = {'title': '새 영화'}
        self.watcher.state.data['movie_catalog_initialized'] = True

    def make_watcher(self):
        watcher = Watcher(self.config, logger=logging.getLogger('start-movies-test'))
        watcher.telegram = Mock()
        watcher.cgv = Mock()
        return watcher

    def command(self, command='/start', chat_id=NEW_CHAT, chat_type='private'):
        self.watcher.telegram.send_message.reset_mock()
        self.watcher.telegram.get_updates.return_value = [{
            'update_id': self.watcher.state.telegram_update_offset,
            'message': {'text': command, 'chat': {'id': chat_id, 'type': chat_type}},
        }]
        self.watcher.sync_subscribers()
        return self.watcher.telegram.send_message.call_args

    def callback(self, data, chat_id=NEW_CHAT):
        self.watcher._handle_movie_callback({
            'id': 'test-callback', 'data': data,
            'message': {'chat': {'id': chat_id, 'type': 'private'}},
            'from': {'id': chat_id, 'is_bot': False},
        })

    def test_new_start_has_empty_selection_and_unchecked_movie_buttons(self):
        call = self.command()
        self.assertEqual(self.watcher.state.selected_movies(self.NEW_CHAT), ())
        self.assertEqual(self.watcher.state.data['subscribers'][self.NEW_CHAT]['movies'], [])
        self.assertEqual(self.watcher.state.alert_mode(self.NEW_CHAT), ALERT_MODE_OPEN_ONLY)
        self.assertIn('알림 종류: 신규 오픈만', call.args[0])
        self.assertIn('영화는 자동 선택되지 않습니다', call.args[0])
        self.assertIn('현재: 없음', call.args[0])
        buttons = [b for row in call.kwargs['reply_markup']['inline_keyboard'] for b in row]
        self.assertTrue(any(b['callback_data'] == f'mv:{self.FILM}:1:0' for b in buttons))
        self.assertTrue(all(not b['text'].startswith('✅') for b in buttons))
        self.assertEqual(self.watcher.cgv.mock_calls, [])

    def test_no_open_or_seat_alerts_until_movie_is_selected(self):
        self.command()
        for film in (self.config.movie_no, self.FILM):
            for category in (ALERT_OPEN, ALERT_SEATS_SWEET):
                self.assertNotIn(self.NEW_CHAT, self.watcher.state.subscriber_ids_for(
                    category, movie_no=film, seats_available=2))
        self.callback(f'mv:{self.FILM}:1:0')
        self.assertEqual(self.watcher.state.selected_movies(self.NEW_CHAT), (self.FILM,))
        self.assertIn(self.NEW_CHAT, self.watcher.state.subscriber_ids_for(
            ALERT_OPEN, movie_no=self.FILM))
        self.assertNotIn(self.NEW_CHAT, self.watcher.state.subscriber_ids_for(
            ALERT_OPEN, movie_no=self.config.movie_no))
        self.assertNotIn(self.NEW_CHAT, self.watcher.state.subscriber_ids_for(
            ALERT_SEATS_SWEET, movie_no=self.FILM, seats_available=2))
        self.assertEqual(self.watcher.cgv.mock_calls, [])

    def test_legacy_selection_and_filters_survive_repeated_start(self):
        owner = self.config.telegram_chat_id
        self.assertNotIn('movies', self.watcher.state.data['subscribers'][owner])
        self.watcher.state.set_min_seats(owner, 2)
        self.watcher.state.set_show_day_selection(owner, 'weekend')
        before = copy.deepcopy(self.watcher.state.data['subscribers'][owner])
        call = self.command(chat_id=owner)
        self.assertEqual(self.watcher.state.data['subscribers'][owner], before)
        self.assertEqual(self.watcher.state.selected_movies(owner), (self.config.movie_no,))
        self.assertIn('기존 영화 선택과 알림 설정은 유지', call.args[0])
        self.assertTrue(any(b['text'].startswith('✅') for row in
            call.kwargs['reply_markup']['inline_keyboard'] for b in row))

    def test_empty_selection_and_explicit_choices_survive_restart(self):
        self.command()
        self.watcher = self.make_watcher()
        self.assertEqual(self.watcher.state.selected_movies(self.NEW_CHAT), ())
        self.assertEqual(self.watcher.state.alert_mode(self.NEW_CHAT), ALERT_MODE_OPEN_ONLY)
        self.callback(f'mv:{self.FILM}:1:0')
        before = copy.deepcopy(self.watcher.state.data['subscribers'][self.NEW_CHAT])
        self.watcher = self.make_watcher()
        self.command()
        self.assertEqual(self.watcher.state.data['subscribers'][self.NEW_CHAT], before)

    def test_stop_then_rejoin_starts_empty_and_keeps_language(self):
        self.command('/language en')
        self.command()
        self.callback(f'mv:{self.FILM}:1:0')
        self.command('/stop')
        call = self.command()
        self.assertEqual(self.watcher.state.selected_movies(self.NEW_CHAT), ())
        self.assertEqual(self.watcher.state.language(self.NEW_CHAT), 'en')
        self.assertEqual(self.watcher.state.alert_mode(self.NEW_CHAT), ALERT_MODE_OPEN_ONLY)
        self.assertIn('No movie is selected automatically', call.args[0])

    def test_mode_opt_in_and_repeated_start_preserve_existing_preferences(self):
        self.command()
        self.callback(f'mv:{self.FILM}:1:0')
        self.command('/mode_all')
        self.assertIn(self.NEW_CHAT, self.watcher.state.subscriber_ids_for(
            ALERT_SEATS_SWEET, movie_no=self.FILM, seats_available=2))
        for mode in (ALERT_MODE_ALL, ALERT_MODE_SEATS_ONLY):
            self.watcher.state.set_alert_mode(self.NEW_CHAT, mode)
            before = copy.deepcopy(self.watcher.state.data['subscribers'][self.NEW_CHAT])
            self.command()
            self.assertEqual(self.watcher.state.data['subscribers'][self.NEW_CHAT], before)

    def test_status_explains_cancellation_opt_in_in_both_languages(self):
        self.command()
        for language in ('ko', 'en'):
            self.watcher.state.set_language(self.NEW_CHAT, language)
            text = self.command('/status').args[0]
            self.assertIn('알림 종류: 신규 오픈만' if language == 'ko' else 'Alerts: Openings only', text)
            self.assertIn('취소표 알림도 받고 싶다면 /mode ' if language == 'ko'
                          else 'Want cancellation-seat alerts too? Use /mode ', text)
            self.assertNotRegex(text, r'/[A-Za-z0-9_]+[가-힣ㄱ-ㅎㅏ-ㅣ]')

    def test_single_movie_new_subscriber_defaults_to_openings_and_status_guides_mode(self):
        self.config = dataclasses.replace(self.config, multi_movie_enabled=False)
        self.watcher = self.make_watcher()
        self.command()
        self.assertEqual(self.watcher.state.alert_mode(self.NEW_CHAT), ALERT_MODE_OPEN_ONLY)
        self.assertIn('취소표 알림도 받고 싶다면 /mode ', self.command('/status').args[0])

    def test_aliases_and_bot_mentions_show_the_picker(self):
        for command in ('/subscribe', '/start@YongamBot', '/start referral'):
            with self.subTest(command=command):
                self.watcher.state.remove_subscriber(self.NEW_CHAT)
                call = self.command(command)
                self.assertEqual(self.watcher.state.selected_movies(self.NEW_CHAT), ())
                self.assertIn('inline_keyboard', call.kwargs['reply_markup'])

    def test_closed_signups_do_not_register_or_offer_picker(self):
        self.config = dataclasses.replace(self.config, new_subscriptions_enabled=False)
        self.watcher = self.make_watcher()
        for language in ('ko', 'en'):
            self.watcher.state.set_language(self.NEW_CHAT, language)
            call = self.command()
            self.assertFalse(self.watcher.state.is_subscribed(self.NEW_CHAT))
            self.assertNotIn('reply_markup', call.kwargs)
        call = self.command(chat_id=self.config.telegram_chat_id)
        self.assertIn('inline_keyboard', call.kwargs['reply_markup'])

    def test_group_start_does_not_add_inoperable_buttons_or_default_movie(self):
        call = self.command(chat_id='-12345', chat_type='group')
        self.assertEqual(self.watcher.state.selected_movies('-12345'), ())
        self.assertNotIn('reply_markup', call.kwargs)
        self.assertIn('개인 채팅에서만 가능합니다', call.args[0])

    def test_single_movie_mode_keeps_legacy_start_behavior(self):
        self.config = dataclasses.replace(self.config, multi_movie_enabled=False)
        self.watcher = self.make_watcher()
        call = self.command()
        self.assertEqual(self.watcher.state.selected_movies(self.NEW_CHAT), (self.config.movie_no,))
        self.assertNotIn('reply_markup', call.kwargs)

    def test_deferred_start_persists_empty_selection_and_buttons(self):
        self.watcher.telegram.send_message.side_effect = TelegramDeferred(
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5))
        self.command()
        self.watcher = self.make_watcher()
        self.assertEqual(self.watcher.state.selected_movies(self.NEW_CHAT), ())
        pending = self.watcher.state.pending_deliveries()
        self.assertEqual(len(pending), 1)
        self.assertIn('inline_keyboard', pending[0][1]['reply_markup'])
        self.watcher._retry_pending_deliveries()
        call = self.watcher.telegram.send_message.call_args
        self.assertIn('inline_keyboard', call.kwargs['reply_markup'])

    def test_both_languages_open_mode_and_initial_catalog_notice(self):
        self.watcher.state.data['movie_catalog_initialized'] = False
        self.watcher.config = dataclasses.replace(self.config, open_only_mode=True)
        for language in ('ko', 'en'):
            self.watcher.state.set_language(self.NEW_CHAT, language)
            call = self.command()
            self.assertIn('최초 영화 목록 수집' if language == 'ko' else 'Building the initial catalog', call.args[0])
            self.assertIn('신규 오픈 전용' if language == 'ko' else 'Openings-only', call.args[0])
            self.assertIn('/language', call.args[0])
            self.assertNotRegex(call.args[0], r'/[A-Za-z0-9_]+[가-힣ㄱ-ㅎㅏ-ㅣ]')
            self.assertEqual(self.watcher.state.language(self.NEW_CHAT), language)

    def test_large_selected_catalog_start_fits_telegram_in_both_languages(self):
        self.command()
        selected = []
        for index in range(60):
            number = str(40000000 + index)
            self.watcher.state.data['movie_catalog'][number] = {'title': '🎬' * 55}
            selected.append(number)
        self.watcher.state.data['subscribers'][self.NEW_CHAT]['movies'] = selected
        for language in ('ko', 'en'):
            self.watcher.state.set_language(self.NEW_CHAT, language)
            call = self.command()
            self.assertLessEqual(len(call.args[0].encode('utf-16-le')) // 2, 4096)
            self.assertIn('/status', call.args[0])
            self.assertEqual(len(call.kwargs['reply_markup']['inline_keyboard']), 9)


if __name__ == '__main__':
    unittest.main()
