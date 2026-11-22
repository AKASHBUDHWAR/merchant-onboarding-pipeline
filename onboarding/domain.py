import re
from datetime import date
from difflib import SequenceMatcher

# Each transition is keyed by its identifier in the specification.
TRANSITIONS = dict(line.split(" ", 1) for line in """
T01 DRAFT SUBMITTED
T02 SUBMITTED DOC_PROCESSING
T03 DOC_PROCESSING VALIDATING
T04 DOC_PROCESSING AWAITING_MERCHANT_INFO
T05 DOC_PROCESSING MANUAL_REVIEW
T06 VALIDATING VERIFICATION_IN_PROGRESS
T07 VALIDATING AWAITING_MERCHANT_INFO
T08 VALIDATING MANUAL_REVIEW
T09 VALIDATING REJECTED
T10 AWAITING_MERCHANT_INFO DOC_PROCESSING
T11 AWAITING_MERCHANT_INFO VALIDATING
T12 AWAITING_MERCHANT_INFO VERIFICATION_IN_PROGRESS
T13 AWAITING_MERCHANT_INFO EXPIRED
T14 AWAITING_MERCHANT_INFO MANUAL_REVIEW
T15 VERIFICATION_IN_PROGRESS RISK_ASSESSMENT
T16 VERIFICATION_IN_PROGRESS AWAITING_MERCHANT_INFO
T17 VERIFICATION_IN_PROGRESS MANUAL_REVIEW
T18 VERIFICATION_IN_PROGRESS REJECTED
T19 RISK_ASSESSMENT APPROVED
T20 RISK_ASSESSMENT MANUAL_REVIEW
T21 RISK_ASSESSMENT REJECTED
T22 MANUAL_REVIEW APPROVED
T23 MANUAL_REVIEW AWAITING_MERCHANT_INFO
T24 MANUAL_REVIEW REJECTED
T25 MANUAL_REVIEW VERIFICATION_IN_PROGRESS
T26 MANUAL_REVIEW DUPLICATE_MERGED
T27 APPROVED PROVISIONING
T28 PROVISIONING ACTIVE
T29 PROVISIONING PROVISIONING_FAILED
T30 PROVISIONING_FAILED PROVISIONING
T31 PROVISIONING_FAILED ACTIVE
T32 PROVISIONING_FAILED MANUAL_REVIEW
T33 * ON_HOLD
T34 ON_HOLD *
T35 * WITHDRAWN
T36 * VERIFICATION_IN_PROGRESS
T37 ACTIVE ACTIVE
T38 APPROVED MANUAL_REVIEW
""".strip().splitlines())
TERMINAL = {"ACTIVE", "REJECTED", "WITHDRAWN", "EXPIRED", "DUPLICATE_MERGED"}
PRE_APPROVAL = {"DRAFT", "SUBMITTED", "DOC_PROCESSING", "VALIDATING",
                "AWAITING_MERCHANT_INFO", "VERIFICATION_IN_PROGRESS", "RISK_ASSESSMENT",
                "MANUAL_REVIEW"}
DONE = {"PASSED", "WAIVED"}
CHECKS = {"PAN", "GST", "BANK", "RISK", "MCA"}
PAN = r"[A-Z]{5}[0-9]{4}[A-Z]"
GST = rf"[0-9]{{2}}{PAN}[1-9A-Z]Z[0-9A-Z]"
CIN = r"[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}"
IFSC = r"[A-Z]{4}0[A-Z0-9]{6}"
BANK_TYPES = {"BANK_PROOF", "CANCELLED_CHEQUE", "BANK_STATEMENT", "BANK_LETTER"}
FIELD_SCHEMA = {
    "PAN_CARD": ("pan", "name", "father_name", "dob_or_doi", "date_of_incorporation"),
    "GST_CERTIFICATE": ("gstin", "legal_name", "trade_name", "constitution", "address",
                        "date_of_liability", "type_of_registration"),
    "INCORPORATION_CERT": ("cin", "company_name", "date_of_incorporation", "roc"),
    "PARTNERSHIP_DEED": ("firm_name", "partners", "deed_date", "registered"),
    "LLP_AGREEMENT": ("llpin", "llp_name", "designated_partners"),
    "BOARD_RESOLUTION": ("company_name", "authorised_signatory", "resolution_date",
                         "purpose_mentions_payments"),
    "ADDRESS_PROOF": ("name", "address", "doc_subtype", "issue_date"),
    "UDYAM_CERT": ("udyam_number", "enterprise_name", "type", "pan"),
    "SHOP_PHOTO": ("signage_text", "signage_matches_trade_name", "appears_operational"),
    "MOA_AOA": ("company_name", "date"),
    "TRUST_DEED": ("name", "date", "trustees"),
    "REGISTRATION_CERT": ("name", "registration_number", "date"),
    "OTHER": ("name",),
}
for kind in BANK_TYPES:
    FIELD_SCHEMA[kind] = ("account_number", "ifsc", "account_holder_name", "bank_name",
                          "branch", "micr", "cancelled_marking_present", "statement_period", "address")
