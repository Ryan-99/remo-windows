"""Tests for the ESP32 bridge serial input source.

Pure-logic tests: no real COM port and no pyserial are needed.  The listener is
driven through an injected ``_serial_factory``, mirroring how
``test_raw_input_pure.py`` feeds synthetic buffers into the Raw Input listener.
"""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import config, esp32_serial_input


class FakeSerial:
    """Minimal stand-in for ``serial.Serial`` with a scriptable input queue."""

    def __init__(self, **kwargs):
        self.open_kwargs = kwargs
        self.written = []
        self.closed = False
        self.dtr = None
        self.rts = None
        self.reset_called = False
        self._chunks = []
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._chunks.append(data)

    def read(self, _size: int) -> bytes:
        with self._lock:
            if self._chunks:
                return self._chunks.pop(0)
        # Emulate a live but idle port so the reader loop stays responsive to
        # the stop event instead of blocking forever.
        time.sleep(0.005)
        return b""

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def close(self) -> None:
        self.closed = True

    def reset_input_buffer(self) -> None:
        self.reset_called = True


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class FakePortEntry:
    """One entry as ``serial.tools.list_ports.comports()`` would return it."""

    def __init__(self, device, vid=None):
        self.device = device
        self.vid = vid


def fake_serial_module(ports, factory=None, *, comports_raises=False):
    """A stand-in for the ``serial`` package, for the detection tests.

    Injected by patching ``esp32_serial_input.serial``, so these tests run the
    same whether or not pyserial is installed on the machine.
    """

    def comports():
        if comports_raises:
            raise OSError("enumeration failed")
        return list(ports)

    return SimpleNamespace(
        Serial=factory or (lambda **_: FakeSerial()),
        tools=SimpleNamespace(list_ports=SimpleNamespace(comports=comports)),
    )


def answering_factory(connections, opened=None):
    """Serial factory that hands back a prepared connection per port name."""

    def factory(**kwargs):
        port = kwargs.get("port")
        if opened is not None:
            opened.append(port)
        return connections[port]

    return factory


def speaking(line: str) -> FakeSerial:
    """A connection that has already received one protocol line."""

    connection = FakeSerial()
    connection.feed((line + "\n").encode("ascii"))
    return connection


def silent() -> FakeSerial:
    """A connection that never says anything (a wrong port, or a dead board)."""

    return FakeSerial()


class ProtocolParsingTests(unittest.TestCase):
    def test_key_message_round_trips(self):
        message = esp32_serial_input.parse_protocol_line("KEY 0x00F1 D 12 34567")
        self.assertEqual(message.usage, 0x00F1)
        self.assertTrue(message.is_pressed)
        self.assertEqual(message.sequence, 12)
        self.assertEqual(message.uptime_ms, 34567)

    def test_release_is_distinguished_from_press(self):
        message = esp32_serial_input.parse_protocol_line("KEY 0x00F1 U 12 34600")
        self.assertFalse(message.is_pressed)

    def test_metadata_messages(self):
        ready = esp32_serial_input.parse_protocol_line("RDY 1 0.1.0 esp32-c6")
        self.assertEqual(ready.protocol, 1)
        self.assertEqual(ready.firmware, "0.1.0")
        self.assertEqual(ready.board, "esp32-c6")
        self.assertEqual(
            esp32_serial_input.parse_protocol_line("DEV MI RC").remote_name, "MI RC"
        )
        self.assertEqual(
            esp32_serial_input.parse_protocol_line("BAT 88").percent, 88
        )
        self.assertEqual(
            esp32_serial_input.parse_protocol_line("ERR ble timeout").text, "ble timeout"
        )

    def test_surrounding_whitespace_is_tolerated(self):
        message = esp32_serial_input.parse_protocol_line("  KEY 0x004A D 1 100  ")
        self.assertEqual(message.usage, 0x004A)

    def test_human_readable_diagnostics_are_ignored(self):
        # The firmware deliberately interleaves its own banner, GATT dump and
        # decode annotations on the same stream; none may be mistaken for data.
        for line in (
            "",
            "   ",
            "   slot0  usage=0x004A  home",
            "[   41230 ms] handle=0x0023 reportId=1 len=9  01 00 00 4A 00 00 00 00 00",
            "GATT table follows",
            "KEY",
        ):
            self.assertIsNone(
                esp32_serial_input.parse_protocol_line(line), msg=repr(line)
            )

    def test_malformed_key_lines_are_rejected_not_guessed(self):
        for line in (
            "KEY 0xZZ D 1 2",          # not hex
            "KEY 0x004A X 1 2",        # not a valid edge
            "KEY 0x004A D 1",          # missing field
            "KEY 0x004A D 1 2 3",      # extra field
            "BAT not-a-number",
            "RDY 1 0.1.0",             # missing board
        ):
            self.assertIsNone(
                esp32_serial_input.parse_protocol_line(line), msg=repr(line)
            )


