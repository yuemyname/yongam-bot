import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prepare_mac import launch_payload, local_values, private_write, state_summary
from service_entry import main
from watcher import load_dotenv


class MacMigrationTests(unittest.TestCase):
    def test_standby_never_starts_watcher(self):
        with patch.dict(os.environ, {"BOT_RUNTIME_ROLE": "standby"}), patch("service_entry.standby", return_value=0) as pause, patch("watcher.main") as watcher:
            self.assertEqual(main(["--verbose"]), 0)
            pause.assert_called_once()
            watcher.assert_not_called()

    def test_unknown_role_fails_closed(self):
        with patch.dict(os.environ, {"BOT_RUNTIME_ROLE": "typo"}), patch("watcher.main") as watcher:
            self.assertEqual(main([]), 2)
            watcher.assert_not_called()

    def test_local_lock_blocks_second_process_and_releases_after_exit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BOT_RUNTIME_ROLE": "bot"}), patch("watcher.main", return_value=0) as watcher:
            path = Path(tmp) / "runtime.lock"
            with path.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(main(["--runtime-lock", str(path)]), 75)
                watcher.assert_not_called()
            self.assertEqual(main(["--runtime-lock", str(path), "--verbose"]), 0)
            self.assertEqual(main(["--runtime-lock", str(path)]), 0)
            self.assertEqual(watcher.call_count, 2)

    def test_preserves_configuration_but_clears_probes_and_railway_paths(self):
        source = (Path(__file__).parents[1] / "watcher.py").read_text()
        incoming = {"TELEGRAM_BOT_TOKEN": "test-secret", "TELEGRAM_CHAT_ID": "123",
                    "OPEN_ONLY_MODE": "true", "NEW_SUBSCRIPTIONS_ENABLED": "false",
                    "CGV_RECOVERY_REQUEST_ID": "existing", "CGV_PUBLIC_MATRIX_REQUEST_ID": "old",
                    "RAILWAY_VOLUME_MOUNT_PATH": "/data", "UNRELATED_SECRET": "not-copied"}
        values = local_values(incoming, Path("/private/runtime"), source)
        self.assertEqual(values["CGV_RECOVERY_REQUEST_ID"], "existing")
        self.assertEqual(values["CGV_PUBLIC_MATRIX_REQUEST_ID"], "")
        self.assertEqual(values["STATE_FILE"], "/private/runtime/data/notified.json")
        self.assertNotIn("RAILWAY_VOLUME_MOUNT_PATH", values)
        self.assertNotIn("UNRELATED_SECRET", values)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            private_write(p, "".join(f"{k}={json.dumps(v)}\n" for k, v in values.items()).encode())
            self.assertEqual(load_dotenv(p), values)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                private_write(p, b"overwrite")

    def test_state_validation_and_summary_do_not_emit_identity(self):
        state = {"subscribers_initialized": True, "subscribers": {"private-id": {}},
                 "notified": {}, "pending_deliveries": {}, "cgv_recovery": {}}
        summary = state_summary(json.dumps(state).encode())
        self.assertEqual(summary["subscribers"], 1)
        self.assertNotIn("private-id", json.dumps(summary))
        with self.assertRaises(ValueError):
            state_summary(b"{}")

    def test_launch_agent_uses_private_paths_and_no_inline_credentials(self):
        payload = launch_payload(Path("/private/runtime"), Path("/private/release"), "/opt/python3")
        args = payload["ProgramArguments"]
        self.assertEqual(args[:3], ["/usr/bin/caffeinate", "-i", "/opt/python3"])
        self.assertIn("--runtime-lock", args)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", str(payload))


if __name__ == "__main__":
    unittest.main()
