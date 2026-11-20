"""Large incoming report (来件) intake endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_session
from app.errors import bad_request
from app.services.intake import process_intake

router = APIRouter(tags=["intake"])

_PATIENT_URN = "urn:uuid:aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
_OBS_URN = "urn:uuid:bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
_REPORT_URN = "urn:uuid:cccccccc-3333-4333-8333-cccccccccccc"
_PATIENT2_URN = "urn:uuid:dddddddd-4444-4444-8444-dddddddddddd"
_OBS2_URN = "urn:uuid:eeeeeeee-5555-4555-8555-eeeeeeeeeeee"

_INTAKE_EXAMPLE = {
    "resourceType": "Bundle",
    "type": "transaction",
    "entry": [
        {
            "fullUrl": _REPORT_URN,
            "resource": {
                "resourceType": "DiagnosticReport",
                "status": "final",
                "code": {"coding": [{"system": "http://loinc.org", "code": "24323-8"}]},
                "subject": {"reference": _PATIENT_URN},
                "result": [{"reference": _OBS_URN}],
            },
            "request": {"method": "POST", "url": "DiagnosticReport"},
        },
        {
            "fullUrl": _PATIENT_URN,
            "resource": {
                "resourceType": "Patient",
                "identifier": [
                    {"system": "http://hospital/mrn", "value": "DEMO-LARGE-1"}
                ],
            },
            "request": {"method": "POST", "url": "Patient"},
        },
        {
            "fullUrl": _OBS_URN,
            "resource": {
                "resourceType": "Observation",
                "status": "final",
                "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7"}]},
                "subject": {"reference": _PATIENT_URN},
                "valueQuantity": {"value": 5.5, "unit": "mmol/L"},
            },
            "request": {"method": "POST", "url": "Observation"},
        },
        {
            "fullUrl": _PATIENT2_URN,
            "resource": {
                "resourceType": "Patient",
                "identifier": [
                    {"system": "http://hospital/mrn", "value": "DEMO-LARGE-2"}
                ],
            },
            "request": {"method": "POST", "url": "Patient"},
        },
        {
            "fullUrl": _OBS2_URN,
            "resource": {
                "resourceType": "Observation",
                "status": "final",
                "code": {"coding": [{"system": "http://loinc.org", "code": "2345-8"}]},
                "subject": {"reference": _PATIENT2_URN},
                "valueQuantity": {"value": 6.2, "unit": "mmol/L"},
            },
            "request": {"method": "POST", "url": "Observation"},
        },
    ],
}


_PARTIAL_DEMO_EXAMPLE = {
    "resourceType": "Bundle",
    "type": "transaction",
    "entry": [
        # --- group 1 (valid) ---
        {
            "fullUrl": "urn:uuid:1a000001-1111-4111-8111-000000000001",
            "resource": {
                "resourceType": "Patient",
                "identifier": [
                    {"system": "http://hospital/mrn", "value": "DEMO-FAIL-G1"}
                ],
            },
            "request": {"method": "POST", "url": "Patient"},
        },
        {
            "fullUrl": "urn:uuid:1a000002-2222-4222-8222-000000000002",
            "resource": {
                "resourceType": "Observation",
                "status": "final",
                "code": {"coding": [{"code": "2345-7"}]},
                "subject": {
                    "reference": "urn:uuid:1a000001-1111-4111-8111-000000000001"
                },
            },
            "request": {"method": "POST", "url": "Observation"},
        },
        # --- group 2 (fails: report patient B, observation patient A) ---
        {
            "fullUrl": "urn:uuid:2a000001-1111-4111-8111-000000000001",
            "resource": {
                "resourceType": "Patient",
                "identifier": [
                    {"system": "http://hospital/mrn", "value": "DEMO-FAIL-A"}
                ],
            },
            "request": {"method": "POST", "url": "Patient"},
        },
        {
            "fullUrl": "urn:uuid:2a000002-2222-4222-8222-000000000002",
            "resource": {
                "resourceType": "Patient",
                "identifier": [
                    {"system": "http://hospital/mrn", "value": "DEMO-FAIL-B"}
                ],
            },
            "request": {"method": "POST", "url": "Patient"},
        },
        {
            "fullUrl": "urn:uuid:2a000003-3333-4333-8333-000000000003",
            "resource": {
                "resourceType": "Observation",
                "status": "final",
                "code": {"coding": [{"code": "NA"}]},
                "subject": {
                    "reference": "urn:uuid:2a000001-1111-4111-8111-000000000001"
                },
            },
            "request": {"method": "POST", "url": "Observation"},
        },
        {
            "fullUrl": "urn:uuid:2a000004-4444-4444-8444-000000000004",
            "resource": {
                "resourceType": "DiagnosticReport",
                "status": "final",
                "code": {"coding": [{"code": "24323-8"}]},
                "subject": {
                    "reference": "urn:uuid:2a000002-2222-4222-8222-000000000002"
                },
                "result": [
                    {
                        "reference": (
                            "urn:uuid:2a000003-3333-4333-8333-000000000003"
                        )
                    }
                ],
            },
            "request": {"method": "POST", "url": "DiagnosticReport"},
        },
    ],
}


@router.post(
    "/fhir/intake",
    summary="Submit a large incoming report (multi-group, resumable)",
    description=(
        "Accepts a POST-only FHIR R4 transaction Bundle of up to 100 entries "
        "under one accession number (header `Accession-Number`). Entries joined "
        "directly or indirectly through `subject` / `result` `urn:uuid` "
        "references form a group; groups are submitted in order of first "
        "appearance and each group commits atomically with its receipt. A group "
        "failure stops submission, but earlier groups stay queryable. Re-sending "
        "the same number with identical content skips completed groups and "
        "resumes the rest, returning resources and the failure position in the "
        "original entry order. A different payload under the same number gets "
        "409. In the first example the first three entries are group 1 and the "
        "last two are group 2; unrelated entries become separate groups. Use the "
        "second example (`LAB-ACC-DEMO-FAIL`) to demo partial failure and resume."
    ),
    responses={
        200: {
            "description": (
                "Receipt in original entry order. `status` is `completed` or "
                "`partial`; `failure` is null when complete. Response headers "
                "`X-Intake-Status` (and, when partial, `X-Intake-Failed-Group` / "
                "`X-Intake-Failed-Entry`) summarise the outcome."
            ),
            "content": {
                "application/fhir+json": {
                    "example": {
                        "resourceType": "Bundle",
                        "type": "transaction-response",
                        "accessionNumber": "LAB-ACC-2001",
                        "idempotencyKey": "LAB-ACC-2001",
                        "status": "partial",
                        "entry": [
                            {
                                "response": {
                                    "status": "201 Created",
                                    "etag": 'W/"1"',
                                    "location": "Patient/6f1c.../_history/1",
                                }
                            }
                        ],
                        "resourceMap": [
                            {
                                "requestIndex": 0,
                                "method": "POST",
                                "resourceType": "Patient",
                                "id": "6f1c...",
                                "version": 1,
                                "action": "created",
                                "fullUrl": _PATIENT_URN,
                            }
                        ],
                        "groups": [
                            {
                                "groupIndex": 0,
                                "firstEntryIndex": 0,
                                "entryIndexes": [0, 1, 2],
                                "state": "completed",
                            },
                            {
                                "groupIndex": 1,
                                "firstEntryIndex": 3,
                                "entryIndexes": [3, 4],
                                "state": "failed",
                            },
                        ],
                        "failure": {
                            "groupIndex": 1,
                            "requestIndex": 4,
                            "failedGroupEntryIndexes": [3, 4],
                            "status": 422,
                            "outcome": {
                                "resourceType": "OperationOutcome",
                                "issue": [
                                    {
                                        "severity": "error",
                                        "code": "unprocessable",
                                        "details": {"text": "unprocessable"},
                                        "diagnostics": "Bundle.entry[4]: ...",
                                    }
                                ],
                            },
                        },
                    }
                }
            },
        },
        400: {
            "description": (
                "Invalid header/payload, a non-POST entry, an absolute/external "
                "reference, or a linked group over 20 entries. Nothing was stored."
            )
        },
        409: {
            "description": (
                "Accession number reused with different content; reuse it only "
                "for identical retries."
            )
        },
        422: {
            "description": (
                "An internal urn:uuid reference cannot be resolved or has the "
                "wrong resource type. Nothing was stored."
            )
        },
    },
)
def submit_intake_bundle(
    bundle: dict = Body(
        ...,
        openapi_examples={
            "two_groups": {
                "summary": "Two groups under one accession number",
                "description": (
                    "Submit with `Accession-Number: LAB-ACC-2001`; send the exact "
                    "same body again to replay (or resume after a partial "
                    "failure)."
                ),
                "value": _INTAKE_EXAMPLE,
            },
            "partial_failure_demo": {
                "summary": "Partial failure + resume demo",
                "description": (
                    "Group 1 is valid; group 2 links an observation of patient A "
                    "to a report of patient B, so it fails at apply time. Submit "
                    "with `Accession-Number: LAB-ACC-DEMO-FAIL`: group 1 commits "
                    "and stays readable, group 2 fails, group 3 never starts. "
                    "Re-send the identical body to get the same partial receipt "
                    "without duplicating group 1."
                ),
                "value": _PARTIAL_DEMO_EXAMPLE,
            },
        },
    ),
    accession_number: str = Header(
        ...,
        alias="Accession-Number",
        description=(
            "Incoming report/accession number (来件编号). Reuse the same value "
            "with the same payload to resume after a partial failure."
        ),
        examples=["LAB-ACC-2001"],
    ),
    session: Session = Depends(get_session),
):
    if not accession_number.strip():
        raise bad_request(
            "Header 'Accession-Number' (incoming report/accession number) is required."
        )
    if not isinstance(bundle, dict):
        raise bad_request("Request body must be a JSON FHIR Bundle object.")
    receipt = process_intake(session, bundle, accession_number.strip())
    headers = {"X-Intake-Status": receipt["status"]}
    if receipt.get("failure"):
        headers["X-Intake-Failed-Group"] = str(receipt["failure"]["groupIndex"])
        headers["X-Intake-Failed-Entry"] = str(receipt["failure"]["requestIndex"])
    return JSONResponse(
        receipt, media_type="application/fhir+json", headers=headers
    )