class LineFramerTests(unittest.TestCase):
    def test_line_split_across_reads_is_reassembled(self):
        framer = esp32_serial_input.LineFramer()
        self.assertEqual(framer.feed(b"KEY 0x00F1 D 1 "), [])
        self.assertEqual(framer.feed(b"100\n"), ["KEY 0x00F1 D 1 100"])

    def test_several_lines_in_one_chunk(self):
        framer = esp32_serial_input.LineFramer()
        self.assertEqual(
            framer.feed(b"RDY 1 0.1 esp32-c6\nBAT 88\nKEY 0x0081 D 5 900\n"),
            ["RDY 1 0.1 esp32-c6", "BAT 88", "KEY 0x0081 D 5 900"],
        )

    def test_crlf_is_stripped(self):
        framer = esp32_serial_input.LineFramer()
        self.assertEqual(framer.feed(b"BAT 42\r\n"), ["BAT 42"])

    def test_an_unterminated_flood_is_dropped_rather_than_buffered(self):
        framer = esp32_serial_input.LineFramer(max_line_bytes=64)
        self.assertEqual(framer.feed(b"x" * 200), [])
        # The oversized fragment is discarded entirely, so the next real line
        # comes back clean instead of carrying 200 bytes of garbage.
        self.assertEqual(framer.feed(b"BAT 7\n"), ["BAT 7"])

    def test_blank_lines_are_framed_and_left_for_the_parser_to_ignore(self):
        framer = esp32_serial_input.LineFramer()
        self.assertEqual(framer.feed(b"\nBAT 7\n"), ["", "BAT 7"])
        self.assertIsNone(esp32_serial_input.parse_protocol_line(""))


class UsageMappingTests(unittest.TestCase):
    def test_the_three_usages_windows_drops_are_mapped(self):
        # This is the entire reason the input source exists: these three are
        # discarded by kbdhid before any user-mode code can see them.
        self.assertEqual(esp32_serial_input.button_id_for_usage(0x00F1), "back")
        self.assertEqual(esp32_serial_input.button_id_for_usage(0x0080), "volume_up")
        self.assertEqual(esp32_serial_input.button_id_for_usage(0x0081), "volume_down")

    def test_unknown_usage_is_not_guessed(self):
        self.assertIsNone(esp32_serial_input.button_id_for_usage(0x9999))

    def test_every_declared_usage_maps_to_a_known_button(self):
        for usage, button_id in esp32_serial_input.device_profile.BUTTON_USAGE_IDS.items():
            self.assertEqual(
                esp32_serial_input.button_id_for_usage(usage), button_id, msg=hex(usage)
            )


