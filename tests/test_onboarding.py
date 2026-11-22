import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from onboarding.api import create_app
from onboarding.domain import DomainError, TRANSITIONS, decide, gst_checksum, gst_valid, transition
from onboarding.storage import ROOT, json_text, one

API = "/api/v1"
ADMIN = {"x-user-id": "admin", "x-role": "ops_admin"}
SECOND_ADMIN = {"x-user-id": "admin-2", "x-role": "ops_admin"}
MERCHANT = {"x-user-id": "demo-merchant", "x-role": "merchant"}
OTHER = {"x-user-id": "different-merchant", "x-role": "merchant"}


class Clock:
    now = 1800000000.0

    def __call__(self):
        return self.now


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.app = create_app(self.temp.name, clock=self.clock, worker=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.flow = self.app.state.workflow

    def request(self, method, path, data=None, status=200, headers=ADMIN):
        kwargs = {"json": data} if data is not None else {}
        result = self.client.request(method, API + path, headers=headers, **kwargs)
        self.assertEqual(result.status_code, status, result.text)
        return result.json()

    def run_scenario(self, name):
        return self.request("POST", "/ops/simulate/scenario", {"name": name}, 202)["id"]

    def advance(self, seconds=0):
        self.clock.now += seconds
        self.flow.tick()

    def settle(self, seconds=6):
        for _ in range(seconds):
            self.advance()
            self.clock.now += 1

    def detail(self, app_id):
        return self.request("GET", f"/ops/applications/{app_id}")

    def assert_state(self, app_id, state):
        detail = self.detail(app_id)
        self.assertEqual(detail["application"]["state"], state, detail["exceptions"])
        return detail

    def fixture(self, name):
        return json.loads((ROOT / "fixtures/applications" / f"{name}.json").read_text())

    def respond(self, app_id, payload):
        status = self.request("GET", f"/applications/{app_id}", headers=MERCHANT)
        self.request("POST", f"/applications/{app_id}/info-requests/{status['info_request_id']}/respond", payload, 202, MERCHANT)

    def review(self, task_id, action, headers=ADMIN, **changes):
        return self.request("POST", f"/ops/review-tasks/{task_id}/decide",
                            dict(decision=action, reason_codes=["TEST_REASON"], notes="Synthetic fixture", **changes), headers=headers)

    def test_straight_through_and_duplicate_callback(self):
        app_id = self.run_scenario("happy_pvt_ltd")
        self.settle()
        detail = self.assert_state(app_id, "ACTIVE")
        self.assertEqual(len([t for t in detail["transitions"] if t["to_state"] == "ACTIVE"]), 1)
        self.assertTrue(detail["application"]["merchant_id"].startswith("PL27"))
        self.assertNotEqual(detail["form"]["declared_pan"], self.fixture("happy_pvt_ltd")["application"]["declared_pan"])
        self.assertEqual(self.request("GET", "/ops/metrics")["callback_totals"]["DUPLICATE"], 1)
        self.assertTrue(self.request("GET", "/ops/audit/verify")["valid"])

    def test_gst_cancelled_requests_corrected_gstin_then_activates(self):
        app_id = self.run_scenario("gst_cancelled")
        self.settle()
        detail = self.assert_state(app_id, "MANUAL_REVIEW")
        task = detail["tasks"][0]["id"]
        self.assertEqual(detail["checks"][1]["required"], 1)
        self.review(task, "REQUEST_INFO", info_items=[
            {"kind": "DOCUMENT", "doc_type": "GST_CERTIFICATE", "message": "Provide an active GST certificate."}
        ])
        fixture = self.fixture("gst_cancelled")
        form = fixture["application"]
        base = form["declared_gstin"][:12] + "2Z"
        gst = base + gst_checksum(base)
        self.respond(app_id, {"fields": {"declared_gstin": gst}, "documents": [
            {"doc_type": "GST_CERTIFICATE", "payload": {"gstin": gst, "legal_name": form["legal_name"]}}
        ]})
        self.settle()
        self.assert_state(app_id, "ACTIVE")

    def test_blurred_bank_document_replacement(self):
        app_id = self.run_scenario("blurred_cheque")
        self.settle()
        self.assert_state(app_id, "AWAITING_MERCHANT_INFO")
        fixture = self.fixture("blurred_cheque")
        self.respond(app_id, {"documents": [{"doc_type": "BANK_PROOF", "payload": fixture["application"]["bank_account"]}]})
        self.settle()
        self.assert_state(app_id, "ACTIVE")

    def test_pan_conflict_replaced(self):
        app_id = self.run_scenario("pan_conflict")
        self.settle()
        self.assert_state(app_id, "AWAITING_MERCHANT_INFO")
        form = self.fixture("pan_conflict")["application"]
        self.respond(app_id, {"documents": [{"doc_type": "PAN_CARD",
                                              "payload": {"pan": form["declared_pan"], "name": form["legal_name"]}}]})
        self.settle()
        self.assert_state(app_id, "ACTIVE")

    def test_late_first_bank_result_supersedes_retry(self):
        app_id = self.run_scenario("late_callback")
        self.advance()
        self.advance(.11)
        self.advance(.9)
        self.advance(29.11)
        self.advance(.05)
        self.settle()
        detail = self.assert_state(app_id, "ACTIVE")
        self.assertTrue(any(c["processing_status"] == "LATE_APPLIED" for c in detail["callbacks"]))
        bank = next(c for c in detail["checks"] if c["check_type"] == "BANK")
        self.assertEqual(bank["attempt_count"], 2)

    def test_bank_change_before_and_after_activation(self):
        app_id = self.run_scenario("bank_change")
        self.advance()
        base = f"/applications/{app_id}/change-requests"
        patch = {"change_type": "BANK_ACCOUNT_CHANGED", "fields": {"bank_account": {"account_number": "900000999999"}}}
        self.request("POST", base, patch, 202, MERCHANT)
        self.settle()
        detail = self.assert_state(app_id, "ACTIVE")
        banks = [check for check in detail["checks"] if check["check_type"] == "BANK"]
        self.assertEqual({check["status"] for check in banks}, {"STALE", "PASSED"})
        patch["fields"]["bank_account"]["account_number"] = "900000888888"
        self.request("POST", base, patch, 202, MERCHANT)
        self.settle()
        detail = self.assert_state(app_id, "ACTIVE")
        self.assertTrue(detail["form"]["bank_account"]["account_number"].endswith("9999"))
        task = next(t["id"] for t in detail["tasks"] if t["task_type"] == "BANK_CHANGE_REVIEW")
        self.assertEqual(self.review(task, "APPROVE")["status"], "AWAITING_SECOND_REVIEW")
        self.request("POST", f"/ops/review-tasks/{task}/decide", {"decision": "CONFIRM"}, 403)
        self.review(task, "CONFIRM", SECOND_ADMIN)
        self.assertTrue(self.detail(app_id)["form"]["bank_account"]["account_number"].endswith("8888"))

    def test_mid_failure_and_replay(self):
        app_id = self.run_scenario("mid_failure")
        self.settle()
        for _ in range(5):
            self.advance(30)
        detail = self.assert_state(app_id, "PROVISIONING_FAILED")
        self.assertEqual(detail["provisioning"]["attempts"], 5)
        letter = self.request("GET", "/ops/dead-letters")[0]
        self.request("POST", f"/ops/dead-letters/{letter['id']}/replay", {"clear_simulation": True})
        self.settle()
        self.assert_state(app_id, "ACTIVE")

    def test_pep_manual_review_and_reject(self):
        app_id = self.run_scenario("high_risk_mcc_pep")
        self.settle()
        detail = self.assert_state(app_id, "MANUAL_REVIEW")
        self.assertEqual(detail["tasks"][0]["priority"], "HIGH")
        self.review(detail["tasks"][0]["id"], "REJECT")
        self.assert_state(app_id, "REJECTED")
        merchant = self.request("GET", f"/applications/{app_id}", headers=MERCHANT)
        self.assertNotIn("PEP_UNDECLARED", json.dumps(merchant))

    def test_exact_active_duplicate_blocked(self):
        self.run_scenario("happy_pvt_ltd")
        self.settle()
        app_id = self.run_scenario("duplicate_active")
        self.settle()
        detail = self.assert_state(app_id, "REJECTED")
        self.assertIn("DUP_ACTIVE_MERCHANT", [item["code"] for item in detail["exceptions"]])

    def test_verification_callback_credentials_and_orphan(self):
        callback = dict(provider_event_id="example", provider_request_id="unrecognized", status="PASSED",
                        result_code="PAN_VALID", provider_timestamp=self.clock.now, result={})
        content = json_text(callback).encode()
        stamp = str(int(self.clock.now))
        headers = {"X-Event-Id": "example", "X-Timestamp": stamp, "X-Signature": "invalid"}
        url = API + "/callbacks/PAN"
        result = self.client.post(url, content=content, headers=headers)
        self.assertEqual(result.status_code, 401)
        headers["X-Signature"] = self.flow.store.sign("PAN", stamp, "example", content)
        for expected in ["ORPHAN", "DUPLICATE"]:
            result = self.client.post(url, content=content, headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["status"], expected)

    def test_out_of_order_signed_callback(self):
        app_id = self.run_scenario("gst_cancelled")
        with self.flow.store.transaction() as db:
            db.execute("UPDATE application_payloads SET simulation=? WHERE application_id=?",
                       (json_text({"GST": {"out_of_order": True, "latency_ms": 100}}), app_id))
        self.settle()
        detail = self.assert_state(app_id, "MANUAL_REVIEW")
        self.assertIn("OUT_OF_ORDER", [callback["processing_status"] for callback in detail["callbacks"]])
        self.assertTrue(any(c["check_type"] == "GST" and c["status"] == "FAILED" for c in detail["checks"]))

    def test_idempotency_ownership_and_info_expiry(self):
        form = self.fixture("happy_pvt_ltd")["application"]
        headers = MERCHANT | {"Idempotency-Key": "create-draft"}
        first = self.request("POST", "/applications", form, 201, headers)
        self.assertEqual(first, self.request("POST", "/applications", form, 201, headers))
        self.request("POST", "/applications", form | {"legal_name": "New name"}, 409, headers)
        self.request("GET", f"/applications/{first['id']}", status=404, headers=OTHER)
        app_id = self.run_scenario("blurred_cheque")
        self.settle()
        self.advance(73 * 3600)
        self.assert_state(app_id, "EXPIRED")

    def test_persistence_and_audit_tampering(self):
        app_id = self.run_scenario("happy_pvt_ltd")
        with TestClient(create_app(self.temp.name, clock=self.clock, worker=False)) as restarted:
            restarted.app.state.workflow.tick()
            self.clock.now += 1
            restarted.app.state.workflow.tick()
            self.clock.now += 1
            restarted.app.state.workflow.tick()
            self.assertEqual(restarted.get(f"{API}/applications/{app_id}", headers=MERCHANT).json()["state"], "ACTIVE")
        with self.flow.store.transaction() as db:
            db.execute("UPDATE audit_log SET action='MODIFIED' WHERE id=1")
        self.assertFalse(self.request("GET", "/ops/audit/verify")["valid"])

    def test_file_constraints_and_aadhaar_minimization(self):
        form = self.fixture("happy_pvt_ltd")["application"]
        self.request("POST", "/applications", form | {"aadhaar_number": "999988887777"}, 422, MERCHANT)
        app_id = self.request("POST", "/applications", form, 201, MERCHANT)["id"]
        url = f"{API}/applications/{app_id}/documents"
        response = self.client.post(url, data={"doc_type": "PAN_CARD"},
                                    files={"file": ("fake.pdf", b"not a pdf", "application/pdf")}, headers=MERCHANT)
        self.assertEqual(response.status_code, 415)
        response = self.client.post(url, data={"doc_type": "PAN_CARD"},
                                    files={"file": ("empty.pdf", b"", "application/pdf")}, headers=MERCHANT)
        self.assertEqual(response.status_code, 413)

    def test_transition_rules_and_gstin_checksum(self):
        current_states = {"T33": "DRAFT", "T34": "ON_HOLD", "T35": "DRAFT",
                          "T36": "APPROVED"}
        for rule, row in TRANSITIONS.items():
            source, target = row.split()
            with self.subTest(rule=rule):
                source = current_states.get(rule, source)
                result = transition(source, rule, resume_state="DRAFT")
                self.assertEqual(result, "DRAFT" if rule == "T34" else target)
                with self.assertRaises(DomainError):
                    transition(source, rule, guard=False, resume_state="DRAFT")
        for check, value in [("valid", "27ZZZCZ1000Z1Z"), ("valid", "29ABCDE1234F1Z")]:
            self.assertTrue(gst_valid(value + gst_checksum(value)), check)
        self.assertFalse(gst_valid("27ZZZCZ1000Z1Z!"))
        self.assertFalse(gst_valid("27ZZZCZ1000Z1Z" + ("0" if gst_checksum("27ZZZCZ1000Z1Z") != "0" else "1")))
        cfg = self.flow.store.defaults
        for score, expected in [(20, "APPROVE"), (52, "REVIEW"), (80, "REJECT")]:
            self.assertEqual(decide([], "5411", [], score, cfg)["decision"], expected)


if __name__ == "__main__":
    unittest.main()
