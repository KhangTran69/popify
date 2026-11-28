import json
from scripts.check_operations import alert_transitions, update_state, main


def snapshot(*codes, status="attention"):
    return {"status": status, "alerts": [{"code": code, "severity": "warning", "message": "Synthetic", "action": "Inspect"} for code in codes]}


def test_deduplicates_and_reminds_and_resolves():
    state, events = alert_transitions({}, snapshot("orders_overdue"), 100, 60)
    assert events[0]["transition"] == "opened"
    state, events = alert_transitions(state, snapshot("orders_overdue"), 130, 60)
    assert not events
    state, events = alert_transitions(state, snapshot("orders_overdue"), 160, 60)
    assert events[0]["transition"] == "reminder"
    state, events = alert_transitions(state, snapshot(status="healthy"), 180, 60)
    assert not state and events == [{"code": "orders_overdue", "transition": "resolved"}]


def test_unavailable_does_not_falsely_resolve_business_alerts():
    state, events = alert_transitions({"orders_overdue": 100}, snapshot("database_unavailable", status="unavailable"), 120, 60)
    assert state == {"orders_overdue": 100, "database_unavailable": 120}
    assert len(events) == 1 and events[0]["code"] == "database_unavailable"
    state, events = alert_transitions(state, snapshot("orders_overdue"), 130, 60)
    assert events == [{"code": "database_unavailable", "transition": "resolved"}]


def test_private_atomic_state_file(tmp_path):
    path = tmp_path / "state.json"
    events = update_state(path, snapshot("orders_overdue"), 900)
    assert events[0]["transition"] == "opened"
    assert path.stat().st_mode & 0o777 == 0o600
    assert set(json.loads(path.read_text())) == {"orders_overdue"}
    assert update_state(path, snapshot("orders_overdue"), 900) == []


def test_unavailable_exit_does_not_leak_exception(monkeypatch, capsys):
    from scripts import check_operations
    def unavailable():
        raise RuntimeError("private connection details")
    monkeypatch.setattr(check_operations, "SessionLocal", unavailable)
    assert main([]) == 2
    result = capsys.readouterr().out
    assert "private connection details" not in result
    assert json.loads(result)["alerts"][0]["code"] == "database_unavailable"


def test_durable_outbox_replays_until_acknowledged(tmp_path):
    from scripts.check_operations import update_outbox
    path = tmp_path / "outbox.json"
    first = update_outbox(path, snapshot("orders_overdue"))
    assert first[0]["transition"] == "opened"
    assert update_outbox(path, snapshot("orders_overdue")) == first
    resolved = update_outbox(path, snapshot(status="healthy"))
    assert [e["transition"] for e in resolved] == ["opened", "resolved"]
    assert resolved[0]["event_id"] != resolved[1]["event_id"]
    assert update_outbox(path, acknowledge=first[0]["event_id"]) == resolved[1:]
    assert update_outbox(path, acknowledge=first[0]["event_id"]) == resolved[1:]
    assert update_outbox(path, acknowledge=resolved[1]["event_id"]) == []
    assert path.stat().st_mode & 0o777 == 0o600


def test_outbox_full_does_not_discard_unacknowledged_events(tmp_path, monkeypatch):
    import pytest
    from scripts import check_operations as monitor
    monkeypatch.setattr(monitor, "MAX_PENDING_EVENTS", 1)
    path = tmp_path / "outbox.json"
    first = monitor.update_outbox(path, snapshot("orders_overdue"))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="full"):
        monitor.update_outbox(path, snapshot(status="healthy"))
    assert path.read_bytes() == before
    monitor.update_outbox(path, acknowledge=first[0]["event_id"])
    resolved = monitor.update_outbox(path, snapshot(status="healthy"))
    assert resolved[0]["transition"] == "resolved"


