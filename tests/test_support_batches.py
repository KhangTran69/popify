from datetime import timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import main
from app.config import settings
from app.db import Base, get_db
from app.models import Case, Approval, utcnow


def test_support_batch_legacy_and_future_retry(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    def override():
        with sessions() as db:
            yield db
    monkeypatch.setitem(main.app.dependency_overrides, get_db, override)
    monkeypatch.setattr(settings, "preview_mode", False)
    monkeypatch.setattr(settings, "llm_mode", "mock")
    with sessions() as db:
        for i in range(4):
            db.add(Case(message="Synthetic inquiry", request_key=f"batch-{i}", status="queued"))
        db.add(Case(message="future", request_key="future", status="failed", attempts=1,
                    next_attempt_at=utcnow()+timedelta(hours=1)))
        db.commit()
    client = TestClient(main.app)
    url = "/api/n8n/triage-next"
    headers = {"X-N8N-Secret": settings.n8n_webhook_secret}
    assert client.post(url+"?batch_size=3").status_code == 401
    assert client.post(url+"?batch_size=6", headers=headers).status_code == 422
    assert client.post(url+"?batch_size=3", headers=headers).json()["outcomes"]["needs_review"] == 3
    response = client.post(url, headers=headers).json()
    assert response["status"] == "needs_review" and "outcomes" not in response
    assert client.post(url+"?batch_size=3", headers=headers).json()["status"] == "idle"
    with sessions() as db:
        assert len(list(db.scalars(select(Approval)))) == 4
        future = db.scalar(select(Case).where(Case.request_key == "future"))
        assert future.status == "failed" and future.attempts == 1
    engine.dispose()


def test_extra_support_poll_is_mock_only(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    def override():
        with sessions() as db:
            yield db
    monkeypatch.setitem(main.app.dependency_overrides, get_db, override)
    monkeypatch.setattr(settings, "preview_mode", False)
    monkeypatch.setattr(settings, "llm_mode", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "")
    with sessions() as db:
        db.add(Case(message="Synthetic inquiry", request_key="extra-poll", status="queued"))
        db.commit()
    client = TestClient(main.app)
    url = "/api/n8n/triage-next?batch_size=5&mock_only=true"
    headers = {"X-N8N-Secret": settings.n8n_webhook_secret}
    assert client.post(url).status_code == 401
    assert client.post(url, headers=headers).json()["status"] == "disabled"
    with sessions() as db:
        case = db.scalar(select(Case).where(Case.request_key == "extra-poll"))
        assert case.status == "queued" and case.attempts == 0
        assert not list(db.scalars(select(Approval)))
    monkeypatch.setattr(settings, "llm_mode", "mock")
    assert client.post(url, headers=headers).json()["outcomes"]["needs_review"] == 1
    with sessions() as db:
        case = db.scalar(select(Case).where(Case.request_key == "extra-poll"))
        assert case.status == "needs_review" and case.attempts == 1
        assert len(list(db.scalars(select(Approval)))) == 1
    engine.dispose()
