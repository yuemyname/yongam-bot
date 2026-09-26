from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import copy
import dataclasses
import datetime as dt
import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from watcher import (
    ALERT_OPEN, ALERT_SEATS_SWEET, ALERT_REPLY, ALERT_SYSTEM, ALERT_NOTICE,
    ALERT_MODE_OPEN_ONLY, Config, StateStore, Watcher,
)
from test_watcher import make_config


FIXED_NOW = dt.datetime(2026, 9, 26, 0, 0, tzinfo=dt.timezone.utc)


class FrozenDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW.astimezone(tz) if tz else FIXED_NOW.replace(tzinfo=None)


class BroadcastRotationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = StateStore(self.directory / "state.json")

    def ordered(self, store=None):
        return sorted((store or self.store).pending_deliveries(), key=Watcher._delivery_sort_key)

    def test_each_recipient_leads_once_in_a_full_rotation(self):
        recipients = ("C", "A", "B")
        orders = [self.store.rotate_broadcast_recipients(ALERT_OPEN, recipients) for _ in range(4)]
        self.assertEqual(orders, [("A", "B", "C"), ("B", "C", "A"), ("C", "A", "B"), ("A", "B", "C")])

    def test_categories_are_independent_and_replies_do_not_consume_turns(self):
        ids = ("A", "B", "C")
        self.store.rotate_broadcast_recipients(ALERT_OPEN, ids)
        self.store.rotate_broadcast_recipients(ALERT_OPEN, ids)
        self.assertEqual(self.store.rotate_broadcast_recipients(ALERT_SEATS_SWEET, ids), ids)
        self.assertEqual(self.store.rotate_broadcast_recipients(ALERT_NOTICE, ids), ids)
        before = copy.deepcopy(self.store.data["broadcast_rotation"])
        for category in (ALERT_REPLY, ALERT_SYSTEM):
            self.assertEqual(self.store.rotate_broadcast_recipients(category, ("C", "A")), ("C", "A"))
        self.assertEqual(self.store.data["broadcast_rotation"], before)
        self.assertEqual(self.store.rotate_broadcast_recipients(ALERT_OPEN, ids)[0], "C")

    def test_changed_recipients_use_successor_of_previous_first(self):
        self.assertEqual(self.store.rotate_broadcast_recipients(ALERT_OPEN, ("A", "B", "C"))[0], "A")
        self.assertEqual(self.store.rotate_broadcast_recipients(ALERT_OPEN, ("D", "C", "B")), ("B", "C", "D"))
        self.assertEqual(self.store.rotate_broadcast_recipients(ALERT_OPEN, ("A", "D", "C")), ("C", "D", "A"))
        self.assertEqual(self.store.rotate_broadcast_recipients(ALERT_OPEN, ("A", "B")), ("A", "B"))

    def test_empty_single_and_duplicate_recipient_inputs(self):
        self.assertEqual(self.store.rotate_broadcast_recipients(ALERT_OPEN, ()), ())
        self.assertEqual(self.store.data["broadcast_rotation"], {})
        self.assertEqual(self.store.rotate_broadcast_recipients(ALERT_OPEN, ("A", "A")), ("A",))
        self.assertEqual(self.store.rotate_broadcast_recipients(ALERT_OPEN, ("A",)), ("A",))

    def test_frozen_timestamps_and_json_sorting_preserve_rotated_queue_order(self):
        with patch("watcher.dt.datetime", FrozenDateTime):
            self.store.queue_broadcast_deliveries(("C", "A", "B"), "first", ALERT_OPEN)
            self.store.queue_broadcast_deliveries(("C", "A", "B"), "second", ALERT_OPEN)
        reloaded = StateStore(self.store.path)
        reloaded.load()
        records = [record for _, record in self.ordered(reloaded)]
        self.assertEqual([r["chat_id"] for r in records], ["A", "B", "C", "B", "C", "A"])
        self.assertEqual([r["queue_sequence"] for r in records], list(range(1, 7)))
        self.assertEqual(len({r["queued_at"] for r in records}), 1)
        self.assertEqual(reloaded.rotate_broadcast_recipients(ALERT_OPEN, ("A", "B", "C"))[0], "C")

    def test_duplicate_broadcast_does_not_consume_a_turn_or_sequence(self):
        ids = ("A", "B", "C")
        self.assertEqual(self.store.queue_broadcast_deliveries(ids, "first", ALERT_OPEN), 3)
        before = copy.deepcopy(self.store.data)
        self.assertEqual(self.store.queue_broadcast_deliveries(ids, "first", ALERT_OPEN), 0)
        self.assertEqual(self.store.data, before)
        self.store.queue_broadcast_deliveries(ids, "second", ALERT_OPEN)
        self.assertEqual(self.store.data["broadcast_rotation"][ALERT_OPEN], "B")

    def test_priority_remains_ahead_of_rotation_and_timestamp(self):
        self.store.queue_broadcast_deliveries(("A", "B"), "seats", ALERT_SEATS_SWEET)
        self.store.queue_pending_delivery("A", "reply", ALERT_REPLY)
        self.store.queue_broadcast_deliveries(("A", "B"), "opening", ALERT_OPEN)
        self.assertEqual([r["category"] for _, r in self.ordered()], [ALERT_OPEN, ALERT_OPEN, ALERT_REPLY, ALERT_SEATS_SWEET, ALERT_SEATS_SWEET])

    def test_concurrent_batches_advance_once_and_remain_contiguous(self):
        ids = ("D", "B", "C", "A")
        with patch("watcher.dt.datetime", FrozenDateTime), ThreadPoolExecutor(max_workers=4) as pool:
            added = list(pool.map(lambda i: self.store.queue_broadcast_deliveries(ids, f"event-{i}", ALERT_OPEN), range(4)))
        self.assertEqual(added, [4, 4, 4, 4])
        records = [r for _, r in self.ordered()]
        self.assertEqual(Counter(records[i]["chat_id"] for i in range(0, 16, 4)), Counter(ids))
        for i in range(0, 16, 4):
            self.assertEqual(len({r["text"] for r in records[i:i + 4]}), 1)
            self.assertEqual({r["chat_id"] for r in records[i:i + 4]}, set(ids))
        self.assertEqual(json.loads(self.store.path.read_text()), self.store.data)

    def test_legacy_state_and_pending_messages_are_preserved(self):
        self.store.add_subscriber("A")
        self.store.queue_pending_delivery("A", "old", ALERT_OPEN)
        legacy = copy.deepcopy(self.store.data)
        legacy.pop("broadcast_rotation")
        legacy.pop("delivery_sequence")
        for record in legacy["pending_deliveries"].values():
            record.pop("queue_sequence")
        self.store.path.write_text(json.dumps(legacy))
        reloaded = StateStore(self.store.path)
        reloaded.load()
        for key, value in legacy.items():
            self.assertEqual(reloaded.data[key], value)
        reloaded.queue_broadcast_deliveries(("A", "B"), "new", ALERT_OPEN)
        self.assertEqual(reloaded.pending_delivery_count(), 3)
        self.assertEqual(reloaded.data["subscribers"], legacy["subscribers"])

    def test_invalid_persisted_rotation_fails_closed(self):
        for name, value in (("broadcast_rotation", []), ("broadcast_rotation", {"open": 123}), ("delivery_sequence", -1), ("delivery_sequence", True)):
            self.store.path.write_text(json.dumps({name: value}))
            with self.assertRaises(RuntimeError):
                StateStore(self.store.path).load()

    def make_watcher(self):
        config = dataclasses.replace(make_config(self.directory), telegram_broadcast_workers=1, seat_alert_sweet_only=True)
        logger = logging.getLogger(f"rotation-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.state.remove_subscriber(config.telegram_chat_id)
        for chat in ("C", "A", "B"):
            watcher.state.add_subscriber(chat)
        watcher.telegram.send_message = Mock()
        return watcher

    def test_watcher_enqueues_in_rotation_after_filters_and_restart(self):
        watcher = self.make_watcher()
        watcher.state.set_alert_mode("C", ALERT_MODE_OPEN_ONLY)
        original_subscribers = copy.deepcopy(watcher.state.data["subscribers"])
        with patch.object(watcher, "_delivery_worker_running", return_value=True):
            watcher._broadcast_message("seat-1", category=ALERT_SEATS_SWEET, seats_available=1)
            watcher._broadcast_message("seat-2", category=ALERT_SEATS_SWEET, seats_available=1)
            watcher._broadcast_message("open-1", category=ALERT_OPEN)
        records = [r for _, r in self.ordered(watcher.state)]
        self.assertEqual([r["chat_id"] for r in records if r["text"] == "seat-1"], ["A", "B"])
        self.assertEqual([r["chat_id"] for r in records if r["text"] == "seat-2"], ["B", "A"])
        self.assertEqual([r["chat_id"] for r in records if r["text"] == "open-1"], ["A", "B", "C"])
        restarted = Watcher(watcher.config, logger=watcher.logger)
        with patch.object(restarted, "_delivery_worker_running", return_value=True):
            restarted._broadcast_message("open-2", category=ALERT_OPEN)
        records = [r for _, r in self.ordered(restarted.state)]
        self.assertEqual([r["chat_id"] for r in records if r["text"] == "open-2"], ["B", "C", "A"])
        self.assertEqual(restarted.state.data["subscribers"], original_subscribers)
        watcher.telegram.send_message.assert_not_called()

    def test_synchronous_broadcast_and_retries_preserve_turns(self):
        watcher = self.make_watcher()
        watcher._broadcast_message("first", category=ALERT_OPEN)
        watcher._broadcast_message("second", category=ALERT_OPEN)
        self.assertEqual([call.kwargs["chat_id"] for call in watcher.telegram.send_message.call_args_list], ["A", "B", "C", "B", "C", "A"])
        before = copy.deepcopy(watcher.state.data["broadcast_rotation"])
        watcher.state.queue_pending_delivery("B", "retry", ALERT_OPEN)
        watcher._retry_pending_deliveries()
        self.assertEqual(watcher.state.data["broadcast_rotation"], before)
        self.assertEqual(watcher.state.pending_delivery_count(), 0)

    def test_running_worker_uses_restored_rotation_order(self):
        watcher = self.make_watcher()
        with patch.object(watcher, "_delivery_worker_running", return_value=True), patch("watcher.dt.datetime", FrozenDateTime):
            watcher._broadcast_message("first", category=ALERT_OPEN)
            watcher._broadcast_message("second", category=ALERT_OPEN)
        restarted = Watcher(watcher.config, logger=watcher.logger)
        sent = []
        restarted.telegram.send_message = lambda text, **kw: sent.append((text, kw["chat_id"]))
        restarted.start_delivery_worker()
        try:
            deadline = time.monotonic() + 2
            while restarted.state.pending_delivery_count() and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            restarted.stop_delivery_worker(timeout=2)
        self.assertEqual(sent, [("first", "A"), ("first", "B"), ("first", "C"), ("second", "B"), ("second", "C"), ("second", "A")])
        self.assertEqual(restarted.state.pending_delivery_count(), 0)
        self.assertEqual(restarted.state.data["broadcast_rotation"][ALERT_OPEN], "B")


if __name__ == "__main__":
    unittest.main()
