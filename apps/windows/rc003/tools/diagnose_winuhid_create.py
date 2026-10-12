"""Safe WinUHid ABI probe.

This script never starts the Remo bridge and never submits an input report.
It loads the selected DLL, prints the native ABI/configuration details, tries
to create one keyboard collection, and immediately destroys it if successful.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import struct
import sys
from pathlib import Path

from ovb_rc003 import winuhid


def _last_error() -> dict[str, object]:
    getter = getattr(ctypes, "get_last_error", None)
    code = int(getter()) if getter else 0
    message = ""
    if code:
        try:
            message = ctypes.FormatError(code).strip()
        except Exception:
            pass
    return {"code": code, "message": message}


def _set_last_error(code: int) -> None:
    setter = getattr(ctypes, "set_last_error", None)
    if setter:
        setter(int(code))


def _probe(path: Path, *, create_consumer: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "python": sys.version,
        "platform": sys.platform,
        "machine": platform.machine(),
        "pointer_size": ctypes.sizeof(ctypes.c_void_p),
        "dll": str(path),
        "dll_size": path.stat().st_size,
        "dll_sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        "config_size": ctypes.sizeof(winuhid._WinUHidDeviceConfig),
        "config_offsets": {
            name: getattr(winuhid._WinUHidDeviceConfig, name).offset
            for name, _ in winuhid._WinUHidDeviceConfig._fields_
        },
        "descriptor_length": len(winuhid.KEYBOARD_REPORT_DESCRIPTOR),
    }
    dll = ctypes.WinDLL(str(path), use_last_error=True)
    get_version = dll.WinUHidGetDriverInterfaceVersion
    get_version.restype = ctypes.c_uint32
    _set_last_error(0)
    version = int(get_version())
    result["interface_version"] = version
    result["version_last_error"] = _last_error()

    create = dll.WinUHidCreateDevice
    create.argtypes = [ctypes.POINTER(winuhid._WinUHidDeviceConfig)]
    create.restype = ctypes.c_void_p
    destroy = dll.WinUHidDestroyDevice
    destroy.argtypes = [ctypes.c_void_p]
    destroy.restype = None

    descriptor = (ctypes.c_uint8 * len(winuhid.KEYBOARD_REPORT_DESCRIPTOR)).from_buffer_copy(
        winuhid.KEYBOARD_REPORT_DESCRIPTOR
    )
    instance_text = "RemoWinUHidProbe"
    encoded = instance_text.encode("utf-16-le")
    instance = (ctypes.c_uint16 * (len(instance_text) + 1))()
    for index in range(len(instance_text)):
        instance[index] = struct.unpack_from("<H", encoded, index * 2)[0]
    config = winuhid._WinUHidDeviceConfig(
        0,
        0,
        1,
        1,
        len(winuhid.KEYBOARD_REPORT_DESCRIPTOR),
        ctypes.cast(descriptor, ctypes.POINTER(ctypes.c_uint8)),
        (ctypes.c_ubyte * 16)(),
        ctypes.cast(instance, ctypes.POINTER(ctypes.c_uint16)),
        None,
        0,
    )
    result["config_values"] = {
        "supported_events": int(config.supported_events),
        "vendor_id": int(config.vendor_id),
        "product_id": int(config.product_id),
        "version_number": int(config.version_number),
        "report_descriptor_length": int(config.report_descriptor_length),
        "report_descriptor_ptr": hex(ctypes.addressof(descriptor)),
        "instance_id_ptr": hex(ctypes.addressof(instance)),
        "hardware_ids_ptr": None,
        "read_report_period_us": int(config.read_report_period_us),
    }
    _set_last_error(0)
    handle = create(ctypes.byref(config))
    result["create_last_error"] = _last_error()
    result["create_handle"] = hex(int(handle)) if handle else None
    if handle and create_consumer:
        consumer_descriptor = (ctypes.c_uint8 * len(winuhid.CONSUMER_REPORT_DESCRIPTOR)).from_buffer_copy(
            winuhid.CONSUMER_REPORT_DESCRIPTOR
        )
        consumer_instance_text = "RemoWinUHidProbeConsumer"
        consumer_encoded = consumer_instance_text.encode("utf-16-le")
        consumer_instance = (ctypes.c_uint16 * (len(consumer_instance_text) + 1))()
        for index in range(len(consumer_instance_text)):
            consumer_instance[index] = struct.unpack_from("<H", consumer_encoded, index * 2)[0]
        consumer_config = winuhid._WinUHidDeviceConfig(
            0, 0, 2, 1, len(winuhid.CONSUMER_REPORT_DESCRIPTOR),
            ctypes.cast(consumer_descriptor, ctypes.POINTER(ctypes.c_uint8)),
            (ctypes.c_ubyte * 16)(),
            ctypes.cast(consumer_instance, ctypes.POINTER(ctypes.c_uint16)), None, 0,
        )
        _set_last_error(0)
        consumer_handle = create(ctypes.byref(consumer_config))
        result["consumer_create_last_error"] = _last_error()
        result["consumer_create_handle"] = hex(int(consumer_handle)) if consumer_handle else None
        if consumer_handle:
            destroy(consumer_handle)
    if handle:
        destroy(handle)
        result["destroyed"] = True
    else:
        result["destroyed"] = False
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dll", type=Path, default=None)
    parser.add_argument("--consumer", action="store_true", help="also create/destroy a consumer collection")
    args = parser.parse_args()
    if sys.platform != "win32":
        print(json.dumps({"error": "Windows only", "platform": sys.platform}, indent=2))
        return 2
    path = args.dll or next((candidate for candidate in winuhid.dll_candidates() if candidate.is_file()), None)
    if path is None:
        print(json.dumps({"error": "WinUHid.dll not found"}, indent=2))
        return 2
    try:
        print(json.dumps(_probe(Path(path), create_consumer=args.consumer), indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}", "last_error": _last_error()}, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
