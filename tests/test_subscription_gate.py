import dataclasses
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from watcher import (
    ALERT_MODE_OPEN_ONLY, Config, ConfigurationError, SEAT_SELECTION_SWEET,
    SHOW_DAY_WEEKEND, StateStore, Watcher,
)
from test_watcher import make_config


class SubscriptionGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(temporary.name)),
            subscriptions_enabled=True,
            new_subscriptions_enabled=False,
        )
        self.logger = logging.getLogger(f"subscription-gate-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        self.watcher = self.restart()
        self.update_id = 0

    def restart(self, config=None):
        watcher = Watcher(config or self.config, logger=self.logger)
        watcher.telegram = Mock()
        watcher.cgv = Mock()
        return watcher

    def command(self, text, chat_id=111222, chat_type="private"):
        self.update_id += 1
        self.watcher.telegram.get_updates.return_value = [{
            "update_id": self.update_id,
            "message": {"text": text, "chat": {
                "id": chat_id, "type": chat_type, "first_name": "테스트",
            }},
        }]
        self.assertTrue(self.watcher.sync_subscribers())
        return self.watcher.telegram.send_message.call_args.args[0]

    def test_default_is_open_and_environment_can_close_it(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_env_file(self.config.project_dir / ".env")
            self.assertTrue(config.new_subscriptions_enabled)
        with patch.dict(os.environ, {"NEW_SUBSCRIPTIONS_ENABLED": "false"}, clear=True):
            config = Config.from_env_file(self.config.project_dir / ".env")
            self.assertFalse(config.new_subscriptions_enabled)
        with patch.dict(os.environ, {"NEW_SUBSCRIPTIONS_ENABLED": "typo"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Config.from_env_file(self.config.project_dir / ".env")

    def test_new_users_groups_and_aliases_are_blocked_and_offset_is_saved(self):
        original_ids = self.watcher.state.subscriber_ids()
        for chat_id, chat_type in ((111222, "private"), (-111222, "supergroup")):
            for command in ("/start", "/subscribe", "/start@YongsanBot invite"):
                with self.subTest(chat_type=chat_type, command=command):
                    reply = self.command(command, chat_id, chat_type)
                    self.assertIn("신규 구독을 잠시 중단", reply)
                    self.assertFalse(self.watcher.state.is_subscribed(str(chat_id)))
        reloaded = StateStore(self.config.state_file)
        reloaded.load()
        self.assertEqual(reloaded.subscriber_ids(), original_ids)
        self.assertEqual(reloaded.telegram_update_offset, self.update_id + 1)
        self.watcher.cgv.fetch_date.assert_not_called()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()

    def test_existing_subscriber_can_change_settings_without_start_resetting_them(self):
        self.watcher.state.add_subscriber("111222")
        for command in ("/mode_open", "/day_weekend", "/seat_sweet", "/count_2"):
            self.assertNotIn("신규 구독을 잠시 중단", self.command(command))
        state = self.watcher.state
        self.assertEqual(state.alert_mode("111222"), ALERT_MODE_OPEN_ONLY)
        self.assertEqual(state.show_day_selection("111222"), SHOW_DAY_WEEKEND)
        self.assertEqual(state.seat_selection("111222"), SEAT_SELECTION_SWEET)
        self.assertEqual(state.min_seats("111222"), 2)
        stored = dict(state.data["subscribers"]["111222"])
        self.assertIn("이미", self.command("/start"))
        self.assertIn("이미", self.command("/subscribe"))
        self.assertIn("구독 중", self.command("/status"))
        self.assertEqual(state.data["subscribers"]["111222"], stored)
        self.watcher = self.restart()
        self.assertEqual(self.watcher.state.data["subscribers"]["111222"], stored)

    def test_stop_works_but_rejoining_stays_blocked_after_restart(self):
        self.watcher.state.add_subscriber("111222")
        reply = self.command("/stop")
        self.assertIn("구독을 해지", reply)
        self.assertIn("재구독할 수 없습니다", reply)
        self.watcher = self.restart()
        self.assertFalse(self.watcher.state.is_subscribed("111222"))
        self.assertIn("신규 구독을 잠시 중단", self.command("/start"))
        self.assertFalse(self.watcher.state.is_subscribed("111222"))
        self.assertIn("신규 구독을 잠시 중단", self.command("/unsubscribe"))

    def test_information_and_non_subscriber_settings_explain_the_closure(self):
        for command in (
            "/help", "/desc", "/description", "/status", "/mode", "/mode_open",
            "/day_weekend", "/seat_sweet", "/count_2",
        ):
            with self.subTest(command=command):
                reply = self.command(command)
                self.assertIn("신규 구독을 잠시 중단", reply)
                self.assertLess(len(reply), 4096)
                self.assertFalse(self.watcher.state.is_subscribed("111222"))

    def test_reopening_allows_start_without_changing_other_subscribers(self):
        self.command("/start")
        previous = dict(self.watcher.state.data["subscribers"][self.config.telegram_chat_id])
        self.watcher = self.restart(dataclasses.replace(self.config, new_subscriptions_enabled=True))
        self.assertIn("구독이 완료", self.command("/start"))
        self.assertTrue(self.watcher.state.is_subscribed("111222"))
        self.assertEqual(
            self.watcher.state.data["subscribers"][self.config.telegram_chat_id], previous,
        )
        self.assertNotIn("신규 구독을 잠시 중단", self.command("/help"))

    def test_subscription_commands_and_restart_preserve_the_cgv_halt(self):
        self.watcher.state.set_cgv_recovery({
            "request_id": "test-existing-halt", "status": "halted",
            "reason": "HTTP 403", "announced_status": "halted",
        })
        self.watcher.state.save()
        original = self.watcher.state.cgv_recovery()
        self.watcher = self.restart()
        self.command("/start")
        self.command("/status")
        self.assertEqual(self.watcher.state.cgv_recovery(), original)
        self.assertTrue(self.watcher.run_cycle().cgv_paused)
        self.watcher.cgv.fetch_date.assert_not_called()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
