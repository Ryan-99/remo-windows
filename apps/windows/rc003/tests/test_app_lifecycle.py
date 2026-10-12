from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from ovb_rc003 import app as app_module


class _FakeSignal:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _FakeApp:
    instances = []

    def __init__(
        self,
        *,
        run_error: BaseException | None = None,
        status_publisher=None,
    ) -> None:
        self.run_error = run_error
        self.status_publisher = status_publisher
        self.finish = asyncio.Event()
        self.stop_calls = 0
        type(self).instances.append(self)

    async def run_forever(self) -> None:
        if self.run_error is not None:
            raise self.run_error
        await self.finish.wait()

    async def stop(self) -> None:
        self.stop_calls += 1
        self.finish.set()


class AppLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeApp.instances = []

    def test_external_stop_waits_for_bridge_cleanup_before_closing_signal(self):
        signal = _FakeSignal()

        async def external_stop(_signal, *, shutdown_event):
            self.assertFalse(shutdown_event.is_set())
            return True

        with mock.patch.object(app_module, "RC003App", _FakeApp), mock.patch.object(
            app_module.bridge_control,
            "create_bridge_stop_signal",
            return_value=signal,
        ), mock.patch.object(
            app_module.bridge_control,
            "wait_for_stop_request",
            side_effect=external_stop,
        ):
            asyncio.run(app_module._run())

        instance = _FakeApp.instances[0]
        self.assertGreaterEqual(instance.stop_calls, 1)
        self.assertTrue(instance.finish.is_set())
        self.assertEqual(signal.close_calls, 1)

    def test_bridge_failure_still_stops_waiter_and_closes_signal(self):
        signal = _FakeSignal()

        class FailingApp(_FakeApp):
            def __init__(self, *, status_publisher=None):
                super().__init__(
                    run_error=RuntimeError("bridge failed"),
                    status_publisher=status_publisher,
                )

        async def local_waiter(_signal, *, shutdown_event):
            await shutdown_event.wait()
            return False

        with mock.patch.object(app_module, "RC003App", FailingApp), mock.patch.object(
            app_module.bridge_control,
            "create_bridge_stop_signal",
            return_value=signal,
        ), mock.patch.object(
            app_module.bridge_control,
            "wait_for_stop_request",
            side_effect=local_waiter,
        ):
            with self.assertRaisesRegex(RuntimeError, "bridge failed"):
                asyncio.run(app_module._run())

        self.assertGreaterEqual(FailingApp.instances[0].stop_calls, 1)
        self.assertEqual(signal.close_calls, 1)

    def test_waiter_failure_still_settles_bridge_and_closes_signal(self):
        signal = _FakeSignal()

        async def failing_waiter(_signal, *, shutdown_event):
            raise RuntimeError("wait failed")

        with mock.patch.object(app_module, "RC003App", _FakeApp), mock.patch.object(
            app_module.bridge_control,
            "create_bridge_stop_signal",
            return_value=signal,
        ), mock.patch.object(
            app_module.bridge_control,
            "wait_for_stop_request",
            side_effect=failing_waiter,
        ):
            with self.assertRaisesRegex(RuntimeError, "wait failed"):
                asyncio.run(app_module._run())

        instance = _FakeApp.instances[0]
        self.assertGreaterEqual(instance.stop_calls, 1)
        self.assertTrue(instance.finish.is_set())
        self.assertEqual(signal.close_calls, 1)

    def test_cancellation_still_settles_both_tasks_and_closes_signal(self):
        signal = _FakeSignal()

        async def local_waiter(_signal, *, shutdown_event):
            await shutdown_event.wait()
            return False

        async def scenario():
            task = asyncio.create_task(app_module._run())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with mock.patch.object(app_module, "RC003App", _FakeApp), mock.patch.object(
            app_module.bridge_control,
            "create_bridge_stop_signal",
            return_value=signal,
        ), mock.patch.object(
            app_module.bridge_control,
            "wait_for_stop_request",
            side_effect=local_waiter,
        ):
            asyncio.run(scenario())

        instance = _FakeApp.instances[0]
        self.assertGreaterEqual(instance.stop_calls, 1)
        self.assertTrue(instance.finish.is_set())
        self.assertEqual(signal.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
