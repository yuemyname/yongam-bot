import dataclasses
from pathlib import Path
import tempfile
import unittest

from test_watcher import make_config
from watcher import BookingSession, SeatSnapshot, seat_change_message


class SeatMessageFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(Path(self.tmp.name))
        self.session = BookingSession(
            date="2026-08-26", start_time="18:00", total_seats=624,
        )
        self.previous = SeatSnapshot(total=5, usable=4)
        self.current = SeatSnapshot(
            total=4, usable=3, mapped_total=4, available_rows=("A", "H", "J"),
            available_seats=(("H", "20"), ("H", "21"), ("J", "7")),
        )

    def details(self, text):
        prefixes = ("상영 시간:", "좌석 번호:", "잔여 좌석:", "변동 좌석수:")
        for obsolete in ("상영 시작시간:", "잔여좌석/총좌석:", "A열 제외 예매 가능:",
                         "명당 예매 가능:", "명당 잔여 좌석:", "A열 제외 잔여 좌석:"):
            self.assertNotIn(obsolete, text)
        return [line for line in text.splitlines() if line.startswith(prefixes)]

    def test_default_scope_labels_and_order(self):
        text = seat_change_message(self.session, self.previous, self.current, self.config)
        self.assertEqual(self.details(text), [
            "상영 시간: 18:00",
            "좌석 번호: H20~21 / J7",
            "잔여 좌석: 4/624석 (이전 5/624석)",
            "변동 좌석수: 4석 → 3석",
        ])
        self.assertLess(text.index("📅 상영일:"), text.index("상영 시간:"))
        self.assertLess(text.index("변동 좌석수:"), text.index("🎟️ 예매 바로가기"))

    def test_sweet_scope_uses_selected_seats_but_keeps_whole_auditorium_ratio(self):
        sweet = SeatSnapshot(
            total=4, usable=1, mapped_total=4, available_rows=("K",),
            available_seats=(("K", "21"),),
        )
        text = seat_change_message(
            self.session, self.previous, self.current, self.config,
            availability=(SeatSnapshot(total=5, usable=0), sweet),
        )
        self.assertEqual(self.details(text), [
            "상영 시간: 18:00",
            "좌석 번호: K21",
            "잔여 좌석: 4/624석 (이전 5/624석)",
            "변동 좌석수: 0석 → 1석",
        ])
        self.assertNotIn("J7", text)

    def test_unchanged_counts_and_missing_numbers_keep_existing_semantics(self):
        current = dataclasses.replace(self.current, available_seats=None)
        text = seat_change_message(self.session, current, current, self.config)
        self.assertEqual(self.details(text), [
            "상영 시간: 18:00",
            "좌석 번호: H열, J열 (좌석 번호 미확인)",
            "잔여 좌석: 4/624석",
            "변동 좌석수: 3석",
        ])

    def test_unclassified_fallback_never_invents_seat_numbers_or_usable_counts(self):
        text = seat_change_message(self.session, None, SeatSnapshot(total=7), self.config)
        self.assertEqual(self.details(text), ["상영 시간: 18:00", "잔여 좌석: 7/624석"])
        self.assertIn("⚠️ A열 여부 미확인", text)


if __name__ == "__main__":
    unittest.main()
