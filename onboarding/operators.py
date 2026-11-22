import json
import re

from .domain import CHECKS, DONE, IFSC, TERMINAL, canonical, require, required_checks
from .storage import audit, insert, json_text, many, one, uid, unjson, update

UNDECIDED = "status NOT IN ('DECIDED','CANCELLED')"
SENSITIVE = {"pan", "declared_pan", "account_number", "dob", "dob_or_doi", "aadhaar_last4",
             "email", "phone", "gstin", "declared_gstin"}


def mask(value, kind):
    if value is None:
        return None
    value = str(value)
    if kind in {"pan", "declared_pan"}:
        return value[:2] + "******" + value[-1:]
    if kind in {"account_number", "aadhaar_last4", "phone", "gstin", "declared_gstin"}:
        return "****" + value[-4:]
    return "****"


class Operators:
    def create_task(self, db, app_id, task_type, priority="MEDIUM", context=None):
        existing = one(db, f"""SELECT id FROM review_tasks WHERE application_id=? AND task_type=? AND {UNDECIDED}""",
                       (app_id, task_type))
        if existing:
            return existing["id"]
        now, task_id = self.now(), uid()
        cfg = self.store.config(db)["review"]
        due = now + cfg["sla_hours"][priority] * 3600
        exceptions = [row["id"] for row in many(db, "SELECT id FROM exceptions WHERE application_id=? AND status='OPEN'", (app_id,))]
        assignment = None
        reviewers = ("reviewer-1", "reviewer-2")
        if cfg["auto_assign"] == "round_robin":
            count = db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0]
            assignment = reviewers[count % len(reviewers)]
        elif cfg["auto_assign"] == "least_loaded":
            assignment = min(reviewers, key=lambda actor: db.execute(
                f"SELECT COUNT(*) FROM review_tasks WHERE assigned_to=? AND {UNDECIDED}", (actor,)).fetchone()[0])
        insert(db, "review_tasks", id=task_id, application_id=app_id, task_type=task_type,
               priority=priority, status="ASSIGNED" if assignment else "OPEN", assigned_to=assignment,
               exception_ids=json_text(exceptions), context=json_text(context or {"summary": task_type.replace("_", " ").title()}),
               sla_due_at=due, created_at=now)
        update(db, "applications", "id=?", (app_id,), priority=priority)
        self.job(db, app_id, "REVIEW_SLA", {"task_id": task_id}, due - now)
        self.event(db, app_id, "ReviewTaskCreated", {"id": task_id, "type": task_type})
        return task_id

    def review(self, db, app_id, rule, task_type, priority="MEDIUM"):
        self.move(db, app_id, rule, "ReviewTaskCreated", reasons=[task_type])
        return self.create_task(db, app_id, task_type, priority)

    def task(self, db, task_id):
        task = one(db, "SELECT * FROM review_tasks WHERE id=?", (task_id,))
        require(task, "Review task not found.", "NOT_FOUND", 404)
        return task

    def close_tasks(self, db, app_id, exclude=None):
        db.execute(f"UPDATE review_tasks SET status='CANCELLED' WHERE application_id=? AND {UNDECIDED} AND id<>COALESCE(?, '')",
                   (app_id, exclude))

    def claim(self, db, task, actor):
        require(task["status"] in {"OPEN", "ASSIGNED", "IN_PROGRESS", "ESCALATED"}, "Task cannot be claimed.")
        require(task["assigned_to"] in {None, actor[1]} or task["status"] == "ESCALATED" or actor[1] == "admin",
                "Task is assigned to another reviewer.")
        update(db, "review_tasks", "id=?", (task["id"],), assigned_to=actor[1], status="IN_PROGRESS")
        audit(db, self.now(), task["application_id"], "REVIEW_CLAIMED", actor, {"id": task["id"]})

    def review_action(self, db, task, command, actor, role):
        task_id, app_id = task["id"], task["application_id"]
        app = self.app(db, app_id)
        change = self.pending_bank_change(db, app_id)
        allow_status = {"OPEN", "ASSIGNED", "IN_PROGRESS", "ESCALATED", "AWAITING_SECOND_REVIEW"}
        require(task["status"] in allow_status, "Review task is closed.")
        action = command["decision"]
        if action != "CONFIRM":
            require(task["assigned_to"] in {None, actor[1]} or role == "ops_admin"
                    or task["status"] == "ESCALATED", "Task is assigned to another reviewer.")
        if action == "CONFIRM":
            proposal = one(db, "SELECT * FROM review_proposals WHERE task_id=?", (task_id,))
            require(role == "ops_admin" and task["status"] == "AWAITING_SECOND_REVIEW" and proposal
                    and proposal["actor"] != actor[1], "A different administrator must confirm.", "FORBIDDEN", 403)
            command = self.store.open(proposal["payload"])
            action = command["decision"]
        else:
            require(task["status"] != "AWAITING_SECOND_REVIEW", "Task is awaiting second review.")
        code_list = command["reason_codes"]
        require(bool(code_list) and bool(command["notes"].strip()), "Reason codes and notes are required.", "REASON_REQUIRED", 422)
        actor_type = ("OPERATOR", actor[1])
        hard_check = one(db, """SELECT id FROM verification_checks WHERE application_id=? AND superseded_by IS NULL
                                AND result_code IN ('PAN_INVALID','PAN_DEACTIVATED','PAN_NOT_FOUND')""", (app_id,))
        two = self.store.config(db)["review"]["four_eyes_required_for"]
        needs_second = (action == "MANUAL_MID" or
                        (action == "APPROVE" and task["task_type"] == "BANK_CHANGE_REVIEW" and "BANK_CHANGE_AFTER_APPROVAL" in two) or
                        (action == "APPROVE" and task["task_type"] == "RISK_REVIEW" and "RISK_OVERRIDE" in two) or
                        (action == "WAIVE_CHECK" and hard_check and "HARD_REJECT_OVERRIDE" in two))
        if needs_second and task["status"] != "AWAITING_SECOND_REVIEW":
            insert(db, "review_proposals", task_id=task_id, actor=actor[1], payload=self.store.seal(command))
            update(db, "review_tasks", "id=?", (task_id,), status="AWAITING_SECOND_REVIEW", decision=action)
            audit(db, self.now(), app_id, "SECOND_REVIEW_REQUESTED", actor_type, {"id": task_id, "decision": action})
            return "AWAITING_SECOND_REVIEW"
        if task["status"] == "AWAITING_SECOND_REVIEW":
            update(db, "review_tasks", "id=?", (task_id,), second_reviewer=actor[1])
        if action == "ESCALATE":
            update(db, "review_tasks", "id=?", (task_id,), status="ESCALATED", priority="HIGH",
                   decision_notes=self.store.seal(command["notes"]))
            audit(db, self.now(), app_id, "REVIEW_ESCALATED", actor_type, {"id": task_id, "codes": code_list})
            return "ESCALATED"
        update(db, "review_tasks", "id=?", (task_id,), status="DECIDED", decision=action,
               decision_notes=self.store.seal(command["notes"]), decided_at=self.now())
        if task["task_type"] == "BANK_CHANGE_REVIEW":
            require(change and action in {"APPROVE", "REJECT"}, "Choose approve or reject for a settlement bank change.")
            if action == "APPROVE":
                require(change["status"] == "PENDING_REVIEW", "The new settlement bank check has not passed.")
                form = self.form(app)
                form["bank_account"] = self.store.open(change["new_value"])["bank"]
                self.save_form(db, app_id, form)
            update(db, "change_requests", "id=?", (change["id"],), status="APPROVED" if action == "APPROVE" else "REJECTED")
            self.event(db, app_id, "SettlementBankChangeDecided", {"change_request_id": change["id"], "decision": action})
        elif action == "APPROVE":
            cfg = self.store.config(db)
            needed = required_checks(self.form(app), cfg)
            checks = many(db, "SELECT * FROM verification_checks WHERE application_id=? AND superseded_by IS NULL", (app_id,))
            require(needed.issubset({c["check_type"] for c in checks if c["status"] in DONE}),
                    "Required checks must pass or receive an authorized waiver.")
            require(not one(db, f"SELECT id FROM review_tasks WHERE application_id=? AND {UNDECIDED}", (app_id,)),
                    "Resolve other review tasks first.")
            self.move(db, app_id, "T22", "ReviewDecided", actor=actor_type, reasons=code_list)
        elif action == "REJECT":
            self.move(db, app_id, "T24", "ReviewDecided", actor=actor_type, reasons=code_list)
        elif action == "REQUEST_INFO":
            require(command.get("info_items"), "At least one information item is required.", "INVALID_INFO_ITEMS", 422)
            self.close_tasks(db, app_id)
            self.ask_info(db, app_id, "T23", command["info_items"], actor_type)
        elif action == "RERUN_CHECKS":
            self.rerun(db, app, command.get("rerun_checks"), actor_type)
        elif action == "MERGE":
            target = self.app(db, command.get("merge_into"))
            require(target["id"] != app_id and target["state"] not in TERMINAL, "Merge target must be another application in progress.")
            require(one(db, """SELECT id FROM exceptions WHERE application_id=? AND source='DEDUPE'
                               AND EXISTS (SELECT 1 FROM json_each(details,'$.matches')
                               WHERE json_extract(value,'$.application_id')=?)""", (app_id, target["id"])),
                    "Merge target is not a recorded duplicate.")
            update(db, "applications", "id=?", (app_id,), duplicate_of=target["id"])
            self.move(db, app_id, "T26", "ReviewDecided", actor=actor_type, reasons=code_list)
        elif action == "HOLD":
            self.hold(db, app, actor_type)
        elif action == "WAIVE_CHECK":
            require(role == "ops_admin", "Only an administrator may waive a check.", "FORBIDDEN", 403)
            self.waive(db, command.get("check_id"), actor_type, app_id)
            self.create_task(db, app_id, "WAIVER_REVIEW")
        elif action == "MANUAL_MID":
            mid = command.get("merchant_id") or ""
            require(re.fullmatch(r"[A-Z0-9-]{5,40}", mid), "Provide a valid merchant ID.", "INVALID_MID", 422)
            require(not one(db, "SELECT id FROM merchant_accounts WHERE merchant_id=? AND application_id<>?", (mid, app_id)),
                    "Merchant ID is already in use.", "DUPLICATE_MID")
            update(db, "applications", "id=?", (app_id,), merchant_id=mid)
            update(db, "merchant_accounts", "application_id=?", (app_id,), merchant_id=mid,
                   activated_at=self.now(), provisioning_status="ACTIVE")
            self.move(db, app_id, "T31", "OpsManualMidAssigned", actor=actor_type, reasons=code_list)
        elif action == "ROLLBACK":
            update(db, "merchant_accounts", "application_id=?", (app_id,), merchant_id=None, capabilities="[]",
                   provisioning_status="ROLLED_BACK")
            update(db, "applications", "id=?", (app_id,), merchant_id=None)
            self.review(db, app_id, "T32", "PROVISIONING_FAILURE")
        self.event(db, app_id, "ReviewDecided", {"id": task_id, "decision": action, "codes": code_list}, actor_type)
        audit(db, self.now(), app_id, "REVIEW_DECIDED", actor_type,
              {"id": task_id, "decision": action, "codes": code_list})
        return "DECIDED"

    def rerun(self, db, app, kinds, actor):
        if not kinds:
            kinds = required_checks(self.form(app), self.store.config(db))
        kinds = set(kinds)
        require(kinds <= CHECKS, "Unknown check type.", "INVALID_CHECK_TYPE", 422)
        require(app["state"] in {"MANUAL_REVIEW", "VERIFICATION_IN_PROGRESS"}, "Checks cannot be rerun now.")
        if app["state"] == "MANUAL_REVIEW":
            require(one(db, "SELECT id FROM verification_checks WHERE application_id=?", (app["id"],)),
                    "Complete document validation before rerunning verification.")
            self.close_tasks(db, app["id"])
            self.move(db, app["id"], "T25", "ReviewDecided", actor=actor)
        self.reset_checks(db, app["id"], kinds, force=True)
        audit(db, self.now(), app["id"], "CHECKS_RERUN", actor, after=sorted(kinds))

    def waive(self, db, check_id, actor, app_id=None):
        check = one(db, "SELECT * FROM verification_checks WHERE id=? AND superseded_by IS NULL", (check_id,))
        require(check and (app_id is None or check["application_id"] == app_id), "Check not found.", "NOT_FOUND", 404)
        app = self.app(db, check["application_id"])
        require(app["state"] == "MANUAL_REVIEW" and check["status"] in {"FAILED", "INCONCLUSIVE", "TIMED_OUT", "ERROR"},
                "Check cannot be waived.")
        require(check["check_type"] != "RISK" and check["result_code"] not in {"PAN_INVALID", "PAN_DEACTIVATED", "PAN_NOT_FOUND"},
                "Hard-reject and risk checks cannot be waived.", "FORBIDDEN", 403)
        update(db, "verification_checks", "id=?", (check_id,), status="WAIVED",
               waived_by=actor[1], waiver_reason="See audit log")
        audit(db, self.now(), app["id"], "CHECK_WAIVED", actor, {"id": check_id}, check["status"], "WAIVED")

    def hold(self, db, app, actor):
        self.move(db, app["id"], "T33", "OpsHold", actor=actor)
        update(db, "application_payloads", "application_id=?", (app["id"],),
               resume_state=app["state"], held_at=self.now())

    def resume(self, db, app, actor):
        self.move(db, app["id"], "T34", "OpsResume", actor=actor)
        delta = self.now() - app["held_at"]
        db.execute("""UPDATE scheduled_jobs SET run_at=run_at+? WHERE reference_id=? AND status='SCHEDULED'
                       AND job_type NOT IN ('MOCK_CALLBACK','PROVISION_START')""", (delta, app["id"]))
        for table, column, filter_ in (("info_requests", "due_at", "status='OPEN'"),
                                      ("review_tasks", "sla_due_at", UNDECIDED)):
            db.execute(f"UPDATE {table} SET {column}={column}+? WHERE application_id=? AND {filter_}",
                       (delta, app["id"]))
        update(db, "application_payloads", "application_id=?", (app["id"],), resume_state=None, held_at=None)
        self.evaluate(db, app["id"])

    def retry_provision(self, db, app, actor, *, clear_simulation=False):
        app_id = app["id"]
        self.move(db, app_id, "T30", "OpsRetryProvisioning", actor=actor)
        update(db, "merchant_accounts", "application_id=?", (app_id,), attempts=0)
        if clear_simulation:
            sim = json.loads(app["simulation"])
            sim.pop("MID", None)
            update(db, "application_payloads", "application_id=?", (app_id,), simulation=json_text(sim))
        self.close_tasks(db, app_id)
        self.resolve_exceptions(db, app_id, ("PROVISIONING",))
        self.job(db, app_id, "PROVISION", {"attempt": 1})

    def pending_bank_change(self, db, app_id):
        return one(db, """SELECT * FROM change_requests WHERE application_id=? AND change_type='BANK_ACCOUNT_CHANGED'
                          AND status IN ('PENDING_REVERIFICATION','PENDING_REVIEW','FAILED_REVERIFICATION') ORDER BY created_at DESC LIMIT 1""", (app_id,))

    def change(self, db, app, kind, form):
        configured = self.store.config(db)["reverification_triggers"]
        require(kind in configured, "Unsupported change type.", "INVALID_CHANGE", 422)
        changed_bank = kind == "BANK_ACCOUNT_CHANGED"
        if changed_bank:
            bank = form["bank_account"]
            require(re.fullmatch(r"[0-9]{9,18}", canonical(bank["account_number"])) and
                    re.fullmatch(IFSC, canonical(bank["ifsc"])), "Provide valid bank details.", "INVALID_BANK", 422)
            require(bank != self.form(app).get("bank_account"), "The bank details did not change.", "INVALID_BANK", 422)
        app_id, change_id = app["id"], uid()
        actor = ("MERCHANT", app["owner"])
        checks = set(configured[kind])
        if kind == "LEGAL_NAME_CHANGED" and not form.get("declared_gstin"):
            checks.discard("GST")
        if app["state"] == "ACTIVE":
            require(changed_bank and not self.pending_bank_change(db, app_id),
                    "Only one settlement bank change can be pending for an active merchant.")
            self.move(db, app_id, "T37", "ApplicationDataChanged", actor=actor)
        else:
            require(app["state"] in {"VERIFICATION_IN_PROGRESS", "RISK_ASSESSMENT", "MANUAL_REVIEW", "APPROVED"},
                    "Changes cannot be submitted at this stage.")
            self.save_form(db, app_id, form, bank=changed_bank)
            if checks:
                self.close_tasks(db, app_id)
                self.resolve_exceptions(db, app_id, ("PROVIDER", "RISK"))
                self.move(db, app_id, "T36", "ApplicationDataChanged", actor=actor)
        insert(db, "change_requests", id=change_id, application_id=app_id, change_type=kind,
               old_value=json_text({"bank_last4": (self.form(app).get("bank_account") or {}).get("account_number", "")[-4:]}),
               new_value=self.store.seal({"bank": form.get("bank_account")}) if changed_bank else None,
               status="PENDING_REVERIFICATION" if app["state"] == "ACTIVE" else "APPLIED",
               requested_by=app["owner"], checks_reopened=json_text(sorted(checks)), created_at=self.now())
        if changed_bank:
            self.exception(db, app_id, "BANK_CHANGED_AFTER_VERIFICATION", "INFO", "SYSTEM")
        self.event(db, app_id, "ApplicationDataChanged", {"change_request_id": change_id, "type": kind}, actor)
        audit(db, self.now(), app_id, "APPLICATION_DATA_CHANGED", actor, {"id": change_id, "type": kind})
        if checks:
            self.reset_checks(db, app_id, checks, form=form, force=True)
        return change_id

    def bank_change_result(self, db, app, change, status):
        update(db, "change_requests", "id=?", (change["id"],),
               status="PENDING_REVIEW" if status == "PASSED" else "FAILED_REVERIFICATION")
        self.create_task(db, app["id"], "BANK_CHANGE_REVIEW", "HIGH", {"change_request_id": change["id"]})

    def ops_detail(self, db, app):
        app_id = app["id"]
        form = self.form(app)
        for key in SENSITIVE & form.keys():
            form[key] = mask(form[key], key)
        form["contact"] = {key: mask(value, key) if key in SENSITIVE else value for key, value in form["contact"].items()}
        if form.get("bank_account"):
            form["bank_account"]["account_number"] = mask(form["bank_account"].get("account_number"), "account_number")
        for person in form.get("persons", []):
            for key in SENSITIVE & person.keys():
                person[key] = mask(person[key], key)
        fields = many(db, "SELECT * FROM extracted_fields WHERE application_id=? ORDER BY created_at", (app_id,))
        for field in fields:
            kind = field["field_name"]
            for key in ("raw_value", "normalized_value"):
                field[key] = self.store.open(field[key])
                if kind in SENSITIVE:
                    field[key] = mask(field[key], kind)
        reconciliation = many(db, "SELECT * FROM field_reconciliation WHERE application_id=?", (app_id,))
        for entry in reconciliation:
            kind = entry["field_name"]
            for key in ("form_value", "resolved_value"):
                if entry[key]:
                    value = self.store.open(entry[key])
                    entry[key] = mask(value, kind) if kind in SENSITIVE else value
            entry["doc_values"] = self.store.open(entry["doc_values"])
            if kind in SENSITIVE:
                for value in entry["doc_values"]:
                    value["value"] = mask(value["value"], kind)
        return {"application": {key: app[key] for key in ("id", "reference_no", "state", "version",
                                                          "risk_score", "risk_band", "priority", "merchant_id")},
                "form": form,
                "documents": many(db, "SELECT id,doc_type,status,version,mime_type,page_count,quality_score FROM documents WHERE application_id=?", (app_id,)),
                "fields": fields, "reconciliation": reconciliation,
                "checks": [unjson(row, "result") for row in many(db, """SELECT id,check_type,status,required,result,result_code,
                    attempt_count,completed_at,superseded_by FROM verification_checks WHERE application_id=?""", (app_id,))],
                "callbacks": many(db, """SELECT i.provider,i.provider_event_id,i.provider_request_id,i.processing_status,i.received_at
                    FROM inbound_callbacks i JOIN provider_requests p ON p.provider_request_id=i.provider_request_id
                    JOIN verification_checks c ON c.id=p.check_id WHERE c.application_id=?""", (app_id,)),
                "exceptions": [unjson(row, "details") for row in many(db, "SELECT * FROM exceptions WHERE application_id=?", (app_id,))],
                "tasks": many(db, "SELECT id,task_type,status,priority,assigned_to,sla_due_at FROM review_tasks WHERE application_id=?", (app_id,)),
                "notifications": many(db, "SELECT event,message,occurred_at FROM notification_outbox WHERE application_id=?", (app_id,)),
                "changes": many(db, "SELECT id,change_type,status,created_at FROM change_requests WHERE application_id=?", (app_id,)),
                "audit": [unjson(row, "target", "before", "after") for row in many(db,
                    "SELECT id,actor_type,actor_id,action,target,before,after,hash,occurred_at FROM audit_log WHERE application_id=? ORDER BY id", (app_id,))],
                "provisioning": self.capabilities(db, app_id), **self.timeline(db, app_id)}

    def capabilities(self, db, app_id):
        row = one(db, "SELECT provisioning_status,capabilities,attempts,last_error FROM merchant_accounts WHERE application_id=?", (app_id,))
        return unjson(row, "capabilities") if row else None

    def timeline(self, db, app_id):
        transitions = [unjson(row, "reason_codes", "guard_results") for row in many(db, "SELECT * FROM state_transitions WHERE application_id=? ORDER BY id", (app_id,))]
        events = [json.loads(row["payload"]) for row in many(db, "SELECT payload FROM outbox_events WHERE aggregate_id=? ORDER BY id", (app_id,))]
        return {"transitions": transitions, "events": events}