class PortTierTests(unittest.TestCase):
    """The ranking half of detection: how plausible is this port."""

    def test_espressif_usb_ids_are_the_top_tier(self):
        # 0x303A is the chip's own USB-Serial/JTAG peripheral - no bridge chip
        # in between, so nothing else can be masquerading as it.
        self.assertEqual(esp32_serial_input._port_tier(FakePortEntry("COM3", 0x303A)), 0)

    def test_common_usb_uart_bridges_are_the_second_tier(self):
        # A dev board wired through one of these still carries the bridge
        # firmware, so it must stay in the probed set - it just ranks below a
        # board the host can identify directly.
        for vid in (0x1A86, 0x10C4, 0x0403, 0x067B):
            self.assertEqual(
                esp32_serial_input._port_tier(FakePortEntry("COM9", vid)),
                1,
                msg=hex(vid),
            )

    def test_everything_else_is_unknown(self):
        for vid in (None, 0x1234, 0x8087, 0x046D):
            self.assertEqual(
                esp32_serial_input._port_tier(FakePortEntry("COM1", vid)), 2, msg=vid
            )

    def test_a_port_object_without_a_vid_attribute_is_unknown(self):
        # Some platforms' port objects simply lack the attribute; that is not
        # evidence of anything, so it must not raise.
        self.assertEqual(esp32_serial_input._port_tier(object()), 2)


class CandidateOrderingTests(unittest.TestCase):
    def _candidates(self, ports, **kwargs):
        with mock.patch.object(
            esp32_serial_input, "serial", fake_serial_module(ports)
        ):
            return esp32_serial_input.iter_bridge_candidates(**kwargs)

    def test_a_native_board_outranks_a_bridge_chip_however_they_enumerate(self):
        candidates = self._candidates(
            [
                FakePortEntry("COM7", 0x1A86),  # listed first
                FakePortEntry("COM4", 0x303A),
                FakePortEntry("COM1", None),  # unrelated, never a candidate
            ]
        )
        self.assertEqual(candidates, ["COM4", "COM7"])

    def test_ports_with_unknown_ids_are_excluded_unless_asked_for(self):
        ports = [
            FakePortEntry("COM2", 0x303A),
            FakePortEntry("COM5", 0x1234),
            FakePortEntry("COM6", 0x9999),
        ]
        self.assertEqual(self._candidates(ports), ["COM2"])
        self.assertEqual(
            self._candidates(ports, include_unknown=True), ["COM2", "COM5", "COM6"]
        )

    def test_two_identical_boards_resolve_deterministically(self):
        # USB IDs cannot tell these apart, so enumeration order is the tiebreak.
        # A stable result is what makes a retry land on the same board.
        ports = [FakePortEntry("COM8", 0x303A), FakePortEntry("COM3", 0x303A)]
        self.assertEqual(self._candidates(ports), ["COM8", "COM3"])

    def test_no_ports_at_all_is_not_an_error(self):
        self.assertEqual(self._candidates([]), [])

    def test_a_host_without_pyserial_has_no_candidates(self):
        with mock.patch.object(esp32_serial_input, "serial", None):
            self.assertEqual(esp32_serial_input.iter_bridge_candidates(), [])

    def test_enumeration_failure_is_not_an_error(self):
        # A platform hiccup here must surface later as "no board found", not as
        # a traceback out of a settings page.
        with mock.patch.object(
            esp32_serial_input,
            "serial",
            fake_serial_module([], comports_raises=True),
        ):
            self.assertEqual(esp32_serial_input.iter_bridge_candidates(), [])