REQUIRED_FIELDS = {
    "PAN_CARD": ("pan", "name"), "GST_CERTIFICATE": ("gstin", "legal_name"),
    "INCORPORATION_CERT": ("cin", "company_name"), "PARTNERSHIP_DEED": ("firm_name", "partners"),
    "LLP_AGREEMENT": ("llpin",), "BOARD_RESOLUTION": ("authorised_signatory",),
    "ADDRESS_PROOF": ("address", "issue_date"), "UDYAM_CERT": ("udyam_number",),
}
for kind in BANK_TYPES:
    REQUIRED_FIELDS[kind] = ("account_number", "account_holder_name") if kind == "BANK_STATEMENT" else (
        "account_number", "ifsc", "account_holder_name")


class DomainError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status


def require(allowed, message, code="ILLEGAL_TRANSITION", status=409):
    if not allowed:
        raise DomainError(code, message, status)


def transition(state, event, *, guard=True, resume_state=None):
    source, target = TRANSITIONS[event].split()
    allowed = source == state or source == "*"
    if event == "T33":
        allowed = state not in TERMINAL | {"ON_HOLD"}
    if event == "T34":
        allowed = allowed and resume_state in PRE_APPROVAL | {"APPROVED", "PROVISIONING", "PROVISIONING_FAILED"}
        target = resume_state
    if event == "T35":
        allowed = (resume_state if state == "ON_HOLD" else state) in PRE_APPROVAL
    if event == "T36":
        allowed = state in {"VERIFICATION_IN_PROGRESS", "RISK_ASSESSMENT", "MANUAL_REVIEW", "APPROVED"}
    require(allowed and guard, f"{event} cannot be applied from {state}.")
    return target


def gst_checksum(base):
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    total = 0
    for index, char in enumerate(reversed(base)):
        value = alphabet.index(char) * (2 if index % 2 == 0 else 1)
        total += value // 36 + value % 36
    return alphabet[-total % 36]


def gst_valid(value):
    return bool(re.fullmatch(GST, value)) and gst_checksum(value[:-1]) == value[-1]


def canonical(value):
    return str(value or "").strip().upper()


def normalize_name(value, config):
    tokens = re.findall(r"[\w&]+", str(value or "").lower())
    while tokens and tokens[0] in {"mr", "mrs", "ms", "dr", "shri", "smt"}:
        tokens.pop(0)
    for suffix in sorted(config["legal_suffixes"], key=len, reverse=True):
        ending = suffix.split()
        if tokens[-len(ending):] == ending:
            tokens = tokens[:-len(ending)]
            break
    return " ".join(tokens)


def similarity(left, right):
    return SequenceMatcher(None, left, right, autojunk=False).ratio() if left and right else 0


def name_score(left, right, config):
    a = set(normalize_name(left, config).split())
    b = set(normalize_name(right, config).split())
    common = " ".join(sorted(a & b))
    left = (common + " " + " ".join(sorted(a - b))).strip()
    right = (common + " " + " ".join(sorted(b - a))).strip()
    return max(similarity(left, right), similarity(common, left), similarity(common, right))


def required_checks(form, config):
    checks = set(config["required_checks"][form["business_type"]])
    rule = config["gst_required_if"]
    if ((form.get("annual_turnover_inr") or 0) >= rule["annual_turnover_inr_gte"]
            or form["business_category"] in rule["or_business_category_in"]):
        checks.add("GST")
    return checks


def issue(code, field=None, doc_type=None, severity="BLOCKING", review=False):
    label = (field or "").replace("_", " ")
    messages = {
        "DOC_MISSING": f"Please upload {doc_type}.",
        "FIELD_LOW_CONFIDENCE": f"Please confirm or replace the document value for {label}.",
        "FIELD_CONFLICT_FORM_DOC": f"Please confirm {label} and update the form or supporting document.",
    }
    return dict(code=code, field=field, doc_type=doc_type, severity=severity, review=review,
                message=messages.get(code, f"Please check {label or doc_type or 'your application'}."))


