import dataclasses
import datetime as dt
import logging
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from test_watcher import make_config
from watcher import BookingSession, Config, Watcher


class MovieSummaryTests(unittest.TestCase):
    DAY = dt.date(2026, 11, 15)
    MOVIE = '30009999'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(make_config(Path(temporary.name)), multi_movie_enabled=True)
        self.watcher = self.restart()
        self.chat = self.config.telegram_chat_id
        self.watcher.state.data['movie_catalog'][self.MOVIE] = {'title': '선택한 영화'}
        self.watcher.state.select_movie(self.chat, self.MOVIE, True)
        clock = patch.object(Config, 'local_now', return_value=dt.datetime(
            2026, 11, 15, 12, tzinfo=dt.timezone(dt.timedelta(hours=9))))
        clock.start()
        self.addCleanup(clock.stop)

    def restart(self):
        watcher = Watcher(self.config, logger=logging.getLogger('movie-summary-tests'))
        watcher.cgv = Mock()
        watcher.telegram = Mock()
        return watcher

    def showing(self, day=DAY, time='14:30', remaining=20, **kwargs):
        return BookingSession(date=day.isoformat(), start_time=time,
                              remaining_seats=remaining, total_seats=624,
                              movie_no=self.MOVIE, **kwargs)

    def cache(self, sessions):
        for session in sessions:
            record = self.watcher.state.data['movie_sessions'].setdefault(
                session.date, {'checked_at': '2026-11-15T11:59:00+09:00', 'sessions': []})
            record['sessions'].append(dataclasses.asdict(session))

    def summaries(self):
        return [r['text'] for _, r in sorted(self.watcher.state.pending_deliveries(),
                key=Watcher._delivery_sort_key) if r['category'] == 'movie_summary']

    def render(self, language='ko'):
        self.watcher.state.set_language(self.chat, language)
        self.watcher._queue_movie_summary(self.chat, self.MOVIE)
        return self.summaries()

    def test_grouped_days_sorted_times_and_one_date_selected_link_per_day(self):
        tomorrow = self.DAY + dt.timedelta(days=1)
        self.cache([self.showing(tomorrow, '18:00', 12), self.showing(time='20:00', remaining=4),
                    self.showing(time='14:30', remaining=20), self.showing(tomorrow, '10:00', 5)])
        messages = self.render()
        self.assertEqual(len(messages), 1)
        text = messages[0]
        self.assertIn('2026-11-15\n14:30 - 20석\n20:00 - 4석\nhttps://', text)
        self.assertIn('\n\n2026-11-16\n10:00 - 5석\n18:00 - 12석\nhttps://', text)
        links = [line for line in text.splitlines() if line.startswith('https://')]
        self.assertEqual(len(links), 2)
        for link, date in zip(links, ('20261115', '20261116')):
            query = parse_qs(urlsplit(link).query)
            self.assertEqual(query['scnYmd'], [date])
            self.assertEqual(query['movNo'], [self.MOVIE])
            self.assertEqual(query['siteNo'], ['0013'])
        self.assertNotIn('/624', text)
        self.assertNotIn('조회:', text)
        self.assertNotIn('Checked:', text)
        self.assertNotIn('11:59', text)
        self.assertEqual(self.watcher.cgv.mock_calls, [])

    def test_zero_unknown_and_english_language_are_preserved(self):
        self.cache([self.showing(time='14:00', remaining=0), self.showing(time='17:00', remaining=None)])
        text = self.render('en')[0]
        self.assertIn('14:00 - 0 seats\n17:00 - Unknown', text)
        self.assertIn('Following: 선택한 영화', text)  # Keep movie titles untouched.
        self.assertEqual(text.count('https://'), 1)
        self.assertNotIn('624', text)

    def test_other_movie_past_and_invalid_showings_are_omitted(self):
        self.cache([self.showing(time='11:00'), self.showing(time='invalid'),
                    dataclasses.replace(self.showing(time='16:00'), movie_no='30008888'),
                    self.showing(time='13:00')])
        text = self.render()[0]
        self.assertIn('13:00 - 20석', text)
        for excluded in ('11:00', 'invalid', '16:00'):
            self.assertNotIn(excluded, text)

    def test_extended_hour_keeps_original_cgv_date_and_link(self):
        self.cache([self.showing(time='25:00', remaining=6)])
        with patch.object(Config, 'local_now', return_value=dt.datetime(
                2026, 11, 16, 0, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))):
            text = self.render()[0]
        self.assertIn('2026-11-15\n25:00 - 6석', text)
        self.assertIn('scnYmd=20261115', text)

    def test_empty_summary_has_no_booking_link(self):
        self.cache([self.showing(time='10:00')])
        for language in ('ko', 'en'):
            text = self.render(language)[-1]
            self.assertIn('아직 조회된 예정 회차가 없습니다' if language == 'ko'
                          else 'No upcoming showings are cached yet', text)
            self.assertNotIn('https://', text)

    def test_long_summary_splits_between_dates_without_missing_times_or_links(self):
        self.watcher.state.data['movie_catalog'][self.MOVIE]['title'] = '🎬' * 200
        days = [self.DAY + dt.timedelta(days=index) for index in range(30)]
        self.cache([self.showing(day, clock, index) for index, day in enumerate(days)
                    for clock in ('14:00', '17:00', '20:00')])
        messages = self.render()
        self.assertGreater(len(messages), 1)
        for text in messages:
            self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4000)
            self.assertIn('https://', text)  # Never send an isolated header.
        for index, day in enumerate(days):
            containing = [text for text in messages if day.isoformat() in text.splitlines()]
            self.assertEqual(len(containing), 1)
            text = containing[0]
            self.assertIn(f'{day}\n14:00 - {index}석\n17:00 - {index}석\n20:00 - {index}석', text)
            self.assertEqual(''.join(messages).count('scnYmd=' + day.strftime('%Y%m%d')), 1)

    def test_oversized_single_date_keeps_all_rows_and_only_one_link(self):
        tomorrow = self.DAY + dt.timedelta(days=1)
        self.cache([self.showing(tomorrow, f'{index // 60:02}:{index % 60:02}', index)
                    for index in range(900)])
        messages = self.render()
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(text.encode('utf-16-le')) // 2 <= 4000 for text in messages))
        combined = '\n'.join(messages)
        rows = re.findall(r'^\d{2}:\d{2} - \d+석$', combined, re.M)
        self.assertEqual(len(rows), 900)
        self.assertEqual(len(set(rows)), 900)
        self.assertEqual(combined.count('https://'), 1)

    def test_grouped_summary_is_durable_and_delivers_unchanged(self):
        self.cache([self.showing(time='14:00'), self.showing(time='18:00')])
        messages = self.render()
        self.watcher.state.save()
        self.watcher = self.restart()
        self.watcher._retry_pending_deliveries()
        self.assertEqual([call.args[0] for call in self.watcher.telegram.send_message.call_args_list], messages)
        self.assertEqual(self.watcher.state.pending_delivery_count(), 0)


if __name__ == '__main__':
    unittest.main()
