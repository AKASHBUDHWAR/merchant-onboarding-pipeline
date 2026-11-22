import asyncio
import base64
import json
import logging
import math
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from .domain import CHECKS, FIELD_SCHEMA, DomainError, require
from .metrics import metrics
from .models import (Application, Change, DocSimulation, Reason, Respond, ReviewCommand, Scenario,
                     Simulation, StructuredDocument)
from .storage import ROOT, Store, audit, insert, json_text, many, one, unjson, update, verify_audit
from .workflow import Workflow

PREFIX = "/api/v1"
OPS = {"reviewer-1": "ops_reviewer", "reviewer-2": "ops_reviewer", "admin": "ops_admin", "admin-2": "ops_admin"}
LOGGER = logging.getLogger(__name__)


def actor(request, role):
    user = request.headers.get("x-user-id")
    declared = request.headers.get("x-role")
    require(user and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", user), "Provide a demo user ID.", "UNAUTHORIZED", 401)
    actual = OPS.get(user, "merchant")
    require(declared == actual and (actual == role or role == "ops" and actual in {"ops_reviewer", "ops_admin"}),
            "This role cannot perform the action.", "FORBIDDEN", 403)
    return actual, user


def as_operator(user):
    return ("OPERATOR", user[1])


def check_data(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if "aadhaar" in key.casefold() or "aadhar" in key.casefold():
                require(key == "aadhaar_last4" and (item is None or isinstance(item, str) and re.fullmatch(r"[0-9]{4}", item)),
                        "Only Aadhaar last four digits are accepted.", "UNSUPPORTED_FIELD", 422)
            check_data(item)
    elif isinstance(value, list):
        for item in value:
            check_data(item)
    elif isinstance(value, float):
        require(math.isfinite(value), "Numbers must be finite.", "INVALID_DATA", 422)


def apply_patch(original, changes):
    merged = original.copy()
    for key, value in changes.items():
        current = original.get(key)
        merged[key] = apply_patch(current, value) if isinstance(current, dict) and isinstance(value, dict) else value
    return merged


def create_app(directory=None, *, clock=None, worker=True):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(directory or os.environ.get("ONBOARDING_STORAGE", ROOT / "storage"), **({"clock": clock} if clock else {}))
        app.state.workflow = Workflow(store)

        async def run_worker():
            while True:
                try:
                    await asyncio.to_thread(app.state.workflow.tick)
                except Exception as error:
                    LOGGER.error("Worker iteration failed (%s)", type(error).__name__)
                await asyncio.sleep(.15)

        task = asyncio.create_task(run_worker()) if worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="Merchant Onboarding Pipeline", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse({"error": {"code": error.code, "message": error.message, "details": {}}}, status_code=error.status)

    async def invalid_input(request, error):
        return JSONResponse({"error": {"code": "INVALID_INPUT", "message": "Check the request fields.",
                                      "details": {"fields": [list(item["loc"]) for item in error.errors()][:20]}}}, status_code=422)

    app.add_exception_handler(ValidationError, invalid_input)
    app.add_exception_handler(RequestValidationError, invalid_input)

    async def body(request):
        try:
            raw = await request.json()
        except (ValueError, UnicodeError):
            raise DomainError("INVALID_JSON", "Expected a JSON object.", 422) from None
        require(isinstance(raw, dict), "Expected a JSON object.", "INVALID_JSON", 422)
        check_data(raw)
        return raw

    def mutate(role, *, empty=False, code=200, upload=False):
        def decorator(handler):
            async def route(request: Request):
                user = actor(request, role)
                flow = request.app.state.workflow
                if upload and request.headers.get("content-type", "").startswith("multipart/form-data"):
                    with flow.store.transaction(write=False) as db:
                        max_size = flow.store.config(db)["extraction"]["max_file_mb"] * 1024 * 1024
                    try:
                        async with request.form(max_files=1, max_fields=4, max_part_size=1024 * 1024) as fields:
                            file = fields.get("file")
                            require(isinstance(file, UploadFile), "Include a file.", "FILE_REQUIRED", 422)
                            binary = await file.read(max_size + 1)
                            require(len(binary) <= max_size, "File exceeds the configured limit.", "FILE_SIZE", 413)
                            payload = json.loads(fields.get("payload", "{}"))
                            simulation = json.loads(fields.get("simulate", "{}"))
                            raw = dict(doc_type=fields.get("doc_type"), payload=payload, simulate=simulation,
                                       mime=file.content_type, file=base64.b64encode(binary).decode())
                            check_data(raw)
                    except (ValueError, TypeError):
                        raise DomainError("INVALID_MULTIPART", "Invalid structured document metadata.", 422) from None
                else:
                    raw = {} if empty and not await request.body() else await body(request)
                    if upload:
                        require(not ({"file", "mime"} & raw.keys()), "Use multipart to attach a file.", "INVALID_DOCUMENT", 422)

                def apply():
                    with flow.store.transaction() as db:
                        key = request.headers.get("idempotency-key")
                        scope = f"{user[1]}:{request.method}:{request.url.path}"
                        fingerprint = flow.store.fingerprint(raw) if key else None
                        if key:
                            require(len(key) <= 150, "Idempotency key is too long.", "INVALID_KEY", 422)
                            prior = one(db, "SELECT * FROM api_idempotency WHERE scope=? AND key=?", (scope, key))
                            if prior:
                                require(prior["fingerprint"] == fingerprint, "Idempotency key already used for a different request.", "IDEMPOTENCY_CONFLICT")
                                return flow.store.open(prior["reply_enc"])
                        result = handler(flow, db, user, raw, **request.path_params)
                        if key:
                            insert(db, "api_idempotency", scope=scope, key=key, fingerprint=fingerprint,
                                   reply_enc=flow.store.seal(result))
                        return result

                return JSONResponse(await asyncio.to_thread(apply), status_code=code)
            route.__name__ = handler.__name__
            return route
        return decorator

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def console():
        return (ROOT / "onboarding/console.html").read_text()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post(PREFIX + "/applications")
    @mutate("merchant", code=201)
    def create_application(flow, db, user, raw):
        form = Application.model_validate(raw).model_dump()
        app_id = flow.create(db, form, user[1])
        return {"id": app_id, "reference_no": flow.app(db, app_id)["reference_no"], "state": "DRAFT"}

    @app.patch(PREFIX + "/applications/{app_id}")
    @mutate("merchant")
    def edit_application(flow, db, user, raw, app_id):
        current = flow.app(db, app_id, user[1])
        require(current["state"] in {"DRAFT", "AWAITING_MERCHANT_INFO"}, "Application cannot be edited now.")
        form = Application.model_validate(apply_patch(flow.form(current), raw)).model_dump()
        flow.save_form(db, app_id, form)
        audit(db, flow.now(), app_id, "APPLICATION_EDITED", ("MERCHANT", user[1]), {"fields": sorted(raw)})
        return {"id": app_id, "state": current["state"]}

    @app.post(PREFIX + "/applications/{app_id}/documents")
    @mutate("merchant", upload=True, code=201)
    def upload_document(flow, db, user, raw, app_id):
        current = flow.app(db, app_id, user[1])
        encoded, mime = raw.pop("file", None), raw.pop("mime", None)
        document = StructuredDocument.model_validate(raw)
        return flow.upload(db, current, document.doc_type, document.payload,
                           document.simulate.model_dump(exclude_none=True), base64.b64decode(encoded) if encoded is not None else None, mime)

    @app.delete(PREFIX + "/applications/{app_id}/documents/{doc_id}")
    @mutate("merchant", empty=True)
    def delete_document(flow, db, user, raw, app_id, doc_id):
        current = flow.app(db, app_id, user[1])
        require(current["state"] == "DRAFT", "Only draft documents can be deleted.")
        doc = one(db, "SELECT * FROM documents WHERE id=? AND application_id=? AND status<>'SUPERSEDED'", (doc_id, app_id))
        require(doc, "Document not found.", "NOT_FOUND", 404)
        update(db, "documents", "id=?", (doc_id,), status="SUPERSEDED")
        audit(db, flow.now(), app_id, "DOCUMENT_DELETED", ("MERCHANT", user[1]), {"document_id": doc_id})
        return {"id": doc_id, "deleted": True}

    @app.post(PREFIX + "/applications/{app_id}/submit")
    @mutate("merchant", empty=True, code=202)
    def submit_application(flow, db, user, raw, app_id):
        flow.submit(db, flow.app(db, app_id, user[1]))
        return {"id": app_id, "state": "SUBMITTED"}

    def current_info(db, app_id):
        row = one(db, "SELECT id,round_no,items,due_at FROM info_requests WHERE application_id=? AND status='OPEN'", (app_id,))
        return unjson(row, "items") if row else None

    @app.get(PREFIX + "/applications/{app_id}")
    def get_application(request: Request, app_id: str):
        user = actor(request, "merchant")
        flow = request.app.state.workflow
        with flow.store.transaction(write=False) as db:
            current = flow.app(db, app_id, user[1])
            info = current_info(db, app_id)
            result = {key: current[key] for key in ("id", "reference_no", "state", "merchant_id")}
            result["action_required"] = info["items"] if info else []
            result["due_at"] = info["due_at"] if info else None
            result["info_request_id"] = info["id"] if info else None
            prior = {row["to_state"] for row in many(db, "SELECT to_state FROM state_transitions WHERE application_id=?", (app_id,))}
            result["progress"] = [{"step": label, "status": "ACTION_REQUIRED" if info and label == "Documents" else
                                   "DONE" if end in prior else "IN_PROGRESS" if phase == current["state"] else "PENDING"}
                                  for label, phase, end in [
                                      ("Documents", "DOC_PROCESSING", "VALIDATING"),
                                      ("Verification", "VERIFICATION_IN_PROGRESS", "RISK_ASSESSMENT"),
                                      ("Review", "MANUAL_REVIEW", "APPROVED"),
                                      ("Activation", "PROVISIONING", "ACTIVE"),
                                  ]]
            result["timeline"] = many(db, "SELECT to_state AS state,occurred_at FROM state_transitions WHERE application_id=? ORDER BY id", (app_id,))
            result["notifications"] = many(db, "SELECT event,message,occurred_at FROM notification_outbox WHERE application_id=?", (app_id,))
            if current["state"] == "ACTIVE":
                result["provisioning"] = flow.capabilities(db, app_id)
            return result

    @app.get(PREFIX + "/applications/{app_id}/info-requests/current")
    def get_info(request: Request, app_id: str):
        user = actor(request, "merchant")
        flow = request.app.state.workflow
        with flow.store.transaction(write=False) as db:
            flow.app(db, app_id, user[1])
            return {"request": current_info(db, app_id)}

    @app.post(PREFIX + "/applications/{app_id}/info-requests/{rid}/respond")
    @mutate("merchant", code=202)
    def respond(flow, db, user, raw, app_id, rid):
        current = flow.app(db, app_id, user[1])
        response = Respond.model_validate(raw)
        form = Application.model_validate(apply_patch(flow.form(current), response.fields)).model_dump() if response.fields else None
        flow.respond(db, current, rid, form, response.documents, response.confirm_fields)
        return {"id": app_id, "state": flow.app(db, app_id)["state"]}

    @app.post(PREFIX + "/applications/{app_id}/withdraw")
    @mutate("merchant", empty=True)
    def withdraw(flow, db, user, raw, app_id):
        flow.app(db, app_id, user[1])
        flow.move(db, app_id, "T35", "MerchantWithdrew", actor=("MERCHANT", user[1]))
        db.execute("UPDATE verification_checks SET status='STALE' WHERE application_id=? AND status IN ('PENDING','IN_FLIGHT')", (app_id,))
        return {"id": app_id, "state": "WITHDRAWN"}

    @app.post(PREFIX + "/applications/{app_id}/change-requests")
    @mutate("merchant", code=202)
    def change(flow, db, user, raw, app_id):
        current = flow.app(db, app_id, user[1])
        command = Change.model_validate(raw)
        expected = {"BANK_ACCOUNT_CHANGED": "bank_account", "LEGAL_NAME_CHANGED": "legal_name",
                    "GSTIN_CHANGED": "declared_gstin", "AUTHORISED_SIGNATORY_CHANGED": "persons",
                    "ADDRESS_CHANGED": "registered_address"}[command.change_type]
        require(set(command.fields) == {expected} and command.fields[expected] is not None,
                "Provide the field that corresponds to the change type.", "INVALID_CHANGE", 422)
        form = Application.model_validate(apply_patch(flow.form(current), command.fields)).model_dump()
        if expected == "declared_gstin":
            from .domain import canonical, gst_valid
            value = canonical(form[expected])
            require(gst_valid(value) and value[2:12] == canonical(form.get("declared_pan")),
                    "Provide a GSTIN for the registered PAN.", "INVALID_CHANGE", 422)
        return {"id": flow.change(db, current, command.change_type, form), "state": flow.app(db, app_id)["state"]}

    async def handle_callback(request, provider):
        provider = provider.upper()
        require(provider in CHECKS, "Unknown provider.", "NOT_FOUND", 404)
        flow = request.app.state.workflow
        raw = await request.body()
        require(len(raw) <= 256_000, "Callback is too large.", "INVALID_CALLBACK", 413)
        event_id = request.headers.get("x-event-id") or ""
        stamp = request.headers.get("x-timestamp")
        signature = request.headers.get("x-signature")
        require(re.fullmatch(r"[A-Za-z0-9_\-]{1,120}", event_id), "Invalid callback event ID.", "INVALID_CALLBACK", 422)
        try:
            payload = json.loads(raw)
            require(isinstance(payload, dict), "Callback body must be an object.", "INVALID_CALLBACK", 422)
            check_data(payload)
        except (ValueError, UnicodeError):
            payload = {}
        if payload.get("provider_event_id", event_id) != event_id or not isinstance(payload.get("provider_request_id"), str):
            payload = {}

        def apply():
            with flow.store.transaction() as db:
                return flow.callback(db, provider, stamp, event_id, signature, raw, payload)

        status, http_status = await asyncio.to_thread(apply)
        return JSONResponse({"status": status}, status_code=http_status)

    @app.post(PREFIX + "/callbacks/{provider}")
    @app.post("/callbacks/{provider}", include_in_schema=False)
    async def callback(request: Request, provider: str):
        return await handle_callback(request, provider)

    def ops_read(request, fn, admin=False):
        actor(request, "ops_admin" if admin else "ops")
        flow = request.app.state.workflow
        with flow.store.transaction(write=False) as db:
            return fn(flow, db)

    @app.get(PREFIX + "/ops/applications")
    def queue(request: Request, state: str = "", priority: str = "", exception: str = "", q: str = "", sort: str = "newest"):
        ordering = {"oldest": "created_at ASC", "priority": "CASE priority WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, created_at", "newest": "created_at DESC"}
        require(sort in ordering, "Unknown sort order.", "INVALID_SORT", 422)

        def query(flow, db):
            return many(db, f"""SELECT id,reference_no,state,priority,legal_name,business_type,merchant_id FROM applications a
                WHERE (?='' OR state=?) AND (?='' OR priority=?) AND (?='' OR EXISTS
                  (SELECT 1 FROM exceptions WHERE application_id=a.id AND code=? AND status='OPEN'))
                AND (?='' OR INSTR(LOWER(legal_name),LOWER(?))>0 OR INSTR(LOWER(reference_no),LOWER(?))>0)
                ORDER BY {ordering[sort]} LIMIT 200""", (state, state, priority, priority, exception, exception, q, q, q))
        return ops_read(request, query)

    @app.get(PREFIX + "/ops/applications/{app_id}")
    def detail(request: Request, app_id: str):
        return ops_read(request, lambda flow, db: flow.ops_detail(db, flow.app(db, app_id)))

    @app.get(PREFIX + "/ops/applications/{app_id}/timeline")
    def timeline(request: Request, app_id: str):
        return ops_read(request, lambda flow, db: flow.timeline(db, flow.app(db, app_id)["id"]))

    @app.get(PREFIX + "/ops/applications/{app_id}/events")
    async def events(request: Request, app_id: str, after: int = 0):
        ops_read(request, lambda flow, db: flow.app(db, app_id))
        flow = request.app.state.workflow
        cursor = max(after, 0)
        if request.headers.get("last-event-id", "").isdigit():
            cursor = max(cursor, int(request.headers["last-event-id"]))

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                with flow.store.transaction(write=False) as db:
                    rows = many(db, """SELECT id,payload FROM outbox_events WHERE aggregate_id=? AND id>?
                                      AND published_at IS NOT NULL ORDER BY id LIMIT 100""", (app_id, cursor))
                for row in rows:
                    cursor = row["id"]
                    yield f"id: {cursor}\ndata: {row['payload']}\n\n"
                if not rows:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get(PREFIX + "/ops/review-tasks")
    def tasks(request: Request, assigned_to: str = "", status: str = ""):
        user = actor(request, "ops")
        assigned_to = user[1] if assigned_to == "me" else assigned_to
        return ops_read(request, lambda flow, db: many(db, """SELECT id,application_id,task_type,priority,status,
            assigned_to,sla_due_at,created_at FROM review_tasks WHERE (?='' OR assigned_to=?) AND
            (?='' OR status=?) ORDER BY sla_due_at LIMIT 200""", (assigned_to, assigned_to, status, status)))

    @app.post(PREFIX + "/ops/review-tasks/{task_id}/claim")
    @mutate("ops", empty=True)
    def claim(flow, db, user, raw, task_id):
        flow.claim(db, flow.task(db, task_id), as_operator(user))
        return {"id": task_id, "status": "IN_PROGRESS"}

    @app.post(PREFIX + "/ops/review-tasks/{task_id}/decide")
    @mutate("ops")
    def decide_task(flow, db, user, raw, task_id):
        command = ReviewCommand.model_validate(raw).model_dump(exclude_none=True)
        status = flow.review_action(db, flow.task(db, task_id), command, as_operator(user), user[0])
        return {"id": task_id, "status": status}

    @app.post(PREFIX + "/ops/applications/{app_id}/fields/{field}/override")
    @mutate("ops")
    def override(flow, db, user, raw, app_id, field):
        current = flow.app(db, app_id)
        reason = Reason.model_validate({"reason": raw.get("reason")}).reason
        require(current["state"] == "MANUAL_REVIEW" and "value" in raw, "An active review and a value are required.")
        match = one(db, """SELECT f.id FROM extracted_fields f JOIN documents d ON d.id=f.document_id
                       WHERE f.application_id=? AND f.field_name=? AND d.status<>'SUPERSEDED'
                       AND (? IS NULL OR d.id=?) ORDER BY f.created_at DESC LIMIT 1""",
                    (app_id, field, raw.get("document_id"), raw.get("document_id")))
        require(match, "Extracted field not found.", "NOT_FOUND", 404)
        update(db, "extracted_fields", "id=?", (match["id"],), normalized_value=flow.store.seal(raw["value"]),
               status="OPERATOR_OVERRIDDEN", overridden_by=user[1], override_reason=flow.store.seal(reason))
        audit(db, flow.now(), app_id, "FIELD_OVERRIDDEN", as_operator(user), {"id": match["id"], "field": field})
        return {"status": "OPERATOR_OVERRIDDEN"}

    @app.post(PREFIX + "/ops/checks/{check_id}/waive")
    @mutate("ops_admin")
    def waive(flow, db, user, raw, check_id):
        reason = Reason.model_validate(raw).reason
        flow.waive(db, check_id, as_operator(user))
        update(db, "verification_checks", "id=?", (check_id,), waiver_reason=flow.store.seal(reason))
        return {"id": check_id, "status": "WAIVED"}

    @app.post(PREFIX + "/ops/checks/{check_id}/rerun")
    @mutate("ops")
    def rerun(flow, db, user, raw, check_id):
        Reason.model_validate(raw)
        check = one(db, "SELECT * FROM verification_checks WHERE id=? AND superseded_by IS NULL", (check_id,))
        require(check, "Check not found.", "NOT_FOUND", 404)
        flow.rerun(db, flow.app(db, check["application_id"]), [check["check_type"]], as_operator(user))
        return {"state": "VERIFICATION_IN_PROGRESS"}

    @app.post(PREFIX + "/ops/applications/{app_id}/hold")
    @mutate("ops")
    def hold(flow, db, user, raw, app_id):
        reason = Reason.model_validate(raw).reason
        flow.hold(db, flow.app(db, app_id), as_operator(user))
        audit(db, flow.now(), app_id, "APPLICATION_HELD", as_operator(user), after=flow.store.seal(reason))
        return {"state": "ON_HOLD"}

    @app.post(PREFIX + "/ops/applications/{app_id}/resume")
    @mutate("ops", empty=True)
    def resume(flow, db, user, raw, app_id):
        flow.resume(db, flow.app(db, app_id), as_operator(user))
        return {"state": flow.app(db, app_id)["state"]}

    @app.post(PREFIX + "/ops/applications/{app_id}/provisioning/retry")
    @mutate("ops", empty=True)
    def provisioning_retry(flow, db, user, raw, app_id):
        flow.retry_provision(db, flow.app(db, app_id), as_operator(user), clear_simulation=raw.get("clear_simulation") is True)
        return {"state": "PROVISIONING"}

    @app.post(PREFIX + "/ops/applications/{app_id}/provisioning/manual-mid")
    @mutate("ops")
    def manual_mid(flow, db, user, raw, app_id):
        current = flow.app(db, app_id)
        require(current["state"] == "PROVISIONING_FAILED", "Manual assignment requires a provisioning failure.")
        task_id = flow.create_task(db, app_id, "PROVISIONING_FAILURE", "HIGH")
        command = ReviewCommand.model_validate(raw | {"decision": "MANUAL_MID"}).model_dump(exclude_none=True)
        return {"status": flow.review_action(db, flow.task(db, task_id), command, as_operator(user), user[0]), "task_id": task_id}

    @app.get(PREFIX + "/ops/dead-letters")
    def dead_letters(request: Request):
        return ops_read(request, lambda flow, db: [unjson(row, "payload") for row in many(db, "SELECT * FROM dead_letters ORDER BY created_at DESC LIMIT 200")])

    @app.post(PREFIX + "/ops/dead-letters/{letter_id}/replay")
    @mutate("ops")
    def replay(flow, db, user, raw, letter_id):
        row = one(db, "SELECT * FROM dead_letters WHERE id=? AND status='PARKED'", (letter_id,))
        require(row, "Parked dead letter not found.", "NOT_FOUND", 404)
        current = flow.app(db, row["reference_id"])
        payload = json.loads(row["payload"])
        if row["source"] == "PROVISIONING":
            flow.retry_provision(db, current, as_operator(user), clear_simulation=raw.get("clear_simulation") is True)
        elif row["source"] == "PROVIDER_CALL":
            flow.rerun(db, current, [payload["check_type"]], as_operator(user))
        elif row["source"] == "WORKFLOW_JOB":
            flow.job(db, current["id"], payload["job_type"], payload["data"])
        else:
            raise DomainError("REUPLOAD_REQUIRED", "Open an information request for a replacement document.")
        update(db, "dead_letters", "id=?", (letter_id,), status="REPLAYED", replayed_at=flow.now())
        audit(db, flow.now(), current["id"], "DEAD_LETTER_REPLAYED", as_operator(user), {"id": letter_id})
        return {"status": "REPLAYED"}

    @app.get(PREFIX + "/ops/audit/verify")
    def verify(request: Request):
        return ops_read(request, lambda flow, db: verify_audit(db), admin=True)

    @app.get(PREFIX + "/ops/metrics")
    def get_metrics(request: Request, to: float | None = None):
        try:
            start = float(request.query_params.get("from", "0"))
        except ValueError:
            raise DomainError("INVALID_WINDOW", "Use Unix seconds for the reporting window.", 422) from None
        require(math.isfinite(start) and (to is None or math.isfinite(to) and to >= start),
                "Use a valid reporting window.", "INVALID_WINDOW", 422)
        return ops_read(request, lambda flow, db: metrics(db, start, to if to is not None else flow.now(), flow.now()))

    @app.get(PREFIX + "/ops/config")
    def get_config(request: Request):
        return ops_read(request, lambda flow, db: flow.store.config(db), admin=True)

    @app.put(PREFIX + "/ops/config")
    @mutate("ops_admin")
    def update_config(flow, db, user, raw):
        previous = flow.store.config(db)
        config = apply_patch(previous, raw)
        validate_config(config, flow.store.defaults)
        db.execute("INSERT OR REPLACE INTO app_settings VALUES ('policy',?)", (json_text(config),))
        audit(db, flow.now(), None, "CONFIG_CHANGED", as_operator(user), {"sections": sorted(raw)})
        return config

    @app.post(PREFIX + "/ops/applications/{app_id}/pii/reveal")
    @mutate("ops_admin")
    def reveal(flow, db, user, raw, app_id):
        Reason.model_validate({"reason": raw.get("reason")})
        form = flow.form(flow.app(db, app_id))
        field = raw.get("field")
        require(field in ("declared_pan", "bank_account.account_number"), "Unknown reveal field.", "INVALID_FIELD", 422)
        value = form.get("declared_pan") if field == "declared_pan" else (form.get("bank_account") or {}).get("account_number")
        audit(db, flow.now(), app_id, "PII_VIEWED", as_operator(user), {"field": field, "reason_enc": flow.store.seal(raw["reason"])})
        return {"field": field, "value": value}

    @app.post(PREFIX + "/ops/simulate/scenario")
    @mutate("ops", code=202)
    def scenario(flow, db, user, raw):
        command = Scenario.model_validate(raw)
        path = ROOT / "fixtures/applications" / (command.name + ".json")
        require(path.is_file(), "Scenario not found.", "NOT_FOUND", 404)
        fixture = json.loads(path.read_text())
        if command.name == "duplicate_active":
            require(one(db, "SELECT id FROM applications WHERE state='ACTIVE' AND legal_name='Demo Saffron Private Limited'"),
                    "Run happy_pvt_ltd and wait for activation first.")
        form = Application.model_validate(fixture["application"]).model_dump()
        simulation = Simulation.model_validate(fixture.get("simulate", {})).model_dump(exclude_none=True)
        app_id = flow.create(db, form, command.owner, simulation)
        current = flow.app(db, app_id)
        for payload in fixture["documents"]:
            document = StructuredDocument.model_validate(payload)
            flow.upload(db, current, document.doc_type, document.payload, document.simulate.model_dump(exclude_none=True))
        flow.submit(db, current)
        return {"id": app_id, "name": command.name, "owner": command.owner, "state": "SUBMITTED"}

    return app


def validate_config(config, defaults):
    def compare(value, default):
        if isinstance(default, dict):
            return isinstance(value, dict) and value.keys() == default.keys() and all(compare(value[key], default[key]) for key in default)
        if isinstance(default, list):
            return isinstance(value, list) and (not default or all(compare(item, default[0]) for item in value))
        if type(default) is float:
            return type(value) in {int, float}
        return type(value) is type(default)

    require(compare(config, defaults), "Policy keys and value types must match the default configuration.", "INVALID_POLICY", 422)
    names, risk, doc = config["name_match"], config["risk"], config["extraction"]
    require(0 <= names["review_threshold"] <= names["pass_threshold"] <= 1 and
            0 <= risk["auto_approve_max_score"] <= risk["manual_review_max_score"] <= 100 and
            0 <= doc["field_confidence_review"] <= doc["field_confidence_auto_accept"] <= 1 and
            0 <= doc["doc_quality_min_score"] <= 1 and doc["mode"] == "structured_only" and
            1 <= doc["max_pages_per_doc"] <= 100 and 1 <= doc["max_file_mb"] <= 25,
            "Invalid validation or risk thresholds.", "INVALID_POLICY", 422)
    for checks in config["required_checks"].values():
        require(set(checks) <= CHECKS and {"PAN", "BANK", "RISK"} <= set(checks),
                "Required checks must include PAN, BANK, and RISK.", "INVALID_POLICY", 422)
    for documents in config["required_documents"].values():
        require(set(documents) <= FIELD_SCHEMA.keys(), "Unknown required document.", "INVALID_POLICY", 422)
    for provider in config["providers"].values():
        require(provider["mode"] in {"sync", "async"} and provider["backoff"] in {"fixed", "exponential"} and
                1 <= provider["max_attempts"] <= 10 and provider["timeout_sec"] > 0 and provider["base_delay_sec"] >= 0,
                "Invalid provider retry policy.", "INVALID_POLICY", 422)
    info = config["info_request"]
    require(info["max_rounds"] >= 1 and info["response_sla_hours"] > 0 and info["expiry_policy"] in {"review", "expire"}
            and all(hour > 0 for hour in info["reminder_hours"]), "Invalid information request policy.", "INVALID_POLICY", 422)
    require(config["late_callback_policy"] in {"accept_if_not_terminal", "always_record_only", "reopen_review"}
            and config["callback_signature"] == "hmac_sha256" and config["callback_clock_skew_sec"] > 0,
            "Invalid callback policy.", "INVALID_POLICY", 422)
    require(config["provisioning"]["mid_format"] == defaults["provisioning"]["mid_format"] and
            config["provisioning"]["compensation_on_capability_failure"] in {"retry_then_partial", "rollback_mid"} and
            all(config["provisioning"]["capabilities_by_category"].values()), "Invalid provisioning policy.", "INVALID_POLICY", 422)
    require(config["pii"]["send_raw_pii_to_llm"] is False and names["algorithm"] == "token_set_ratio"
            and config["pii"]["doc_retention_days"] >= 1,
            "The local demo supports token matching and keeps documents local.", "INVALID_POLICY", 422)


app = create_app()
