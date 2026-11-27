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
