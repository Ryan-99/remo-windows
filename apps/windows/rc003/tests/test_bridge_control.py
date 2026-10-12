from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from ovb_rc003 import bridge_control


class FakeKernel32:
    def __init__(self, *, create_handle=41, open_handle=42, set_result=1):
        self.create_handle = create_handle
        self.open_handle = open_handle
        self.set_result = set_result
        self.calls = []
        self.wait_results = [bridge_control._WAIT_TIMEOUT, bridge_control._WAIT_OBJECT_0]

    def CreateEventW(self, *args):
        self.calls.append(("create", args))
        return self.create_handle

    def OpenEventW(self, *args):
        self.calls.append(("open", args))
        return self.open_handle

    def SetEvent(self, handle):
        self.calls.append(("set", handle))
        return self.set_result

    def WaitForSingleObject(self, handle, timeout):
        self.calls.append(("wait", handle, timeout))
        return self.wait_results.pop(0)

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return 1


class BridgeControlTests(unittest.TestCase):
    def test_create_wait_and_close_use_one_named_event(self):
        api = FakeKernel32()
        signal = bridge_control.create_bridge_stop_signal(kernel32=api)

        self.assertTrue(asyncio.run(bridge_control.wait_for_stop_request(signal)))
        signal.close()

        self.assertEqual(api.calls[0][0], "create")
        self.assertEqual(api.calls[0][1][-1], bridge_control.STOP_EVENT_NAME)
        self.assertEqual([call[0] for call in api.calls], ["create", "wait", "wait", "close"])

    def test_local_shutdown_settles_waiter_without_setting_public_event(self):
        api = FakeKernel32()
        api.wait_results = [bridge_control._WAIT_TIMEOUT]
        signal = bridge_control.create_bridge_stop_signal(kernel32=api)

        async def scenario():
            shutdown = asyncio.Event()
            task = asyncio.create_task(
                bridge_control.wait_for_stop_request(
                    signal,
                    shutdown_event=shutdown,
                )
            )
            await asyncio.sleep(0)
            shutdown.set()
            return await task

        self.assertFalse(asyncio.run(scenario()))
        signal.close()
        self.assertEqual([call[0] for call in api.calls], ["create", "wait", "close"])

    def test_request_stop_reports_not_running_without_open_handle(self):
        api = FakeKernel32(open_handle=0)
        self.assertEqual(
            bridge_control.request_bridge_stop(kernel32=api),
            bridge_control.StopRequestOutcome.NOT_RUNNING,
        )

    def test_request_stop_reports_failure_when_set_event_fails(self):
        api = FakeKernel32(set_result=0)
        self.assertEqual(
            bridge_control.request_bridge_stop(kernel32=api),
            bridge_control.StopRequestOutcome.FAILED,
        )
        self.assertEqual([call[0] for call in api.calls], ["open", "set", "close"])

    def test_request_stop_reports_requested_and_closes_sender_handle(self):
        api = FakeKernel32()
        self.assertEqual(
            bridge_control.request_bridge_stop(kernel32=api),
            bridge_control.StopRequestOutcome.REQUESTED,
        )
        self.assertEqual([call[0] for call in api.calls], ["open", "set", "close"])

    def test_non_windows_is_explicitly_unavailable(self):
        with mock.patch.object(bridge_control.sys, "platform", "linux"):
            self.assertEqual(
                bridge_control.request_bridge_stop(),
                bridge_control.StopRequestOutcome.UNAVAILABLE,
            )
