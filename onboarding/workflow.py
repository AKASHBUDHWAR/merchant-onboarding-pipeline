import base64
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone

from .documents import extract, inspect_file
from .domain import (BANK_TYPES, FIELD_SCHEMA, TERMINAL, DomainError, canonical, issue, name_score,
                     required_checks, require, similarity, transition, validate)
from .operators import Operators
from .providers import Providers
from .storage import audit, insert, json_text, many, one, uid, unjson, update


class Workflow(Providers, Operators):
    def __init__(self, store):
        self.store = store

    def now(self):
        return self.store.clock()

    def app(self, db, app_id, owner=None):
        row = one(db, """SELECT a.*, p.owner, p.data_enc, p.simulation, p.resume_state, p.held_at, p.pan_hash
                         FROM applications a JOIN application_payloads p ON p.application_id=a.id WHERE a.id=?""",
                  (app_id,))
        require(row and (owner is None or row["owner"] == owner), "Application not found.", "NOT_FOUND", 404)
        return row

    def form(self, app):
        return self.store.open(app["data_enc"])

    def event(self, db, app_id, kind, payload, actor=("SYSTEM", "workflow")):
        now = self.now()
        envelope = dict(event_id=uid(), event_type=kind, aggregate_id=app_id, occurred_at=now,
                        actor=dict(type=actor[0], id=actor[1]), correlation_id=app_id,
                        causation_id=None, payload=payload)
        insert(db, "outbox_events", aggregate_id=app_id, event_type=kind,
               payload=json_text(envelope), created_at=now)

    def move(self, db, app_id, rule, trigger, *, actor=("SYSTEM", "workflow"), reasons=(), guard=True):
        app = self.app(db, app_id)
        new_state = transition(app["state"], rule, guard=guard, resume_state=app["resume_state"])
        now = self.now()
        changes = dict(state=new_state, state_entered_at=now, updated_at=now, version=app["version"] + 1)
        if new_state in {"APPROVED", "REJECTED"}:
            changes["decided_at"] = now
        update(db, "applications", "id=? AND version=?", (app_id, app["version"]), **changes)
        insert(db, "state_transitions", application_id=app_id, from_state=app["state"], to_state=new_state,
               trigger_event=trigger, guard_results=json_text({"transition": rule, "guard": True}),
               actor_type=actor[0], actor_id=actor[1], reason_codes=json_text(list(reasons)), occurred_at=now)
        audit(db, now, app_id, "STATE_CHANGED", actor, before=app["state"], after=new_state)
        self.event(db, app_id, "ApplicationStateChanged",
                   dict(from_state=app["state"], to_state=new_state, trigger=trigger, rule=rule), actor)
        if new_state in TERMINAL - {"ACTIVE"}:
            db.execute("UPDATE scheduled_jobs SET status='CANCELLED' WHERE reference_id=? AND status='SCHEDULED' AND job_type<>'MOCK_CALLBACK'", (app_id,))
            db.execute("UPDATE info_requests SET status='CANCELLED' WHERE application_id=? AND status='OPEN'", (app_id,))
            db.execute("UPDATE review_tasks SET status='CANCELLED' WHERE application_id=? AND status NOT IN ('DECIDED','CANCELLED')", (app_id,))
        if new_state == "REJECTED":
            days = self.store.config(db)["pii"]["doc_retention_days"]
            self.job(db, app_id, "PURGE_DOCUMENTS", delay=days * 86400)
        if new_state == "APPROVED":
            self.job(db, app_id, "PROVISION_START")
        if new_state in {"ACTIVE", "REJECTED", "EXPIRED"} and rule != "T37":
            self.notify(db, app_id, new_state, f"Your application status is {new_state.lower()}.")
        return new_state

    def job(self, db, app_id, kind, data=None, delay=0):
        insert(db, "scheduled_jobs", id=uid(), job_type=kind, reference_id=app_id,
               run_at=self.now() + delay, payload=json_text(data or {}), status="SCHEDULED")

    def notify(self, db, app_id, kind, message):
        insert(db, "notification_outbox", application_id=app_id, event=kind, message=message, occurred_at=self.now())

    def exception(self, db, app_id, code, severity="BLOCKING", source="SYSTEM", details=None):
        existing = one(db, "SELECT id FROM exceptions WHERE application_id=? AND code=? AND source=? AND details=? AND status='OPEN'",
                       (app_id, code, source, json_text(details)))
        if existing:
            return existing["id"]
        exception_id = uid()
        insert(db, "exceptions", id=exception_id, application_id=app_id, code=code, severity=severity,
               source=source, details=json_text(details), status="OPEN", created_at=self.now())
        return exception_id

    def dead_letter(self, db, app_id, source, payload, code, attempts):
        letter_id = uid()
        insert(db, "dead_letters", id=letter_id, source=source, reference_id=app_id,
               payload=json_text(payload), error=code, attempts=attempts, status="PARKED",
               created_at=self.now())
        self.event(db, app_id, "DeadLettered", {"source": source, "id": letter_id, "code": code})

    def save_form(self, db, app_id, form, *, bank=True):
        pan = canonical(form.get("declared_pan"))
        update(db, "application_payloads", "application_id=?", (app_id,), data_enc=self.store.seal(form),
               pan_hash=self.store.fingerprint(pan) if pan else None)
        update(db, "applications", "id=?", (app_id,), business_type=form["business_type"],
               business_category=form["business_category"], legal_name=form["legal_name"],
               declared_pan=self.store.seal(pan) if pan else None, declared_gstin=canonical(form.get("declared_gstin")) or None,
               declared_cin=canonical(form.get("declared_cin")) or None, mcc=form.get("mcc"),
               trade_name=form.get("trade_name"), website_url=form.get("website_url"),
               annual_turnover_inr=form.get("annual_turnover_inr"),
               expected_monthly_gmv_inr=form.get("expected_monthly_gmv_inr"), avg_ticket_inr=form.get("avg_ticket_inr"),
               registered_address=self.store.seal(form["registered_address"]),
               operating_address=self.store.seal(form.get("operating_address")),
               contact=self.store.seal(form["contact"]), updated_at=self.now())
        db.execute("DELETE FROM persons WHERE application_id=?", (app_id,))
        for p in form.get("persons", []):
            last4 = p.get("aadhaar_last4")
            insert(db, "persons", id=uid(), application_id=app_id, role=p["role"], full_name=p["full_name"],
                   pan=self.store.seal(p["pan"]) if p.get("pan") else None,
                   dob=self.store.seal(p["dob"]) if p.get("dob") else None,
                   aadhaar_last4=self.store.seal(last4) if last4 else None, ownership_pct=p.get("ownership_pct"),
                   email=self.store.seal(p["email"]) if p.get("email") else None,
                   phone=self.store.seal(p["phone"]) if p.get("phone") else None, is_pep=p.get("is_pep"))
        if bank and form.get("bank_account"):
            self.save_bank(db, app_id, form["bank_account"])

    def save_bank(self, db, app_id, bank):
        previous = one(db, "SELECT * FROM bank_accounts WHERE application_id=? AND is_active=1", (app_id,))
        account = canonical(bank.get("account_number"))
        fingerprint = self.store.fingerprint(account)
        if previous and previous["account_number_hash"] == fingerprint and previous["ifsc"] == canonical(bank.get("ifsc")) and previous["account_holder_name"] == bank.get("account_holder_name"):
            return
        new_id = uid()
        insert(db, "bank_accounts", id=new_id, application_id=app_id, account_holder_name=bank.get("account_holder_name") or "",
               account_number_enc=self.store.seal(account), account_number_hash=fingerprint,
               account_number_last4=account[-4:], ifsc=canonical(bank.get("ifsc")), bank_name=bank.get("bank_name"),
               account_type=bank.get("account_type"), created_at=self.now())
        if previous:
            update(db, "bank_accounts", "id=?", (previous["id"],), is_active=0, superseded_by=new_id)

    def create(self, db, form, owner, simulation=None):
        app_id = uid()
        now = self.now()
        sequence = db.execute("SELECT COALESCE(MAX(rowid),0)+1 FROM applications").fetchone()[0]
        ref = f"APP-{datetime.fromtimestamp(now, timezone.utc).year}-{sequence:06d}"
        insert(db, "applications", id=app_id, reference_no=ref, channel=form["channel"],
               business_type=form["business_type"], business_category=form["business_category"],
               legal_name=form["legal_name"], registered_address="", contact="", state="DRAFT",
               state_entered_at=now, created_at=now, updated_at=now)
        insert(db, "application_payloads", application_id=app_id, owner=owner,
               data_enc=self.store.seal(form), simulation=json_text(simulation or {}))
        self.save_form(db, app_id, form)
        self.event(db, app_id, "ApplicationCreated", {"reference_no": ref}, ("MERCHANT", owner))
        audit(db, now, app_id, "APPLICATION_CREATED", ("MERCHANT", owner))
        return app_id

    def upload(self, db, app, kind, payload, simulation, file=None, mime=None):
        require(app["state"] in {"DRAFT", "AWAITING_MERCHANT_INFO"}, "Documents cannot be uploaded now.")
        require(kind in FIELD_SCHEMA, "Unknown document type.", "INVALID_DOCUMENT", 422)
        payload = {key: payload[key] for key in FIELD_SCHEMA[kind] if key in payload}
        app_id, doc_id = app["id"], uid()
        replacement_kinds = sorted(BANK_TYPES) if kind in BANK_TYPES else [kind]
        placeholders = ",".join("?" for _ in replacement_kinds)
        previous = one(db, f"""SELECT * FROM documents WHERE application_id=? AND doc_type IN ({placeholders})
                               AND status<>'SUPERSEDED' ORDER BY version DESC LIMIT 1""",
                       (app_id, *replacement_kinds))
        pages, status, uri, digest = None, "UPLOADED", None, None
        if file is not None:
            pages, status = inspect_file(file, mime, self.store.config(db)["extraction"])
            uri = f"docs/{app_id}/{doc_id}.enc"
            digest = hashlib.sha256(file).hexdigest()
        insert(db, "documents", id=doc_id, application_id=app_id, doc_type=kind, declared_doc_type=kind,
               source="STRUCTURED_PAYLOAD" if file is None else "FILE", storage_uri=uri, mime_type=mime,
               sha256=digest, page_count=pages, status=status, version=previous["version"] + 1 if previous else 1,
               replaces_document=previous["id"] if previous else None, uploaded_at=self.now())
        insert(db, "document_payloads", document_id=doc_id, payload_enc=self.store.seal(payload),
               simulation=json_text(simulation))
        if previous:
            update(db, "documents", "id=?", (previous["id"],), status="SUPERSEDED")
        if uri:
            path = self.store.directory / uri
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text(self.store.seal(base64.b64encode(file).decode()))
            path.chmod(0o600)
        self.event(db, app_id, "DocumentUploaded", {"document_id": doc_id, "doc_type": kind})
        audit(db, self.now(), app_id, "DOCUMENT_UPLOADED", ("MERCHANT", app["owner"]), target={"document_id": doc_id})
        return {"id": doc_id, "status": status}

    def submit(self, db, app):
        has_docs = one(db, "SELECT id FROM documents WHERE application_id=? AND status<>'SUPERSEDED'", (app["id"],))
        self.move(db, app["id"], "T01", "ApplicationSubmitted", actor=("MERCHANT", app["owner"]), guard=bool(has_docs))
        now = self.now()
        update(db, "applications", "id=?", (app["id"],), submitted_at=now)
        self.job(db, app["id"], "EXTRACT")

    def process_docs(self, db, app, attempt=1):
        app_id = app["id"]
        if app["state"] == "SUBMITTED":
            self.move(db, app_id, "T02", "ExtractDocuments")
        elif app["state"] != "DOC_PROCESSING":
            return
        cfg = self.store.config(db)
        documents = many(db, """SELECT d.*, p.payload_enc, p.simulation FROM documents d JOIN document_payloads p
                                ON p.document_id=d.id WHERE d.application_id=? AND d.status<>'SUPERSEDED'""", (app_id,))
        problems = []
        for doc in documents:
            if doc["status"] in {"EXTRACTED", "STRUCTURED"}:
                continue
            status, quality, kind, fields = extract(doc, self.store.open(doc["payload_enc"]),
                                                  json.loads(doc["simulation"]), cfg["extraction"])
            update(db, "documents", "id=?", (doc["id"],), detected_doc_type=kind, quality_score=quality, status=status)
            self.event(db, app_id, "DocumentClassified", {"document_id": doc["id"], "type": kind, "quality": quality})
            for key, entry in fields.items():
                value = self.store.seal(entry["value"])
                insert(db, "extracted_fields", id=uid(), document_id=doc["id"], application_id=app_id,
                       field_name=key, raw_value=value, normalized_value=value, confidence=entry["confidence"],
                       extractor="structured", status=entry["status"], created_at=self.now())
            if fields:
                self.event(db, app_id, "FieldsExtracted", {"document_id": doc["id"], "fields": [
                    {"name": key, "confidence": entry["confidence"], "status": entry["status"]} for key, entry in fields.items()]})
            if status == "TAMPER_SUSPECTED":
                self.exception(db, app_id, "DOC_TAMPER_SUSPECTED", "CRITICAL", "EXTRACTION")
                self.review(db, app_id, "T05", "TAMPER_SUSPECTED", "HIGH")
                return
            if status == "EXTRACTION_ERROR":
                if attempt < 3:
                    self.job(db, app_id, "EXTRACT", {"attempt": attempt + 1}, delay=2 ** attempt)
                else:
                    self.exception(db, app_id, "EXTRACTION_SYSTEM_ERROR", source="EXTRACTION")
                    self.dead_letter(db, app_id, "EXTRACTION", {}, "EXTRACTION_SYSTEM_ERROR", attempt)
                    self.review(db, app_id, "T05", "EXTRACTION_FALLBACK")
                return
            if status not in {"STRUCTURED", "EXTRACTED"}:
                problems.append(issue("DOC_" + status, doc_type=doc["doc_type"]))
        if problems:
            for problem in problems:
                self.exception(db, app_id, problem["code"], source="EXTRACTION", details=problem)
            if app["info_request_round"] < cfg["info_request"]["max_rounds"]:
                self.ask_info(db, app_id, "T04", problems)
            else:
                self.review(db, app_id, "T05", "DOC_REVIEW")
            return
        self.move(db, app_id, "T03", "AllDocumentsProcessed")
        self.job(db, app_id, "VALIDATE")

    def extracted(self, db, app_id):
        result = {}
        for doc in many(db, "SELECT id,doc_type FROM documents WHERE application_id=? AND status IN ('STRUCTURED','EXTRACTED')", (app_id,)):
            fields = {}
            for f in many(db, "SELECT * FROM extracted_fields WHERE document_id=?", (doc["id"],)):
                fields[f["field_name"]] = dict(value=self.store.open(f["normalized_value"]),
                                               confidence=f["confidence"], status=f["status"])
            result[doc["doc_type"]] = {"id": doc["id"], "fields": fields}
        return result

    def duplicate_matches(self, db, app, form, cfg):
        settings = cfg["duplicate_detection"]
        if not settings["enabled"]:
            return []
        matches = []
        person_fingerprints = {self.store.fingerprint(canonical(p["pan"])) for p in form.get("persons", [])
                               if p.get("pan") and p["role"] in {"PARTNER", "DIRECTOR"}}
        current_bank = one(db, "SELECT account_number_hash FROM bank_accounts WHERE application_id=? AND is_active=1", (app["id"],))
        for row in many(db, """SELECT a.id, a.state FROM applications a WHERE a.id<>?
                                AND (a.state='ACTIVE' OR (a.state NOT IN ('DRAFT','REJECTED','WITHDRAWN','EXPIRED','DUPLICATE_MERGED')
                                AND a.rowid < (SELECT rowid FROM applications WHERE id=?)))""", (app["id"], app["id"])):
            other = self.app(db, row["id"])
            previous = self.form(other)
            keys = {}
            if app["pan_hash"] and app["pan_hash"] == other["pan_hash"]:
                keys["pan"] = 1
            if app["declared_gstin"] and app["declared_gstin"] == other["declared_gstin"]:
                keys["gstin"] = 1
            bank = one(db, "SELECT account_number_hash FROM bank_accounts WHERE application_id=? AND is_active=1", (other["id"],))
            if current_bank and bank and current_bank == bank:
                keys["bank_account_hash"] = 0.9
            if one(db, """SELECT d.id FROM documents d JOIN documents o ON d.sha256=o.sha256 WHERE
                           d.application_id=? AND o.application_id=? AND d.status<>'SUPERSEDED' AND o.status<>'SUPERSEDED' LIMIT 1""",
                   (app["id"], other["id"])):
                keys["document_sha256"] = 0.9
            if any(self.store.fingerprint(canonical(p["pan"])) in person_fingerprints
                   for p in previous.get("persons", []) if p.get("pan") and p["role"] in {"PARTNER", "DIRECTOR"}):
                keys["shared_director_or_partner"] = 0.6
            same_pin = form["registered_address"].get("pincode") == previous["registered_address"].get("pincode")
            if same_pin and name_score(form["legal_name"], previous["legal_name"], cfg["name_match"]) >= settings["fuzzy"]["business_name_threshold"]:
                keys["name_and_pincode"] = 0.7
            for key in ("phone", "email"):
                value = str(form["contact"].get(key) or "").casefold()
                if settings["fuzzy"].get(key + "_exact") and value and value == str(previous["contact"].get(key) or "").casefold():
                    keys[key] = 0.5
            address = form["registered_address"].get("line1", "").casefold()
            other_address = previous["registered_address"].get("line1", "").casefold()
            if same_pin and similarity(address, other_address) >= settings["fuzzy"]["address_threshold"]:
                keys["address"] = 0.4
            if not keys:
                continue
            remaining = 1
            for weight in keys.values():
                remaining *= 1 - weight
            second_outlet = (set(keys) & {"pan", "gstin", "bank_account_hash", "document_sha256"} == {"pan"}
                             and app["declared_gstin"] and other["declared_gstin"]
                             and app["declared_gstin"][:2] != other["declared_gstin"][:2])
            exact = bool(set(settings["exact_keys"]) & keys.keys()) and not second_outlet
            matches.append(dict(application_id=other["id"], state=other["state"], keys=list(keys),
                                score=round(1 - remaining, 4), exact=exact))
        return matches

    def run_validation(self, db, app):
        if app["state"] != "VALIDATING":
            return
        app_id = app["id"]
        cfg, form = self.store.config(db), self.form(app)
        problems, reconciliation = validate(form, self.extracted(db, app_id), cfg)
        override_fields = {row["field_name"] for row in many(db, """SELECT f.field_name FROM extracted_fields f
            JOIN documents d ON d.id=f.document_id WHERE f.application_id=? AND d.status<>'SUPERSEDED'
            AND f.status='OPERATOR_OVERRIDDEN'""", (app_id,))}
        if override_fields:
            problems.append(issue("OPERATOR_OVERRIDDEN", severity="BLOCKING", review=True))
        db.execute("DELETE FROM field_reconciliation WHERE application_id=?", (app_id,))
        for doc_id, key, expected, observed, score, matched in reconciliation:
            db.execute("""INSERT INTO field_reconciliation VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(application_id,field_name) DO UPDATE SET match_score=MIN(excluded.match_score,match_score),
                       status=CASE WHEN excluded.status='MISMATCH' THEN 'MISMATCH' ELSE status END""",
                       (app_id, key, self.store.seal(expected), self.store.seal([{"document_id": doc_id, "value": observed}]),
                        None, self.store.seal(expected) if matched else None, score, "MATCH" if matched else "MISMATCH"))
        self.resolve_exceptions(db, app_id, ("VALIDATION", "EXTRACTION", "DEDUPE"))
        for problem in problems:
            self.exception(db, app_id, problem["code"], problem["severity"], "VALIDATION", problem)
        blockers = [problem for problem in problems if problem["severity"] == "BLOCKING"]
        fixable = [problem for problem in blockers if not problem["review"]]
        if fixable:
            self.event(db, app_id, "ValidationFailed", {"codes": [problem["code"] for problem in fixable]})
            if app["info_request_round"] < cfg["info_request"]["max_rounds"] and not (
                    app["info_request_round"] > 0 and any(p["code"] == "FIELD_CONFLICT_FORM_DOC" for p in fixable)):
                self.ask_info(db, app_id, "T07", fixable)
            else:
                self.review(db, app_id, "T08", "FIELD_CONFIRMATION")
            return
        matches = self.duplicate_matches(db, app, form, cfg)
        if matches:
            self.event(db, app_id, "DuplicateDetected", {"matches": matches})
            active = next((m for m in matches if m["exact"] and m["state"] == "ACTIVE"), None)
            code = "DUP_ACTIVE_MERCHANT" if active else "DUP_INFLIGHT_APPLICATION" if any(m["exact"] for m in matches) else "DUP_FUZZY_MATCH"
            self.exception(db, app_id, code, "BLOCKING", "DEDUPE", {"matches": matches})
            if any("bank_account_hash" in m["keys"] and "pan" not in m["keys"] for m in matches):
                self.exception(db, app_id, "SHARED_BANK_ACCT", "CRITICAL", "DEDUPE")
            if active and cfg["duplicate_detection"]["action_on_exact_active_merchant"] == "BLOCK":
                self.move(db, app_id, "T09", "DuplicateDetected", reasons=[code])
            else:
                self.review(db, app_id, "T08", "DUPLICATE_REVIEW")
            return
        if any(p["review"] and p["code"] != "OPERATOR_OVERRIDDEN" for p in blockers):
            self.review(db, app_id, "T08", "NAME_MISMATCH")
            return
        self.event(db, app_id, "ValidationPassed", {})
        self.move(db, app_id, "T06", "ValidationPassed")
        checks = required_checks(form, cfg)
        if canonical(form.get("declared_gstin")):
            checks.add("GST")
        self.reset_checks(db, app_id, checks)

    def resolve_exceptions(self, db, app_id, sources):
        for source in sources:
            update(db, "exceptions", "application_id=? AND source=? AND status='OPEN'",
                   (app_id, source), status="RESOLVED", resolved_at=self.now())

    def ask_info(self, db, app_id, rule, items, actor=("SYSTEM", "workflow")):
        app = self.app(db, app_id)
        policy = self.store.config(db)["info_request"]
        require(app["info_request_round"] < policy["max_rounds"], "Information request limit reached.")
        round_no, rid, now = app["info_request_round"] + 1, uid(), self.now()
        due = now + policy["response_sla_hours"] * 3600
        public = [{k: item[k] for k in ("field", "doc_type", "message", "kind") if item.get(k)} for item in items]
        for item in public:
            item["item_id"] = uid()
            item.setdefault("kind", "DOCUMENT" if item.get("doc_type") and not item.get("field") else "FIELD" if item.get("field") else "CLARIFICATION")
        self.move(db, app_id, rule, "InfoRequested", actor=actor)
        update(db, "applications", "id=?", (app_id,), info_request_round=round_no)
        insert(db, "info_requests", id=rid, application_id=app_id, round_no=round_no,
               items=json_text(public), status="OPEN", due_at=due, created_at=now)
        self.event(db, app_id, "InfoRequested", {"id": rid, "round": round_no})
        self.notify(db, app_id, "INFO_REQUESTED", "Please provide the requested information.")
        for hour in policy["reminder_hours"]:
            if hour < policy["response_sla_hours"]:
                self.job(db, app_id, "INFO_REMINDER", {"id": rid}, hour * 3600)
        self.job(db, app_id, "INFO_EXPIRY", {"id": rid}, due - now)
        return rid

    def respond(self, db, app, rid, form, docs, confirmations=None):
        row = one(db, "SELECT * FROM info_requests WHERE id=? AND application_id=? AND status='OPEN'", (rid, app["id"]))
        require(row and app["state"] == "AWAITING_MERCHANT_INFO" and row["due_at"] >= self.now(),
                "Information request is closed.")
        if form:
            previous_form = self.form(app)
            self.save_form(db, app["id"], form)
            sim = json.loads(app["simulation"])
            if canonical(previous_form.get("declared_gstin")) != canonical(form.get("declared_gstin")) and sim.get("GST", {}).get("result_code") == "GST_CANCELLED":
                sim.pop("GST")
                update(db, "application_payloads", "application_id=?", (app["id"],), simulation=json_text(sim))
        for doc in docs:
            self.upload(db, app, doc.doc_type, doc.payload, doc.simulate.model_dump(exclude_none=True))
        for field in confirmations or []:
            db.execute("""UPDATE extracted_fields SET status='MERCHANT_CONFIRMED'
                          WHERE application_id=? AND field_name=? AND status IN ('LOW_CONFIDENCE','NEEDS_CONFIRMATION')""",
                       (app["id"], field))
        if not docs:
            docs = many(db, """SELECT id FROM documents WHERE application_id=? AND uploaded_at>=?
                               AND status IN ('UPLOADED','PASSWORD_PROTECTED')""", (app["id"], row["created_at"]))
        update(db, "info_requests", "id=?", (rid,), status="FULFILLED", fulfilled_at=self.now())
        actor = ("MERCHANT", app["owner"])
        if docs:
            self.move(db, app["id"], "T10", "MerchantInfoSubmitted", actor=actor)
            self.job(db, app["id"], "EXTRACT")
        else:
            cfg = self.store.config(db)
            stored_form = self.form(self.app(db, app["id"]))
            origin = one(db, """SELECT from_state FROM state_transitions WHERE application_id=?
                                  AND to_state='AWAITING_MERCHANT_INFO' ORDER BY id DESC LIMIT 1""", (app["id"],))
            problems, _ = validate(stored_form, self.extracted(db, app["id"]), cfg)
            if origin and origin["from_state"] == "VERIFICATION_IN_PROGRESS" and not any(
                    p["severity"] == "BLOCKING" for p in problems) and not self.duplicate_matches(db, self.app(db, app["id"]), stored_form, cfg):
                self.move(db, app["id"], "T12", "MerchantInfoSubmitted", actor=actor)
                self.reset_checks(db, app["id"], required_checks(stored_form, cfg))
            else:
                self.move(db, app["id"], "T11", "MerchantInfoSubmitted", actor=actor)
                self.job(db, app["id"], "VALIDATE")
        self.event(db, app["id"], "MerchantInfoSubmitted", {"id": rid}, actor)

    def info_job(self, db, app, job_kind, rid):
        if app["state"] != "AWAITING_MERCHANT_INFO":
            return
        info = one(db, "SELECT * FROM info_requests WHERE id=? AND status='OPEN'", (rid,))
        if not info:
            return
        if job_kind == "INFO_REMINDER":
            self.notify(db, app["id"], job_kind, "Your information request is still open.")
        else:
            update(db, "info_requests", "id=?", (rid,), status="EXPIRED")
            cfg = self.store.config(db)["info_request"]
            if cfg.get("expiry_policy") == "review":
                self.review(db, app["id"], "T14", "FIELD_CONFIRMATION")
            else:
                self.move(db, app["id"], "T13", "InfoRequestExpired", reasons=["INFO_NOT_PROVIDED"])

    def purge_documents(self, db, app):
        if app["state"] != "REJECTED":
            return
        app_id = app["id"]
        docs = many(db, "SELECT id,storage_uri FROM documents WHERE application_id=? AND status<>'PURGED'", (app_id,))
        for doc in docs:
            if doc["storage_uri"]:
                (self.store.directory / doc["storage_uri"]).unlink(missing_ok=True)
            db.execute("DELETE FROM document_payloads WHERE document_id=?", (doc["id"],))
            update(db, "documents", "id=?", (doc["id"],), storage_uri=None, sha256=None, status="PURGED")
        db.execute("DELETE FROM extracted_fields WHERE application_id=?", (app_id,))
        db.execute("DELETE FROM field_reconciliation WHERE application_id=?", (app_id,))
        audit(db, self.now(), app_id, "DOCUMENTS_PURGED", target={"count": len(docs)})

    def tick(self, limit=50):
        completed = 0
        for _ in range(limit):
            with self.store.transaction() as db:
                row = one(db, """SELECT j.* FROM scheduled_jobs j JOIN applications a ON a.id=j.reference_id
                    WHERE j.status='SCHEDULED' AND j.run_at<=? AND (a.state<>'ON_HOLD' OR j.job_type='MOCK_CALLBACK')
                    ORDER BY j.run_at,j.rowid LIMIT 1""", (self.now(),))
                if not row:
                    break
                app = self.app(db, row["reference_id"])
                data, kind = json.loads(row["payload"]), row["job_type"]
                db.execute("SAVEPOINT run_job")
                try:
                    if kind == "EXTRACT":
                        self.process_docs(db, app, **data)
                    elif kind == "VALIDATE":
                        self.run_validation(db, app)
                    elif kind.startswith("INFO_"):
                        self.info_job(db, app, kind, data["id"])
                    elif kind == "PURGE_DOCUMENTS":
                        self.purge_documents(db, app)
                    elif kind == "REVIEW_SLA":
                        db.execute("UPDATE review_tasks SET status='ESCALATED' WHERE id=? AND status IN ('OPEN','ASSIGNED','IN_PROGRESS')",
                                   (data["task_id"],))
                    else:
                        self.provider_job(db, app, kind, data)
                except (sqlite3.Error, ValueError, KeyError, TypeError, OSError, DomainError) as error:
                    db.execute("ROLLBACK TO run_job")
                    self.dead_letter(db, app["id"], "WORKFLOW_JOB", {"job_type": kind, "data": data}, type(error).__name__, 1)
                    logging.getLogger(__name__).error("Job %s failed for application %s (%s)", kind, app["id"], type(error).__name__)
                    update(db, "scheduled_jobs", "id=?", (row["id"],), status="FAILED")
                else:
                    update(db, "scheduled_jobs", "id=?", (row["id"],), status="DONE")
                db.execute("RELEASE run_job")
                db.execute("UPDATE outbox_events SET published_at=? WHERE published_at IS NULL", (self.now(),))
                completed += 1
        if not completed:
            with self.store.transaction() as db:
                db.execute("UPDATE outbox_events SET published_at=? WHERE published_at IS NULL", (self.now(),))
        return completed
