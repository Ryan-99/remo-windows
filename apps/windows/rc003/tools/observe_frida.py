"""Standalone, read-only RC003 Gadget session. Does not import/start RC003App.

Uses the existing pinned Gadget and injector; saves PID-checked handshake,
heartbeat and physical edges independently of UI/actions. Close the socket on
exit so observe-v1 detaches its hook; the loaded DLL can reconnect next session.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ovb_rc003.frida_compat import decode_rc003_ioctl_output, payload_usages
from ovb_rc003.frida_hid_tap_injector import inject_current_process
from ovb_rc003 import frida_hid_tap_runtime as runtime

BUTTONS = {
    0x3E: "mic", 0xF1: "back", 0x28: "ok", 0x35: "tv", 0x4A: "home",
    0x4F: "right", 0x50: "left", 0x51: "down", 0x52: "up", 0x65: "menu",
    0x66: "power", 0x80: "volume_up", 0x81: "volume_down",
}


def valid_report(message: dict) -> tuple[bytes, set[int]] | None:
    raw = message.get("raw")
    if not isinstance(raw, str) or len(raw) != 18:
        return None
    try:
        data = bytes.fromhex(raw)
    except ValueError:
        return None
    payload = decode_rc003_ioctl_output(data)
    if payload is None:
        return None
    usages = payload_usages(payload)
    if not usages.issubset(BUTTONS):
        return None
    return data, usages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    status_path = args.output / "status.json"
    stop_path = args.output / "stop-requested"
    if stop_path.exists():
        parser.error("Choose a fresh output directory (stop-requested exists).")
    status = {"observer_pid": os.getpid(), "mode": "observe_only", "stage": "preflight",
              "reports": 0, "heartbeats": 0, "downs": {}, "ups": {}}
    events = (args.output / "events.jsonl").open("a", encoding="utf-8", buffering=1)

    def record(kind: str, **fields) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        events.write(json.dumps({"time": timestamp, "kind": kind, **fields}) + "\n")
        status["updated_at"] = timestamp
        temp = status_path.with_suffix(".tmp")
        temp.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
        temp.replace(status_path)
        if kind != "report":
            print(json.dumps({"kind": kind, **fields}), flush=True)

    active: set[int] = set()
    downs: Counter[str] = Counter()
    ups: Counter[str] = Counter()
    client = None
    try:
        if not runtime.gadget_archive_available():
            raise RuntimeError("Pinned Gadget archive absent or hash mismatch")
        host_pid = runtime.find_rc003_hidogatt_host_pid()
        if host_pid is None:
            raise RuntimeError("No unique RC003 HID host; connect the remote first")
        status["host_pid"] = host_pid
        deadline = time.monotonic() + max(30, args.seconds)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            server.bind(("127.0.0.1", runtime.HID_TAP_PORT))
            server.listen(1)
            server.settimeout(1)
            status["stage"] = "injecting"
            record("listening", port=runtime.HID_TAP_PORT, pid=host_pid)
            # An already-loaded Gadget reconnects itself. Avoid repeatedly
            # LoadLibrary-ing it (and increasing its loader reference count).
            server.settimeout(3)
            try:
                client, _ = server.accept()
                record("existing_gadget_connected")
            except socket.timeout:
                inject_current_process(host_pid)
                record("loader_completed")
            server.settimeout(1)
            ready = False
            last_heartbeat = time.monotonic()
            last_seq = None
            buffer = b""
            if client is not None:
                client.settimeout(1)
            status["stage"] = "waiting_for_handshake"
            record("waiting_for_handshake")
            while time.monotonic() < deadline and not stop_path.exists():
                if runtime.find_rc003_hidogatt_host_pid() != host_pid:
                    raise RuntimeError("Host changed; start a new observation session")
                if client is None:
                    try:
                        client, _ = server.accept()
                    except socket.timeout:
                        continue
                    client.settimeout(1)
                    ready = False
                    last_heartbeat = time.monotonic()
                    last_seq = None
                    buffer = b""
                try:
                    chunk = client.recv(4096)
                    if not chunk:
                        raise ConnectionError("Gadget socket closed")
                    buffer += chunk
                    if len(buffer) > 65536:
                        raise ValueError("Gadget frame exceeds limit")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        message = json.loads(line)
                        if not isinstance(message, dict) or message.get("pid") != host_pid:
                            raise ValueError("Unexpected Gadget frame or host PID")
                        kind = message.get("kind")
                        if kind == "ready":
                            if message.get("hook_installed") is not True or message.get("protocol") != "observe-v1":
                                raise RuntimeError("Missing hook or stale injected script; do not accept as connected")
                            ready = True
                            last_heartbeat = time.monotonic()
                            status["stage"] = "connected_waiting_for_keys"
                            record("ready", pid=host_pid, hook_installed=True, protocol=message["protocol"])
                        elif not ready:
                            raise ValueError("Report before verified handshake")
                        elif kind == "heartbeat":
                            if message.get("hook_installed") is not True:
                                raise RuntimeError("Gadget hook inactive")
                            last_heartbeat = time.monotonic()
                            status["heartbeats"] += 1
                            status["io_counts"] = message.get("counts", {})
                            record("heartbeat", counts=status["io_counts"])
                        elif kind == "gatt_read":
                            seq = message.get("seq")
                            if not isinstance(seq, int) or (last_seq is not None and seq != last_seq + 1):
                                raise ValueError("Gadget sequence gap or invalid sequence")
                            last_seq = seq
                            parsed = valid_report(message)
                            if parsed is None:
                                record("rejected_report")
                                continue
                            data, current = parsed
                            pressed = current - active
                            released = active - current
                            active = current
                            downs.update(BUTTONS[u] for u in pressed)
                            ups.update(BUTTONS[u] for u in released)
                            status.update(stage="receiving_reports", reports=status["reports"] + 1,
                                          downs=dict(downs), ups=dict(ups), active=[BUTTONS[u] for u in sorted(active)])
                            record("report", seq=seq, raw=data.hex(), active=status["active"])
                            if pressed or released:
                                record("edges", down=[BUTTONS[u] for u in sorted(pressed)], up=[BUTTONS[u] for u in sorted(released)])
                        elif kind == "error":
                            raise RuntimeError(str(message.get("message")))
                except socket.timeout:
                    if time.monotonic() - last_heartbeat > 15:
                        # A queued connection from a previous observer may carry
                        # ready but no live heartbeat. Discard it, keep listening.
                        record("stale_connection", interrupted_active=[BUTTONS[u] for u in sorted(active)])
                        client.close()
                        client = None
                        active.clear()
                        status["stage"] = "waiting_for_reconnect"
                        record("waiting_for_reconnect")
                except ConnectionError as exc:
                    record("disconnected", reason=str(exc), interrupted_active=[BUTTONS[u] for u in sorted(active)])
                    active.clear()  # Never count a synthetic release as physical evidence.
                    client.close()
                    client = None
                    status["stage"] = "waiting_for_reconnect"
                    record("waiting_for_reconnect")
            status["stage"] = "stopped"
            record("stopped", reason="requested" if stop_path.exists() else "duration_elapsed")
        return 0
    except Exception as exc:
        status.update(stage="error", error=f"{type(exc).__name__}: {exc}")
        record("error", message=status["error"])
        return 1
    finally:
        if client is not None:
            client.close()
        events.close()


if __name__ == "__main__":
    raise SystemExit(main())
