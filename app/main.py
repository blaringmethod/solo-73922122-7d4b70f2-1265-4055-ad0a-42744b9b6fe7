"""FastAPI application entry point."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import FhirError
from app.routers import bundle, intake, resources

app = FastAPI(
    title="Laboratory FHIR Research Ingest API",
    version="1.1.0",
    description=(
        "Ingests FHIR R4 transaction Bundles (Patient / Observation / "
        "DiagnosticReport) into the research library with idempotent, "
        "atomic, patient-scoped semantics. Try POST /fhir from this page. "
        "Reports larger than 20 entries use POST /fhir/intake (multi-group, "
        "resumable)."
    ),
)

app.include_router(bundle.router)
app.include_router(intake.router)
app.include_router(resources.router)


@app.exception_handler(FhirError)
async def fhir_error_handler(request: Request, exc: FhirError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_outcome(),
        media_type="application/fhir+json",
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Keep error bodies in FHIR OperationOutcome form for both ingest routes.
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(part) for part in first.get("loc", ()))
    diagnostics = f"{loc}: {first.get('msg', 'invalid request')}" if loc else "invalid request"
    return JSONResponse(
        status_code=400,
        content={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "invalid",
                    "details": {"text": "invalid"},
                    "diagnostics": diagnostics,
                }
            ],
        },
        media_type="application/fhir+json",
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak exception details to the caller; the transaction is rolled
    # back by the session dependency, so no partial writes survive.
    return JSONResponse(
        status_code=500,
        content={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "exception",
                    "details": {"text": "internal-error"},
                    "diagnostics": "An unexpected internal error occurred.",
                }
            ],
        },
        media_type="application/fhir+json",
    )


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "lab-fhir-research-ingest",
        "docs": "/docs",
        "submit": "POST /fhir (header Idempotency-Key required, max 20 entries)",
        "submit_large": (
            "POST /fhir/intake (header Accession-Number required, max 100 entries, "
            "grouped/resumable)"
        ),
    }
