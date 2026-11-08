import copy
import dataclasses
import datetime as dt
import itertools
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from movie_features import public_guide, public_help
from watcher import Config, Watcher
from test_watcher import make_config, _kst


class PublicGuideTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(temporary.name)), subscriptions_enabled=True,
            multi_movie_enabled=True, seat_alert_sweet_only=True,
            poll_interval_seconds=60, seat_poll_interval_seconds=120,
            scan_mode="frontier", cursor_probe_days=5,
        )
        clock = patch.object(Config, "local_now", return_value=_kst(dt.date(2026, 11, 6)))
        clock.start()
        self.addCleanup(clock.stop)
        self.watcher = self.make_watcher()

    def make_watcher(self, **settings):
        watcher = Watcher(dataclasses.replace(self.config, **settings),
                          logger=logging.getLogger("public-guide-test"))
        watcher.telegram = Mock()
        watcher.cgv = Mock()
        return watcher

    def command(self, command):
        chat = self.config.telegram_chat_id
        self.watcher.telegram.send_message.reset_mock()
        self.watcher.telegram.get_updates.return_value = [{
            "update_id": self.watcher.state.telegram_update_offset,
            "message": {"text": command, "chat": {"id": chat, "type": "private"}},
        }]
        self.assertTrue(self.watcher.sync_subscribers())
        self.watcher.telegram.send_message.assert_called_once()
        return self.watcher.telegram.send_message.call_args.args[0]

    def test_live_command_path_returns_distinct_help_and_detailed_description(self):
        for language in ("ko", "en"):
            with self.subTest(language=language):
                self.watcher.state.set_language(self.config.telegram_chat_id, language)
                help_text = self.command("/help")
                description = self.command("/desc")
                self.assertEqual(help_text, public_help(language, self.config))
                self.assertEqual(description, public_guide(language, self.config))
                self.assertNotEqual(help_text, description)
                self.assertGreater(len(description), len(help_text) * 2)
                for detail in ("SweetSpot:", "/date 20261106", "/time 22:00 02:00"):
                    self.assertIn(detail, description)
                    self.assertNotIn(detail, help_text)
                self.assertIn("/desc", help_text)
                self.assertIn("/help", description)

    def test_description_aliases_and_bot_mentions_use_detailed_guide(self):
        for language in ("ko", "en"):
            self.watcher.state.set_language(self.config.telegram_chat_id, language)
            expected = self.command("/desc")
            for command in ("/description", "/desc@YongamBot", "/description@YongamBot"):
                self.assertEqual(self.command(command), expected)

    def test_donation_commands_use_buy_me_a_coffee_in_both_languages(self):
        chat = self.config.telegram_chat_id
        for language in ("ko", "en"):
            self.watcher.state.set_language(chat, language)
            subscribers = copy.deepcopy(self.watcher.state.data["subscribers"])
            for command in ("/coffee", "/donate", "/coffee@YongamBot", "/donate@YongamBot"):
                with self.subTest(language=language, command=command):
                    text = self.command(command)
                    self.assertIn("https://buymeacoffee.com/yuemyname", text)
                    self.assertIn("Buy Me a Coffee", text)
                    self.assertNotIn("ko-fi", text.lower())
                    self.assertIn("모든 알림 기능은 동일" if language == "ko"
                                  else "All alerts are available regardless of donations", text)
                    self.assertEqual(self.watcher.state.language(chat), language)
                    self.assertEqual(self.watcher.state.data["subscribers"], subscribers)
        self.assertEqual(self.watcher.cgv.mock_calls, [])

    def test_both_replies_fit_telegram_and_stay_distinct_in_all_modes(self):
        for multi, open_only, sweet, signups, english in itertools.product((False, True), repeat=5):
            with self.subTest(multi=multi, open_only=open_only, sweet=sweet, signups=signups, en=english):
                self.watcher = self.make_watcher(
                    multi_movie_enabled=multi, open_only_mode=open_only,
                    seat_alert_sweet_only=sweet, new_subscriptions_enabled=signups,
                )
                self.watcher.state.set_language(self.config.telegram_chat_id, "en" if english else "ko")
                help_text, description = self.command("/help"), self.command("/desc")
                self.assertNotEqual(help_text, description)
                for text in (help_text, description):
                    self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
                    self.assertIn("/developer", text)
                    self.assertNotIn("/statss", text)
                    if not signups:
                        self.assertIn("New subscriptions are closed" if english else "신규 구독을 잠시 중단", text)

    def test_guide_explains_current_policy_not_old_odyssey_defaults(self):
        text = self.command("/desc")
        for phrase in (
            "CGV 용산아이파크몰 IMAX", "5일 뒤까지", "60초", "120초",
            "자동 선택되지는 않습니다", "매진이거나", "수가 같아도 다시",
            "날짜·시간·좌석·최소 좌석 제한은 적용하지 않습니다",
            "YYYYMMDD", "/date 20261106 20261108", "기간이 지나도 자동 해제되지",
            "위치 미확인 좌석은 제외", "전체 좌석 전환은 불가",
            "J11~34, K11~34, L11~34", "H13~32, I13~32", "F16~29, G16~29",
            "2석이 서로 붙어 있다는 뜻은 아닙니다", "A열만 남은 경우·0석",
            "상영 시간 → 좌석 번호 → 잔여 좌석 → 변동 좌석수",
            "이전 수를 모르거나 같으면 현재 수만", "직접 예매·결제",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("28일", text)
        self.assertNotIn("오디세이 선택을 유지", text)

    def test_open_only_does_not_advertise_inactive_seat_commands(self):
        self.watcher = self.make_watcher(open_only_mode=True)
        for language in ("ko", "en"):
            self.watcher.state.set_language(self.config.telegram_chat_id, language)
            for command in ("/help", "/desc"):
                text = self.command(command)
                self.assertIn("/movies", text)
                self.assertIn("/mode_open", text)
                self.assertIn("/day_weekend", text)
                for inactive in ("/mode_seats", "/mode_all", "/seat_sweet", "/count_2", "/time ", "/date "):
                    self.assertNotIn(inactive, text)
                self.assertNotIn("120초", text)
                self.assertNotIn("120s apart", text)

    def test_guides_do_not_fetch_cgv_subscribe_or_change_preferences(self):
        chat = self.config.telegram_chat_id
        state = self.watcher.state
        state.set_language(chat, "en")
        state.set_min_seats(chat, 2)
        state.save()
        before = copy.deepcopy(state.data["subscribers"])
        languages = copy.deepcopy(state.data["languages"])
        for command in ("/help", "/desc", "/description"):
            self.command(command)
        self.assertEqual(state.data["subscribers"], before)
        self.assertEqual(state.data["languages"], languages)
        self.assertEqual(self.watcher.cgv.mock_calls, [])
        self.assertEqual(state.pending_deliveries(), ())
        self.watcher = self.make_watcher()
        self.assertEqual(self.watcher.state.language(chat), "en")
        self.assertTrue(self.command("/desc").startswith("🎬 About"))


if __name__ == "__main__":
    unittest.main()