class ProbesProtocolTests(unittest.TestCase):
    """The confirmation half of detection: does this port talk the protocol."""

    def _probe(self, connection, **kwargs):
        module = fake_serial_module([], factory=lambda **_: connection)
        with mock.patch.object(esp32_serial_input, "serial", module):
            return esp32_serial_input._speaks_bridge_protocol(
                "COM_TEST", baud=115200, timeout=kwargs.pop("timeout", 0.2), **kwargs
            )

    def test_a_boot_banner_confirms_the_port(self):
        self.assertTrue(self._probe(speaking("RDY 1 0.1.0 esp32-c6")))

    def test_any_protocol_line_confirms_the_port(self):
        # A board that has been running since before the app started printed
        # its banner long ago; the other tags have to count too.
        for line in ("DEV MI RC", "BAT 88", "KEY 0x00F1 D 1 100", "ERR ble timeout"):
            self.assertTrue(self._probe(speaking(line)), msg=line)

    def test_every_probe_tag_is_one_the_parser_understands(self):
        # If these two ever drift, the probe will confidently select a board
        # whose lines the parser then discards - a silent dead input source.
        for tag in esp32_serial_input.BRIDGE_PROTOCOL_TAGS:
            sample = {
                "RDY": "RDY 1 0.1.0 esp32-c6",
                "DEV": "DEV MI RC",
                "BAT": "BAT 88",
                "KEY": "KEY 0x00F1 D 1 100",
                "ERR": "ERR ble timeout",
            }[tag]
            self.assertIsNotNone(
                esp32_serial_input.parse_protocol_line(sample), msg=sample
            )

    def test_a_line_split_across_reads_still_confirms(self):
        connection = FakeSerial()
        connection.feed(b"RDY 1 0.1")
        connection.feed(b".0 esp32-c6\n")
        self.assertTrue(self._probe(connection))

    def test_human_readable_diagnostics_do_not_confirm_a_port(self):
        connection = FakeSerial()
        connection.feed(b"  slot0 usage=0x004A home\nGATT table follows\n")
        self.assertFalse(self._probe(connection, timeout=0.05))

    def test_a_silent_port_is_rejected(self):
        self.assertFalse(self._probe(silent(), timeout=0.05))

    def test_a_port_that_cannot_open_is_rejected_rather_than_raising(self):
        def explode(**_kwargs):
            raise OSError("access denied")

        module = fake_serial_module([], factory=explode)
        with mock.patch.object(esp32_serial_input, "serial", module):
            self.assertFalse(
                esp32_serial_input._speaks_bridge_protocol(
                    "COM_TEST", baud=115200, timeout=0.05
                )
            )

    def test_a_port_that_dies_mid_probe_is_rejected_rather_than_raising(self):
        connection = silent()
        connection.read = lambda _size: (_ for _ in ()).throw(OSError("device gone"))
        self.assertFalse(self._probe(connection, timeout=0.05))

    def test_the_silent_board_is_nudged_with_the_restart_command(self):
        # This is what makes detection work against a board that booted hours
        # ago: the firmware's own 'r' rescans and re-emits RDY/DEV.
        connection = silent()
        self._probe(connection, timeout=0.3)
        self.assertIn(esp32_serial_input.BRIDGE_RESTART_COMMAND, connection.written)

    def test_the_probe_never_drives_the_reset_lines(self):
        connection = speaking("RDY 1 0.1.0 esp32-c6")
        self._probe(connection)
        self.assertIs(connection.dtr, False)
        self.assertIs(connection.rts, False)

    def test_the_probe_always_releases_the_port(self):
        # Detection runs against every candidate in turn and the winner is
        # opened again immediately afterwards; a handle left behind here would
        # make that second open fail on Windows.
        for connection in (speaking("RDY 1 0.1.0 esp32-c6"), silent()):
            self._probe(connection, timeout=0.05)
            self.assertTrue(connection.closed)


