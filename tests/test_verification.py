import sys
from types import SimpleNamespace

from scripts import verify


def test_verifier_reports_missing_docker_without_skipping(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["verify", "postgres"])
    monkeypatch.setattr(verify.shutil, "which", lambda _: None)
    assert verify.run() == 2
    assert "UNAVAILABLE: Docker is not installed" in capsys.readouterr().err


def test_verifier_reports_missing_compose_service_as_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["verify", "postgres"])
    monkeypatch.setattr(verify.shutil, "which", lambda _: "/usr/bin/docker")
    calls = []
    def docker(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="api\n")
    monkeypatch.setattr(verify.subprocess, "run", docker)
    assert verify.run() == 2
    assert "services are not running: db" in capsys.readouterr().err
    assert len(calls) == 2


def test_verifier_stops_after_a_failing_check(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sys, "argv", ["verify", "local"])
    def failed(command):
        calls.append(command)
        return SimpleNamespace(returncode=3)
    monkeypatch.setattr(verify.subprocess, "run", failed)
    assert verify.run() == 3
    assert len(calls) == 1
    assert "later checks were not run" in capsys.readouterr().err


def test_preview_interrupt_is_reported_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["verify", "preview"])
    def interrupted(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(verify.subprocess, "run", interrupted)
    assert verify.run() == 130
    assert "Stopped by operator" in capsys.readouterr().err
