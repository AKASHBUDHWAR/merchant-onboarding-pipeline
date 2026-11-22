import hmac
import json
from datetime import datetime, timezone

from .domain import DONE, TERMINAL, canonical, decide, issue, required_checks
from .storage import insert, json_text, many, one, uid, update

PASS_CODES = {"PAN": "PAN_VALID", "GST": "GST_ACTIVE", "BANK": "BANK_VERIFIED",
              "RISK": "RISK_SCORED", "MCA": "MCA_ACTIVE"}
FAIL_CODES = {"PAN": "PAN_INVALID", "GST": "GST_CANCELLED", "BANK": "BANK_ACCT_INVALID",
              "RISK": "RISK_FAILED", "MCA": "MCA_NOT_ACTIVE"}
REVIEW_STATES = {"MANUAL_REVIEW", "APPROVED"}
FIXABLE = {"BANK_ACCT_INVALID", "BANK_IFSC_INVALID", "PAN_NAME_MISMATCH"}


class Providers:
    def check_inputs(self, form, kind):
        keys = {
            "PAN": ("declared_pan", "legal_name", "persons"),
            "GST": ("declared_gstin", "declared_pan", "legal_name", "registered_address"),
            "BANK": ("bank_account", "legal_name"),
            "RISK": ("declared_pan", "declared_gstin", "legal_name", "mcc", "persons", "registered_address",
                     "expected_monthly_gmv_inr", "website_url"),
            "MCA": ("declared_cin", "legal_name", "persons"),
        }
        return {key: form.get(key) for key in keys[kind]}

    def reset_checks(self, db, app_id, types, *, form=None, force=False, optional=()):
        app = self.app(db, app_id)
        form = form or self.form(app)
        needed = required_checks(form, self.store.config(db))
        for kind in sorted(types):
            previous = one(db, """SELECT * FROM verification_checks WHERE application_id=? AND check_type=?
                                    AND superseded_by IS NULL""", (app_id, kind))
            fingerprint = self.store.fingerprint(self.check_inputs(form, kind))
            required = int(kind in needed and kind not in optional)
            if previous and previous["input_hash"] == fingerprint and previous["status"] in DONE and not force:
                update(db, "verification_checks", "id=?", (previous["id"],), required=required)
                continue
            check_id = uid()
            insert(db, "verification_checks", id=check_id, application_id=app_id, check_type=kind, status="PENDING",
                   required=required, input_snapshot=json_text({"fields": list(self.check_inputs(form, kind))}), input_hash=fingerprint)
            if previous:
                update(db, "verification_checks", "id=?", (previous["id"],), status="STALE", superseded_by=check_id)
                self.event(db, app_id, "CheckStale", {"check_id": previous["id"], "replacement": check_id})
            self.job(db, app_id, "DISPATCH", {"check_id": check_id, "expected": 0})
        self.evaluate(db, app_id)

    def dispatch(self, db, app, check_id, expected):
        check = one(db, "SELECT * FROM verification_checks WHERE id=?", (check_id,))
        if not check or check["status"] != "PENDING" or check["attempt_count"] != expected:
            return
        active_change = app["state"] == "ACTIVE" and self.pending_bank_change(db, app["id"])
        if app["state"] not in {"VERIFICATION_IN_PROGRESS", "MANUAL_REVIEW"} and not active_change:
            return
        kind, now = check["check_type"], self.now()
        settings = self.store.config(db)["providers"][kind]
        health = one(db, "SELECT * FROM provider_health WHERE provider=?", (kind,))
        if health and health["open_until"] > now:
            self.job(db, app["id"], "DISPATCH", {"check_id": check_id, "expected": expected}, health["open_until"] - now)
            return
        simulation = json.loads(app["simulation"])
        mock = simulation.get(kind, {})
        attempt = expected + 1
        timeout = mock.get("timeout_ms", settings["timeout_sec"] * 1000) / 1000
        request_id = uid()
        key = f"{app['id']}:{kind}:{check['input_hash']}:{check_id}:{attempt}"
        update(db, "verification_checks", "id=?", (check_id,), status="IN_FLIGHT",
               attempt_count=attempt, deadline_at=now + timeout, started_at=check["started_at"] or now)
        insert(db, "provider_requests", id=request_id, provider_request_id=request_id, check_id=check_id, provider=kind,
               request_payload=check["input_snapshot"], attempt_no=attempt, idempotency_key=key, status="ACKED",
               sent_at=now, acked_at=now, timeout_at=now + timeout, http_status=202 if settings["mode"] == "async" else 200)
        self.event(db, app["id"], "CheckDispatched", {"check_id": check_id, "type": kind, "attempt": attempt})
        outcome = mock.get("outcome", "PASS")
        if attempt <= mock.get("fail_first_n_attempts", 0) or outcome == "ERROR_5XX":
            update(db, "provider_requests", "id=?", (request_id,), status="FAILED", http_status=503, latency_ms=0)
            failures = (health["failures"] if health else 0) + 1
            db.execute("INSERT OR REPLACE INTO provider_health VALUES (?,?,?)",
                       (kind, failures, now + 15 if failures % 3 == 0 else 0))
            self.retry_check(db, app, check, attempt, "ERROR")
            return
        db.execute("INSERT OR REPLACE INTO provider_health VALUES (?,0,0)", (kind,))
        self.job(db, app["id"], "CHECK_TIMEOUT", {"request_id": request_id}, timeout)
        late = mock.get("late_callback_after_ms") if attempt == 1 else None
        if late is None and outcome in {"TIMEOUT", "NO_CALLBACK"}:
            return
        delay = late / 1000 if late is not None else mock.get("latency_ms", 500) / 1000
        status = {"FAIL": "FAILED", "INCONCLUSIVE": "INCONCLUSIVE"}.get(outcome, "PASSED")
        if kind == "PAN" and canonical(self.form(app).get("declared_pan")).endswith("9Z"):
            status = "FAILED"
        code = mock.get("result_code") or (PASS_CODES[kind] if status == "PASSED" else
                                            FAIL_CODES[kind] if status == "FAILED" else f"{kind}_INCONCLUSIVE")
        result = {}
        if kind == "RISK":
            result = {"risk_score": mock.get("risk_score", 20), "flags": mock.get("flags", [])}
            if any(p.get("is_pep") for p in self.form(app).get("persons", [])):
                result["flags"] = sorted(set(result["flags"]) | {"PEP_DECLARED"})
            if one(db, "SELECT id FROM exceptions WHERE application_id=? AND code='V-X-12' AND status='OPEN'", (app["id"],)):
                result["flags"] = sorted(set(result["flags"]) | {"GMV_TURNOVER_MISMATCH"})
        if kind in {"PAN", "BANK"} and "name_match_score" in mock:
            result["name_match_score"] = mock["name_match_score"]
        data = {"request_id": request_id, "event_id": uid(), "status": status, "result_code": code,
                "provider_timestamp": now + delay, "result": result, "bad_signature": mock.get("bad_signature", False)}
        if mock.get("out_of_order"):
            data.update(status="FAILED", result_code=FAIL_CODES[kind])
        if settings["mode"] == "sync" and delay < timeout and not mock.get("bad_signature") and not mock.get("out_of_order"):
            self.mock_callback(db, data)
        else:
            self.job(db, app["id"], "MOCK_CALLBACK", data, delay)
        for index in range(mock.get("duplicate_callbacks", 0)):
            self.job(db, app["id"], "MOCK_CALLBACK", data, delay + (index + 1) * 0.01)
        if mock.get("out_of_order"):
            older = data | {"event_id": uid(), "status": "PASSED", "result_code": PASS_CODES[kind],
                            "provider_timestamp": now + delay - 1}
            self.job(db, app["id"], "MOCK_CALLBACK", older, delay + 0.05)

    def mock_callback(self, db, data):
        req = one(db, "SELECT provider FROM provider_requests WHERE id=?", (data["request_id"],))
        provider, event_id = req["provider"], data["event_id"]
        payload = {key: data[key] for key in ("status", "result_code", "provider_timestamp", "result")}
        payload.update(provider_event_id=event_id, provider_request_id=data["request_id"])
        body = json_text(payload).encode()
        stamp = str(int(self.now()))
        signature = self.store.sign(provider, stamp, event_id, body)
        self.callback(db, provider, stamp, event_id, "bad" if data["bad_signature"] else signature, body, payload)

    def count_callback(self, db, status):
        db.execute("""INSERT INTO callback_counts VALUES (?,1)
                       ON CONFLICT(status) DO UPDATE SET count=count+1""", (status,))

    def callback(self, db, provider, timestamp, event_id, signature, body, payload):
        cfg = self.store.config(db)
        try:
            within_skew = abs(self.now() - int(timestamp)) <= cfg["callback_clock_skew_sec"]
        except (ValueError, TypeError):
            within_skew = False
        valid = within_skew and hmac.compare_digest(
            self.store.sign(provider, timestamp, event_id, body), signature or "")
        if not valid:
            self.count_callback(db, "REJECTED_SIGNATURE")
            insert(db, "inbound_callbacks", id=uid(), provider=provider, provider_event_id="invalid-" + uid(),
                   signature_valid=0, payload="{}", received_at=self.now(), processing_status="REJECTED_SIGNATURE")
            return "REJECTED_SIGNATURE", 401
        previous = one(db, "SELECT id FROM inbound_callbacks WHERE provider=? AND provider_event_id=?", (provider, event_id))
        if previous:
            self.count_callback(db, "DUPLICATE")
            return "DUPLICATE", 200
        request_id = payload.get("provider_request_id")
        req = one(db, "SELECT * FROM provider_requests WHERE provider=? AND provider_request_id=?", (provider, request_id))
        inbox_id = uid()
        insert(db, "inbound_callbacks", id=inbox_id, provider=provider, provider_event_id=event_id,
               provider_request_id=request_id, signature_valid=1, payload=self.store.seal(payload),
               received_at=self.now(), processing_status="RECEIVED")
        if req:
            status = self.apply_callback(db, req, payload, cfg)
        else:
            status = "ORPHAN"
        update(db, "inbound_callbacks", "id=?", (inbox_id,), processing_status=status)
        self.count_callback(db, status)
        return status, 200

    def apply_callback(self, db, req, payload, cfg):
        check = one(db, "SELECT * FROM verification_checks WHERE id=?", (req["check_id"],))
        app = self.app(db, check["application_id"])
        app_id = app["id"]
        status, code = payload.get("status"), payload.get("result_code")
        if status not in {"PASSED", "FAILED", "INCONCLUSIVE"} or not isinstance(code, str):
            return "FAILED"
        previous = json.loads(check["result"] or "{}")
        stamp = payload.get("provider_timestamp")
        if not isinstance(stamp, (int, float)):
            return "FAILED"
        self.event(db, app_id, "CallbackReceived", {"provider": req["provider"], "provider_request_id": req["id"]})
        if check["status"] == "STALE" or check["superseded_by"]:
            return "LATE"
        if req["status"] == "SUPERSEDED":
            return "LATE"
        if stamp < previous.get("provider_timestamp", float("-inf")):
            return "OUT_OF_ORDER"
        active_change = self.pending_bank_change(db, app_id) if app["state"] == "ACTIVE" and check["check_type"] == "BANK" else None
        if check["status"] == "WAIVED":
            if status == "FAILED" and app["state"] in {"APPROVED", "PROVISIONING"} and cfg["late_callback_policy"] != "always_record_only":
                self.exception(db, app_id, "LATE_ADVERSE_RESULT", "CRITICAL", "PROVIDER")
                if app["state"] == "APPROVED":
                    self.review(db, app_id, "T38", "LATE_ADVERSE_RESULT", "HIGH")
                else:
                    self.fail_provisioning(db, app_id, "LATE_ADVERSE_RESULT", 0)
            return "LATE"
        if app["state"] in TERMINAL and not active_change:
            return "LATE"
        late = req["status"] in {"TIMED_OUT", "FAILED"}
        if late and cfg["late_callback_policy"] == "always_record_only":
            return "LATE"
        if check["status"] in {"PASSED", "FAILED", "INCONCLUSIVE"}:
            if check["status"] != status or check["result_code"] != code:
                self.exception(db, app_id, "PROVIDER_CONFLICTING_CALLBACKS", "BLOCKING", "PROVIDER",
                               {"check_id": check["id"]})
                if app["state"] == "VERIFICATION_IN_PROGRESS":
                    self.review(db, app_id, "T17", "PROVIDER_CONFLICT", "HIGH")
                elif app["state"] == "APPROVED":
                    self.review(db, app_id, "T38", "PROVIDER_CONFLICT", "HIGH")
                elif app["state"] == "PROVISIONING":
                    self.fail_provisioning(db, app_id, "PROVIDER_CONFLICTING_CALLBACKS", 0)
                return "CONFLICT"
            return "LATE" if late else "REDUNDANT"
        result = payload.get("result", {})
        if not isinstance(result, dict):
            return "FAILED"
        result = {key: value for key, value in result.items() if key in {"risk_score", "flags", "name_match_score"}}
        if check["check_type"] == "RISK":
            score, flags = result.get("risk_score"), result.get("flags", [])
            if ((score is not None or status == "PASSED") and (type(score) is not int or not 0 <= score <= 100)
                    or not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags)):
                return "FAILED"
        score = result.get("name_match_score")
        if score is not None and (not isinstance(score, (int, float)) or not 0 <= score <= 1):
            return "FAILED"
        result = {key: value for key, value in result.items() if value is not None}
        result["provider_timestamp"] = stamp
        now = self.now()
        update(db, "verification_checks", "id=?", (check["id"],), status=status, result=json_text(result), result_code=code,
               deadline_at=None, completed_at=now)
        update(db, "provider_requests", "id=?", (req["id"],), status="LATE_CALLBACK" if late else "CALLBACK_RECEIVED",
               latency_ms=max(0, round((now - req["sent_at"]) * 1000)))
        db.execute("UPDATE provider_requests SET status='SUPERSEDED' WHERE check_id=? AND id<>? AND status='ACKED'",
                   (check["id"], req["id"]))
        self.resolve_check_errors(db, app_id, check["id"])
        self.event(db, app_id, "CheckCompleted", {"type": check["check_type"], "status": status, "code": code})
        if late and app["state"] == "MANUAL_REVIEW" and cfg["late_callback_policy"] == "reopen_review":
            self.exception(db, app_id, "LATE_ADVERSE_RESULT" if status == "FAILED" else "LATE_CALLBACK_APPLIED",
                           "BLOCKING" if status == "FAILED" else "INFO", "PROVIDER")
        if active_change:
            self.bank_change_result(db, app, active_change, status)
        else:
            self.evaluate(db, app_id)
        return "LATE_APPLIED" if late else "PROCESSED"

    def resolve_check_errors(self, db, app_id, check_id):
        db.execute("""UPDATE exceptions SET status='RESOLVED',resolved_at=? WHERE application_id=?
                       AND status='OPEN' AND code IN ('CHECK_TIMED_OUT','CHECK_PROVIDER_ERROR')
                       AND json_extract(details,'$.check_id')=?""", (self.now(), app_id, check_id))

    def retry_check(self, db, app, check, attempt, result):
        cfg = self.store.config(db)["providers"][check["check_type"]]
        app_id = app["id"]
        if attempt < cfg["max_attempts"]:
            base = cfg["base_delay_sec"]
            factor = 2 ** (attempt - 1) if cfg["backoff"] == "exponential" else 1
            jitter = int(self.store.fingerprint([check["id"], attempt])[:4], 16) / 65535 * base if factor != 1 or cfg["backoff"] == "exponential" else 0
            delay = base * factor + jitter
            update(db, "verification_checks", "id=?", (check["id"],), status="PENDING", next_attempt_at=self.now() + delay)
            self.job(db, app_id, "DISPATCH", {"check_id": check["id"], "expected": attempt}, delay)
            self.event(db, app_id, "CheckRetryScheduled", {"type": check["check_type"], "attempt": attempt + 1})
            return
        code = "CHECK_TIMED_OUT" if result == "TIMED_OUT" else "CHECK_PROVIDER_ERROR"
        update(db, "verification_checks", "id=?", (check["id"],), status=result, result_code=code, completed_at=self.now())
        self.exception(db, app_id, code, "BLOCKING" if check["required"] else "WARN", "PROVIDER", {"check_id": check["id"]})
        self.dead_letter(db, app_id, "PROVIDER_CALL", {"check_id": check["id"], "check_type": check["check_type"]}, code, attempt)
        change = self.pending_bank_change(db, app_id) if app["state"] == "ACTIVE" else None
        if change:
            self.bank_change_result(db, app, change, result)
        else:
            self.evaluate(db, app_id)

    def evaluate(self, db, app_id):
        app = self.app(db, app_id)
        if app["state"] != "VERIFICATION_IN_PROGRESS":
            return
        checks = many(db, "SELECT * FROM verification_checks WHERE application_id=? AND superseded_by IS NULL", (app_id,))
        cfg, form = self.store.config(db), self.form(app)
        risk = next((json.loads(c["result"] or "{}") for c in checks if c["check_type"] == "RISK"), {})
        score, flags = risk.get("risk_score", 0), risk.get("flags", [])
        needed = required_checks(form, cfg)
        if not needed.issubset({c["check_type"] for c in checks if c["required"]}):
            return
        hard = decide(checks, form.get("mcc"), flags, score, cfg)
        if hard["matched_rule"] == "R1_HARD_REJECT":
            self.event(db, app_id, "DecisionMade", hard)
            self.move(db, app_id, "T18", "CheckCompleted", reasons=[hard["matched_rule"]])
            return
        if any(c["status"] in {"PENDING", "IN_FLIGHT"} for c in checks):
            return
        for check in checks:
            if check["status"] in DONE:
                continue
            code = check["result_code"] or "PARTIAL_VERIFICATION"
            if not check["required"]:
                self.exception(db, app_id, code, "WARN", "PROVIDER", {"check_id": check["id"]})
                continue
            self.exception(db, app_id, code, "BLOCKING", "PROVIDER", {"check_id": check["id"]})
            self.exception(db, app_id, "PARTIAL_VERIFICATION", "BLOCKING", "PROVIDER", {"check_id": check["id"]})
            if code in FIXABLE and app["info_request_round"] < cfg["info_request"]["max_rounds"]:
                field = "bank_account" if check["check_type"] == "BANK" else "legal_name"
                self.ask_info(db, app_id, "T16", [issue(code, field)])
            else:
                self.review(db, app_id, "T17", "CHECK_TIMEOUT" if check["status"] == "TIMED_OUT" else "PARTIAL_VERIFICATION")
            return
        self.move(db, app_id, "T15", "CheckCompleted")
        band = "LOW" if score <= cfg["risk"]["auto_approve_max_score"] else "MEDIUM" if score <= cfg["risk"]["manual_review_max_score"] else "HIGH"
        update(db, "applications", "id=?", (app_id,), risk_score=score, risk_band=band)
        self.event(db, app_id, "RiskScored", {"score": score, "band": band, "flags": flags})
        blockers = one(db, """SELECT id FROM exceptions WHERE application_id=? AND status='OPEN'
                              AND severity IN ('BLOCKING','CRITICAL') AND source<>'PROVIDER'""", (app_id,))
        borderline = any(c["status"] == "PASSED" and json.loads(c["result"] or "{}").get(
            "name_match_score", 1) < cfg["name_match"]["pass_threshold"] for c in checks)
        decision = decide(checks, form.get("mcc"), flags, score, cfg, blocking=bool(blockers), borderline=borderline)
        self.event(db, app_id, "DecisionMade", decision)
        reason = decision["matched_rule"]
        if decision["decision"] == "APPROVE":
            self.move(db, app_id, "T19", "DecisionMade", reasons=[reason])
        elif decision["decision"] == "REJECT":
            self.move(db, app_id, "T21", "DecisionMade", reasons=[reason])
        else:
            self.exception(db, app_id, {"R6_HIGH_RISK_MCC": "HIGH_RISK_MCC", "R4_PEP_REVIEW": "PEP_UNDECLARED"
                                        }.get(reason, reason), "BLOCKING", "RISK")
            self.review(db, app_id, "T20", "RISK_REVIEW", "HIGH" if "PEP_UNDECLARED" in flags else "MEDIUM")

    def fail_provisioning(self, db, app_id, code, attempt):
        self.move(db, app_id, "T29", "ProvisioningFailed", reasons=[code])
        self.exception(db, app_id, code, source="PROVISIONING")
        self.dead_letter(db, app_id, "PROVISIONING", {}, code, attempt)
        self.create_task(db, app_id, "PROVISIONING_FAILURE", "HIGH")

    def provision(self, db, app, attempt):
        if app["state"] != "PROVISIONING":
            return
        app_id, now = app["id"], self.now()
        cfg = self.store.config(db)
        settings = cfg["providers"]["MID"]
        mock = json.loads(app["simulation"]).get("MID", {})
        account = one(db, "SELECT * FROM merchant_accounts WHERE application_id=?", (app_id,))
        if not account:
            mid_id = uid()
            insert(db, "merchant_accounts", id=mid_id, application_id=app_id, provisioning_status="MID_REQUESTED", created_at=now)
            account = one(db, "SELECT * FROM merchant_accounts WHERE id=?", (mid_id,))
        if account["attempts"] != attempt - 1:
            return
        mid = account["merchant_id"]
        if not mid and attempt > mock.get("fail_mid_creation", 0) and mock.get("slow_ms", 0) <= settings["timeout_sec"] * 1000:
            form = self.form(app)
            state = form["registered_address"].get("state_code", "00")
            sequence = db.execute("SELECT rowid FROM merchant_accounts WHERE id=?", (account["id"],)).fetchone()[0]
            mid = cfg["provisioning"]["mid_format"].format(state_code=state, yyMM=datetime.fromtimestamp(now, timezone.utc).strftime("%y%m"), seq=sequence)
            update(db, "applications", "id=?", (app_id,), merchant_id=mid)
            self.event(db, app_id, "MerchantIdCreated", {"merchant_id": mid})
        capability_map = cfg["provisioning"]["capabilities_by_category"]
        capabilities = [{"name": capability, "status": "FAILED" if capability in mock.get("fail_capability", []) else "ENABLED"}
                        for capability in capability_map.get(app["business_category"], capability_map["DEFAULT"])] if mid else []
        code = "MID_CREATION_FAILED" if not mid else "CAPABILITY_CONFIG_FAILED" if any(c["status"] == "FAILED" for c in capabilities) else None
        update(db, "merchant_accounts", "id=?", (account["id"],), merchant_id=mid, attempts=attempt,
               last_error=code, capabilities=json_text(capabilities), provisioning_status="MID_FAILED" if not mid else "CAPABILITIES_CONFIGURING")
        if code and attempt < settings["max_attempts"]:
            self.job(db, app_id, "PROVISION", {"attempt": attempt + 1}, settings["base_delay_sec"] * 2 ** (attempt - 1))
            return
        if code:
            partial = mid and any(c["status"] == "ENABLED" for c in capabilities)
            if not partial or cfg["provisioning"]["compensation_on_capability_failure"] != "retry_then_partial":
                if mid and cfg["provisioning"]["compensation_on_capability_failure"] == "rollback_mid":
                    update(db, "merchant_accounts", "id=?", (account["id"],), merchant_id=None, provisioning_status="ROLLED_BACK",
                           capabilities=json_text([c | {"status": "DISABLED"} for c in capabilities]))
                    update(db, "applications", "id=?", (app_id,), merchant_id=None)
                self.fail_provisioning(db, app_id, code, attempt)
                return
            self.exception(db, app_id, "CAPABILITY_PARTIAL", "WARN", "PROVISIONING")
        update(db, "merchant_accounts", "id=?", (account["id"],), provisioning_status="CAPABILITY_PARTIAL" if code else "ACTIVE", activated_at=now)
        self.event(db, app_id, "ProvisioningSucceeded", {"merchant_id": mid, "capabilities": capabilities})
        self.move(db, app_id, "T28", "ProvisioningSucceeded")

    def provider_job(self, db, app, kind, data):
        if kind == "DISPATCH":
            self.dispatch(db, app, **data)
        elif kind == "MOCK_CALLBACK":
            self.mock_callback(db, data)
        elif kind == "CHECK_TIMEOUT":
            req = one(db, "SELECT * FROM provider_requests WHERE id=? AND status='ACKED'", (data["request_id"],))
            if not req:
                return
            check = one(db, "SELECT * FROM verification_checks WHERE id=?", (req["check_id"],))
            if check["status"] != "IN_FLIGHT" or check["attempt_count"] != req["attempt_no"]:
                return
            update(db, "provider_requests", "id=?", (req["id"],), status="TIMED_OUT")
            self.event(db, app["id"], "CheckTimedOut", {"type": check["check_type"], "attempt": req["attempt_no"]})
            self.retry_check(db, app, check, req["attempt_no"], "TIMED_OUT")
        elif kind == "PROVISION_START" and app["state"] == "APPROVED":
            self.move(db, app["id"], "T27", "ProvisioningRequested")
            self.job(db, app["id"], "PROVISION", {"attempt": 1}, delay=0.1)
        elif kind == "PROVISION":
            self.provision(db, app, **data)
