"""Local usage statistics for the Remo Windows client.

The file is intentionally small, local-only, and bounded. It stores no
Bluetooth identity, device path, audio payload, or recognized speech. Only
button ids and voice-session timing are retained for the statistics page.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config

STATS_FILENAME = "statistics.json"
SCHEMA_VERSION = 1
MAX_EVENTS = 5000


def stats_path(root: Optional[Path] = None) -> Path:
    return (root or config.config_root()) / STATS_FILENAME


def _empty() -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "events": [], "voice_sessions": []}


def _load(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return _empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    sessions = raw.get("voice_sessions") if isinstance(raw.get("voice_sessions"), list) else []
    return {
        "schema_version": SCHEMA_VERSION,
        "events": [e for e in events if isinstance(e, dict)][-MAX_EVENTS:],
        "voice_sessions": [s for s in sessions if isinstance(s, dict)][-MAX_EVENTS:],
    }


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


class StatisticsStore:
    """Thread-safe append-only usage store with period summaries."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._path = stats_path(root)
        self._lock = threading.RLock()
        self._data = _load(self._path)
        self._voice_started_at: Optional[datetime] = None

    @property
    def path(self) -> Path:
        return self._path

    def _commit(self) -> None:
        self._data["events"] = self._data["events"][-MAX_EVENTS:]
        self._data["voice_sessions"] = self._data["voice_sessions"][-MAX_EVENTS:]
        try:
            _save(self._path, self._data)
        except OSError:
            # Statistics must never break the audio/BLE path if the profile is
            # read-only or the disk is full.
            return

    def record_button(self, button_id: str, at: Optional[datetime] = None) -> None:
        if not button_id:
            return
        now = at or datetime.now().astimezone()
        with self._lock:
            self._data["events"].append({
                "kind": "button",
                "button_id": str(button_id),
                "at": now.isoformat(),
            })
            self._commit()

    def voice_started(self, at: Optional[datetime] = None) -> None:
        with self._lock:
            self._voice_started_at = at or datetime.now().astimezone()

    def voice_stopped(self, at: Optional[datetime] = None) -> None:
        with self._lock:
            if self._voice_started_at is None:
                return
            ended = at or datetime.now().astimezone()
            duration = max(0.0, (ended - self._voice_started_at).total_seconds())
            self._data["voice_sessions"].append({
                "started_at": self._voice_started_at.isoformat(),
                "ended_at": ended.isoformat(),
                "duration_seconds": round(duration, 3),
            })
            self._voice_started_at = None
            self._commit()

    def snapshot(
        self,
        period: str = "day",
        now: Optional[datetime] = None,
        *,
        extra_events: Optional[List[dict]] = None,
        extra_sessions: Optional[List[dict]] = None,
    ) -> Dict[str, Any]:
        current = now or datetime.now().astimezone()
        days = {"day": 1, "week": 7, "all": 36500}.get(period, 1)
        cutoff = current - timedelta(days=days)
        with self._lock:
            stored_events = list(self._data["events"])
            stored_sessions = list(self._data["voice_sessions"])
            seen_events = {
                (str(event.get("button_id", "")), str(event.get("at", "")))
                for event in stored_events
            }
            seen_sessions = {str(session.get("started_at", "")) for session in stored_sessions}
            all_events = stored_events + [
                event for event in (extra_events or [])
                if (str(event.get("button_id", "")), str(event.get("at", ""))) not in seen_events
            ]
            all_sessions = stored_sessions + [
                session for session in (extra_sessions or [])
                if str(session.get("started_at", "")) not in seen_sessions
            ]
            events = [e for e in all_events if _parse_at(e.get("at")) >= cutoff]
            sessions = [s for s in all_sessions if _parse_at(s.get("started_at")) >= cutoff]
            by_button: Dict[str, int] = {}
            for event in events:
                button = str(event.get("button_id", ""))
                if button:
                    by_button[button] = by_button.get(button, 0) + 1
            # Keep the complete session records for the UI ranking instead of
            # reducing them to bare durations.  ``longest_seconds`` remains a
            # compatibility field for older callers and saved profiles.
            session_rows = sorted(
                (
                    {
                        "seconds": max(0.0, float(s.get("duration_seconds", 0.0))),
                        "started_at": str(s.get("started_at", "")),
                    }
                    for s in sessions
                ),
                key=lambda row: (-row["seconds"], row["started_at"]),
            )
            durations = [row["seconds"] for row in session_rows]
            bucket_days = [
                current.date() - timedelta(days=offset)
                for offset in range(6, -1, -1)
            ]
            # The chart is always the last 7 days, even when the headline
            # totals are filtered to today.  Older presses would otherwise
            # disappear from the only usage view on the connection page.
            button_by_day = {day: 0 for day in bucket_days}
            voice_by_day = {day: 0.0 for day in bucket_days}
            for event in all_events:
                event_day = _parse_at(event.get("at")).astimezone().date()
                if event_day in button_by_day:
                    button_by_day[event_day] += 1
            for session in all_sessions:
                session_day = _parse_at(session.get("started_at")).astimezone().date()
                if session_day in voice_by_day:
                    voice_by_day[session_day] += max(
                        0.0, float(session.get("duration_seconds", 0.0))
                    )
            return {
                "period": period,
                "button_total": len(events),
                "voice_total_seconds": round(sum(durations), 3),
                "by_button": by_button,
                "voice_sessions": len(sessions),
                "longest_seconds": durations[:5],
                "longest_sessions": session_rows[:5],
                "daily_activity": [
                    {
                        "label": day.strftime("%m/%d"),
                        "button_count": button_by_day[day],
                        "voice_seconds": round(voice_by_day[day], 3),
                    }
                    for day in bucket_days
                ],
            }


