from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Score = Annotated[float, Field(ge=0, le=1)]
Money = Annotated[int, Field(ge=0, le=10**15)]
Business = Literal["PROPRIETORSHIP", "PARTNERSHIP", "LLP", "PRIVATE_LIMITED", "PUBLIC_LIMITED",
                   "TRUST_SOCIETY_NGO", "INDIVIDUAL", "HUF"]
Category = Literal["RETAIL", "RESTAURANT", "ECOMMERCE_SELLER", "SERVICES", "EDUCATION", "HEALTHCARE",
                   "TRAVEL", "UTILITIES", "GOVERNMENT", "OTHER", "INTERSTATE_SUPPLY"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Address(Model):
    line1: Text
    line2: Text | None = None
    city: Text
    state: Text
    state_code: Annotated[str, Field(pattern=r"^[0-9]{2}$")]
    pincode: Text
    country: Literal["IN"] = "IN"


class Contact(Model):
    name: Text
    email: Text
    phone: Text
    designation: Text | None = None


class Person(Model):
    role: Literal["PROPRIETOR", "PARTNER", "DIRECTOR", "AUTHORISED_SIGNATORY", "UBO", "TRUSTEE"]
    full_name: Text
    pan: Text | None = None
    dob: Text | None = None
    ownership_pct: Annotated[float, Field(ge=0, le=100)] | None = None
    is_pep: bool | None = None
    aadhaar_last4: Annotated[str, Field(pattern=r"^[0-9]{4}$")] | None = None
    email: Text | None = None
    phone: Text | None = None


class Bank(Model):
    account_holder_name: Text
    account_number: Text
    ifsc: Text
    bank_name: Text | None = None
    account_type: Literal["CURRENT", "SAVINGS"] | None = None


class Application(Model):
    channel: Literal["MERCHANT_PORTAL", "SALES_AGENT", "PARTNER_API", "BULK_UPLOAD"] = "MERCHANT_PORTAL"
    business_type: Business
    business_category: Category
    legal_name: Text
    trade_name: Text | None = None
    mcc: Annotated[str, Field(pattern=r"^[0-9]{4}$")] | None = None
    declared_pan: Text | None = None
    declared_gstin: Text | None = None
    declared_cin: Text | None = None
    annual_turnover_inr: Money | None = None
    expected_monthly_gmv_inr: Money | None = None
    avg_ticket_inr: Money | None = None
    website_url: Text | None = None
    registered_address: Address
    operating_address: Address | None = None
    contact: Contact
    persons: list[Person] = Field(default_factory=list, max_length=100)
    bank_account: Bank | None = None


class DocSimulation(Model):
    quality_score: Score | None = None
    doc_type_confidence: Score | None = None
    detected_type: Text | None = None
    confidence_override: dict[str, Score] = Field(default_factory=dict)
    tampered: bool = False
    extraction_error: bool = False


class StructuredDocument(Model):
    doc_type: Text
    source: Literal["STRUCTURED_PAYLOAD"] = "STRUCTURED_PAYLOAD"
    payload: dict = Field(default_factory=dict)
    simulate: DocSimulation = Field(default_factory=DocSimulation)


class ProviderSimulation(Model):
    outcome: Literal["PASS", "FAIL", "INCONCLUSIVE", "ERROR_5XX", "TIMEOUT", "NO_CALLBACK"] = "PASS"
    result_code: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,70}$")] | None = None
    latency_ms: int = Field(default=500, ge=0, le=900000)
    timeout_ms: int | None = Field(default=None, ge=1, le=900000)
    duplicate_callbacks: int = Field(default=0, ge=0, le=10)
    late_callback_after_ms: int | None = Field(default=None, ge=0, le=900000)
    out_of_order: bool = False
    bad_signature: bool = False
    fail_first_n_attempts: int = Field(default=0, ge=0, le=20)
    risk_score: int | None = Field(default=None, ge=0, le=100)
    name_match_score: Score | None = None
    flags: list[Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,70}$")]] = Field(default_factory=list, max_length=25)


class MidSimulation(Model):
    fail_mid_creation: int = Field(default=0, ge=0, le=20)
    fail_capability: list[Text] = Field(default_factory=list)
    slow_ms: int = Field(default=0, ge=0, le=900000)


class Simulation(Model):
    PAN: ProviderSimulation | None = None
    GST: ProviderSimulation | None = None
    BANK: ProviderSimulation | None = None
    RISK: ProviderSimulation | None = None
    MCA: ProviderSimulation | None = None
    MID: MidSimulation | None = None


class Respond(Model):
    fields: dict = Field(default_factory=dict)
    documents: list[StructuredDocument] = Field(default_factory=list, max_length=25)
    confirm_fields: list[Text] = Field(default_factory=list, max_length=35)
    clarification: str | None = Field(default=None, max_length=2000)


class InfoItem(Model):
    kind: Literal["DOCUMENT", "FIELD", "CLARIFICATION"]
    doc_type: Text | None = None
    field: Text | None = None
    message: Text


class Reason(Model):
    reason: Text


class ReviewCommand(Model):
    decision: Literal["APPROVE", "REJECT", "REQUEST_INFO", "RERUN_CHECKS", "MERGE", "ESCALATE",
                      "HOLD", "WAIVE_CHECK", "CONFIRM", "MANUAL_MID", "ROLLBACK"]
    reason_codes: list[Annotated[str, Field(pattern=r"^[A-Z0-9_\-]{1,80}$")]] = Field(default_factory=list, max_length=20)
    notes: str = Field(default="", max_length=3000)
    info_items: list[InfoItem] = Field(default_factory=list, max_length=25)
    rerun_checks: list[Text] = Field(default_factory=list, max_length=5)
    merge_into: Text | None = None
    check_id: Text | None = None
    merchant_id: Text | None = None


class Scenario(Model):
    name: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,80}$")]
    owner: Text = "demo-merchant"


class Change(Model):
    change_type: Literal["BANK_ACCOUNT_CHANGED", "LEGAL_NAME_CHANGED", "GSTIN_CHANGED",
                         "AUTHORISED_SIGNATORY_CHANGED", "ADDRESS_CHANGED"]
    fields: dict
