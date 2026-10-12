import asyncio
import unittest

from ovb_rc003 import atvv_protocol as proto
from ovb_rc003.atvv_lifecycle import ATVVLifecycle
from ovb_rc003.atvv_session import ATVVSession, AudioStarted, AudioStopped, CapsReceived


CAPS = CapsReceived(proto.ATVVCapabilities.parse(bytes.fromhex("0b010002030078")))


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.writes, self.errors, self.resets = [], [], []

        async def write(data):
            self.writes.append(data)

        async def reset():
            self.resets.append(True)

        self.life = ATVVLifecycle(write, reset, self.errors.append, interval=0.01, open_timeout=0.08)
        self.life.handle(CAPS)

    async def asyncTearDown(self):
        await self.life.close()

    async def settle(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0.002)

    async def test_idle_no_open_or_extend_and_duplicate_open_is_single(self):
        await asyncio.sleep(0.025)
        self.assertEqual(self.writes, [])
        self.life.request_open()
        self.life.request_open()
        await self.settle(lambda: bool(self.writes))
        self.assertEqual(self.writes, [b"\x0c\x00"])

    async def test_three_streams_renew_correct_id_and_stop_once(self):
        for stream in (0, 7, 255):
            self.life.request_open()
            await asyncio.sleep(0.005)
            self.life.handle(AudioStarted(stream))
            await self.settle(lambda: bytes((0x0e, stream)) in self.writes)
            self.life.handle(AudioStopped())
            await self.settle(lambda: len(self.resets) == (0, 7, 255).index(stream) + 1)
            self.life.handle(AudioStopped())  # close ACK does not reset again
            snapshot = list(self.writes)
            await asyncio.sleep(0.025)
            self.assertEqual(self.writes, snapshot)
        self.assertEqual(len(self.resets), 3)
        self.assertEqual(self.errors, [])

    async def test_replacement_stream_revokes_old_lease(self):
        self.life.handle(AudioStarted(3))
        await self.settle(lambda: b"\x0e\x03" in self.writes)
        self.life.handle(AudioStopped())
        self.life.handle(AudioStarted(9))
        self.writes.clear()
        await self.settle(lambda: b"\x0e\x09" in self.writes)
        self.assertNotIn(b"\x0e\x03", self.writes)
        self.assertNotIn(b"\x0d\x03", self.writes)

    async def test_close_cancels_and_awaits_blocked_write(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked(data):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.life._write = blocked
        self.life.handle(AudioStarted(8))
        await asyncio.wait_for(entered.wait(), 1)
        await self.life.close()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.errors, [])

    async def test_failed_extend_reports_once_and_disables_further_commands(self):
        async def failed(data):
            raise ConnectionError("device gone")

        self.life._write = failed
        self.life.handle(AudioStarted(1))
        await self.settle(lambda: bool(self.errors))
        self.life.request_open()
        self.life.handle(AudioStarted(2))
        await asyncio.sleep(0.025)
        self.assertEqual(len(self.errors), 1)

    async def test_open_waits_for_reset_and_does_not_overlap_writes(self):
        gate, entered = asyncio.Event(), asyncio.Event()

        async def reset():
            entered.set()
            await gate.wait()

        self.life._reset_audio = reset
        self.life.handle(AudioStarted(1))
        self.life.handle(AudioStopped())
        await entered.wait()
        self.life.request_open()
        await asyncio.sleep(0.005)
        self.assertEqual(self.writes, [b"\x0d\x01"])
        gate.set()
        await self.settle(lambda: b"\x0c\x00" in self.writes)

    async def test_missing_audio_start_times_out(self):
        self.life.request_open()
        await self.settle(lambda: bool(self.errors))
        self.assertIsInstance(self.errors[0], TimeoutError)

    async def test_physical_prearm_without_start_search_also_times_out(self):
        self.life.expect_start()
        await self.settle(lambda: bool(self.errors))
        self.assertEqual(self.writes, [])
        self.assertIsInstance(self.errors[0], TimeoutError)

    async def test_lost_audio_stop_does_not_leave_a_permanent_session(self):
        self.life._audio_timeout = 0.02
        self.life.handle(AudioStarted(1))
        await self.settle(lambda: bool(self.errors))
        self.assertIn("stopped delivering audio", str(self.errors[0]))

    async def test_no_renewal_without_session_id_or_supported_version(self):
        self.life.handle(AudioStarted(None))
        await asyncio.sleep(0.025)
        self.assertEqual(self.writes, [])
        with self.assertRaises(ValueError):
            proto.mic_extend_command(0, 0)


class ProtocolBoundaryTests(unittest.TestCase):
    def test_host_close_ack_does_not_end_the_next_stream(self):
        from ovb_rc003.atvv_session import MicCloseAcknowledged
        session = ATVVSession()
        session.handle_control(bytes.fromhex("04000009"))
        self.assertIsInstance(session.handle_control(bytes.fromhex("0000")), MicCloseAcknowledged)
        self.assertTrue(session.mic_open)

    def test_remote_close_is_stop_and_open_error_is_not_ignored(self):
        session = ATVVSession()
        session.handle_control(bytes.fromhex("04000007"))
        self.assertIsInstance(session.handle_control(bytes.fromhex("0d00")), AudioStopped)
        self.assertFalse(session.mic_open)
        from ovb_rc003.atvv_session import ATVVProtocolError
        with self.assertRaises(ATVVProtocolError):
            session.handle_control(bytes.fromhex("0c0001"))