def validate(form, documents, cfg):
    issues, reconciliation = [], []
    app_bank = form.get("bank_account") or {}
    address = form["registered_address"]
    pan, gst, cin = (canonical(form.get(key)) for key in ("declared_pan", "declared_gstin", "declared_cin"))
    checks = required_checks(form, cfg)
    required_docs = set(cfg["required_documents"][form["business_type"]])
    if "GST" in checks:
        required_docs.add("GST_CERTIFICATE")
    present = set(documents)
    if present & BANK_TYPES:
        present.add("BANK_PROOF")
    issues.extend(issue("DOC_MISSING", doc_type=kind) for kind in sorted(required_docs - present))
    for okay, code, field in [
        (re.fullmatch(PAN, pan), "V-FMT-PAN", "declared_pan"),
        ((not gst and "GST" not in checks) or gst_valid(gst), "V-FMT-GSTIN", "declared_gstin"),
        ((not cin and "MCA" not in checks) or re.fullmatch(CIN, cin), "V-FMT-CIN", "declared_cin"),
        (re.fullmatch(IFSC, canonical(app_bank.get("ifsc"))), "V-FMT-IFSC", "bank_account.ifsc"),
        (re.fullmatch(r"[0-9]{9,18}", canonical(app_bank.get("account_number"))), "V-FMT-ACCT",
         "bank_account.account_number"),
        (app_bank.get("account_holder_name"), "FIELD_MISSING", "bank_account.account_holder_name"),
    ]:
        if not okay:
            issues.append(issue(code, field))
    holder_types = dict(INDIVIDUAL="P", PROPRIETORSHIP="P", PARTNERSHIP="F", LLP="F",
                        PRIVATE_LIMITED="C", PUBLIC_LIMITED="C", TRUST_SOCIETY_NGO="TAB", HUF="H")
    if len(pan) >= 4 and pan[3] not in holder_types[form["business_type"]]:
        issues.append(issue("V-X-03", "declared_pan"))
    if gst and gst[2:12] != pan:
        issues.append(issue("V-X-01", "declared_gstin"))
    if gst and gst[:2] not in {str(address.get("state_code")), str((form.get("operating_address") or {}).get("state_code"))}:
        issues.append(issue("V-X-02", "declared_gstin", severity="WARN"))
    for pattern, value, code, field in [
        (r"[1-9][0-9]{5}", address.get("pincode"), "V-FMT-PINCODE", "registered_address.pincode"),
        (r"\+91[6-9][0-9]{9}", form["contact"].get("phone"), "V-FMT-PHONE", "contact.phone"),
    ]:
        if not re.fullmatch(pattern, str(value or "")):
            issues.append(issue(code, field, severity="WARN"))
    persons = form.get("persons", [])
    if not any(p.get("role") == "AUTHORISED_SIGNATORY" for p in persons):
        issues.append(issue("V-C-04", "persons"))
    owners = [p for p in persons if p.get("role") in {"DIRECTOR", "PARTNER"} and p.get("ownership_pct") is not None]
    cutoff = 15 if form["business_type"] == "PARTNERSHIP" else 10
    ubos = {str(p.get("full_name", "")).casefold() for p in persons if p.get("role") == "UBO"}
    if any(p["ownership_pct"] >= cutoff and str(p.get("full_name", "")).casefold() not in ubos for p in owners):
        issues.append(issue("V-C-05", "persons"))
    if owners and abs(sum(p["ownership_pct"] for p in owners) - 100) > 1:
        issues.append(issue("V-X-13", "persons", severity="WARN"))
    turnover, gmv = form.get("annual_turnover_inr"), form.get("expected_monthly_gmv_inr")
    if turnover and gmv and gmv * 12 > turnover * 3:
        issues.append(issue("V-X-12", "expected_monthly_gmv_inr", severity="WARN"))
    for kind, doc in documents.items():
        fields = doc["fields"]
        for key in REQUIRED_FIELDS.get(kind, ()):
            entry = fields.get(key, {})
            if entry.get("value") in (None, "", []) or entry.get("status") in {"LOW_CONFIDENCE", "NEEDS_CONFIRMATION"}:
                issues.append(issue("FIELD_LOW_CONFIDENCE", key, kind))
        comparisons = {"PAN_CARD": {"pan": pan, "name": form["legal_name"]},
                       "GST_CERTIFICATE": {"gstin": gst, "legal_name": form["legal_name"]},
                       "INCORPORATION_CERT": {"cin": cin, "company_name": form["legal_name"]}}
        if kind in BANK_TYPES:
            comparisons[kind] = {"account_number": app_bank.get("account_number"), "ifsc": app_bank.get("ifsc"),
                                 "account_holder_name": form["legal_name"]}
        for key, expected in comparisons.get(kind, {}).items():
            observed = fields.get(key, {}).get("value")
            if not observed or not expected:
                continue
            is_name = "name" in key
            score = name_score(observed, expected, cfg["name_match"]) if is_name else float(canonical(observed) == canonical(expected))
            if key == "account_holder_name" and form["business_type"] == "PROPRIETORSHIP":
                score = max([score] + [name_score(observed, p.get("full_name"), cfg["name_match"])
                                       for p in persons if p.get("role") == "PROPRIETOR"])
            passing = cfg["name_match"]["pass_threshold"] if is_name else 1
            review = is_name and (score >= cfg["name_match"]["review_threshold"] or key == "account_holder_name")
            reconciliation.append((doc["id"], key, expected, observed, score, score >= passing))
            if score < passing:
                issues.append(issue("BANK_NAME_MISMATCH" if key == "account_holder_name" else "FIELD_CONFLICT_FORM_DOC",
                                    key, kind, review=review))
        if kind == "ADDRESS_PROOF" and fields.get("issue_date", {}).get("value"):
            try:
                age = (date.today() - date.fromisoformat(str(fields["issue_date"]["value"]))).days
            except ValueError:
                age = 91
            if not 0 <= age <= 90:
                issues.append(issue("V-X-11", "issue_date", kind, "WARN"))
        if kind == "CANCELLED_CHEQUE" and fields.get("cancelled_marking_present", {}).get("value") is not True:
            issues.append(issue("V-X-14", "cancelled_marking_present", kind, "WARN"))
        if kind == "UDYAM_CERT" and not re.fullmatch(r"UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}", canonical(fields.get("udyam_number", {}).get("value"))):
            issues.append(issue("V-FMT-UDYAM", "udyam_number", kind, "WARN"))
        if kind == "INCORPORATION_CERT" and re.fullmatch(CIN, cin):
            incorporation = str(fields.get("date_of_incorporation", {}).get("value") or "")[:4]
            if incorporation.isdigit() and int(cin[8:12]) > int(incorporation):
                issues.append(issue("V-X-09", "date_of_incorporation", kind, "WARN"))
        if kind == "PARTNERSHIP_DEED":
            partners = fields.get("partners", {}).get("value") or []
            names = {normalize_name(p.get("name"), cfg["name_match"]) for p in partners if isinstance(p, dict)}
            if any(normalize_name(p.get("full_name"), cfg["name_match"]) not in names for p in owners if p["role"] == "PARTNER"):
                issues.append(issue("V-X-10", "partners", kind, review=True))
    return issues, reconciliation


