from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ovb_rc003 import statistics


def test_records_button_and_voice_without_content_or_device_identity(tmp_path):
    store = statistics.StatisticsStore(tmp_path)
    started = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)

    store.record_button("ok", started)
    store.voice_started(started)
    store.voice_stopped(started + timedelta(seconds=12.5))

    snapshot = store.snapshot("day", started + timedelta(minutes=1))
    assert snapshot["button_total"] == 1
    assert snapshot["by_button"] == {"ok": 1}
    assert snapshot["voice_sessions"] == 1
    assert snapshot["voice_total_seconds"] == 12.5
    assert snapshot["longest_sessions"][0]["seconds"] == 12.5
    assert snapshot["longest_sessions"][0]["started_at"] == started.isoformat()
    assert len(snapshot["daily_activity"]) == 7
    assert snapshot["daily_activity"][-1]["button_count"] == 1
    assert snapshot["daily_activity"][-1]["voice_seconds"] == 12.5

    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    serialized = json.dumps(persisted)
    assert "audio" not in serialized
    assert "address" not in serialized
    assert "device_path" not in serialized


def test_period_filters_and_longest_order(tmp_path):
    store = statistics.StatisticsStore(tmp_path)
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    store.record_button("up", now - timedelta(days=3))
    store.record_button("down", now - timedelta(hours=2))
    for duration in (3.0, 15.0, 7.0):
        store.voice_started(now - timedelta(minutes=duration))
        store.voice_stopped(now - timedelta(minutes=duration) + timedelta(seconds=duration))

    day = store.snapshot("day", now)
    week = store.snapshot("week", now)
    assert day["button_total"] == 1
    assert week["button_total"] == 2
    assert day["longest_seconds"] == [15.0, 7.0, 3.0]
    assert day["longest_sessions"][0]["seconds"] == 15.0
    assert day["longest_sessions"][0]["started_at"]


def test_rust_journal_counts_presses_and_closed_voice(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    started = int(datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc).timestamp() * 1000)
    stopped = started + 2500
    (log_dir / "events-1.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "physical_button", "edge": "down", "button": "ok", "timestamp_ms": started}),
            json.dumps({"type": "physical_button", "edge": "up", "button": "ok", "timestamp_ms": started + 40}),
            json.dumps({"type": "voice_shortcut", "state": "held", "timestamp_ms": started}),
            json.dumps({"type": "voice_shortcut", "state": "released", "timestamp_ms": stopped}),
            "not json",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(statistics, "_rust_usage_cache", {"key": None, "events": [], "sessions": []})
    events, sessions = statistics.read_rust_usage(log_dir)
    assert len(events) == 1
    assert events[0]["button_id"] == "ok"
    assert sessions[0]["duration_seconds"] == 2.5

    store = statistics.StatisticsStore(tmp_path)
    snapshot = store.snapshot(
        "day",
        datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc),
        extra_events=events,
        extra_sessions=sessions,
    )
    assert snapshot["button_total"] == 1
    assert snapshot["voice_sessions"] == 1
    assert snapshot["daily_activity"][-1]["button_count"] == 1


def test_corrupt_file_recovers_as_empty_store(tmp_path):
    path = statistics.stats_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json", encoding="utf-8")

    store = statistics.StatisticsStore(tmp_path)
    assert store.snapshot("all")["button_total"] == 0


def test_event_and_session_history_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(statistics, "MAX_EVENTS", 3)
    store = statistics.StatisticsStore(tmp_path)
    base = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    for index in range(5):
        at = base + timedelta(seconds=index)
        store.record_button("ok", at)
        store.voice_started(at)
        store.voice_stopped(at + timedelta(seconds=index + 1))

    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert len(persisted["events"]) == 3
    assert len(persisted["voice_sessions"]) == 3
