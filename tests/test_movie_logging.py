import dataclasses
import datetime as dt
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_watcher import make_config, _kst
from watcher import (
    BookingSession, Config, FetchError, SeatSnapshot, Watcher, _CycleTally,
    _alert_session_line, _session_line, _session_movie_log_label,
)


class MovieLoggingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = dataclasses.replace(make_config(Path(temp.name)), seat_alert_sweet_only=True)
        self.logger = logging.getLogger(self.id())
        self.logger.handlers = [logging.NullHandler()]
        self.watcher = Watcher(self.config, logger=self.logger)
        self.watcher.cgv = Mock()
        self.watcher.telegram = Mock()
        self.session = BookingSession(
            date="2026-11-08", start_time="14:30", screen_name="IMAX관",
            screen_no="018", screen_sequence="1", remaining_seats=1, total_seats=624,
            movie_no=self.config.movie_no, movie_label="첫 번째 영화",
        )
        self.key = self.session.notification_key(site_no=self.config.site_no, movie_no=self.config.movie_no)
        self.watcher.state.mark_notified(self.key, self.session)
        self.watcher.state.set_seat_snapshot(self.key, SeatSnapshot(total=0))
        self.snapshot = SeatSnapshot(total=1, usable=1, mapped_total=1,
                                     available_rows=("J",), available_seats=(("J", "20"),))
        self.watcher.cgv.fetch_seat_snapshot.return_value = self.snapshot
        clock = patch.object(Config, "local_now", return_value=_kst(dt.date(2026, 11, 8)))
        clock.start()
        self.addCleanup(clock.stop)

    def test_session_log_identifies_movie_date_time_and_seat_count(self):
        text = _session_line(self.session, self.config)
        self.assertIn(f"[첫 번째 영화 ({self.config.movie_no})]", text)
        self.assertIn("2026-11-08 14:30", text)
        self.assertIn("IMAX관 / 좌석 1/624석", text)

    def test_missing_title_uses_correct_fallback_without_mislabeling_other_movie(self):
        legacy = dataclasses.replace(self.session, movie_no="", movie_label="")
        self.assertEqual(_session_movie_log_label(legacy, self.config),
                         f"[{self.config.movie_label} ({self.config.movie_no})]")
        other = dataclasses.replace(legacy, movie_no="30009999")
        self.assertEqual(_session_movie_log_label(other, self.config), "[영화명 미확인 (30009999)]")
        self.assertEqual(_session_movie_log_label(legacy), "[영화명 미확인 (코드 미확인)]")

    def test_titles_are_single_line_in_logs_but_telegram_format_is_unchanged(self):
        session = dataclasses.replace(self.session, movie_label="첫\r\n번째\t영화\x00\x1b")
        self.assertEqual(_session_movie_log_label(session), f"[첫 번째 영화 ({self.config.movie_no})]")
        self.assertEqual(session.movie_label, "첫\r\n번째\t영화\x00\x1b")
        self.assertEqual(_alert_session_line(self.session), "• 상영 시작시간 14:30 — 1/624석")

    def test_debug_verdicts_distinguish_movies_with_identical_showtime(self):
        other = dataclasses.replace(self.session, movie_no="30009999", movie_label="두 번째 영화")
        keys = {self.session:self.key, other:"other"}
        tally = _CycleTally()
        with self.assertLogs(self.logger, level="DEBUG") as logs:
            self.watcher._record_verdicts(tally, [self.session, other], {}, keys, {})
            self.logger.debug("회차 판정 %d건 | %s", len(tally.verdicts), " | ".join(tally.verdicts))
        self.assertEqual(len(tally.verdicts), 2)
        self.assertIn(f"[첫 번째 영화 ({self.config.movie_no})] 11-08 14:30", logs.output[0])
        self.assertIn("[두 번째 영화 (30009999)] 11-08 14:30", logs.output[0])

    def test_queue_and_direct_delivery_logs_include_movie(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                self.watcher.state.set_seat_snapshot(self.key, SeatSnapshot(total=0))
                with patch.object(self.watcher, '_delivery_worker_running', return_value=queued):
                    with self.assertLogs(self.logger, level="DEBUG") as logs:
                        self.watcher._alert_for_sessions([self.session], _CycleTally())
                prefix = "좌석 대기열:" if queued else "전송: 예매 가능 좌석"
                line = next(line for line in logs.output if prefix in line)
                self.assertIn(f"[첫 번째 영화 ({self.config.movie_no})]", line)

    def test_sold_out_exclusion_has_movie(self):
        self.watcher.state.set_seat_snapshot(self.key, self.snapshot)
        sold_out = dataclasses.replace(self.session, remaining_seats=0)
        with self.assertLogs(self.logger, level="INFO") as logs:
            self.watcher._alert_for_sessions([sold_out], _CycleTally())
        self.assertIn(f"[첫 번째 영화 ({self.config.movie_no})]", logs.output[0])
        self.assertIn("잔여 좌석이 0석", logs.output[0])

    def test_seat_error_sample_keeps_movie_and_showtime(self):
        for error in (FetchError("timeout"), RuntimeError("test")):
            with self.subTest(error=type(error).__name__):
                self.watcher.state.set_seat_snapshot(self.key, SeatSnapshot(total=0))
                self.watcher.state.clear_deferred(self.key)
                self.watcher.cgv.fetch_seat_snapshot.side_effect = error
                tally = _CycleTally()
                self.watcher._alert_for_sessions([self.session], tally)
                self.assertEqual(tally.seat_detail_errors, 1)
                self.assertIn(f"[첫 번째 영화 ({self.config.movie_no})]", tally.seat_detail_error_sample)
                self.assertIn("2026-11-08 14:30", tally.seat_detail_error_sample)


if __name__ == '__main__':
    unittest.main()