def test_ack_command_is_independent_of_database(tmp_path, monkeypatch, capsys):
    from scripts import check_operations as monitor
    path = tmp_path / "outbox.json"
    event = monitor.update_outbox(path, snapshot("orders_overdue"))[0]
    def unavailable():
        raise AssertionError("ack must not contact database")
    monkeypatch.setattr(monitor, "SessionLocal", unavailable)
    assert monitor.main(["--outbox-file", str(path), "--ack-event", event["event_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["pending_count"] == 0


def test_healthy_snapshot_with_pending_delivery_is_not_clear(tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext
    from scripts import check_operations as monitor
    path = tmp_path / "outbox.json"
    monitor.update_outbox(path, snapshot("orders_overdue"))
    monkeypatch.setattr(monitor, "SessionLocal", lambda: nullcontext(None))
    monkeypatch.setattr(monitor, "operational_snapshot", lambda db: snapshot(status="healthy"))
    assert monitor.main(["--outbox-file", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "healthy"
    assert result["handoff"]["pending_count"] == 2
    assert result["handoff"]["status"] == "awaiting_acknowledgement"
    for event in result["pending_events"]:
        monitor.update_outbox(path, acknowledge=event["event_id"])
    assert monitor.main(["--outbox-file", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["handoff"]["status"] == "clear"



def test_relay_can_drain_full_outbox_without_database(tmp_path, monkeypatch, capsys):
    from scripts import check_operations as monitor
    from contextlib import nullcontext
    monkeypatch.setattr(monitor, "MAX_PENDING_EVENTS", 1)
    path = tmp_path / "outbox.json"
    first = monitor.update_outbox(path, snapshot("orders_overdue"))
    before = path.read_bytes()
    monkeypatch.setattr(monitor, "SessionLocal", lambda: nullcontext(None))
    monkeypatch.setattr(monitor, "operational_snapshot", lambda db: snapshot(status="healthy"))
    assert monitor.main(["--outbox-file", str(path)]) == 2
    assert "state_error" in json.loads(capsys.readouterr().out)
    assert path.read_bytes() == before

    def unavailable():
        raise AssertionError("listing must not contact database")
    monkeypatch.setattr(monitor, "SessionLocal", unavailable)
    assert monitor.main(["--outbox-file", str(path), "--list-pending"]) == 1
    listed = json.loads(capsys.readouterr().out)
    assert listed["pending_events"] == first and listed["pending_count"] == 1
    assert path.read_bytes() == before
    assert monitor.main(["--outbox-file", str(path), "--ack-event", first[0]["event_id"]]) == 0
    capsys.readouterr()
    assert monitor.main(["--outbox-file", str(path), "--list-pending"]) == 0
    assert json.loads(capsys.readouterr().out)["pending_events"] == []
    # Once capacity returns, the original resolution is still emitted.
    resolved = monitor.update_outbox(path, snapshot(status="healthy"))
    assert len(resolved) == 1 and resolved[0]["transition"] == "resolved"


def test_missing_or_invalid_relay_state(tmp_path, capsys):
    from scripts import check_operations as monitor
    path = tmp_path / "outbox.json"
    assert monitor.main(["--outbox-file", str(path), "--list-pending"]) == 0
    assert not path.exists()
    capsys.readouterr()
    path.write_text("{broken")
    before = path.read_bytes()
    assert monitor.main(["--outbox-file", str(path), "--list-pending"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"
    assert path.read_bytes() == before


def test_overdue_alert_delivery_escalates_even_when_business_is_healthy(tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext
    from scripts import check_operations as monitor
    path = tmp_path / "outbox.json"
    monkeypatch.setattr(monitor.time, "time", lambda: 1000)
    original = monitor.update_outbox(path, snapshot("orders_overdue"))
    monkeypatch.setattr(monitor.time, "time", lambda: 1061)
    monkeypatch.setattr(monitor, "SessionLocal", lambda: nullcontext(None))
    monkeypatch.setattr(monitor, "operational_snapshot", lambda db: snapshot(status="healthy"))
    args = ["--outbox-file", str(path), "--max-unacknowledged-seconds", "60"]
    assert monitor.main(args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "healthy" and result["handoff"]["status"] == "overdue"
    assert result["handoff"]["oldest_pending_seconds"] == 61
    assert result["pending_events"][0] == original[0]
    assert monitor.main(args + ["--list-pending"]) == 1
    assert json.loads(capsys.readouterr().out)["handoff"]["status"] == "overdue"
    for event in result["pending_events"]:
        monitor.update_outbox(path, acknowledge=event["event_id"])
    assert monitor.main(args) == 0
    assert json.loads(capsys.readouterr().out)["handoff"]["status"] == "clear"


def test_monitor_rejects_insecure_state_directory_and_files(tmp_path):
    import os
    import pytest
    from scripts.check_operations import update_outbox
    directory = tmp_path / "state"
    directory.mkdir(mode=0o755)
    path = directory / "outbox.json"
    with pytest.raises(ValueError, match="owner-only state directory"):
        update_outbox(path, snapshot("orders_overdue"))
    assert not path.exists()
    directory.chmod(0o700)
    first = update_outbox(path, snapshot("orders_overdue"))
    before = path.read_bytes()
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only regular state file"):
        update_outbox(path)
    assert path.read_bytes() == before
    path.chmod(0o600)
    lock = path.with_name(path.name + ".lock")
    lock.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only regular state file"):
        update_outbox(path)
    lock.unlink()
    os.mkfifo(lock, 0o600)
    with pytest.raises(ValueError, match="owner-only regular state file"):
        update_outbox(path)
    lock.unlink()
    assert update_outbox(path) == first


def test_oversized_state_write_preserves_readable_outbox(tmp_path):
    import pytest
    from scripts.check_operations import update_outbox
    path = tmp_path / "outbox.json"
    first = update_outbox(path, snapshot("orders_overdue"))
    before = path.read_bytes()
    large = snapshot("support_overdue")
    large["alerts"][0]["message"] = "x" * 512_000
    with pytest.raises(ValueError, match="durable storage bound"):
        update_outbox(path, large)
    assert path.read_bytes() == before
    assert update_outbox(path) == first
