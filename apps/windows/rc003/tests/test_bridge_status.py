import json
import os
import tempfile
from unittest import mock
import unittest
from pathlib import Path

from ovb_rc003 import bridge_status


class BridgeStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "config"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_files_report_stopped(self):
        status = bridge_status.read_bridge_status(self.root)
        self.assertEqual(status.state, "stopped")
        self.assertFalse(status.audio_active)

    def test_live_process_status_round_trips(self):
        publisher = bridge_status.BridgeStatusPublisher(self.root, pid=os.getpid())
        self.assertTrue(
            publisher.publish(
                state="connected", detail="RC003 已连接", attempt=2
            )
        )
        status = bridge_status.read_bridge_status(self.root)
        self.assertEqual(status.state, "connected")
        self.assertEqual(status.pid, os.getpid())
        self.assertEqual(status.attempt, 2)

    def test_dead_active_pid_fails_closed_to_stopped(self):
        publisher = bridge_status.BridgeStatusPublisher(self.root, pid=424242)
        publisher.publish(state="voice_active", audio_active=True)
        status = bridge_status.read_bridge_status(
            self.root, _is_process_alive=lambda _pid: False
        )
        self.assertEqual(status.state, "stopped")
        self.assertFalse(status.audio_active)
        self.assertIn("已经退出", status.detail)

    @unittest.skipUnless(os.name == "nt", "Windows process liveness API")
    def test_windows_live_pid_uses_open_process_not_os_kill(self):
        with mock.patch.object(bridge_status.os, "kill", side_effect=OSError(87, "bad parameter")):
            self.assertTrue(bridge_status.is_process_alive(os.getpid()))

    def test_invalid_json_and_schema_fail_closed(self):
        canonical, fallback = bridge_status.status_paths(self.root)
        canonical.parent.mkdir(parents=True)
        canonical.write_text("{broken", encoding="utf-8")
        fallback.parent.mkdir(parents=True)
        fallback.write_text(
            json.dumps({"schema_version": 999, "state": "connected"}),
            encoding="utf-8",
        )
        self.assertEqual(bridge_status.read_bridge_status(self.root).state, "stopped")

    def test_unwritable_canonical_uses_temp_fallback(self):
        canonical, fallback = bridge_status.status_paths(self.root)

        def writer(path, payload):
            if path == canonical:
                raise PermissionError("blocked")
            bridge_status.write_status_atomic(path, payload)

        publisher = bridge_status.BridgeStatusPublisher(
            self.root, pid=os.getpid(), _writer=writer
        )
        self.assertTrue(publisher.publish(state="connecting"))
        self.assertEqual(publisher.last_path, fallback)
        self.assertEqual(bridge_status.read_bridge_status(self.root).state, "connecting")

    def test_newest_valid_location_wins(self):
        canonical, fallback = bridge_status.status_paths(self.root)
        first = bridge_status.BridgeStatus(
            state="connecting", pid=os.getpid(), updated_at="first"
        ).to_dict()
        second = bridge_status.BridgeStatus(
            state="connected", pid=os.getpid(), updated_at="second"
        ).to_dict()
        bridge_status.write_status_atomic(canonical, first)
        bridge_status.write_status_atomic(fallback, second)
        old = 1_700_000_000_000_000_000
        os.utime(canonical, ns=(old, old))
        os.utime(fallback, ns=(old + 1_000, old + 1_000))
        self.assertEqual(bridge_status.read_bridge_status(self.root).state, "connected")

    def test_atomic_write_leaves_no_temporary_file(self):
        canonical, _ = bridge_status.status_paths(self.root)
        publisher = bridge_status.BridgeStatusPublisher(self.root, pid=os.getpid())
        publisher.publish(state="connected")
        self.assertTrue(canonical.is_file())
        self.assertEqual(list(canonical.parent.glob("*.tmp")), [])

    def test_payload_contains_no_device_identity_fields(self):
        canonical, _ = bridge_status.status_paths(self.root)
        publisher = bridge_status.BridgeStatusPublisher(self.root, pid=os.getpid())
        publisher.publish(state="connected")
        payload = json.loads(canonical.read_text(encoding="utf-8"))
        forbidden = {
            "address",
            "bluetooth_address",
            "mac_address",
            "device_path",
            "interface_id",
            "device_token",
        }
        self.assertTrue(forbidden.isdisjoint(payload))

    def test_all_write_failures_are_nonfatal(self):
        publisher = bridge_status.BridgeStatusPublisher(
            self.root,
            _writer=lambda _path, _payload: (_ for _ in ()).throw(
                PermissionError("blocked")
            ),
        )
        self.assertFalse(publisher.publish(state="connecting"))

    def test_button_event_history_preserves_fast_edges(self):
        publisher = bridge_status.BridgeStatusPublisher(self.root, pid=os.getpid())
        publisher.publish(state="connected", button_id="up", button_state="down")
        publisher.publish(state="connected", button_id="up", button_state="up")
        publisher.publish(state="connected", button_id="ok", button_state="down")
        status = bridge_status.read_bridge_status(self.root)
        self.assertEqual(status.button_seq, 3)
        self.assertEqual(
            status.button_events,
            ((1, "up", "down"), (2, "up", "up"), (3, "ok", "down")),
        )


if __name__ == "__main__":
    unittest.main()
