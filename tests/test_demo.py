import pytest

from scripts import demo


class FakeResponse:
    def __init__(self, status_code, text, payload=None):
        self.status_code = status_code
        self.text = text
        self.payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeN8n:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.paths = []

    def post(self, path):
        self.paths.append(path)
        return next(self.responses)


def test_demo_waits_for_workflow_import_to_register_webhook(monkeypatch):
    n8n = FakeN8n([
        FakeResponse(404, 'The requested webhook "orders/process" is not registered'),
        FakeResponse(200, "", {"ok": True}),
    ])
    sleeps = []
    clock = iter([0, 1])
    monkeypatch.setattr(demo.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(demo.time, "sleep", sleeps.append)

    assert demo._trigger_workflow(n8n, "orders/process") == {"ok": True}
    assert n8n.paths == [
        "/webhook/commerceops/orders/process",
        "/webhook/commerceops/orders/process",
    ]
    assert sleeps == [2]


def test_demo_does_not_retry_unrelated_webhook_errors():
    n8n = FakeN8n([FakeResponse(404, "workflow not found")])

    with pytest.raises(RuntimeError, match="HTTP 404"):
        demo._trigger_workflow(n8n, "orders/process")

    assert len(n8n.paths) == 1
