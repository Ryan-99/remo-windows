import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import windows_audio_defaults as route
from ovb_rc003 import windows_diagnostics as diag


def snapshot(
    render=("扬声器 (Realtek(R) Audio)",) * 3,
    capture=("CABLE Output (VB-Audio Virtual Cable)",) * 3,
    render_devices=(),
    capture_devices=("CABLE Output (VB-Audio Virtual Cable)",),
):
    return route.VoiceRouteSnapshot(
        render=render,
        capture=capture,
        render_devices=render_devices or render,
        capture_devices=capture_devices,
    )


class VoiceRoutePlanTests(unittest.TestCase):
    def test_speakers_on_the_cable_move_back_to_the_real_communications_device(self):
        cable = "扬声器 (VB-Audio Virtual Cable)"
        monitor = "2 - PHL 243V7 (AMD High Definition Audio Device)"
        current = snapshot(
            render=(cable, cable, monitor),
            render_devices=(
                "扬声器 (Realtek(R) Audio)",
                cable,
                monitor,
                "CABLE In 16 Ch (VB-Audio Virtual Cable)",
                "扬声器 (ToDesk Virtual Audio)",
            ),
        )
        plan = route.plan_repair(current)
        self.assertEqual(plan.playback_name, monitor)
        self.assertEqual(plan.playback_roles, (0, 1))
        self.assertEqual(plan.capture_name, "")
        self.assertFalse(plan.playback_blocked)

    def test_sixteen_channel_cable_is_also_treated_as_the_virtual_speaker(self):
        self.assertTrue(route.is_virtual_cable_playback("CABLE In 16 Ch (VB-Audio Virtual Cable)"))
        self.assertFalse(route.is_virtual_cable_playback("CABLE Output (VB-Audio Virtual Cable)"))

    def test_default_microphone_is_moved_to_cable_output(self):
        current = snapshot(
            capture=("阵列麦克风 (AMD Audio Device)",) * 3,
            capture_devices=(
                "CABLE Output (VB-Audio Virtual Cable)",
                "阵列麦克风 (AMD Audio Device)",
            ),
        )
        plan = route.plan_repair(current)
        self.assertEqual(plan.capture_name, "CABLE Output (VB-Audio Virtual Cable)")
        self.assertEqual(plan.playback_name, "")

    def test_a_healthy_route_changes_nothing(self):
        plan = route.plan_repair(snapshot())
        self.assertFalse(plan.changes_anything)
        self.assertIn("没有改动", route.repair_message(plan))

    def test_no_real_speaker_does_not_invent_a_playback_target(self):
        cable = "扬声器 (VB-Audio Virtual Cable)"
        current = snapshot(
            render=(cable, cable, cable),
            render_devices=(cable, "扬声器 (ToDesk Virtual Audio)"),
        )
        plan = route.plan_repair(current)
        self.assertEqual(plan.playback_name, "")
        self.assertTrue(plan.playback_blocked)
        self.assertIn("没有找到可改回的真实扬声器", route.repair_message(plan))