class FindBridgePortTests(unittest.TestCase):
    def _find(self, ports, connections, opened=None, **kwargs):
        module = fake_serial_module(ports, factory=answering_factory(connections, opened))
        with mock.patch.object(esp32_serial_input, "serial", module):
            return esp32_serial_input.find_bridge_port(
                probe_timeout=kwargs.pop("probe_timeout", 0.05), **kwargs
            )

    def test_no_candidates_resolves_to_nothing(self):
        self.assertIsNone(self._find([FakePortEntry("COM1", None)], {}))

    def test_with_the_probe_off_the_top_ranked_port_wins_unopened(self):
        # Worth having: starting the app must not open a port at all when the
        # user has turned the confirmation step off.
        opened = []
        found = self._find(
            [FakePortEntry("COM7", 0x1A86), FakePortEntry("COM4", 0x303A)],
            {},
            opened,
            probe=False,
        )
        self.assertEqual(found, "COM4")
        self.assertEqual(opened, [])

    def test_the_answering_port_wins_over_the_higher_ranked_one(self):
        # The whole point of probing: a second Espressif board must not be
        # mistaken for the bridge just because it enumerates first.
        found = self._find(
            [FakePortEntry("COM4", 0x303A), FakePortEntry("COM7", 0x1A86)],
            {"COM4": silent(), "COM7": speaking("DEV MI RC")},
        )
        self.assertEqual(found, "COM7")

    def test_a_bridge_chip_is_found_when_no_native_board_is_present(self):
        found = self._find(
            [FakePortEntry("COM9", 0x1A86)],
            {"COM9": speaking("RDY 1 0.1.0 esp32-c6")},
        )
        self.assertEqual(found, "COM9")

    def test_a_lone_candidate_is_returned_without_being_opened(self):
        # Equivalent to probing it - the fallback would return it either way -
        # but it saves a reboot of the chip, and that reboot is what drops the
        # BLE link.  This is the ordinary one-board setup.
        opened = []
        found = self._find(
            [FakePortEntry("COM4", 0x303A)],
            {"COM4": speaking("RDY 1 0.1.0 esp32-c6")},
            opened,
        )
        self.assertEqual(found, "COM4")
        self.assertEqual(opened, [])

    def test_when_nothing_answers_the_top_ranked_port_is_still_returned(self):
        # Returning it (rather than None) is deliberate: the failure then reads
        # as "COM4 opened but silent" - which names the port - instead of the
        # far less actionable "no bridge found".
        found = self._find(
            [FakePortEntry("COM4", 0x303A), FakePortEntry("COM7", 0x1A86)],
            {"COM4": silent(), "COM7": silent()},
        )
        self.assertEqual(found, "COM4")

    def test_unknown_ports_are_probed_only_when_asked(self):
        ports = [FakePortEntry("COM5", 0x1234)]
        connections = {"COM5": speaking("RDY 1 0.1.0 esp32-c6")}
        self.assertIsNone(self._find(ports, connections))
        self.assertEqual(
            self._find(ports, connections, include_unknown=True), "COM5"
        )

    def test_the_configured_baud_is_used_for_the_probe(self):
        # Two candidates, so the probe actually runs - a baud the board does
        # not speak would make a real probe hear nothing but line noise.
        module = fake_serial_module(
            [FakePortEntry("COM4", 0x303A), FakePortEntry("COM7", 0x1A86)],
            factory=answering_factory(
                {
                    "COM4": silent(),
                    "COM7": speaking("RDY 1 0.1.0 esp32-c6"),
                }
            ),
        )
        seen = []
        original = module.Serial

        def recording(**kwargs):
            seen.append(kwargs)
            return original(**kwargs)

        module.Serial = recording
        with mock.patch.object(esp32_serial_input, "serial", module):
            esp32_serial_input.find_bridge_port(baud=57600, probe_timeout=0.05)
        self.assertEqual([item.get("baudrate") for item in seen], [57600, 57600])


class DetectionWiringTests(unittest.TestCase):
    """The listener must actually use the detection settings it is given."""

    def _finder_for(self, listener):
        return listener._port_finder

    def test_the_probe_settings_travel_from_the_constructor(self):
        recorded = {}

        def recorder(**kwargs):
            recorded.update(kwargs)
            return "COM_TEST"

        listener = esp32_serial_input.Esp32SerialButtonListener(
            lambda *_: None,
            port="",
            baud=57600,
            probe_ports=False,
            probe_timeout=1.25,
            scan_all_ports=True,
        )
        with mock.patch.object(esp32_serial_input, "find_bridge_port", recorder):
            self.assertEqual(self._finder_for(listener)(), "COM_TEST")
        self.assertEqual(
            recorded,
            {
                "probe": False,
                "probe_timeout": 1.25,
                "include_unknown": True,
                "baud": 57600,
            },
        )

    def test_detection_defaults_are_the_safe_ones(self):
        listener = esp32_serial_input.Esp32SerialButtonListener(
            lambda *_: None, port=""
        )
        self.assertTrue(listener._probe_ports)
        self.assertFalse(listener._scan_all_ports)

    def test_a_named_port_bypasses_detection_entirely(self):
        connection = FakeSerial()
        listener = esp32_serial_input.Esp32SerialButtonListener(
            lambda *_: None,
            port="COM_TEST",
            _serial_factory=lambda **_: connection,
            _port_finder=lambda: self.fail("detection ran despite a named port"),
        )
        listener.start()
        listener.stop()
        self.assertEqual(listener.port, "COM_TEST")


