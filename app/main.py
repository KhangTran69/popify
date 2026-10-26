import json, logging, secrets, uuid, time
from math import ceil
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from datetime import timedelta
from fastapi import FastAPI, Request, Depends, HTTPException, Form, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, func, or_, cast, String, exists
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field
import httpx
from sqlalchemy.orm import Session, selectinload
from app.db import Base, engine, get_db
from app.config import settings
from app.models import Order, Delivery, Case, Approval, Audit, ActionExecution, utcnow
from app.presentation import record_navigation, retry_guidance, snapshot_utc
from app.recovery import RECOVERY_CATEGORIES, recovery_counts, recovery_page
from app.preview_scenarios import SCENARIOS, create_preview_scenario
from app.services import (
    verify_shopify,
    process_delivery,
    decide_approval,
    refresh_approval,
    approval_freshness_differences,
    ACTION_CONTRACTS,
    ApprovalEvidenceStale,
    queue_support,
    process_support,
    execute_approved,
    audit,
    as_utc,
    parse_dt,
    SupportRequestKeyConflict,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("commerceops")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["utc"] = lambda value: (
    as_utc(value).strftime("%d %b %Y · %H:%M UTC") if value else "Not recorded"
)
templates.env.filters["snapshot_utc"] = snapshot_utc
templates.env.globals["preview_mode"] = settings.preview_mode


@asynccontextmanager
async def lifespan(app):
    with engine.connect() as connection:
        connection.execute(select(1))
    yield


app = FastAPI(title="CommerceOps", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware, secret_key=settings.app_secret, same_site="lax", https_only=False
)


@app.middleware("http")
async def block_live_integrations_in_preview(request: Request, call_next):
    if settings.preview_mode and (
        request.url.path.startswith("/api/n8n/")
        or request.url.path.startswith("/webhooks/")
    ):
        return JSONResponse(
            {"detail": "Live integrations are disabled in the isolated preview."},
            status_code=409,
        )
    return await call_next(request)


def operator(request: Request):
    if not request.session.get("operator"):
        raise HTTPException(401, "Sign in to access the operator console")
    return request.session["operator"]


def csrf(request):
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def verify_csrf(request, token):
    expected = request.session.get("csrf", "")
    if not expected or not secrets.compare_digest(expected, token):
        raise HTTPException(403, "invalid CSRF token")


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(select(1))
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {
        "status": "ok",
        "mode": "isolated_preview" if settings.preview_mode else "mock_actions",
        "llm": (
            "openai"
            if settings.llm_mode == "openai" and settings.openai_api_key
            else "mock"
        ),
    }


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request, "login.html", {"request": request, "csrf": csrf(request)}
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    if not secrets.compare_digest(
        username, settings.operator_username
    ) or not secrets.compare_digest(password, settings.operator_password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "csrf": csrf(request),
                "error": "The username or password is incorrect.",
            },
            status_code=401,
        )
    request.session.clear()
    request.session["operator"] = username
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...), actor=Depends(operator)):
    verify_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def workflow_activity(db):
    result = []
    for number, label in [
        (1, "Order intake"),
        (2, "Support triage"),
        (3, "Exceptions & actions"),
    ]:
        last = db.scalar(
            select(Audit)
            .where(
                Audit.entity_type == "workflow",
                Audit.entity_id == number,
                Audit.event.in_(["work_completed", "work_failed", "poll_completed"]),
            )
            .order_by(Audit.id.desc())
            .limit(1)
        )
        success = db.scalar(
            select(Audit)
            .where(
                Audit.entity_type == "workflow",
                Audit.entity_id == number,
                Audit.event == "work_completed",
            )
            .order_by(Audit.id.desc())
            .limit(1)
        )
        failure = db.scalar(
            select(Audit)
            .where(
                Audit.entity_type == "workflow",
                Audit.entity_id == number,
                Audit.event == "work_failed",
            )
            .order_by(Audit.id.desc())
            .limit(1)
        )
        recent = bool(
            last and (utcnow() - as_utc(last.created_at)).total_seconds() < 180
        )
        result.append(
            {
                "number": number,
                "label": label,
                "last": last,
                "success": success,
                "failure": failure,
                "recent": recent,
            }
        )
    return result


