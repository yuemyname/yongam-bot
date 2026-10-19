import copy
import dataclasses
import datetime as dt
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from watcher import STATS_MAX_CHARS, TelegramDeferred, Watcher
from test_watcher import make_config


OLD = "30001323"
NEW = "30001450"
ZERO = "30009999"


class MovieStatsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(temporary.name)), subscriptions_enabled=True,
            multi_movie_enabled=True,
        )
        self.watcher = Watcher(self.config, logger=logging.getLogger("movie-stats-tests"))
        self.watcher.telegram = Mock()
        self.watcher.cgv = Mock()
        self.state = self.watcher.state
        self.state.data["movie_catalog"] = {
            OLD: {"title": "오디세이"}, NEW: {"title": "실낙원"},
            ZERO: {"title": "아직 선택자가 없는 영화"},
        }

    def add(self, chat_id, movies):
        self.state.add_subscriber(chat_id, label="비공개이름" + chat_id)
        self.state.data["subscribers"][chat_id]["movies"] = movies

    def command(self, command, chat_id=None):
        self.watcher.telegram.send_message.reset_mock()
        self.watcher.telegram.get_updates.return_value = [{
            "update_id": self.state.telegram_update_offset,
            "message": {"text": command, "chat": {
                "id": chat_id or self.config.telegram_chat_id, "type": "private"}},
        }]
        self.watcher.sync_subscribers()
        return [call.args[0] for call in self.watcher.telegram.send_message.call_args_list]

    def test_counts_legacy_multi_empty_duplicate_and_missing_catalog(self):
        self.add("200", [OLD, NEW])
        self.add("201", [NEW, NEW])
        self.add("202", [])
        self.add("203", "invalid")
        self.add("204", ["30008888"])
        self.add("205", [NEW])
        self.state.remove_subscriber("205")
        self.state.set_alert_mode("200", "open")
        self.state.set_show_day_selection("201", "weekend")
        before = copy.deepcopy(self.state.data)
        stats = self.state.subscriber_breakdown()
        counts = {movie["movie_no"]: movie["count"] for movie in stats["movies"]}
        self.assertEqual(stats["total"], 6)
        self.assertEqual(counts, {OLD: 2, NEW: 2, ZERO: 0, "30008888": 1})
        self.assertEqual(stats["no_movie_selected"], 2)
        self.assertEqual([m["movie_no"] for m in stats["movies"]], [NEW, OLD, "30008888", ZERO])
        self.assertEqual(self.state.data, before)

    def test_stats_survive_restart_without_migrating_subscriber_preferences(self):
        self.add("200", [OLD, NEW])
        self.add("201", [])
        expected = self.state.subscriber_breakdown()
        self.state.save()
        restored = Watcher(self.config, logger=self.watcher.logger)
        self.assertEqual(restored.state.subscriber_breakdown(), expected)
        self.assertNotIn("movies", restored.state.data["subscribers"][self.config.telegram_chat_id])

    def test_selection_and_unsubscription_change_counts_immediately(self):
        chat_id = self.config.telegram_chat_id
        self.assertTrue(self.state.select_movie(chat_id, NEW, True))
        self.assertIn("• 실낙원 (30001450) — 1명", self.watcher._subscriber_stats_message(include_subscribers=False))
        self.state.select_movie(chat_id, OLD, False)
        self.assertIn("• 오디세이 (30001323) — 0명", self.watcher._subscriber_stats_message(include_subscribers=False))
        self.state.remove_subscriber(chat_id)
        stats = self.state.subscriber_breakdown()
        self.assertEqual(stats["total"], 0)
        self.assertTrue(all(movie["count"] == 0 for movie in stats["movies"]))

    def test_both_admin_commands_include_counts_summary_excludes_people(self):
        self.add("987654321", [NEW])
        summary = "".join(self.command("/statsss"))
        self.assertIn("🎬 영화별 선택 인원", summary)
        self.assertIn("• 실낙원 (30001450) — 1명", summary)
        self.assertIn("복수 선택", summary)
        self.assertIn("구독 채팅 1개", summary)
        self.assertNotIn("987654321", summary)
        self.assertNotIn("비공개이름", summary)
        self.assertNotIn("📋 구독자 목록", summary)
        full = "".join(self.command("/statss"))
        self.assertIn("• 실낙원 (30001450) — 1명", full)
        self.assertIn("📋 구독자 목록", full)
        self.assertIn("비공개이름987654321", full)
        self.watcher.cgv.fetch_date.assert_not_called()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()

    def test_non_operator_cannot_see_movie_counts(self):
        self.add("200", [NEW])
        for command in ("/statsss", "/statss"):
            text = "".join(self.command(command, "200"))
            self.assertNotIn("영화별 선택 인원", text)
            self.assertNotIn("30001450", text)

    def test_catalog_not_initialized_still_counts_default_movie(self):
        self.state.data["movie_catalog"] = {}
        stats = self.state.subscriber_breakdown()
        self.assertEqual(stats["movies"], [{"movie_no": OLD, "title": "오디세이", "count": 1}])
        self.assertEqual(stats["no_movie_selected"], 0)

    def test_many_movies_split_without_omitting_counts_or_splitting_normal_lines(self):
        for index in range(150):
            self.state.data["movie_catalog"][str(40000000 + index)] = {"title": "🎬" * 35 + f"영화 {index}"}
        expected = self.watcher._subscriber_stats_message(include_subscribers=False)
        messages = self.command("/statsss")
        self.assertGreater(len(messages), 1)
        self.assertEqual("".join(text.split("\n", 1)[1] for text in messages), expected)
        self.assertTrue(all(len(text.encode("utf-16-le")) // 2 <= STATS_MAX_CHARS for text in messages))
        self.assertTrue(all(text.endswith("\n") for text in messages[:-1]))
        for index in range(150):
            self.assertEqual(expected.count(f"({40000000 + index}) — 0명"), 1)

    def test_oversized_title_and_deferred_parts_are_preserved(self):
        self.state.data["movie_catalog"][NEW]["title"] = "🎬" * 5000
        expected = self.watcher._subscriber_stats_message(include_subscribers=False)
        self.watcher.telegram.send_message.side_effect = TelegramDeferred(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1))
        self.command("/statsss")
        pending = self.state.pending_deliveries()
        chunks = self.watcher._stats_reply_chunks(expected)
        self.assertTrue(all(len(text.encode("utf-16-le")) // 2 <= STATS_MAX_CHARS for text in chunks))
        self.assertEqual(len(pending), len(chunks))
        self.assertEqual({record["text"] for _, record in pending}, set(chunks))
        self.assertTrue(all(record["category"] == "reply" and record["chat_id"] == self.config.telegram_chat_id for _, record in pending))


if __name__ == "__main__":
    unittest.main()
