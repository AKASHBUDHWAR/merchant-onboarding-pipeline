# Specification review

## Defaults and resolved ambiguities

- FastAPI, Python 3.12, SQLite, and a persisted scheduler give the demo one process and one startup command. The API and browser console run on the same origin.
- Document fields come from structured payloads. PDF, PNG, and JPEG intake checks size, type, and page count, then stores the encrypted file. Supply structured metadata alongside a file to extract fields. An unsupported extraction path retries and opens an operator fallback task.
- The checklist omitted public limited companies and HUFs. Public limited companies inherit the private limited checklist. HUFs require PAN, bank, and address evidence with PAN, bank, and risk checks.
- The document listed undeclared PEP as both an auto-reject flag and a high-priority review example. The demo follows the explicit review scenario for PEP. Sanctions, blacklisting, and invalid PAN take the hard rejection path. Cancelled required GST goes to review. Info requests expire after their configured response SLA.
- The deterministic Indian identifier checks are fixture rules. The configured GST threshold is a demo input. Administrative waiver is restricted for hard-reject and risk checks. Reapplying after rejection has no configured cooling-off period.
- The mock providers share the API process. The local callback test credential is generated at runtime from a key stored outside SQLite. Set `ONBOARDING_CALLBACK_SECRET` and `ONBOARDING_DATA_KEY` to supply environment-specific secrets. `ONBOARDING_DATA_KEY` is a Fernet key. Keep the original key with your data when restarting the demo.
- Callback signatures cover `PROVIDER.TIMESTAMP.EVENT_ID.` followed by the raw request body. `X-Timestamp` is Unix seconds. `provider_timestamp` in the JSON body orders events. Header values `X-Event-Id` and `X-Signature` identify and authenticate deliveries.
- Sensitive form and extraction values are encrypted at rest. API responses mask identifier fields by default. PAN and bank account reveal require an admin identity and a reason. Document retention purges rejected application files and extracted values after the configured period. Free-text fields and uploaded binaries should contain synthetic data only.

## Demo API

The base is `/api/v1`. Operator identities are `reviewer-1`, `reviewer-2`, `admin`, and `admin-2`. Set `X-Role` to `ops_reviewer` or `ops_admin` as appropriate. Merchant operations use `X-Role: merchant` and the owning `X-User-Id`. All mutation routes accept an `Idempotency-Key`.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/ops/simulate/scenario \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: admin' -H 'X-Role: ops_admin' \
  -d '{"name":"happy_pvt_ltd","owner":"demo-merchant"}'
```

```bash
curl http://127.0.0.1:8000/api/v1/applications/APPLICATION_ID \
  -H 'X-User-Id: demo-merchant' -H 'X-Role: merchant'
```

For a normal submission, post an application to `/applications`, post each structured document to `/applications/{id}/documents`, then post to `/applications/{id}/submit`. The fixture JSON files show the format of the application and each document. The first endpoint takes only the application object. Fault injection belongs to fixtures run by the operator console.

For callbacks, POST to `/callbacks/{provider}` with raw JSON:

```json
{
  "provider_event_id": "synthetic-event-id",
  "provider_request_id": "request-id",
  "provider_timestamp": 1800000000,
  "status": "PASSED",
  "result_code": "PAN_VALID",
  "result": {}
}
```

## Scope boundaries

The demo covers the main lifecycle, including duplicate and late callbacks, retry and replay, document replacement, operator review, and post-activation bank change. Several detailed behaviors remain outside this implementation:

- Vision/OCR, quality inference, visual evidence overlays, live provider adapters, production authentication, and multi-worker deployment.
- Full provider-specific normalization and comparison of raw response fields. Callback results use the common normalized envelope. Review evidence is structured JSON with templated summaries.
- The full catalog of validation and exception-routing variants, including CIN-to-ROC lookup and a confidence-based GST-not-found correction rule. Documents and form fields can be corrected through the normal information-request flow.
- Capability types such as settlement are simulated. The bank-change flow supports verification and four-eyes approval after activation. A change during the brief `PROVISIONING` state is rejected and can be submitted again after activation.
- The guard table contains all 38 transition identifiers. Unit tests cover the state mapping and guard failure for each rule. Endpoint-level tests cover the fixture paths and common reliability cases. A separate demo recording is outside this delivery.