def heartbeat(db, number, result, completed=False, failed=False):
    audit(
        db,
        "workflow",
        number,
        "work_failed" if failed else "work_completed" if completed else "poll_completed",
        details=result,
    )
    db.commit()
    return result


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, actor=Depends(operator), db: Session = Depends(get_db)):
    count = (
        lambda model, *conditions: db.scalar(
            select(func.count()).select_from(model).where(*conditions)
        )
        or 0
    )
    now = utcnow()
    recovery = recovery_counts(db, now)
    counts = {
        "orders": count(Order),
        "duplicates": count(Audit, Audit.event == "duplicate_delivery_ignored"),
        "exceptions": count(
            Approval, Approval.kind == "exception", Approval.status == "pending"
        ),
        "approvals": count(Approval, Approval.status == "pending"),
        "success": count(ActionExecution, ActionExecution.state == "succeeded"),
        "failures": recovery["total"] - recovery["pending"],
        "queued": count(Delivery, Delivery.status == "received")
        + count(Case, Case.status == "queued")
        + count(Approval, Approval.execution_status == "queued"),
    }
    counts["due_retries"] = (
        count(
            Delivery,
            Delivery.status.in_(["failed", "received"]),
            Delivery.attempts > 0,
            Delivery.next_attempt_at <= now,
        )
        + count(
            Case,
            Case.status.in_(["failed", "queued"]),
            Case.attempts > 0,
            Case.next_attempt_at <= now,
        )
        + count(
            Approval,
            Approval.execution_status.in_(["retryable", "queued"]),
            Approval.attempts > 0,
            Approval.next_attempt_at <= now,
        )
    )
    counts["exhausted"] = (
        count(Delivery, Delivery.status == "exhausted")
        + count(Case, Case.status == "exhausted")
        + count(Approval, Approval.execution_status == "exhausted")
    )
    counts["exhausted_actions"] = count(
        Approval, Approval.replacement_approval_id.is_(None),
        Approval.execution_status == "exhausted",
    )
    counts["needs_operator"] = (
        count(
            Approval,
            Approval.replacement_approval_id.is_(None),
            Approval.execution_status.in_(
                ["exhausted", "ambiguous", "evidence_changed", "invalid_action"]
            ),
        )
        + count(Delivery, Delivery.status == "exhausted")
        + count(Case, Case.status == "exhausted")
    )
    counts["approval_recovery"] = count(
        Approval,
        Approval.replacement_approval_id.is_(None),
        Approval.execution_status.in_(
            ["failed", "retryable", "exhausted", "ambiguous", "evidence_changed", "invalid_action"]
        ),
    )
    counts["case_recovery"] = count(Case, Case.status.in_(["failed", "exhausted"]))
    counts["delivery_recovery"] = count(
        Delivery, Delivery.status.in_(["failed", "exhausted"])
    )
    counts["pending_decisions"] = recovery["pending"]
    counts["scheduled_actions"] = recovery["waiting"]
    counts["eligible_actions"] = recovery["eligible"]
    counts["stale_actions"] = recovery["stale"]
    counts["uncertain_actions"] = recovery["uncertain"]
    counts["recovery"] = recovery
    oldest = min(
        (
            as_utc(value)
            for value in (
                db.scalar(select(func.min(Delivery.received_at)).where(Delivery.status == "received")),
                db.scalar(select(func.min(Case.created_at)).where(Case.status == "queued")),
            )
            if value is not None
        ),
        default=None,
    )
    counts["oldest_queue_minutes"] = max(0, int((now - oldest).total_seconds() // 60)) if oldest else None
    done = db.execute(
        select(Delivery.received_at, Delivery.processed_at)
        .where(Delivery.processed_at.is_not(None))
        .order_by(Delivery.id.desc())
        .limit(500)
    ).all()
    counts["avg_seconds"] = (
        round(
            sum((as_utc(end) - as_utc(start)).total_seconds() for start, end in done)
            / len(done),
            2,
        )
        if done
        else None
    )
    counts["sample"] = len(done)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "actor": actor,
            "csrf": csrf(request),
            "counts": counts,
            "orders": db.scalars(
                select(Order).order_by(Order.id.desc()).limit(5)
            ).all(),
            "approvals": db.scalars(
                select(Approval)
                .where(Approval.status == "pending")
                .order_by(Approval.created_at)
                .limit(5)
            ).all(),
            "activity": workflow_activity(db),
            "title": "Overview",
        },
    )


