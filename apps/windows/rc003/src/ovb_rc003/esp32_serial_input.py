"""ESP32 BLE-bridge serial input source for the RC003.

The RC003 is a Bluetooth LE (HOGP) device with no Classic Bluetooth radio, and
a BLE peripheral accepts exactly one central connection at a time.  That makes
this input source a *replacement* for the Windows path, not an addition to it:
while the bridge board holds the link, Windows receives neither HID buttons nor
the ATVV voice channel.  The mode is therefore opt-in (``input_source``) and
defaults to the existing Windows path.

What this buys is the thing the Windows path cannot provide.  ``mshidumdf`` /
``kbdhid`` silently drop three RC003 usages (0x00F1 back, 0x0080 volume up,
0x0081 volume down) before any user-mode code can observe them, and the HID
service is exclusively opened so nothing else can read the raw reports.  A
separate BLE central reads the GATT characteristics directly, upstream of every
component that discards them, and forwards the raw usages over USB CDC.

The board is a text line source, deliberately: a newline-framed ASCII protocol
is debuggable from any serial terminal, and the same stream that the app parses
is the one a human reads while bringing the hardware up.

Protocol (bridge -> host), one message per line, ASCII, ``\\n``-terminated::

    RDY  <proto> <firmware> <board>     bridge booted, protocol speaks <proto>
    DEV  <remote-name>                  remote currently linked by the bridge
    KEY  <usage-hex> <D|U> <seq> <ms>   one button edge (the payload we need)
    BAT  <percent>                      remote battery, when readable
    ERR  <text>                         bridge-side failure, for the log
    PONG <ms>                           reply to a host PING

Host -> bridge::

    PING                                keepalive / liveness probe

Any line that is not one of the tags above is ignored, so the firmware is free
to interleave human-readable diagnostics (its own banner, GATT dumps, decode
annotations) on the same stream without the parser needing a quiet mode.

Privacy: the protocol carries no Bluetooth address and no device path.  The
remote is identified to this module only by a display name.  See
``config.FORBIDDEN_KEYS`` for the contract this upholds.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from . import device_profile, logging_setup

try:  # pragma: no cover - exercised only on hosts without pyserial
    import serial  # type: ignore[import-untyped]
    import serial.tools.list_ports  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    serial = None  # type: ignore[assignment]


# Protocol revision this module understands.  A bridge reporting a different
# value is still accepted (the tags are additive), but the mismatch is logged
# once so a stale firmware is diagnosable rather than silently wrong.
PROTOCOL_VERSION = 1

# --- Bridge port auto-detection -------------------------------------------
#
# Ports are *ranked* by USB ID and then *confirmed* by protocol.  Neither half
# is sufficient alone:
#
#   * Ranking alone cannot tell two Espressif boards on one machine apart, and
#     it cannot see a board wired through a USB-UART bridge chip at all.
#   * Probing alone would mean opening every serial port on the host, which is
#     a side effect on devices that have nothing to do with this app.
#
# So the probe is confined to a candidate set built from these tiers.  Tier 1
# is the chip's own USB-Serial/JTAG peripheral, with no bridge chip in
# between; tier 2 is the USB-UART bridges ESP32 boards are commonly wired
# through; tier 3 is everything else, and is opt-in only.
ESP32_NATIVE_VIDS = frozenset({0x303A})  # Espressif
ESP32_UART_BRIDGE_VIDS = frozenset({
    0x1A86,  # QinHeng  - CH340 / CH9102 / CH343
    0x10C4,  # SiLabs   - CP2102 / CP2104
    0x0403,  # FTDI     - FT232 / FT2232
    0x067B,  # Prolific - PL2303
})

# Any of these as a line's first token proves the speaker is running the
# bridge firmware.  RDY is what shows up at boot; the rest cover attaching to
# a board that has been running since long before the app started.
BRIDGE_PROTOCOL_TAGS = frozenset({"RDY", "DEV", "BAT", "KEY", "ERR"})

# The firmware's own restart command (see the firmware README's command
# table).  It rescans and re-emits RDY/DEV, which is exactly the handshake a
# probe needs when the boot banner is long gone.
BRIDGE_RESTART_COMMAND = b"r"

DEFAULT_PROBE_TIMEOUT = 2.5

DEFAULT_BAUD = 115200
DEFAULT_START_TIMEOUT = 5.0
DEFAULT_PING_INTERVAL = 10.0
DEFAULT_STOP_TIMEOUT = 3.0

# Callback shape shared with ``raw_input_windows.RawInputButtonListener`` so
# either source can be handed to ``RC003App`` unchanged.
ButtonEventCallback = Callable[[str, bool], None]


class Esp32SerialUnavailableError(RuntimeError):
    """The bridge board could not be opened or is not speaking the protocol."""


# ---------------------------------------------------------------------------
# Pure protocol layer - no threads, no pyserial, fully unit-testable.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyMessage:
    """One button edge reported by the bridge."""

    usage: int
    is_pressed: bool
    sequence: int
    uptime_ms: int


@dataclass(frozen=True)
class ReadyMessage:
    protocol: int
    firmware: str
    board: str


@dataclass(frozen=True)
class DeviceMessage:
    remote_name: str


@dataclass(frozen=True)
class BatteryMessage:
    percent: int


@dataclass(frozen=True)
class ErrorMessage:
    text: str


def parse_protocol_line(line: str) -> Optional[object]:
    """Decode one bridge line into a message, or ``None`` if it is not one.

    Returning ``None`` (rather than raising) is what lets the firmware print
    arbitrary human-readable diagnostics on the same stream: anything that is
    not a well-formed protocol line is simply not for us.
    """

    stripped = line.strip()
    if not stripped:
        return None
    tag, _, rest = stripped.partition(" ")
    rest = rest.strip()

    try:
        if tag == "KEY":
            fields = rest.split()
            if len(fields) != 4:
                return None
            state_token = fields[1].upper()
            if state_token not in ("D", "U"):
                return None
            return KeyMessage(
                usage=int(fields[0], 16),
                is_pressed=state_token == "D",
                sequence=int(fields[2], 10),
                uptime_ms=int(fields[3], 10),
            )
        if tag == "RDY":
            fields = rest.split()
            if len(fields) < 3:
                return None
            return ReadyMessage(
                protocol=int(fields[0], 10),
                firmware=fields[1],
                board=fields[2],
            )
        if tag == "DEV":
            return DeviceMessage(remote_name=rest) if rest else None
        if tag == "BAT":
            return BatteryMessage(percent=int(rest, 10))
        if tag == "ERR":
            return ErrorMessage(text=rest) if rest else None
    except (ValueError, IndexError):
        # A malformed line is a firmware bug, not a reason to drop the link.
        return None
    return None


class LineFramer:
    """Incremental ``\\n`` framing over arbitrary byte chunks.

    A serial read returns whatever happened to be buffered, so a line can be
    split across reads and several lines can arrive in one.  This buffers the
    remainder and hands back only complete lines - the same contract as the
    tap's socket reader in ``frida_compat``.
    """

    def __init__(self, *, max_line_bytes: int = 4096) -> None:
        self._buffer = b""
        self._max_line_bytes = max_line_bytes

    def feed(self, chunk: bytes) -> List[str]:
        if chunk:
            self._buffer += chunk
        lines: List[str] = []
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            lines.append(raw.decode("utf-8", errors="replace").rstrip("\r"))
        # A bridge that never terminates a line must not grow the buffer
        # without bound; drop the fragment and resynchronise on the next
        # newline.
        if len(self._buffer) > self._max_line_bytes:
            self._buffer = b""
        return lines

    def reset(self) -> None:
        self._buffer = b""


def button_id_for_usage(usage: int) -> Optional[str]:
    """Map a HID usage to the app's canonical button id.

    Only usages this client knows about are forwarded.  An unknown usage is
    dropped rather than guessed at - a wrong button id would fire a real user
    action.  Note this covers all three usages Windows discards, which is the
    entire reason this input source exists.
    """

    return device_profile.BUTTON_USAGE_IDS.get(usage)


def _port_tier(port) -> int:
    """How plausibly ``port`` is the bridge: 0 native, 1 bridge chip, 2 other."""

    vid = getattr(port, "vid", None)
    if vid in ESP32_NATIVE_VIDS:
        return 0
    if vid in ESP32_UART_BRIDGE_VIDS:
        return 1
    return 2


def iter_bridge_candidates(*, include_unknown: bool = False) -> List[str]:
    """Serial ports worth probing, most likely first.

    Tier order is the sort key; enumeration order breaks ties, so a host with
    two native ESP32 boards still resolves deterministically.
    """

    if serial is None:
        return []
    try:
        ports = list(serial.tools.list_ports.comports())
    except Exception:  # pragma: no cover - platform enumeration failure
        return []
    ranked = []
    for port in ports:
        tier = _port_tier(port)
        if tier == 2 and not include_unknown:
            continue
        ranked.append((tier, str(port.device)))
    ranked.sort(key=lambda item: item[0])
    return [device for _, device in ranked]


def _speaks_bridge_protocol(port: str, *, baud: int, timeout: float) -> bool:
    """Open ``port`` briefly and look for any line of the bridge protocol.

    Opening the port is usually enough on its own: the ESP32-C6's
    USB-Serial/JTAG peripheral reboots the chip when the host attaches, so the
    firmware's boot banner arrives within about a second.  A board that was
    already running has long since printed that banner, so if the port stays
    silent the firmware's own restart command is sent and we listen again.

    Any failure to open counts as "no" - a port another process is holding is
    not one this source could use anyway - so probing never raises.
    """

    connection = None
    try:
        connection = serial.Serial(
            port=port,
            baudrate=baud,
            timeout=0.1,
            write_timeout=0.5,
            rtscts=False,
            dsrdtr=False,
        )
        # Same reasoning as _open_port(): never drive the reset lines.
        try:
            connection.dtr = False
            connection.rts = False
        except Exception:
            pass
        try:
            connection.reset_input_buffer()
        except Exception:
            pass

        framer = LineFramer()
        started = time.monotonic()
        nudge_at = started + timeout * 0.4
        nudged = False

        while True:
            now = time.monotonic()
            if now - started >= timeout:
                return False
            if not nudged and now >= nudge_at:
                nudged = True
                try:
                    connection.write(BRIDGE_RESTART_COMMAND)
                except Exception:
                    pass
            chunk = connection.read(4096)
            for line in framer.feed(chunk or b""):
                if line.split(" ", 1)[0] in BRIDGE_PROTOCOL_TAGS:
                    return True
    except Exception:
        return False
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def find_bridge_port(
    *,
    probe: bool = True,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    include_unknown: bool = False,
    baud: int = DEFAULT_BAUD,
) -> Optional[str]:
    """Locate the bridge board's serial port, or ``None``.

    Candidates are ranked by USB ID (see the tiers above) and then, unless
    ``probe`` is off, confirmed by listening for the protocol.  Confirmation is
    what makes this work on a machine carrying more than one ESP32 board, where
    USB IDs alone are ambiguous.

    ``include_unknown`` widens the candidate set to every serial port, for a
    board behind an adapter this module does not recognise.  It is off by
    default because it makes the probe open ports belonging to unrelated
    devices.

    If no candidate answers the protocol the highest-ranked one is still
    returned, so the failure surfaces as "port opened but silent" - which
    names the port in the message - rather than the less actionable "no bridge
    found".

    A lone candidate is returned without being probed.  That is not a shortcut
    but an equivalence: the fallback above would return it whichever way the
    probe went, so opening it can only cost.  It is a cost worth naming -
    attaching to an ESP32-C6 reboots the chip, which drops the BLE link, so
    probing a port nobody was competing for would buy a second reconnect at
    every app start.
    """

    candidates = iter_bridge_candidates(include_unknown=include_unknown)
    if not candidates:
        return None
    if not probe or len(candidates) == 1:
        return candidates[0]
    for port in candidates:
        if _speaks_bridge_protocol(port, baud=baud, timeout=probe_timeout):
            return port
    return candidates[0]


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------


class Esp32SerialButtonListener:
    """Reads RC003 button edges from the ESP32 bridge over USB serial.

    Duck-compatible with ``raw_input_windows.RawInputButtonListener``: the same
    ``on_button_event(button_id, is_pressed)`` callback, ``start()``/``stop()``,
    an ``is_running`` property, and the two optional setters ``RC003App``
    probes for with ``getattr``.

    Lifecycle contract (mirrors ``RawInputButtonListener`` - ``RC003App``
    depends on it): ``stop()`` releases every held button, and if the reader
    thread will not join it raises *without* clearing its own reference, so the
    caller can retain the owner and retry rather than opening a second port
    over a still-live one.
    """

    def __init__(
        self,
        on_button_event: ButtonEventCallback,
        on_raw_event: Optional[Callable[[KeyMessage], None]] = None,
        physical_bindings: Optional[Dict[str, str]] = None,
        *,
        on_closed: Optional[Callable[[], None]] = None,
        port: str = "",
        baud: int = DEFAULT_BAUD,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
        probe_ports: bool = True,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
        scan_all_ports: bool = False,
        logger=None,
        _serial_factory: Optional[Callable[..., object]] = None,
        _port_finder: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self._on_button_event = on_button_event
        self._on_raw_event = on_raw_event
        # Invoked once if the reader loop ends for any reason other than a
        # caller-requested stop - an unplugged board would otherwise leave the
        # app reporting "connected" with every button dead.
        self._on_closed = on_closed
        # Accepted for interface parity with the Raw Input listener.  This
        # source identifies buttons by HID usage, so physical bindings have no
        # meaning here; storing them keeps the duck type honest.
        self._physical_bindings: Dict[str, str] = dict(physical_bindings or {})
        self._requested_port = port or ""
        self._baud = int(baud)
        self._start_timeout = float(start_timeout)
        self._ping_interval = float(ping_interval)
        self._stop_timeout = float(stop_timeout)
        self._probe_ports = bool(probe_ports)
        self._probe_timeout = float(probe_timeout)
        self._scan_all_ports = bool(scan_all_ports)
        # Resolved lazily: importing this module must stay side-effect free on
        # hosts without pyserial or without a writable log directory.
        self._logger = logger or logging_setup.get_logger(None)
        if _serial_factory is not None:
            self._serial_factory: Optional[Callable[..., object]] = _serial_factory
        elif serial is not None:
            self._serial_factory = serial.Serial
        else:
            self._serial_factory = None
        if _port_finder is not None:
            self._port_finder = _port_finder
        else:
            # Bound here rather than at the call site so the probe settings
            # travel with the instance and start() stays argument-free.
            self._port_finder = lambda: find_bridge_port(
                probe=self._probe_ports,
                probe_timeout=self._probe_timeout,
                include_unknown=self._scan_all_ports,
                baud=self._baud,
            )

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._connection: Optional[object] = None
        self._state_lock = threading.Lock()
        self._pressed_usages: set[int] = set()
        self._port: Optional[str] = None
        self._protocol_version: Optional[int] = None
        self._remote_name: str = ""
        self._last_battery: Optional[int] = None
        self._last_error: str = ""
        self._failure: Optional[str] = None

    # -- read-only introspection ------------------------------------------

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def port(self) -> Optional[str]:
        return self._port

    @property
    def protocol_version(self) -> Optional[int]:
        return self._protocol_version

    @property
    def remote_name(self) -> str:
        return self._remote_name

    @property
    def battery_percent(self) -> Optional[int]:
        return self._last_battery

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def failure(self) -> Optional[str]:
        return self._failure

    # -- interface parity with RawInputButtonListener ---------------------

    def set_physical_bindings(self, mapping) -> None:
        self._physical_bindings = dict(mapping or {})

    def set_raw_event_callback(self, callback) -> None:
        self._on_raw_event = callback

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Open the bridge port and begin reading.

        Raises ``Esp32SerialUnavailableError`` if the port cannot be resolved
        or opened.  Readiness is bounded by ``start_timeout``: a board that
        opens but never starts talking is reported through ``failure`` rather
        than blocking startup forever.
        """

        if self.is_running:
            return
        if self._serial_factory is None:
            raise Esp32SerialUnavailableError(
                "pyserial is not installed; the ESP32 bridge input source is unavailable"
            )

        port = self._requested_port or self._port_finder()
        if not port:
            raise Esp32SerialUnavailableError(
                "no ESP32 bridge port found; connect the board or name the port"
            )

        self._stop_event.clear()
        self._ready_event.clear()
        self._failure = None
        self._port = str(port)

        try:
            connection = self._open_port(str(port))
        except Exception as exc:
            self._port = None
            raise Esp32SerialUnavailableError(
                f"could not open the ESP32 bridge port: {exc}"
            ) from exc

        self._connection = connection
        thread = threading.Thread(
            target=self._run,
            name="rc003-esp32-serial",
            daemon=True,
        )
        self._thread = thread
        thread.start()

        if not self._ready_event.wait(self._start_timeout):
            # The port is open but the reader never came up.  Tear it down, but
            # only clear the thread reference if it actually died - otherwise
            # retain the owner exactly like the Raw Input listener does.
            try:
                connection.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            if not thread.is_alive():
                self._thread = None
                self._connection = None
                raise Esp32SerialUnavailableError(
                    "the ESP32 bridge reader thread did not start"
                )

    def stop(self) -> None:
        """Stop reading, release held buttons, and join the thread.

        Raises ``RuntimeError`` if the thread outlives ``stop_timeout``, with
        ``self._thread`` left in place so the caller retains the owner and can
        retry rather than leaking a live thread and a held COM port.
        """

        # Release before joining: a bridge that unplugged mid-press would
        # otherwise leave that button latched down in the gesture dispatcher.
        self._release_all()
        self._stop_event.set()

        connection = self._connection
        if connection is not None:
            # Closing the handle is what unblocks a pending read; the reader
            # also honours the stop event between reads.
            try:
                connection.close()  # type: ignore[attr-defined]
            except Exception:
                self._logger.info("esp32 bridge: closing the port during stop failed")

        thread = self._thread
        if thread is not None:
            thread.join(self._stop_timeout)
            if thread.is_alive():
                raise RuntimeError(
                    "ESP32 serial listener thread did not stop; owner retained"
                )
            self._thread = None
        self._connection = None

    # -- internals --------------------------------------------------------

    def _open_port(self, port: str):
        """Open the port, driving neither of the reset lines.

        Measured on the ESP32-C6: opening the port reboots the chip anyway -
        the USB-Serial/JTAG peripheral does that on host attach, and the
        firmware's banner comes back about a second later.  Leaving DTR/RTS
        deasserted does not prevent that, but it does stop us adding a second,
        explicit reset on top, and the board costs one BLE reconnect either
        way rather than two.

        This is the only open of the port during normal startup.  Port
        detection may open candidates earlier (see ``find_bridge_port``), which
        is why it declines to probe when there is nothing to choose between.
        """

        connection = self._serial_factory(
            port=port,
            baudrate=self._baud,
            timeout=0.2,
            write_timeout=0.5,
            rtscts=False,
            dsrdtr=False,
        )
        try:
            connection.dtr = False
            connection.rts = False
        except Exception:
            # Not every platform exposes these; a board that does not support
            # them is still usable, it may just re-enumerate once.
            pass
        try:
            connection.reset_input_buffer()
        except Exception:
            pass
        return connection

    def _run(self) -> None:
        framer = LineFramer()
        self._ready_event.set()
        last_ping = time.monotonic()

        try:
            while not self._stop_event.is_set():
                try:
                    chunk = self._connection.read(4096)  # type: ignore[attr-defined]
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    self._last_error = str(exc)
                    self._failure = f"serial read failed: {exc}"
                    self._logger.info("esp32 bridge: serial read failed: %s", exc)
                    break

                for line in framer.feed(chunk or b""):
                    self._handle_line(line)

                now = time.monotonic()
                if self._ping_interval > 0 and now - last_ping >= self._ping_interval:
                    last_ping = now
                    self._send_ping()
        finally:
            # Never leave a button latched if the loop exits for any reason,
            # including an unexpected exception.
            self._release_all()

            # Release the COM port *before* signalling closure.  ``on_closed``
            # is what asks the supervisor to reconnect, and the reconnect opens
            # this same port; leaving the handle to the garbage collector would
            # let the two overlap and hit "access denied" on Windows.
            #
            # This is a no-op on the ordinary stop path (stop() has already
            # closed it, and pyserial's close() is idempotent).
            connection = self._connection
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    self._logger.info(
                        "esp32 bridge: closing the port after the reader stopped failed"
                    )

            if not self._stop_event.is_set() and self._on_closed is not None:
                try:
                    self._on_closed()
                except Exception:
                    self._logger.exception("esp32 bridge closed callback failed")

    def _handle_line(self, line: str) -> None:
        message = parse_protocol_line(line)
        if message is None:
            return

        if isinstance(message, ReadyMessage):
            self._protocol_version = message.protocol
            self._logger.info("esp32 bridge: ready (firmware=%s)", message.firmware)
            if message.protocol != PROTOCOL_VERSION:
                self._logger.info(
                    "esp32 bridge: protocol %s differs from the supported %s; "
                    "unknown tags will be ignored",
                    message.protocol,
                    PROTOCOL_VERSION,
                )
            return

        if isinstance(message, DeviceMessage):
            self._remote_name = message.remote_name
            # The remote's display name only - never an address or device path.
            self._logger.info("esp32 bridge: remote linked")
            return

        if isinstance(message, BatteryMessage):
            self._last_battery = message.percent
            return

        if isinstance(message, ErrorMessage):
            self._last_error = message.text
            self._logger.info("esp32 bridge reported an error: %s", message.text)
            return

        if isinstance(message, KeyMessage):
            if self._on_raw_event is not None:
                try:
                    self._on_raw_event(message)
                except Exception:
                    self._logger.exception("esp32 bridge raw event callback failed")
            self._dispatch_key(message)

    def _dispatch_key(self, message: KeyMessage) -> None:
        button_id = button_id_for_usage(message.usage)
        if button_id is None:
            # An unknown usage is reported, never mapped - guessing could fire
            # the wrong user action.
            self._logger.info(
                "esp32 bridge: ignoring unrecognised usage 0x%04X", message.usage
            )
            return

        with self._state_lock:
            if message.is_pressed:
                if message.usage in self._pressed_usages:
                    return
                self._pressed_usages.add(message.usage)
            else:
                if message.usage not in self._pressed_usages:
                    return
                self._pressed_usages.discard(message.usage)

        try:
            self._on_button_event(button_id, message.is_pressed)
        except Exception:
            self._logger.exception("esp32 bridge button callback failed")

    def _release_all(self) -> None:
        """Emit a release for every usage currently held down."""

        with self._state_lock:
            held = sorted(self._pressed_usages)
            self._pressed_usages.clear()
        for usage in held:
            button_id = button_id_for_usage(usage)
            if button_id is None:
                continue
            try:
                self._on_button_event(button_id, False)
            except Exception:
                self._logger.exception("esp32 bridge stuck-button release failed")

    def _send_ping(self) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            connection.write(b"PING\n")  # type: ignore[attr-defined]
        except Exception as exc:
            # A failed keepalive is not fatal on its own; the read loop will
            # notice a genuinely dead port.
            self._last_error = str(exc)
