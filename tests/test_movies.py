import dataclasses
import datetime as dt
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from watcher import (BookingSession, CgvClient, Config, FetchError, StateStore,
                     TelegramDeferred, Watcher, extract_sessions, message_chunks,
                     booking_url_for_session, seat_change_message, SeatSnapshot)
from movie_features import localize_alert, public_guide
from test_watcher import make_config, _kst

DAY = dt.date(2026, 8, 26)
OLD = "30001323"
NEW = "30009999"


def row(movie=OLD, title="오디세이", time="1430", **fields):
    return dict({"siteNo": "0013", "movNo": movie, "movNm": title,
                 "scnYmd": "20260826", "scnsrtTm": time, "scnsNo": "018",
                 "scnsNm": "IMAX관", "movkndDsplNm": "IMAX LASER 2D",
                 "scnSseq": "1", "frSeatCnt": 20, "stcnt": 624}, **fields)


class MovieTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = dataclasses.replace(make_config(Path(temp.name)), multi_movie_enabled=True,
                                          subscriptions_enabled=True, open_only_mode=True)
        clock = patch.object(Config, "local_now", return_value=_kst(DAY))
        clock.start()
        self.addCleanup(clock.stop)
        self.rows = [row()]
        self.watcher = self.restart()
        self.chat = self.config.telegram_chat_id

    def restart(self):
        watcher = Watcher(self.config, logger=logging.getLogger("movie-tests"))
        watcher.cgv = Mock()
        watcher.cgv.fetch_date.side_effect = lambda *a, **k: {"statusCode": 0, "data": self.rows}
        watcher.cgv.fetch_seat_snapshot.side_effect = AssertionError("unneeded seats")
        watcher.telegram = Mock()
        return watcher

    def cycle(self):
        with patch.object(self.watcher, "_delivery_worker_running", return_value=True):
            return self.watcher.run_cycle()

    def pending(self, category):
        return [r for _,r in self.watcher.state.pending_deliveries() if r["category"] == category]

    def callback(self, data, *, owner=None, chat_type="private"):
        self.watcher._handle_movie_callback({"id": "callback-test", "data": data,
            "message": {"chat": {"id": self.chat, "type": chat_type}},
            "from": {"id": owner or self.chat, "is_bot": False}})

    def command(self, command):
        self.watcher.telegram.get_updates.return_value = [{
            "update_id": self.watcher.state.telegram_update_offset,
            "message": {"text": command, "chat": {"id": self.chat, "type": "private"}}}]
        self.watcher.sync_subscribers()
        return self.watcher.telegram.send_message.call_args.args[0]

    def test_baseline_is_silent_and_legacy_openings_still_work(self):
        self.rows += [row(NEW, "새 영화", "1800")]
        self.cycle()
        self.assertTrue(self.watcher.state.movie_catalog_ready())
        self.assertEqual(self.pending("movie"), [])
        self.assertEqual(len(self.pending("open")), 1)
        self.assertEqual(self.pending("open")[0]["movie_no"], OLD)
        self.assertEqual(self.watcher.state.selected_movies(self.chat), (OLD,))

    def test_failed_initial_scan_never_finishes_baseline(self):
        self.watcher.cgv.fetch_date.side_effect = FetchError("HTTP 403")
        self.cycle()
        self.assertFalse(self.watcher.state.movie_catalog_ready())
        self.assertEqual(self.pending("movie"), [])

    def test_new_discovery_notifies_once_across_restarts_without_autofollow(self):
        self.cycle()
        self.rows.append(row(NEW, "새 영화", "1800"))
        self.cycle()
        self.assertEqual(len(self.pending("movie")), 1)
        self.assertFalse(self.watcher.state.wants_movie(self.chat, NEW))
        self.assertIn("mv:"+NEW+":1", json.dumps(self.pending("movie")[0]["reply_markup"]))
        self.watcher = self.restart()
        self.cycle()
        self.assertEqual(len(self.pending("movie")), 1)

    def test_subscribe_queues_summary_once_and_keeps_other_filters(self):
        self.rows.append(row(NEW, "새 영화", "1800"))
        self.cycle()
        state = self.watcher.state
        state.set_show_day_selection(self.chat, "weekend")
        state.set_min_seats(self.chat, 2)
        state.set_seat_date_range(self.chat, ("2026-09-01", None))
        self.callback(f"mv:{NEW}:1")
        self.callback(f"mv:{NEW}:1")
        self.assertCountEqual(state.selected_movies(self.chat), [OLD, NEW])
        self.assertEqual(len(self.pending("movie_summary")), 1)
        self.assertIn("18:00", self.pending("movie_summary")[0]["text"])
        self.assertIn("movNo="+NEW, self.pending("movie_summary")[0]["text"])
        self.assertEqual(state.show_day_selection(self.chat), "weekend")
        self.assertEqual(state.min_seats(self.chat), 2)
        self.assertEqual(state.seat_date_range(self.chat), ("2026-09-01", None))
        state.save()
        self.watcher = self.restart()
        self.assertCountEqual(self.watcher.state.selected_movies(self.chat), [OLD, NEW])

    def test_deselect_drops_pending_alert_and_summary_but_not_discovery(self):
        self.rows.append(row(NEW, "새 영화", "1800"))
        self.cycle()
        self.callback(f"mv:{NEW}:1")
        state = self.watcher.state
        for category in ("open", "seats_sweet", "movie"):
            state.queue_pending_delivery(self.chat, category, category, movie_no=NEW)
        self.callback(f"mv:{NEW}:0:{state.movie_revision(self.chat)}")
        self.assertFalse(state.wants_movie(self.chat, NEW))
        self.assertEqual(self.pending("movie_summary"), [])
        self.assertFalse(any(r["movie_no"] == NEW for r in self.pending("open")))
        self.assertEqual(len(self.pending("movie")), 1)

    def test_deselect_default_cancels_legacy_queue_without_metadata(self):
        self.watcher.state.queue_pending_delivery(self.chat, "legacy", "open")
        self.callback(f"mv:{OLD}:0:0")
        self.assertEqual(self.pending("open"), [])
        self.assertEqual(self.watcher.state.selected_movies(self.chat), ())

    def test_unauthorized_and_group_callbacks_cannot_change_settings(self):
        self.callback(f"mv:{OLD}:0:0", owner="somebody-else")
        self.callback(f"mv:{OLD}:0:0", chat_type="group")
        self.assertEqual(self.watcher.state.selected_movies(self.chat), (OLD,))
        self.assertEqual(self.watcher.telegram.answer_callback.call_count, 2)

    def test_stale_menu_cannot_undo_newer_choice(self):
        self.callback(f"mv:{OLD}:0:0")
        self.callback(f"mv:{OLD}:1:1")
        self.callback(f"mv:{OLD}:0:0")
        self.assertTrue(self.watcher.state.wants_movie(self.chat, OLD))

    def test_unsubscribed_user_cannot_resubscribe_from_old_button(self):
        self.watcher.state.remove_subscriber(self.chat)
        self.callback(f"mv:{OLD}:1")
        self.assertFalse(self.watcher.state.is_subscribed(self.chat))
        self.assertEqual(self.pending("movie_summary"), [])

    def test_queued_movie_and_markup_survive_save_load_and_delivery(self):
        self.cycle()
        self.rows.append(row(NEW, "새 영화", "1800"))
        self.cycle()
        self.watcher = self.restart()
        self.watcher._retry_pending_deliveries()
        calls = self.watcher.telegram.send_message.call_args_list
        movie_calls = [c for c in calls if "새 영화 발견" in c.args[0]]
        self.assertEqual(len(movie_calls), 1)
        self.assertIn("reply_markup", movie_calls[0].kwargs)
        self.assertEqual(self.watcher.state.pending_delivery_count(), 0)

    def test_enqueue_then_selection_change_rechecked_at_retry(self):
        self.watcher.state.queue_pending_delivery(self.chat, "old movie", "open", movie_no=OLD)
        self.watcher.state.data["subscribers"][self.chat]["movies"] = []
        self.watcher._retry_pending_deliveries()
        self.watcher.telegram.send_message.assert_not_called()

    def test_multiple_movies_have_different_dedup_and_links(self):
        sessions = extract_sessions({"data": [row(), row(NEW, "다른 영화")]}, requested_date=DAY, expected_site="0013", require_movie=True)
        self.assertEqual(len(sessions), 2)
        self.assertNotEqual(*[s.notification_key(site_no="0013", movie_no=OLD) for s in sessions])
        chunks = message_chunks(sessions, self.config)
        self.assertEqual(len(chunks), 2)
        for text, group in chunks:
            self.assertIn(group[0].movie_label, text)
            self.assertIn("movNo="+group[0].movie_no, text)

    def test_no_ordinary_auditorium_or_other_theater_in_catalog(self):
        rows = [row(), row(NEW, "IMAX라는 영화 제목", scnsNm="일반관", movkndDsplNm="2D"), row(NEW, "다른 극장", siteNo="0001")]
        sessions = extract_sessions({"data": rows}, requested_date=DAY, expected_site="0013", require_movie=True)
        self.assertEqual([s.movie_no for s in sessions], [OLD])

    def test_failure_keeps_last_good_catalog_and_summary_cache(self):
        self.cycle()
        before = json.dumps(self.watcher.state.data["movie_sessions"])
        self.watcher.cgv.fetch_date.side_effect = FetchError("timeout")
        self.cycle()
        self.assertEqual(json.dumps(self.watcher.state.data["movie_sessions"]), before)

    def test_valid_empty_day_clears_cached_showings_not_catalog(self):
        self.cycle()
        self.rows = []
        self.cycle()
        self.assertIn(OLD, self.watcher.state.movie_catalog())
        self.assertEqual(self.watcher.state.data["movie_sessions"][DAY.isoformat()]["sessions"], [])

    def test_language_persists_and_movie_title_is_not_translated(self):
        self.assertIn("Language: English", self.command("/language en"))
        self.watcher.state.save()
        self.watcher = self.restart()
        self.assertEqual(self.watcher.state.language(self.chat), "en")
        self.assertIn("오디세이", self.command("/status"))
        self.assertIn("Subscribed", self.command("/status"))
        self.assertIn("Choose movies", self.command("/help"))
        self.assertIn("Language", self.command("/stop"))
        self.assertEqual(self.watcher.state.language(self.chat), "en")

    def test_language_callback_and_menu_are_local_no_cgv_requests(self):
        self.callback("lang:en")
        self.command("/movies")
        self.watcher.cgv.fetch_date.assert_not_called()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
        self.assertEqual(self.watcher.state.language(self.chat), "en")
        self.assertIn("Choose movies", self.watcher.telegram.send_message.call_args.args[0])

    def test_english_open_seat_messages_preserve_titles_urls_and_seats(self):
        session = BookingSession(date="2026-08-26", start_time="14:30", movie_no=NEW,
                                 movie_label="잔여 좌석: 그대로인 영화", remaining_seats=3, total_seats=624)
        for text in (message_chunks([session], self.config)[0][0], seat_change_message(session, None, SeatSnapshot(total=3, usable=2, available_seats=(("J","20"),("K","21"))), self.config)):
            english = localize_alert(text, "en")
            self.assertIn("잔여 좌석: 그대로인 영화", english)
            self.assertIn("movNo="+NEW, english)
            self.assertIn("Show date: 2026-08-26 (Wed)", english)
            self.assertNotIn("상영 시간", english)

    def test_all_public_help_commands_present_in_both_languages(self):
        for lang in ("ko", "en"):
            text = public_guide(lang, self.config)
            self.assertLess(len(text), 4096)
            for command in ("/movies", "/language", "/start", "/stop", "/mode_all", "/mode_open", "/mode_seats", "/day_weekend", "/date_all", "/time_all", "/seat_all", "/seat_sweet", "/count_1", "/count_2", "/developer", "/coffee", "/help", "/desc"):
                self.assertIn(command, text)
            self.assertNotIn("/statss", text)

    def test_menu_is_paginated_and_callback_data_under_limit(self):
        for index in range(25):
            self.watcher.state.data["movie_catalog"][str(40000000+index)] = {"title": "영화"+str(index)}
        text, markup = self.watcher._movie_menu(self.chat)
        self.assertLessEqual(len(markup["inline_keyboard"]), 9)
        self.assertIn("1/4", text)
        for rows in markup["inline_keyboard"]:
            for button in rows:
                self.assertLessEqual(len(button["callback_data"].encode()), 64)
        self.callback("mp:1")
        self.assertIn("2/4", self.pending("reply")[-1]["text"])

    def test_summary_does_not_include_past_shows(self):
        self.rows = [row(NEW, "새 영화", "0800"), row(NEW, "새 영화", "1800")]
        self.cycle()
        self.callback(f"mv:{NEW}:1")
        text = self.pending("movie_summary")[0]["text"]
        self.assertNotIn("08:00", text)
        self.assertIn("18:00", text)

    def test_no_seat_api_for_unselected_movie(self):
        self.config = dataclasses.replace(self.config, open_only_mode=False)
        self.watcher = self.restart()
        self.rows = [row(NEW, "새 영화", "1800")]
        self.cycle()
        self.cycle()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
        self.assertEqual(self.pending("open"), [])

    def test_new_movie_notice_ignores_mode_weekday_and_seat_filters(self):
        state = self.watcher.state
        state.set_alert_mode(self.chat, "seats")
        state.set_show_day_selection(self.chat, "weekend")
        self.cycle()
        self.rows.append(row(NEW, "새 영화", "1800"))
        self.cycle()
        self.assertEqual(len(self.pending("movie")), 1)
        self.assertEqual(self.pending("open"), [])

    def test_selecting_movie_does_not_replay_all_old_openings(self):
        self.rows.append(row(NEW, "새 영화", "1800"))
        self.cycle()
        self.callback(f"mv:{NEW}:1")
        self.cycle()
        self.assertEqual(len(self.pending("movie_summary")), 1)
        self.assertFalse(any(r["movie_no"] == NEW for r in self.pending("open")))
        self.rows.append(row(NEW, "새 영화", "2100"))
        self.cycle()
        self.assertEqual(len([r for r in self.pending("open") if r["movie_no"] == NEW]), 1)

    def test_api_uses_observed_theater_endpoint_without_movie_or_credentials(self):
        client = CgvClient(self.config)
        client._get_json = Mock(return_value={"statusCode": 0, "data": [row()]})
        client.fetch_date(DAY)
        call = client._get_json.call_args
        self.assertEqual(urlsplit(call.args[0]).path, "/api/v1/booking/searchMovScnInfo")
        self.assertEqual(parse_qs(urlsplit(call.args[0]).query), {"coCd":["A420"], "siteNo":["0013"], "scnYmd":["20260826"], "rtctlScopCd":["08"]})
        self.assertEqual(call.kwargs["referer"], "https://cgv.co.kr/cnm/movieBook/cinema")

    def test_api_malformed_or_wrong_day_is_error_not_empty_success(self):
        client = CgvClient(self.config)
        for payload in ({"statusCode":1,"data":[]}, {"statusCode":0,"data":{}}, {"statusCode":0,"data":[{}]}, {"statusCode":0,"data":[row(scnYmd="20260827")]}):
            with self.subTest(payload=payload):
                client._get_json = Mock(return_value=payload)
                with self.assertRaises(FetchError):
                    client.fetch_date(DAY)

    def test_late_show_time_and_neighbor_cinema_are_supported(self):
        client = CgvClient(self.config)
        payload = {"statusCode": 0, "data": [row(time="2500"), row(NEW, "씨네드쉐프", siteNo="P013")]}
        client._get_json = Mock(return_value=payload)
        sessions = self.watcher._extract_sessions({DAY: client.fetch_date(DAY)})
        self.assertEqual(len(sessions), 1)
        session = next(iter(sessions.values()))
        self.assertEqual(session.start_time, "25:00")
        self.assertEqual(session.start_datetime("Asia/Seoul"), _kst(DAY+dt.timedelta(days=1), 1))
        self.watcher.state.set_seat_time_range(self.chat, ("00:00", "02:00"))
        self.assertTrue(self.watcher.state.wants_seat_time(self.chat, session.start_time))
        self.watcher.state.queue_pending_delivery(self.chat, "night", "seats", show_time="25:00")
        self.assertEqual(self.pending("seats")[0]["show_time"], "25:00")

    def test_english_sold_out_and_unknown_row_labels(self):
        text = "• 상영 시작시간 25:00 — 0/624석 (매진 · 취소표 나오면 알림)\n⚠️ A열 여부 미확인 · 전체 잔여 수 기준 알림"
        translated = localize_alert(text, "en")
        self.assertNotRegex(translated, r"[가-힣]")
        self.assertIn("25:00", translated)

    def test_deferred_movie_menu_keeps_markup(self):
        self.watcher.telegram.send_message.side_effect = TelegramDeferred(_kst(DAY)+dt.timedelta(days=1))
        self.command("/movies")
        self.assertIn("inline_keyboard", self.pending("reply")[0]["reply_markup"])


if __name__ == "__main__":
    unittest.main()
