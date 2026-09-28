import copy
import dataclasses
import datetime as dt
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from watcher import (
    ADMIN_USER_STATUS_COMMAND, ALERT_MODE_OPEN_ONLY, ALERT_MODE_SEATS_ONLY,
    ALERT_REPLY, SEAT_SELECTION_ALL, SHOW_DAY_WEEKEND, StateStore, Watcher,
)
from test_watcher import make_config


class UserStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(temporary.name)), subscriptions_enabled=True,
            seat_alert_sweet_only=True,
        )
        self.logger = logging.getLogger(f"user-status-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        self.watcher = self.restart()
        self.operator = self.config.telegram_chat_id
        self.target = "123456789"
        state = self.watcher.state
        state.add_subscriber(self.target, label="테스트 구독자", chat_type="private")
        state.set_alert_mode(self.target, ALERT_MODE_SEATS_ONLY)
        state.set_show_day_selection(self.target, SHOW_DAY_WEEKEND)
        state.set_seat_date_range(self.target, ("2026-10-03", None))
        state.set_seat_time_range(self.target, ("22:00", "02:00"))
        state.set_min_seats(self.target, 2)
        # Verify that the global override, not the stored preference, is shown.
        state.data["subscribers"][self.target]["seat_selection"] = SEAT_SELECTION_ALL
        state.save()

    def restart(self, **settings):
        watcher = Watcher(dataclasses.replace(self.config, **settings), logger=self.logger)
        watcher.telegram = Mock()
        watcher.cgv = Mock()
        return watcher

    def command(self, text, *, chat_id=None, chat_type="private", sender=None):
        chat_id = chat_id or self.operator
        self.watcher.telegram.send_message.reset_mock()
        message = {
            "text": text, "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": chat_id, "is_bot": False} if sender is None else sender,
        }
        self.watcher.telegram.get_updates.return_value = [{
            "update_id": self.watcher.state.telegram_update_offset,
            "message": message,
        }]
        self.assertTrue(self.watcher.sync_subscribers())
        if not self.watcher.telegram.send_message.called:
            return ""
        self.watcher.telegram.send_message.assert_called_once()
        call = self.watcher.telegram.send_message.call_args
        self.assertEqual(call.kwargs["chat_id"], chat_id)
        return call.args[0]

    def test_operator_sees_same_status_as_subscriber_and_does_not_modify_settings(self):
        own_reply = self.command("/status", chat_id=self.target)
        before = copy.deepcopy(self.watcher.state.data)
        reply = self.command(f"{ADMIN_USER_STATUS_COMMAND} {self.target}")
        self.assertIn(own_reply, reply)
        for text in (self.target, "테스트 구독자", "잔여 좌석만", "주말(토·일)",
                     "2026-10-03부터 (포함)", "22:00", "02:00", "명당 좌석만", "2석 이상"):
            self.assertIn(text, reply)
        self.assertNotIn("모든 A열 제외 좌석", reply)
        self.assertLessEqual(len(reply.encode("utf-16-le")) // 2, 4096)
        after = copy.deepcopy(self.watcher.state.data)
        # A handled incoming message only advances the durable update offset.
        for snapshot in (before, after):
            snapshot.pop("telegram_update_offset", None)
        self.assertEqual(after, before)
        reloaded = StateStore(self.config.state_file)
        reloaded.load()
        self.assertEqual(reloaded.data["subscribers"], before["subscribers"])
        self.assertEqual(self.watcher.cgv.mock_calls, [])

    def test_mentions_and_negative_group_target_are_supported(self):
        target = "-1001234567890"
        self.watcher.state.add_subscriber(target, label="테스트 그룹", chat_type="supergroup")
        reply = self.command(f"/user_status@YongamBot {target}")
        self.assertIn(f"채팅ID: {target}", reply)
        self.assertIn("채팅 유형: 그룹", reply)

    def test_non_operators_cannot_learn_if_target_exists(self):
        for viewer in (self.target, "555555"):
            for target in (self.target, "999999", ""):
                with self.subTest(viewer=viewer, target=target):
                    reply = self.command(f"/user_status {target}", chat_id=viewer)
                    self.assertEqual(reply, "사용 가능한 명령어를 보려면 /help를 보내주세요.")
        self.assertNotIn("테스트 구독자", self.command(f"/status {self.target}", chat_id="555555"))

    def test_group_commands_and_invalid_sender_fail_closed(self):
        for sender in ({}, "invalid", {"id": self.target}, {"id": self.operator, "is_bot": True}):
            with self.subTest(sender=sender):
                reply = self.command(f"/user_status {self.target}", sender=sender)
                self.assertNotIn(self.target, reply)
                self.assertNotIn("테스트 구독자", reply)
        group = "-1009876543210"
        self.watcher = self.restart(telegram_chat_id=group)
        for chat_type in ("group", "supergroup", "channel"):
            reply = self.command(
                f"/user_status {self.target}", chat_id=group,
                chat_type=chat_type, sender={"id": self.operator, "is_bot": False},
            )
            self.assertNotIn("테스트 구독자", reply)
        # Even a private-chat type cannot make a group ID match the sender.
        self.assertNotIn("테스트 구독자", self.command(
            f"/user_status {self.target}", chat_id=group,
            sender={"id": self.operator, "is_bot": False},
        ))

    def test_invalid_arguments_show_guide_without_registering_anyone(self):
        before = copy.deepcopy(self.watcher.state.data["subscribers"])
        for argument in ("", "@someone", "123 456", "0", "+123", "00123", "-0", "9" * 100,
                         "테스트 구독자", "１２３"):
            with self.subTest(argument=argument):
                reply = self.command(f"/user_status {argument}")
                self.assertIn("사용법: /user_status 채팅ID", reply)
                self.assertNotIn("테스트 구독자", reply)
        self.assertEqual(self.watcher.state.data["subscribers"], before)

    def test_unknown_and_unsubscribed_targets_are_not_invented_or_registered(self):
        for target in ("555555", self.target):
            self.watcher.state.remove_subscriber(target)
            before = copy.deepcopy(self.watcher.state.data["subscribers"])
            reply = self.command(f"/user_status {target}")
            self.assertIn("현재 구독 목록에 없는", reply)
            self.assertNotIn("알림 종류:", reply)
            self.assertEqual(self.watcher.state.data["subscribers"], before)

    def test_open_only_and_closed_signups_match_targets_own_status(self):
        self.watcher = self.restart(open_only_mode=True, new_subscriptions_enabled=False)
        for mode in (ALERT_MODE_SEATS_ONLY, ALERT_MODE_OPEN_ONLY):
            self.watcher.state.set_alert_mode(self.target, mode)
            own_reply = self.command("/status", chat_id=self.target)
            reply = self.command(f"/user_status {self.target}")
            self.assertIn(own_reply, reply)
            self.assertIn("신규 오픈 전용 운영", reply)
            self.assertIn("알림 상영일: 주말(토·일)", reply)
            if mode == ALERT_MODE_SEATS_ONLY:
                self.assertIn("현재 수신 알림: 없음", reply)
            else:
                self.assertIn("현재 수신 알림: 신규 예매 오픈", reply)

    def test_legacy_record_and_control_characters_in_label(self):
        self.watcher.state.data["subscribers"][self.target] = {"label": "A\nB\tC"}
        reply = self.command(f"/user_status {self.target}")
        self.assertIn("이름: A B C\n", reply)
        self.assertIn("신규 오픈 + 잔여 좌석", reply)
        self.assertIn("전체 날짜", reply)
        self.assertIn("채팅 유형: 미상", reply)

    def test_deferred_reply_is_queued_for_operator_only(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.watcher.state.pause_telegram_sends(now + dt.timedelta(hours=1), now)
        before = copy.deepcopy(self.watcher.state.data["subscribers"])
        self.assertEqual(self.command(f"/user_status {self.target}"), "")
        pending = self.watcher.state.pending_deliveries()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][1]["chat_id"], self.operator)
        self.assertEqual(pending[0][1]["category"], ALERT_REPLY)
        self.assertEqual(self.watcher.state.data["subscribers"], before)
        self.watcher.telegram.send_message.assert_not_called()

    def test_lookup_survives_restart_and_does_not_require_operator_subscription(self):
        self.watcher.state.remove_subscriber(self.operator)
        self.watcher.state.save()
        self.watcher = self.restart(new_subscriptions_enabled=False)
        self.assertIn("테스트 구독자", self.command(f"/user_status {self.target}"))
        self.assertFalse(self.watcher.state.is_subscribed(self.operator))

    def test_command_is_not_advertised_in_public_help_or_menu(self):
        for command in ("/help", "/desc", "/start"):
            self.assertNotIn("/user_status", self.command(command, chat_id=self.target))
        repo = Path(__file__).resolve().parents[1]
        self.assertNotIn("/user_status", (repo / "README.md").read_text())
        development = (repo / "DEVELOPMENT.md").read_text()
        self.assertIn("/user_status", development)
        self.assertNotIn("user_status", development.split("```text", 1)[1].split("```", 1)[0])


if __name__ == "__main__":
    unittest.main()
