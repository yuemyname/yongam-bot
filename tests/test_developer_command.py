import copy
import dataclasses
import itertools
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from watcher import Watcher
from test_watcher import make_config


class DeveloperCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(temporary.name)), subscriptions_enabled=True,
        )
        self.logger = logging.getLogger(f"developer-command-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        self.watcher = self.make_watcher()

    def make_watcher(self, **settings):
        watcher = Watcher(dataclasses.replace(self.config, **settings), logger=self.logger)
        watcher.telegram = Mock()
        watcher.cgv = Mock()
        return watcher

    def command(self, text, *, chat_id="111222", chat_type="private"):
        self.watcher.telegram.send_message.reset_mock()
        self.watcher.telegram.get_updates.return_value = [{
            "update_id": self.watcher.state.telegram_update_offset,
            "message": {"text": text, "chat": {"id": chat_id, "type": chat_type}},
        }]
        self.assertTrue(self.watcher.sync_subscribers())
        self.watcher.telegram.send_message.assert_called_once()
        call = self.watcher.telegram.send_message.call_args
        self.assertEqual(call.kwargs["chat_id"], chat_id)
        return call.args[0]

    def test_developer_links_to_instagram_without_subscribing_or_forwarding(self):
        before = copy.deepcopy(self.watcher.state.data["subscribers"])
        reply = self.command("/developer")
        self.assertIn("https://www.instagram.com/silverflowerstar/", reply)
        self.assertIn("DM", reply)
        self.assertIn("CGV 공식 고객센터가 아닌", reply)
        # A bare @mention in Telegram would link to a Telegram user, not Instagram.
        self.assertNotIn("@silverflowerstar", reply)
        self.assertEqual(self.watcher.state.data["subscribers"], before)
        self.assertEqual(self.watcher.cgv.mock_calls, [])

    def test_command_supports_bot_mentions_and_existing_subscribers(self):
        self.watcher.state.add_subscriber("-111222", chat_type="supergroup")
        before = copy.deepcopy(self.watcher.state.data["subscribers"])
        reply = self.command("/developer@YongamBot", chat_id="-111222", chat_type="supergroup")
        self.assertIn("https://www.instagram.com/silverflowerstar/", reply)
        self.assertEqual(self.watcher.state.data["subscribers"], before)

    def test_help_description_and_command_work_in_all_operating_modes(self):
        for open_only, sweet_only, signups_enabled in itertools.product((False, True), repeat=3):
            with self.subTest(open_only=open_only, sweet_only=sweet_only, signups=signups_enabled):
                self.watcher = self.make_watcher(
                    open_only_mode=open_only, seat_alert_sweet_only=sweet_only,
                    new_subscriptions_enabled=signups_enabled,
                )
                for command in ("/help", "/desc", "/description@YongamBot"):
                    reply = self.command(command)
                    self.assertIn("/developer", reply)
                    self.assertIn("개발자 인스타그램 문의", reply)
                    self.assertLessEqual(len(reply.encode("utf-16-le")) // 2, 4096)
                self.assertIn("https://www.instagram.com/silverflowerstar/", self.command("/developer"))
                self.assertFalse(self.watcher.state.is_subscribed("111222"))
                self.assertEqual(self.watcher.cgv.mock_calls, [])

    def test_user_docs_and_botfather_list_include_developer(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        development = (root / "DEVELOPMENT.md").read_text(encoding="utf-8")
        self.assertIn("`/developer`", readme)
        self.assertIn("https://www.instagram.com/silverflowerstar/", readme)
        self.assertIn("developer - 개발자 인스타그램 문의", development)


if __name__ == "__main__":
    unittest.main()
