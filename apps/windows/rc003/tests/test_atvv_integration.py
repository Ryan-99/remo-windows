"""Transport + lifecycle integration, without pairing or changing Windows state."""
import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace

from ovb_rc003.ble_transport_winrt import RC003BleSession
from ovb_rc003.identity import RC003Candidate
from .fakes.fake_winrt import FakeWinRTEnvironment, FakeEventArgs


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = FakeWinRTEnvironment()
        self.errors = []
        self.session = RC003BleSession(
            lambda _: None, winrt=self.env.build_winrt_modules(), on_error=self.errors.append
        )
        self.candidate = RC003Candidate(name=self.env.name, hardware_match=False, handle=self.env.discovered_info)

    async def asyncTearDown(self):
        await self.session.close()

    async def settle(self, condition):
        async with asyncio.timeout(1):
            while not condition():
                await asyncio.sleep(0.002)

    async def ready(self):
        await self.session.connect(self.candidate)
        self.env.control_characteristic.fire(bytes.fromhex("0b010002030078"))
        await self.session.wait_ready(1)

    async def test_caps_required_and_wait_is_bounded(self):
        await self.session.connect(self.candidate)
        with self.assertRaises(TimeoutError):
            await self.session.wait_ready(0.01)
        self.session.send_mic_open_threadsafe()
        await asyncio.sleep(0.01)
        self.assertEqual(self.env.tx_characteristic.write_history, [bytes.fromhex("0a0100000303")])

    async def test_three_sessions_restore_real_notify_tokens(self):
        await self.ready()
        self.session._lifecycle._interval = 0.01
        for stream in (1, 2, 3):
            self.session.send_mic_open_threadsafe()
            await self.settle(lambda: self.env.tx_characteristic.write_history.count(b"\x0c\x00") == stream)
            self.env.control_characteristic.fire(bytes((4, 0, 0, stream)))
            await self.settle(lambda: bytes((14, stream)) in self.env.tx_characteristic.write_history)
            self.env.control_characteristic.fire(b"\x00\x02")
            await self.settle(lambda: self.env.audio_characteristic.cccd_history.count(1) == stream + 1)
            self.assertEqual(len(self.env.audio_characteristic._handlers), 1)
            self.env.control_characteristic.fire(b"\x00\x00")
            await asyncio.sleep(0.005)
        self.assertEqual(self.env.audio_characteristic.cccd_history, [1, 0, 1, 0, 1, 0, 1])
        self.assertEqual(self.errors, [])

    async def test_disconnect_notification_burst_keeps_first_deadline(self):
        await self.ready()
        self.session._schedule_disconnect_confirmation()
        deadline = self.session._disconnect_handle.when()
        self.session._schedule_disconnect_confirmation()
        self.assertEqual(self.session._disconnect_handle.when(), deadline)

    async def test_stale_callback_after_close_does_not_enqueue(self):
        await self.ready()
        callback = next(iter(self.env.audio_characteristic._handlers.values()))
        await self.session.close()
        callback(None, FakeEventArgs(b"stale"))
        self.assertTrue(self.session._event_queue.empty())

    async def test_gatt_keepalive_released_before_device_close(self):
        hold = SimpleNamespace(can_maintain_connection=True, maintain_connection=False)
        closed = []
        hold.close = lambda: closed.append(not self.env.device.closed)
        self.env.device.bluetooth_device_id = object()

        async def open_hold(device_id):
            self.assertIs(device_id, self.env.device.bluetooth_device_id)
            return hold

        self.session._winrt = replace(self.session._winrt, gatt_session=SimpleNamespace(from_device_id_async=open_hold))
        await self.ready()
        self.assertTrue(hold.maintain_connection)
        await self.session.close()
        self.assertFalse(hold.maintain_connection)
        self.assertEqual(closed, [True])

    async def test_observe_mode_never_sends_open_extend_or_session_reset(self):
        self.session = RC003BleSession(lambda _: None, winrt=self.env.build_winrt_modules(), manage_voice=False)
        await self.ready()
        self.session.send_mic_open_threadsafe()
        self.env.control_characteristic.fire(bytes.fromhex("04000001"))
        self.env.control_characteristic.fire(b"\x00\x02")
        await asyncio.sleep(0.04)
        self.assertEqual(self.env.tx_characteristic.write_history, [bytes.fromhex("0a0100000303")])
        self.assertEqual(self.env.audio_characteristic.cccd_history, [1])