def decide(checks, mcc, flags, score, cfg, *, blocking=False, borderline=False, overridden=False):
    risk = cfg["risk"]
    codes = {row["result_code"] for row in checks}
    hard = set(risk["hard_reject_flags"]) - {"PEP_UNDECLARED"}
    rules = [
        ("R1_HARD_REJECT", "REJECT", bool(hard & set(flags) or {"PAN_INVALID", "PAN_DEACTIVATED", "PAN_NOT_FOUND"} & codes)),
        ("R2_GST_REQUIRED_FAILED", "REVIEW", any(c["check_type"] == "GST" and c["required"] and
         c["result_code"] in {"GST_CANCELLED", "GST_SUSPENDED", "GST_NOT_FOUND"} for c in checks)),
        ("R3_REQUIRED_NOT_PASSED", "WAIT", any(c["required"] and c["status"] not in DONE for c in checks)),
        ("R4_PEP_REVIEW", "REVIEW", "PEP_UNDECLARED" in flags or "PEP_DECLARED" in flags),
        ("R4_RISK_HIGH", "REJECT", score > risk["manual_review_max_score"]),
        ("R5_RISK_MEDIUM", "REVIEW", score > risk["auto_approve_max_score"]),
        ("R6_HIGH_RISK_MCC", "REVIEW", mcc in risk["high_risk_mcc"]),
        ("R7_WAIVERS_PRESENT", "REVIEW", overridden or any(c["status"] == "WAIVED" for c in checks)),
        ("R8_NAME_MATCH_BORDERLINE", "REVIEW", borderline),
        ("R9_OPEN_BLOCKING_EXCEPTIONS", "REVIEW", blocking),
        ("R10_APPROVE", "APPROVE", True),
    ]
    trace = [{"id": rule, "matched": matched} for rule, _, matched in rules]
    for rule, decision, matched in rules:
        if matched:
            return {"decision": decision, "matched_rule": rule, "rule_trace": trace}