class ListenerTests(unittest.TestCase):
    def _make(self, **kwargs):
        events = []
        connection = FakeSerial()
        listener = esp32_serial_input.Esp32SerialButtonListener(
            lambda button_id, is_pressed: events.append((button_id, is_pressed)),
            port="COM_TEST",
            _serial_factory=lambda **_: connection,
            _port_finder=lambda: "COM_TEST",
            **kwargs,
        )
        return listener, connection, events

    def test_press_and_release_reach_the_callback(self):
        listener, connection, events = self._make()
        listener.start()
        try:
            connection.feed(b"KEY 0x00F1 D 1 100\n")
            self.assertTrue(wait_for(lambda: ("back", True) in events))
            connection.feed(b"KEY 0x00F1 U 2 200\n")
            self.assertTrue(wait_for(lambda: ("back", False) in events))
        finally:
            listener.stop()
        self.assertEqual(events, [("back", True), ("back", False)])

    def test_duplicate_press_without_release_is_not_replayed(self):
        listener, connection, events = self._make()
        listener.start()
        try:
            connection.feed(b"KEY 0x004A D 1 100\n")
            self.assertTrue(wait_for(lambda: events.count(("home", True)) == 1))
            connection.feed(b"KEY 0x004A D 2 120\n")
            time.sleep(0.05)
        finally:
            listener.stop()
        self.assertEqual(events.count(("home", True)), 1)

    def test_release_of_a_button_never_pressed_is_ignored(self):
        listener, connection, events = self._make()
        listener.start()
        try:
            connection.feed(b"KEY 0x004A U 1 100\n")
            time.sleep(0.05)
        finally:
            listener.stop()
        self.assertEqual(events, [])

    def test_unknown_usage_is_reported_but_not_dispatched(self):
        listener, connection, events = self._make()
        listener.start()
        try:
            connection.feed(b"KEY 0x1234 D 1 100\n")
            time.sleep(0.05)
        finally:
            listener.stop()
        self.assertEqual(events, [])

    def test_stop_releases_a_button_left_held(self):
        # A bridge unplugged mid-press must not latch that button down in the
        # gesture dispatcher across the reconnect.
        listener, connection, events = self._make()
        listener.start()
        connection.feed(b"KEY 0x0081 D 1 100\n")
        self.assertTrue(wait_for(lambda: ("volume_down", True) in events))
        listener.stop()
        self.assertIn(("volume_down", False), events)

    def test_stop_is_idempotent_and_closes_the_port(self):
        listener, connection, events = self._make()
        listener.start()
        listener.stop()
        listener.stop()
        self.assertTrue(connection.closed)
        self.assertFalse(listener.is_running)

    def test_human_lines_do_not_disturb_the_reader(self):
        listener, connection, events = self._make()
        listener.start()
        try:
            connection.feed(b"  banner line\nKEY 0x003E D 1 100\n  slot0 usage\n")
            self.assertTrue(wait_for(lambda: ("mic", True) in events))
        finally:
            listener.stop()

    def test_metadata_lines_populate_listener_state(self):
        listener, connection, events = self._make()
        listener.start()
        try:
            connection.feed(b"RDY 1 9.9.9 esp32-c6\nDEV MI RC\nBAT 77\n")
            self.assertTrue(wait_for(lambda: listener.battery_percent == 77))
            self.assertEqual(listener.protocol_version, 1)
            self.assertEqual(listener.remote_name, "MI RC")
        finally:
            listener.stop()

    def test_keepalive_is_written_to_the_bridge(self):
        listener, connection, events = self._make(ping_interval=0.05)
        listener.start()
        try:
            self.assertTrue(wait_for(lambda: b"PING\n" in connection.written))
        finally:
            listener.stop()

    def test_no_port_means_unavailable(self):
        listener = esp32_serial_input.Esp32SerialButtonListener(
            lambda *_: None,
            port="",
            _serial_factory=lambda **_: FakeSerial(),
            _port_finder=lambda: None,
        )
        with self.assertRaises(esp32_serial_input.Esp32SerialUnavailableError):
            listener.start()

    def test_a_port_that_cannot_open_is_reported_as_unavailable(self):
        def explode(**_kwargs):
            raise OSError("access denied")

        listener = esp32_serial_input.Esp32SerialButtonListener(
            lambda *_: None,
            port="COM_TEST",
            _serial_factory=explode,
            _port_finder=lambda: "COM_TEST",
        )
        with self.assertRaises(esp32_serial_input.Esp32SerialUnavailableError):
            listener.start()
        self.assertIsNone(listener.port)

    def test_closed_callback_fires_when_the_stream_ends_unexpectedly(self):
        closed = threading.Event()
        connection = FakeSerial()
        listener = esp32_serial_input.Esp32SerialButtonListener(
            lambda *_: None,
            on_closed=closed.set,
            port="COM_TEST",
            _serial_factory=lambda **_: connection,
            _port_finder=lambda: "COM_TEST",
        )
        listener.start()
        # A read failure is how an unplugged board shows up.
        connection.read = lambda _size: (_ for _ in ()).throw(OSError("device gone"))
        self.assertTrue(closed.wait(2.0))
        listener.stop()

    def test_the_port_is_released_before_the_closed_callback_fires(self):
        # on_closed asks the supervisor to reconnect, and the reconnect opens
        # this same port.  If the handle were still held the two would overlap
        # and Windows would refuse the second open.
        connection = FakeSerial()
        closed = threading.Event()
        observed = []
        listener = esp32_serial_input.Esp32SerialButtonListener(
            lambda *_: None,
            on_closed=lambda: (observed.append(connection.closed), closed.set()),
            port="COM_TEST",
            _serial_factory=lambda **_: connection,
            _port_finder=lambda: "COM_TEST",
        )
        listener.start()
        connection.read = lambda _size: (_ for _ in ()).throw(OSError("device gone"))
        self.assertTrue(closed.wait(2.0))
        listener.stop()
        self.assertEqual(observed, [True])

    def test_an_ordinary_stop_does_not_report_a_disconnect(self):
        closed = threading.Event()
        listener, connection, _events = self._make()
        listener._on_closed = closed.set
        listener.start()
        listener.stop()
        self.assertFalse(closed.is_set())

    def test_the_board_is_not_reset_when_the_port_opens(self):
        # Asserting DTR/RTS in the wrong order resets an ESP32-C6 over
        # USB-Serial/JTAG, which would drop the BLE link on every app start.
        listener, connection, events = self._make()
        listener.start()
        try:
            self.assertIs(connection.dtr, False)
            self.assertIs(connection.rts, False)
            self.assertTrue(connection.reset_called)
        finally:
            listener.stop()


