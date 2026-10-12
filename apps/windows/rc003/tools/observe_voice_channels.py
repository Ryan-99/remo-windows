"""Compare RC003 Raw Input and ATVV against the separately running Frida tap.

Reuses project adapters. Only subscribes and requests capabilities; never sends
MIC_OPEN, keyboard shortcuts or PCM to an audio device. No audio payload is saved.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ovb_rc003 import ble_transport_winrt as ble, hid_identity, identity
from ovb_rc003.raw_input_windows import RawInputButtonListener, enumerate_matching_device_paths


async def run(output: Path, seconds: int) -> int:
    output.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    pending = asyncio.Queue(maxsize=2048)
    counters = Counter()
    state = {"pid": os.getpid(), "stage": "starting", "audio_packets": 0,
             "audio_bytes": 0, "queue_drops": 0, "counts": {}}
    events = (output / "events.jsonl").open("a", encoding="utf-8", buffering=1)

    def record(kind, **fields):
        stamp = datetime.now(timezone.utc).isoformat()
        events.write(json.dumps({"time": stamp, "kind": kind, **fields}) + "\n")
        counters[kind] += 1
        state.update(updated_at=stamp, counts=dict(counters))
        temporary = output / "status.tmp"
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output / "status.json")
        if kind != "raw_input":
            print(json.dumps({"kind": kind, **fields}), flush=True)

    def enqueue(item):
        try:
            pending.put_nowait(item)
        except asyncio.QueueFull:
            state["queue_drops"] += 1

    def emit(kind, **fields):
        # Timestamp on callback entry, not after disk serialization.
        fields["observed_at"] = datetime.now(timezone.utc).isoformat()
        loop.call_soon_threadsafe(enqueue, (kind, fields))

    def raw_event(event):
        emit("raw_input", source=event.source, button=event.button_id,
             pressed=event.is_pressed, vkey=event.vkey, scan=event.make_code,
             flags=event.flags, report_length=len(event.report),
             usages=list(event.usages), decode_error=event.decode_error)

    class ObservedSession(ble.RC003BleSession):
        _keepalive = None

        async def connect(self, candidate):
            await super().connect(candidate)
            self._keepalive = self._service.session
            if not self._keepalive.can_maintain_connection:
                raise RuntimeError("ATVV session cannot maintain the BLE connection")
            self._keepalive.maintain_connection = True
            emit("connection_hold_enabled")

        async def close(self):
            try:
                await super().close()
            finally:
                if self._keepalive is not None:
                    try:
                        self._keepalive.maintain_connection = False
                        self._keepalive.close()
                    finally:
                        self._keepalive = None

        def _process_control(self, payload):
            emit("atvv_control", raw=payload.hex())
            super()._process_control(payload)

        def _process_audio(self, payload):
            # Count only; avoid decoding, persisting or playing microphone data.
            state["audio_packets"] += 1
            state["audio_bytes"] += len(payload)

    listener = RawInputButtonListener(
        on_button_event=lambda button, pressed: emit("raw_edge", button=button, pressed=pressed),
        on_raw_event=raw_event,
    )
    def make_session():
        return ObservedSession(
            on_pcm_frame=lambda _: None,
            on_control_event=lambda event: emit("atvv_event", event=type(event).__name__, detail=asdict(event)),
            on_error=lambda exc: emit("channel_error", error=f"{type(exc).__name__}: {exc}"),
            on_disconnected=lambda: emit("ble_disconnected"),
            loop=loop,
            manage_voice=False,
        )

    session = make_session()
    result = 0
    try:
        paths = enumerate_matching_device_paths()
        if paths:
            device_path = hid_identity.select_single_device_path(paths)
            listener.start(device_path)
            state["raw_available"] = True
            record("raw_ready", matched_collections=len(paths))
        else:
            state["raw_available"] = False
            record("raw_unavailable", reason="No RC003 collection exposed to this process")
        candidate = identity.select_single_candidate(await asyncio.wait_for(ble.discover_candidates(), 20))
        await asyncio.wait_for(session.connect(candidate), 30)
        state["stage"] = "waiting_for_caps"
        record("atvv_subscribed", microphone_open_sent=False)
        deadline = loop.time() + seconds
        next_status = loop.time() + 5
        while loop.time() < deadline and not (output / "stop-requested").exists():
            try:
                kind, fields = await asyncio.wait_for(pending.get(), 1)
                if kind == "ble_disconnected":
                    state["stage"] = "ble_disconnected"
                elif kind == "atvv_event" and fields.get("event") == "CapsReceived":
                    state["stage"] = "observing"
                record(kind, **fields)
                if kind == "ble_disconnected":
                    await asyncio.wait_for(session.close(), 12)
                    state["stage"] = "reconnecting"
                    record("reconnecting")
                    # Rediscover the selected device and rebuild subscriptions;
                    # a Python heartbeat alone is not evidence of a live BLE link.
                    session = make_session()
                    candidate = identity.select_single_candidate(await asyncio.wait_for(ble.discover_candidates(), 20))
                    await asyncio.wait_for(session.connect(candidate), 30)
                    state["stage"] = "waiting_for_caps"
                    record("atvv_resubscribed")
            except asyncio.TimeoutError:
                pass
            if loop.time() >= next_status:
                state["ble_queue_drops"] = session.dropped_event_count
                state["gatt_session_status"] = str(session._keepalive.session_status) if session._keepalive else None
                state["connection_status"] = str(session._device.connection_status) if session._device else None
                record("heartbeat", audio_packets=state["audio_packets"])
                next_status = loop.time() + 5
        state["stage"] = "stopping"
        record("stopping")
    except Exception as exc:
        result = 1
        state.update(stage="error", error=f"{type(exc).__name__}: {exc}")
        record("error", message=state["error"])
    finally:
        # Production transport closes the voice session/CCCDs on teardown.
        try:
            await asyncio.wait_for(session.close(), 12)
        except Exception as exc:
            result = 1
            record("cleanup_error", error=str(exc))
        try:
            listener.stop()
        except Exception as exc:
            result = 1
            record("cleanup_error", error=str(exc))
        state["stage"] = "stopped" if result == 0 else "error"
        record("finished", exit_code=result)
        events.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=900)
    args = parser.parse_args()
    code = asyncio.run(run(args.output, max(30, args.seconds)))
    sys.stdout.flush()
    sys.stderr.flush()
    # Cleanup completed above; avoid known WinRT interpreter-finalization crash.
    os._exit(code)