@app.get("/recovery", response_class=HTMLResponse)
def recovery_workspace(
    request: Request,
    category: str = Query("", max_length=30),
    q: str = Query("", max_length=150),
    page: int = Query(1, ge=1),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    if category and category not in RECOVERY_CATEGORIES:
        raise HTTPException(422, "Unknown recovery category")
    rows, total, page, pages = recovery_page(db, category, q, page)
    counts = recovery_counts(db)
    params = {"category": category, "q": q}
    return templates.TemplateResponse(
        request,
        "recovery.html",
        {
            "request": request,
            "actor": actor,
            "csrf": csrf(request),
            "title": "Recovery workspace",
            "rows": rows,
            "counts": counts,
            "categories": RECOVERY_CATEGORIES,
            "category": category,
            "q": q,
            "page": page,
            "pages": pages,
            "total": total,
            "previous": "?" + urlencode({**params, "page": page - 1}),
            "next": "?" + urlencode({**params, "page": page + 1}),
            "record_query": urlencode({"from_queue": "recovery", "q": q, "status": category, "page": page}),
        },
    )


@app.get("/demo", response_class=HTMLResponse)
def guided_demo_home(
    request: Request, actor=Depends(operator), db: Session = Depends(get_db)
):
    preview_examples = (
        db.scalars(select(Approval).order_by(Approval.id).limit(50)).all()
        if settings.preview_mode
        else []
    )
    return templates.TemplateResponse(
        request,
        "demo_preview.html" if settings.preview_mode else "demo.html",
        {
            "request": request,
            "actor": actor,
            "csrf": csrf(request),
            "title": "Guided demo",
            "delivery": None,
            "delay_hours": settings.order_delay_hours,
            "preview_examples": preview_examples,
            "preview_scenarios": SCENARIOS,
        },
    )


@app.post("/demo/preview/scenarios")
def preview_scenario_create(
    request: Request,
    scenario: str = Form(...),
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    if not settings.preview_mode:
        raise HTTPException(404)
    try:
        approval = create_preview_scenario(db, scenario, actor)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return RedirectResponse(f"/approvals/{approval.id}", status_code=303)


@app.post("/demo/start")
def guided_demo_start(
    request: Request,
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    run_id = uuid.uuid4().hex[:12]
    now = utcnow()
    payload = {
        "id": f"guided-{run_id}",
        "name": f"#GUIDE-{run_id[:6].upper()}",
        "email": "alex@example.test",
        "currency": "USD",
        "total_price": "84.00",
        "financial_status": "paid",
        "fulfillment_status": None,
        "created_at": (now - timedelta(hours=settings.order_delay_hours + 1)).isoformat(),
        "updated_at": now.isoformat(),
    }
    delivery = Delivery(
        webhook_id=f"guided-{run_id}",
        topic="orders/create",
        shop_domain="demo.myshopify.com",
        payload=payload,
        status="received",
    )
    db.add(delivery)
    db.flush()
    audit(
        db,
        "delivery",
        delivery.id,
        "guided_demo_started",
        actor,
        details={"mode": "mock", "workflow": "01"},
        correlation_id=delivery.webhook_id,
    )
    db.commit()
    return RedirectResponse(f"/demo/{delivery.id}", status_code=303)


def guided_demo_records(delivery, db):
    order = db.scalar(
        select(Order).where(
            Order.shop_domain == delivery.shop_domain,
            Order.external_id == str(delivery.payload.get("id")),
        )
    )
    exception = None
    case = None
    reply = None
    if order:
        exception = db.scalar(
            select(Approval).where(
                Approval.kind == "exception",
                Approval.subject_type == "order",
                Approval.subject_id == order.id,
                Approval.proposed_action == "review_delayed_order",
            )
        )
        case = db.scalar(
            select(Case).where(Case.request_key == f"guided-support:{delivery.id}")
        )
        if case:
            reply = db.scalar(
                select(Approval).where(
                    Approval.kind == "support",
                    Approval.subject_type == "case",
                    Approval.subject_id == case.id,
                    Approval.proposed_action == "send_reply",
                )
            )
    automatic_work_pending = (
        delivery.status in ("received", "failed")
        or (order is not None and exception is None)
        or (case is not None and case.status in ("queued", "failed"))
        or any(
            approval is not None
            and approval.status == "approved"
            and approval.execution_status in ("queued", "retryable")
            for approval in (exception, reply)
        )
    )
    return order, exception, case, reply, automatic_work_pending


@app.get("/demo/{delivery_id}", response_class=HTMLResponse)
def guided_demo(
    delivery_id: int,
    request: Request,
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    delivery = db.get(Delivery, delivery_id)
    if not delivery or not delivery.webhook_id.startswith("guided-"):
        raise HTTPException(404, "Guided demo run not found")
    order, exception, case, reply, automatic_work_pending = guided_demo_records(
        delivery, db
    )
    source_created = order.created_at if order else parse_dt(delivery.payload.get("created_at"))
    order_age_hours = max(0, int((utcnow() - as_utc(source_created)).total_seconds() // 3600))
    return templates.TemplateResponse(
        request,
        "demo.html",
        {
            "request": request,
            "actor": actor,
            "csrf": csrf(request),
            "title": "Guided demo",
            "delivery": delivery,
            "order": order,
            "exception": exception,
            "case": case,
            "reply": reply,
            "auto_refresh": automatic_work_pending,
            "delay_hours": settings.order_delay_hours,
            "order_age_hours": order_age_hours,
            "now": utcnow(),
        },
    )


@app.post("/demo/{delivery_id}/support")
def guided_demo_support(
    delivery_id: int,
    request: Request,
    message: str = Form(...),
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session=Depends(get_db),
):
    verify_csrf(request, csrf_token)
    delivery = db.get(Delivery, delivery_id)
    if not delivery or not delivery.webhook_id.startswith("guided-"):
        raise HTTPException(404, "Guided demo run not found")
    order, exception, _, _, _ = guided_demo_records(delivery, db)
    if not order or not exception:
        raise HTTPException(409, "Run the order and exception workflows before drafting support.")
    message = message.strip()
    if not message or len(message) > 1000:
        raise HTTPException(422, "Enter a message between 1 and 1000 characters.")
    try:
        queue_support(
            db,
            message,
            order.order_number,
            order.email,
            request_key=f"guided-support:{delivery.id}",
        )
    except SupportRequestKeyConflict:
        raise HTTPException(409, "This guided run already has a different support inquiry.")
    return RedirectResponse(f"/demo/{delivery.id}", status_code=303)


@app.post("/demo/{delivery_id}/run/{phase}")
def guided_demo_run_workflow(
    delivery_id: int,
    phase: str,
    request: Request,
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session=Depends(get_db),
):
    verify_csrf(request, csrf_token)
    if settings.preview_mode:
        raise HTTPException(409, "n8n workflow execution is disabled in the isolated preview.")
    triggers = {
        "order": (1, "orders/process"),
        "support": (2, "support/triage"),
        "exceptions": (3, "exceptions/scan"),
    }
    if phase not in triggers:
        raise HTTPException(404, "Workflow step not found")
    delivery = db.get(Delivery, delivery_id)
    if not delivery or not delivery.webhook_id.startswith("guided-"):
        raise HTTPException(404, "Guided demo run not found")
    order, _, case, _, _ = guided_demo_records(delivery, db)
    if phase == "support" and case is None:
        raise HTTPException(409, "Queue a support inquiry before running triage.")
    number, path = triggers[phase]
    audit(
        db,
        "workflow",
        number,
        "guided_demo_triggered",
        actor,
        details={"source": "guided_demo"},
        correlation_id=delivery.webhook_id,
    )
    db.commit()
    try:
        response = httpx.post(
            f"{settings.n8n_base_url.rstrip('/')}/webhook/commerceops/{path}",
            timeout=15,
        )
        response.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError):
        log.warning(
            json.dumps(
                {
                    "event": "guided_workflow_trigger_failed",
                    "workflow": number,
                    "correlation_id": delivery.webhook_id,
                }
            )
        )
        raise HTTPException(503, "n8n did not respond. Check its health, then retry this step.")
    return RedirectResponse(f"/demo/{delivery.id}", status_code=303)


@app.get("/queues/{section}", response_class=HTMLResponse)
def queue_page(
    section: str,
    request: Request,
    q: str = Query("", max_length=150),
    status: str = Query("", max_length=40),
    page: int = Query(1, ge=1),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    configs = {
        "orders": (
            Order,
            Order.fulfillment_status,
            [Order.order_number, Order.external_id, Order.email],
            ["unfulfilled", "fulfilled", "partial"],
        ),
        "cases": (
            Case,
            Case.status,
            [cast(Case.id, String), Case.customer_email, Case.category, Case.order_ref],
            ["queued", "needs_review", "resolved_mock", "failed", "exhausted", "recovery", "scheduled", "eligible"],
        ),
        "approvals": (
            Approval,
            Approval.status,
            [cast(Approval.id, String), Approval.proposed_action, Approval.kind],
            ["pending", "approved", "rejected"],
        ),
        "failures": (
            Approval,
            Approval.execution_status,
            [
                cast(Approval.id, String),
                Approval.proposed_action,
                Approval.failure_detail,
            ],
            [
                "failed",
                "retryable",
                "exhausted",
                "ambiguous",
                "evidence_changed",
                "invalid_action",
                "scheduled",
                "eligible",
                "stale",
                "uncertain",
            ],
        ),
        "deliveries": (
            Delivery,
            Delivery.status,
            [Delivery.webhook_id, Delivery.error],
            ["received", "processed", "failed", "exhausted", "recovery", "scheduled", "eligible"],
        ),
    }
    if section not in configs:
        raise HTTPException(404, "Queue not found")
    model, state, fields, options = configs[section]
    conditions = []
    if section == "failures":
        conditions.extend([
            state.in_("failed retryable exhausted ambiguous evidence_changed invalid_action".split()),
            Approval.replacement_approval_id.is_(None),
        ])
    if q:
        conditions.append(
            or_(
                *[
                    field.ilike(
                        "%"
                        + q.replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                        + "%",
                        escape="\\",
                    )
                    for field in fields
                ]
            )
        )
    now = utcnow()
    if section == "failures" and status:
        if status == "scheduled":
            conditions.extend([state.in_(["retryable", "queued"]), Approval.next_attempt_at > now])
        elif status == "eligible":
            conditions.extend([state.in_(["retryable", "queued"]), or_(Approval.next_attempt_at.is_(None), Approval.next_attempt_at <= now)])
        elif status == "stale":
            conditions.append(state == "evidence_changed")
        elif status == "uncertain":
            conditions.append(state == "ambiguous")
        else:
            conditions.append(state == status)
    elif status == "recovery" and section in ("cases", "deliveries"):
        conditions.append(state.in_(["failed", "exhausted"]))
    elif status == "scheduled" and section in ("cases", "deliveries"):
        conditions.extend([state == "failed", model.next_attempt_at > now])
    elif status == "eligible" and section in ("cases", "deliveries"):
        conditions.extend([state.in_(["failed", "queued", "received"]), or_(model.next_attempt_at.is_(None), model.next_attempt_at <= now)])
    elif status:
        conditions.append(state == status)
    total = db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0
    pages = max(1, ceil(total / 12))
    page = min(page, pages)
    query = select(model)
    if section == "cases":
        query = query.options(selectinload(Case.order))
    rows = db.scalars(
        query
        .where(*conditions)
        .order_by(model.id.desc())
        .offset((page - 1) * 12)
        .limit(12)
    ).all()
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "request": request,
            "actor": actor,
            "csrf": csrf(request),
            "section": section,
            "title": {
                "cases": "Support cases",
                "failures": "Action recovery",
                "deliveries": "Delivery queue",
            }.get(section, section.title()),
            "rows": rows,
            "q": q,
            "status": status,
            "options": options,
            "page": page,
            "pages": pages,
            "total": total,
            "record_query": urlencode(
                {"from_queue": section, "q": q, "status": status, "page": page}
            ),
            "previous": "?" + urlencode({"q": q, "status": status, "page": page - 1}),
            "next": "?" + urlencode({"q": q, "status": status, "page": page + 1}),
        },
    )


def detail_response(kind, record_id, request, actor, db, status_code=200):
    model = {"order": Order, "case": Case, "approval": Approval, "delivery": Delivery}[
        kind
    ]
    entity = db.get(model, record_id)
    if not entity:
        raise HTTPException(404, "Record not found")
    related = []
    related_cases_limited = False
    conditions = [(Audit.entity_type == kind) & (Audit.entity_id == record_id)]
    if kind == "approval":
        correlation_id = (entity.supporting_facts or {}).get("correlation_id")
        if correlation_id:
            conditions.append(Audit.correlation_id == correlation_id)
    if kind in ("order", "case"):
        subjects = [record_id]
        approval_condition = (Approval.subject_type == kind) & (
            Approval.subject_id == record_id
        )
        if kind == "order":
            case_ids = list(db.scalars(
                select(Case.id)
                .where(Case.order_id == record_id)
                .order_by(Case.id.desc())
                .limit(101)
            ))
            related_cases_limited = len(case_ids) > 100
            case_ids = case_ids[:100]
            approval_condition = or_(
                approval_condition,
                (Approval.subject_type == "case") & Approval.subject_id.in_(case_ids),
            )
            conditions.append(
                (Audit.entity_type == "case") & Audit.entity_id.in_(case_ids)
            )
        related = db.scalars(
            select(Approval).where(approval_condition).order_by(Approval.id.desc()).limit(50)
        ).all()
        conditions.append(
            (Audit.entity_type == "approval")
            & Audit.entity_id.in_([a.id for a in related])
        )
    timeline = list(
        reversed(
            db.scalars(
                select(Audit)
                .where(or_(*conditions))
                .order_by(Audit.created_at.desc(), Audit.id.desc())
                .limit(100)
            ).all()
        )
    )
    title = (
        f"Order {entity.order_number}"
        if kind == "order"
        else f"{kind.title()} #{entity.id}"
    )
    detail_query, back_url = record_navigation(request.query_params, kind)
    subject = None
    current_order = None
    freshness_differences = []
    approval_history = []
    prior_approval = None
    refresh_blocked_by_execution = False
    if kind == "approval":
        subject = db.get(Order if entity.subject_type == "order" else Case, entity.subject_id)
        current_order = subject if entity.subject_type == "order" else subject.order if subject else None
        freshness_differences = approval_freshness_differences(db, entity)
        prior_execution = db.scalar(
            select(ActionExecution).where(
                ActionExecution.idempotency_key == f"approval:{entity.id}"
            )
        )
        refresh_blocked_by_execution = bool(
            prior_execution and prior_execution.state in ("started", "succeeded", "ambiguous")
        )
        approval_history = db.scalars(
            select(Approval)
            .where(
                Approval.kind == entity.kind,
                Approval.subject_type == entity.subject_type,
                Approval.subject_id == entity.subject_id,
                Approval.id != entity.id,
            )
            .order_by(Approval.created_at.desc(), Approval.id.desc())
            .limit(20)
        ).all()
        prior_approval = db.scalar(
            select(Approval).where(Approval.replacement_approval_id == entity.id)
        )
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "request": request,
            "actor": actor,
            "csrf": csrf(request),
            "title": title,
            "entity": entity,
            "kind": kind,
            "timeline": timeline,
            "related": related,
            "back_url": back_url,
            "detail_query": detail_query,
            "retry_guidance": retry_guidance,
            "now": utcnow(),
            "preview_run_due": kind == "approval"
            and entity.execution_status in ("queued", "retryable")
            and (not entity.next_attempt_at or as_utc(entity.next_attempt_at) <= utcnow()),
            "subject": subject,
            "current_order": current_order,
            "freshness_differences": freshness_differences,
            "approval_history": approval_history,
            "prior_approval": prior_approval,
            "refresh_blocked_by_execution": refresh_blocked_by_execution,
            "refresh_supported": kind == "approval"
            and (entity.kind, entity.subject_type, entity.proposed_action) in ACTION_CONTRACTS
            and not refresh_blocked_by_execution,
            "timeline_limited": len(timeline) == 100,
            "related_cases_limited": related_cases_limited,
        },
        status_code=status_code,
    )


@app.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(
    order_id: int,
    request: Request,
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    return detail_response("order", order_id, request, actor, db)


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_detail(
    case_id: int,
    request: Request,
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    return detail_response("case", case_id, request, actor, db)


@app.get("/approvals/{approval_id}", response_class=HTMLResponse)
def approval_detail(
    approval_id: int,
    request: Request,
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    return detail_response("approval", approval_id, request, actor, db)


@app.get("/deliveries/{delivery_id}", response_class=HTMLResponse)
def delivery_detail(
    delivery_id: int,
    request: Request,
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    return detail_response("delivery", delivery_id, request, actor, db)


@app.post("/deliveries/{delivery_id}/retry")
def retry_delivery(
    delivery_id: int,
    request: Request,
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    delivery = db.scalar(
        select(Delivery).where(Delivery.id == delivery_id).with_for_update()
    )
    if not delivery:
        raise HTTPException(404)
    if delivery.status not in ("failed", "exhausted"):
        raise HTTPException(409, "Delivery is not retryable or already queued")
    delivery.status = "received"
    # Manual requeue must not shorten an existing backoff deadline.
    audit(
        db,
        "delivery",
        delivery.id,
        "manual_retry_queued",
        actor,
        correlation_id=delivery.webhook_id,
    )
    db.commit()
    suffix, _ = record_navigation(request.query_params, "delivery")
    return RedirectResponse(f"/deliveries/{delivery_id}{suffix}", status_code=303)


@app.post("/approvals/{approval_id}/decision")
def approval_decision(
    approval_id: int,
    request: Request,
    decision: str = Form(...),
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    if decision not in ("approved", "rejected"):
        raise HTTPException(422, "invalid decision")
    try:
        approval = decide_approval(db, approval_id, decision, actor)
        if settings.preview_mode and decision == "approved":
            # The disposable preview has no workflow runner. Execute only its
            # registered mock adapter so the scenario records a real local result.
            execute_approved(db, approval)
            db.commit()
    except LookupError:
        raise HTTPException(404)
    except ApprovalEvidenceStale:
        return detail_response("approval", approval_id, request, actor, db, status_code=409)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    suffix, _ = record_navigation(request.query_params, "approval")
    return RedirectResponse(f"/approvals/{approval_id}{suffix}", status_code=303)


@app.post("/approvals/{approval_id}/refresh")
def refresh_approval_route(
    approval_id: int,
    request: Request,
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    try:
        replacement = refresh_approval(db, approval_id, actor)
    except LookupError:
        raise HTTPException(404)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    suffix, _ = record_navigation(request.query_params, "approval")
    return RedirectResponse(f"/approvals/{replacement.id}{suffix}", status_code=303)


@app.post("/approvals/{approval_id}/retry")
def retry(
    approval_id: int,
    request: Request,
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    approval = db.scalar(
        select(Approval).where(Approval.id == approval_id).with_for_update()
    )
    if not approval:
        raise HTTPException(404)
    if approval.status != "approved" or approval.execution_status not in (
        "failed",
        "retryable",
        "exhausted",
    ):
        raise HTTPException(
            409, "action is not retryable; ambiguous outcomes require reconciliation"
        )
    approval.execution_status = "queued"
    if approval.next_attempt_at is None:
        approval.next_attempt_at = utcnow()
    audit(db, "approval", approval.id, "manual_retry_queued", actor)
    db.commit()
    suffix, _ = record_navigation(request.query_params, "approval")
    return RedirectResponse(f"/approvals/{approval_id}{suffix}", status_code=303)


@app.post("/approvals/{approval_id}/preview-run")
def preview_run(
    approval_id: int,
    request: Request,
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    if not settings.preview_mode:
        raise HTTPException(404)
    approval = db.get(Approval, approval_id)
    if not approval:
        raise HTTPException(404)
    if approval.status != "approved" or approval.execution_status not in ("queued", "retryable"):
        raise HTTPException(409, "This preview action is not ready for a local mock attempt")
    if approval.next_attempt_at and as_utc(approval.next_attempt_at) > utcnow():
        raise HTTPException(409, "The recorded backoff deadline has not arrived")
    execute_approved(db, approval)
    audit(db, "approval", approval.id, "preview_mock_attempt_requested", actor)
    db.commit()
    suffix, _ = record_navigation(request.query_params, "approval")
    return RedirectResponse(f"/approvals/{approval_id}{suffix}", status_code=303)


@app.post("/webhooks/shopify/orders")
async def shopify_order(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(413, "Webhook exceeds 1 MB")
    if not verify_shopify(raw, request.headers.get("x-shopify-hmac-sha256")):
        raise HTTPException(401, "invalid webhook signature")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(422, "Order payload must be an object")
    webhook_id = request.headers.get("x-shopify-webhook-id", "")
    topic = request.headers.get("x-shopify-topic", "orders/create")
    shop = request.headers.get("x-shopify-shop-domain")
    if not request.headers.get("x-shopify-webhook-id"):
        raise HTTPException(400, "missing Shopify delivery ID")
    if not shop or not shop.lower().endswith(".myshopify.com"):
        raise HTTPException(400, "missing or invalid Shopify shop domain")
    if (
        settings.shopify_store_domain
        and shop.lower() != settings.shopify_store_domain.lower()
    ):
        raise HTTPException(403, "unexpected Shopify shop")
    if topic not in ("orders/create", "orders/updated"):
        raise HTTPException(422, "unsupported Shopify topic")
    if len(webhook_id) > 150 or len(shop) > 255:
        raise HTTPException(422, "Invalid header length")
    shop = shop.lower()
    existing = db.scalar(select(Delivery).where(Delivery.webhook_id == webhook_id))
    if existing:
        db.add(
            Audit(
                correlation_id=webhook_id,
                entity_type="delivery",
                entity_id=existing.id,
                event="duplicate_delivery_ignored",
                details={"webhook_id": webhook_id},
            )
        )
        db.commit()
        return JSONResponse({"received": True, "duplicate": True}, status_code=200)
    delivery = Delivery(
        webhook_id=webhook_id,
        event_id=request.headers.get("x-shopify-event-id"),
        topic=topic,
        shop_domain=shop,
        payload=payload,
        status="received",
    )
    try:
        db.add(delivery)
        db.flush()
        audit(
            db,
            "delivery",
            delivery.id,
            "delivery_received",
            details={"topic": topic},
            correlation_id=webhook_id,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Delivery).where(Delivery.webhook_id == webhook_id))
        if existing:
            audit(
                db,
                "delivery",
                existing.id,
                "duplicate_delivery_ignored",
                correlation_id=webhook_id,
            )
            db.commit()
            return JSONResponse({"received": True, "duplicate": True}, status_code=200)
        raise
    db.refresh(delivery)
    return JSONResponse({"received": True, "delivery_id": delivery.id}, status_code=202)


@app.post("/api/n8n/process-delivery/{delivery_id}")
def n8n_process(
    delivery_id: int,
    x_n8n_secret: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not secrets.compare_digest(x_n8n_secret or "", settings.n8n_webhook_secret):
        raise HTTPException(401)
    return process_delivery(db, delivery_id)


@app.post("/api/n8n/process-next")
def n8n_next(
    x_n8n_secret: str | None = Header(default=None), db: Session = Depends(get_db)
):
    if not secrets.compare_digest(x_n8n_secret or "", settings.n8n_webhook_secret):
        raise HTTPException(401)
    pending = db.scalar(
        select(Delivery)
        .where(
            or_(
                Delivery.next_attempt_at.is_(None), Delivery.next_attempt_at <= utcnow()
            ),
            (Delivery.status == "received")
            | (
                (Delivery.status == "failed")
                & (Delivery.attempts < settings.max_delivery_attempts)
            ),
        )
        .order_by(Delivery.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    result = process_delivery(db, pending.id) if pending else {"status": "idle"}
    return heartbeat(
        db, 1, result,
        completed=result["status"] == "processed",
        failed=result["status"] in ("failed", "exhausted"),
    )


class Inquiry(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    order_ref: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=320)
    request_key: str | None = Field(default=None, max_length=150)


@app.post("/api/support", status_code=202)
def support_api(
    data: Inquiry,
    x_n8n_secret: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not secrets.compare_digest(x_n8n_secret or "", settings.n8n_webhook_secret):
        raise HTTPException(401)
    if not data.message.strip():
        raise HTTPException(422, "Message is required")
    try:
        case = queue_support(db, data.message, data.order_ref, data.email, data.request_key)
    except SupportRequestKeyConflict:
        raise HTTPException(409, "request_key is already associated with different inquiry details")
    return {"case_id": case.id, "status": case.status}


@app.post("/api/n8n/triage-next")
def triage_next(
    x_n8n_secret: str | None = Header(default=None), db: Session = Depends(get_db)
):
    if not secrets.compare_digest(x_n8n_secret or "", settings.n8n_webhook_secret):
        raise HTTPException(401)
    case = db.scalar(
        select(Case)
        .where(
            Case.status.in_(["queued", "failed"]),
            or_(Case.next_attempt_at.is_(None), Case.next_attempt_at <= utcnow()),
        )
        .order_by(Case.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not case:
        return heartbeat(db, 2, {"status": "idle"})
    case = process_support(db, case)
    return heartbeat(
        db,
        2,
        {"case_id": case.id, "status": case.status},
        completed=case.status == "needs_review",
        failed=case.status in ("failed", "exhausted"),
    )


@app.post("/cases/{case_id}/retry")
def retry_case(
    case_id: int,
    request: Request,
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    case = db.scalar(select(Case).where(Case.id == case_id).with_for_update())
    if not case:
        raise HTTPException(404)
    if case.status not in ("failed", "exhausted"):
        raise HTTPException(409, "Case is not retryable")
    case.status = "queued"
    # Preserve the earliest safe retry time, including manual requests.
    audit(
        db,
        "case",
        case.id,
        "manual_retry_queued",
        actor,
        correlation_id=case.correlation_id,
    )
    db.commit()
    suffix, _ = record_navigation(request.query_params, "case")
    return RedirectResponse(f"/cases/{case.id}{suffix}", 303)


@app.get("/support", response_class=HTMLResponse)
def support_form(request: Request, actor=Depends(operator)):
    return templates.TemplateResponse(
        request,
        "support.html",
        {
            "request": request,
            "actor": actor,
            "csrf": csrf(request),
            "mock": True,
            "llm": settings.llm_mode,
            "request_key": uuid.uuid4().hex,
            "title": "New inquiry",
        },
    )


@app.post("/support")
def support_submit(
    request: Request,
    message: str = Form(...),
    order_ref: str = Form(""),
    email: str = Form(""),
    request_key: str = Form(...),
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    if (
        not message.strip()
        or len(message) > 5000
        or len(order_ref) > 100
        or len(email) > 320
        or len(request_key) > 150
    ):
        raise HTTPException(422, "Check the inquiry fields")
    try:
        case = queue_support(
            db, message, order_ref or None, email or None, request_key=request_key
        )
    except SupportRequestKeyConflict:
        raise HTTPException(409, "This request key is already associated with different inquiry details.")
    return RedirectResponse(f"/cases/{case.id}", status_code=303)


@app.post("/api/demo/order")
def demo_order(
    request: Request,
    csrf_token: str = Form(...),
    actor=Depends(operator),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    from app.services import process_delivery

    payload = {
        "id": f"DEMO-{uuid.uuid4().hex[:6]}",
        "name": f"#D{uuid.uuid4().hex[:5].upper()}",
        "email": "alex@example.test",
        "currency": "USD",
        "total_price": "84.00",
        "fulfillment_status": None,
        "financial_status": "paid",
        "created_at": utcnow().isoformat(),
        "updated_at": utcnow().isoformat(),
    }
    d = Delivery(
        webhook_id="demo-" + uuid.uuid4().hex,
        topic="orders/create",
        shop_domain="demo.myshopify.com",
        payload=payload,
        status="received",
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return RedirectResponse(f"/deliveries/{d.id}", 303)


@app.post("/api/n8n/delayed-exceptions")
def scan_delayed(
    x_n8n_secret: str | None = Header(default=None), db: Session = Depends(get_db)
):
    from app.services import create_approval

    if not secrets.compare_digest(x_n8n_secret or "", settings.n8n_webhook_secret):
        raise HTTPException(401)
    cutoff = utcnow() - timedelta(hours=settings.order_delay_hours)
    already_approved = exists(
        select(Approval.id).where(
            Approval.kind == "exception",
            Approval.subject_type == "order",
            Approval.subject_id == Order.id,
            Approval.proposed_action == "review_delayed_order",
        )
    )
    delayed_condition = (
        (Order.fulfillment_status == "unfulfilled")
        & (Order.created_at < cutoff)
        & ~already_approved
    )
    rows = db.scalars(
        select(Order)
        .where(delayed_condition)
        .order_by(Order.created_at, Order.id)
        .limit(100)
    ).all()
    created = []
    before = db.scalar(select(func.count(Approval.id)))
    for o in rows:
        a = create_approval(
            db,
            "exception",
            "order",
            o.id,
            "review_delayed_order",
            {
                "order_number": o.order_number,
                "order_id": o.id,
                "hours_threshold": settings.order_delay_hours,
                "created_at": o.created_at.isoformat(),
                "order_fulfillment_status": o.fulfillment_status,
                "order_financial_status": o.financial_status,
                "order_total": f"{o.currency} {o.total}",
                "order_source_updated_at": o.source_updated_at.isoformat(),
            },
        )
        created.append(a.id)
    db.commit()
    new_exceptions = (db.scalar(select(func.count(Approval.id))) or 0) - before
    delayed_scan_has_more = bool(db.scalar(select(exists().where(delayed_condition))))
    # n8n schedules the action worker; browser decisions only enqueue work.
    pending = db.scalars(
        select(Approval)
        .where(
            Approval.status == "approved",
            Approval.execution_status.in_(["queued", "retryable"]),
            or_(
                Approval.next_attempt_at.is_(None), Approval.next_attempt_at <= utcnow()
            ),
        )
        .order_by(Approval.id)
        .limit(20)
        .with_for_update(skip_locked=True)
    ).all()
    action_outcomes = {
        "succeeded": 0,
        "retryable": 0,
        "exhausted": 0,
        "ambiguous": 0,
        "evidence_changed": 0,
        "invalid_action": 0,
    }
    for approval in pending:
        execute_approved(db, approval)
        if approval.execution_status in action_outcomes:
            action_outcomes[approval.execution_status] += 1
    db.commit()
    return heartbeat(
        db,
        3,
        {
            "open_delayed_orders": len(created),
            "new_exceptions": new_exceptions,
            "delayed_scan_has_more": delayed_scan_has_more,
            "actions_processed": len(pending),
            "action_outcomes": action_outcomes,
        },
        completed=bool(new_exceptions or action_outcomes["succeeded"]),
        failed=any(
            action_outcomes[state]
            for state in (
                "retryable",
                "exhausted",
                "ambiguous",
                "evidence_changed",
                "invalid_action",
            )
        ),
    )


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    if "text/html" in request.headers.get("accept", ""):
        if exc.status_code == 401:
            return RedirectResponse("/login", 303)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "request": request,
                "title": str(exc.status_code),
                "message": str(exc.detail),
                "actor": request.session.get("operator"),
                "csrf": csrf(request),
            },
            status_code=exc.status_code,
        )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "request": request,
                "title": "Check your input",
                "message": "A required field is missing or invalid. Go back and check the form.",
                "actor": request.session.get("operator"),
                "csrf": csrf(request),
            },
            status_code=422,
        )
    return JSONResponse(
        {"detail": [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]},
        status_code=422,
    )


@app.middleware("http")
async def request_log(request, call_next):
    correlation = str(uuid.uuid4())
    started = time.monotonic()
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    log.info(
        json.dumps(
            {
                "event": "http_request",
                "correlation_id": correlation,
                "method": request.method,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        )
    )
    return response


app.mount("/static", StaticFiles(directory="app/static"), name="static")