class CaptureHandoffTests(unittest.TestCase):
    def test_opening_remembers_the_real_microphone_and_selects_the_cable(self):
        mic = "阵列麦克风 (AMD Audio Device)"
        cable = "CABLE Output (VB-Audio Virtual Cable)"
        current = snapshot(
            capture=(mic, mic, mic),
            capture_devices=(cable, mic),
        )
        self.assertEqual(route.remembered_capture(current.capture), (mic, mic, mic))
        self.assertEqual(route.cable_capture_name(current), cable)
        self.assertEqual(route.roles_to_switch(current.capture, cable), (0, 1, 2))

    def test_a_microphone_already_on_the_cable_keeps_the_saved_real_device(self):
        mic = "阵列麦克风 (AMD Audio Device)"
        cable = "CABLE Output (VB-Audio Virtual Cable)"
        remembered = route.remembered_capture((cable, cable, mic), (mic, "", cable))
        self.assertEqual(remembered, (mic, "", mic))

    def test_restore_skips_a_missing_device_and_a_role_already_restored(self):
        mic = "阵列麦克风 (AMD Audio Device)"
        other = "耳机麦克风"
        assignments = route.roles_to_restore(
            ("CABLE Output (VB-Audio Virtual Cable)", mic, "CABLE Output (VB-Audio Virtual Cable)"),
            (mic, mic, other),
            (mic,),
        )
        self.assertEqual(assignments, ((0, mic),))

    def test_session_switches_on_open_and_restores_on_close(self):
        mic = "阵列麦克风 (AMD Audio Device)"
        cable = "CABLE Output (VB-Audio Virtual Cable)"
        state = {"current": snapshot(capture=(mic,) * 3, capture_devices=(cable, mic))}
        applied = []

        def read():
            return state["current"]

        def apply_roles(name, roles):
            applied.append((name, roles))
            capture = list(state["current"].capture)
            for role in roles:
                capture[role] = name
            state["current"] = snapshot(capture=tuple(capture), capture_devices=(cable, mic))

        path = Path(self._path())
        with mock.patch.dict(os.environ, {"REMO_CAPTURE_HANDOFF": "1"}):
            opened = route.engage_capture_session(
                path, read=read, apply_roles=apply_roles, pid=10, pid_running=lambda _pid: False
            )
            self.assertTrue(opened.restore_on_exit)
            self.assertIn("虚拟声卡", opened.message)
            self.assertEqual(applied, [(cable, (0, 1, 2))])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["capture"], [mic, mic, mic])
            self.assertNotIn("SWD", path.read_text(encoding="utf-8"))

            route.restore_capture_session(
                path, read=read, apply_roles=apply_roles, pid=10, pid_running=lambda _pid: False
            )
        self.assertEqual(applied[-1], (mic, (0, 1, 2)))
        self.assertFalse(path.exists())

    def test_a_second_live_remo_does_not_take_or_restore_the_microphone(self):
        mic = "阵列麦克风 (AMD Audio Device)"
        cable = "CABLE Output (VB-Audio Virtual Cable)"
        path = Path(self._path())
        path.write_text(
            json.dumps({"owner_pid": 10, "capture": [mic, mic, mic]}),
            encoding="utf-8",
        )
        current = snapshot(capture=(cable,) * 3, capture_devices=(cable, mic))
        with mock.patch.dict(os.environ, {"REMO_CAPTURE_HANDOFF": "1"}):
            opened = route.engage_capture_session(
                path,
                read=lambda: current,
                apply_roles=lambda *_args: self.fail("switched"),
                pid=20,
                pid_running=lambda pid: pid == 10,
            )
            self.assertFalse(opened.restore_on_exit)
            route.restore_capture_session(
                path,
                read=lambda: current,
                apply_roles=lambda *_args: self.fail("restored"),
                pid=20,
                pid_running=lambda pid: pid == 10,
            )
        self.assertTrue(path.exists())

    def test_handoff_can_be_disabled_for_tests(self):
        with mock.patch.dict(os.environ, {"REMO_CAPTURE_HANDOFF": "0"}):
            result = route.engage_capture_session(
                Path(self._path()),
                read=lambda: self.fail("read"),
            )
        self.assertFalse(result.restore_on_exit)

    def _path(self) -> str:
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        return str(Path(directory) / "capture-handoff.json")


class VoiceRouteCheckTests(unittest.TestCase):
    def test_virtual_speaker_fails_with_the_echo_explanation(self):
        cable = "扬声器 (VB-Audio Virtual Cable)"
        current = snapshot(render=(cable, cable, "扬声器 (Realtek(R) Audio)"))
        result = diag.check_voice_route(read=lambda: current)
        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertEqual(result.check_id, "voice_route")
        self.assertIn("回声", result.detail)
        self.assertNotIn("{", result.detail)

    def test_wrong_default_microphone_fails(self):
        current = snapshot(capture=("阵列麦克风 (AMD Audio Device)",) * 3)
        result = diag.check_voice_route(read=lambda: current)
        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("CABLE Output", result.detail)

    def test_healthy_route_passes_and_reminds_about_the_input_method_picker(self):
        result = diag.check_voice_route(read=lambda: snapshot())
        self.assertEqual(result.status, diag.CheckStatus.PASS)
        self.assertIn("扬声器", result.detail)

    def test_missing_cable_is_unsupported_rather_than_a_false_failure(self):
        current = snapshot(
            render=("扬声器 (Realtek(R) Audio)",) * 3,
            capture=("阵列麦克风 (AMD Audio Device)",) * 3,
            capture_devices=("阵列麦克风 (AMD Audio Device)",),
        )
        result = diag.check_voice_route(read=lambda: current)
        self.assertEqual(result.status, diag.CheckStatus.UNSUPPORTED)

    def test_unreadable_defaults_do_not_leak_the_exception(self):
        def _boom():
            raise RuntimeError(r"\\?\SWD#MMDEVAPI#{secret}")

        result = diag.check_voice_route(read=_boom)
        self.assertEqual(result.status, diag.CheckStatus.UNSUPPORTED)
        self.assertNotIn("secret", result.detail)
        self.assertNotIn("MMDEVAPI", result.detail)


if __name__ == "__main__":
    unittest.main()