def rust_log_directory() -> Path:
    """Directory where the Rust daemon appends ``events-*.jsonl``."""

    override = os.environ.get("REMO_LOG_DIR")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path(tempfile.gettempdir())
    return root / "Remo" / "rust" / "logs"


_rust_usage_cache: Dict[str, Any] = {"key": None, "events": [], "sessions": []}


def read_rust_usage(log_dir: Optional[Path] = None) -> Tuple[List[dict], List[dict]]:
    """Button presses and closed voice sessions from the Rust daemon journal.

    The Python bridge is not the process that receives RC003 keys. The daemon
    writes one JSON object per line and never stores audio or device identity.
    Only ``physical_button`` downs and completed voice holds are counted.
    Voice audio events from the daemon do not carry a timestamp, so a hold
    is taken from ``voice_shortcut`` ``held`` / ``released`` pairs.
    """

    directory = log_dir or rust_log_directory()
    if not directory.is_dir():
        return [], []
    files = sorted(path for path in directory.glob("events-*.jsonl") if path.is_file())
    try:
        key = (str(directory), tuple(
            (path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in files
        ))
    except OSError:
        return [], []
    if key == _rust_usage_cache.get("key"):
        return list(_rust_usage_cache["events"]), list(_rust_usage_cache["sessions"])

    events: List[dict] = []
    sessions: List[dict] = []
    voice_started_at: Optional[datetime] = None
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            at = _from_unix_ms(payload.get("timestamp_ms"))
            if at is None:
                continue
            if kind == "physical_button" and payload.get("edge") == "down":
                button = str(payload.get("button") or "")
                if button:
                    events.append({
                        "kind": "button",
                        "button_id": button,
                        "at": at.isoformat(),
                    })
            elif kind == "voice_shortcut" and payload.get("state") == "held":
                voice_started_at = at
            elif kind == "voice_shortcut" and payload.get("state") == "released" and voice_started_at is not None:
                duration = max(0.0, (at - voice_started_at).total_seconds())
                sessions.append({
                    "started_at": voice_started_at.isoformat(),
                    "ended_at": at.isoformat(),
                    "duration_seconds": round(duration, 3),
                })
                voice_started_at = None
    events = events[-MAX_EVENTS:]
    sessions = sessions[-MAX_EVENTS:]
    _rust_usage_cache["key"] = key
    _rust_usage_cache["events"] = events
    _rust_usage_cache["sessions"] = sessions
    return list(events), list(sessions)


def _from_unix_ms(value: Any) -> Optional[datetime]:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc).astimezone()


def _parse_at(value: Any) -> datetime:
    if not isinstance(value, str):
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