class ConfigTests(unittest.TestCase):
    def test_defaults_keep_the_existing_windows_path(self):
        defaults = config.default_config()
        self.assertEqual(defaults["input_source"], config.INPUT_SOURCE_WINDOWS_HID)
        self.assertEqual(defaults["esp32_serial_port"], "")
        self.assertEqual(defaults["esp32_serial_baud"], 115200)

    def test_defaults_detect_the_board_without_scanning_unrelated_ports(self):
        defaults = config.default_config()
        self.assertTrue(defaults["esp32_serial_probe"])
        self.assertEqual(defaults["esp32_serial_probe_timeout"], 2.5)
        # Off by default: the probe opens ports, and this would have it open
        # devices that have nothing to do with the app.
        self.assertFalse(defaults["esp32_serial_scan_all_ports"])

    def test_the_new_keys_are_not_forbidden_identity_fields(self):
        # config.save_config() refuses FORBIDDEN_KEYS; these must stay savable.
        for key in (
            "input_source",
            "esp32_serial_port",
            "esp32_serial_baud",
            "esp32_serial_probe",
            "esp32_serial_probe_timeout",
            "esp32_serial_scan_all_ports",
            "esp32_start_timeout",
            "esp32_ping_interval",
        ):
            self.assertNotIn(key, config.FORBIDDEN_KEYS)

    def test_esp32_bridge_is_a_recognised_input_source(self):
        self.assertIn(config.INPUT_SOURCE_ESP32_BRIDGE, config.INPUT_SOURCES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
