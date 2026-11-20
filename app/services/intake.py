"""Large incoming report (来件) intake: grouped, resumable transaction Bundles.

A single laboratory report can exceed the regular 20-entry limit of
``POST /fhir``.  The large-intake endpoint accepts up to 100 POST-only entries
under one accession number.  Entries that are directly or indirectly connected
through ``subject`` / ``result`` references form one dependency group; groups
are submitted in order of their first appearance.

Semantics implemented here:

* Every reference inside the payload must be an internal ``urn:uuid`` and must
  resolve to an entry of the same payload.  An unresolved reference, an
  external absolute reference, or a group with more than 20 entries rejects the
  whole incoming report *before* anything is stored.
* Groups are applied one by one, each in its own database transaction together
  with its progress record.  When a group fails, processing stops; groups that
  already committed stay queryable through the normal read API.
* The accession number plus a canonical hash of the payload is the identity of
  the incoming report.  An identical retry skips completed groups and resumes
  the unfinished ones, returning the resource mapping and failure position in
  the caller's original entry order.  A different payload under the same
  accession number is rejected with 409.
* Progress is persisted in PostgreSQL, so a retry after a process restart
  resumes rather than reapplying completed groups.  Per-group row locks make
  concurrent identical retries serialize instead of double-storing resources.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import FhirError, bad_request, conflict
from app.models import IntakeGroupRecord, IntakeRecord
from app.services.bundle import (
    ParsedEntry,
    _canonicalize,
    apply_entries,
    content_hash,
    entry_receipt_parts,
    validate_bundle,
)

_ENTRY_INDEX_RE = re.compile(r"Bundle\.entry\[(\d+)\]")

STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_PROCESSING = "processing"
GROUP_PENDING = "pending"
GROUP_COMPLETED = "completed"
GROUP_FAILED = "failed"


@dataclass
class PlannedGroup:
    group_index: int
    first_entry_index: int
    entry_indexes: list[int]
    entries: list[ParsedEntry]


# --------------------------------------------------------------------------- #
# Planning: validate the payload and split it into dependency groups
# --------------------------------------------------------------------------- #

def validate_intake_bundle(bundle: object) -> list[ParsedEntry]:
    """Full pre-ingest validation, using original entry indexes in messages."""
    return validate_bundle(
        bundle,
        max_entries=settings.MAX_INTAKE_ENTRIES,
        post_only=True,
        require_urn_refs=True,
        # The same patient may legitimately be repeated across groups; the
        # conditional-create logic reuses the existing row.
        dedupe_patient_identifiers=False,
    )


def plan_groups(entries: list[ParsedEntry]) -> list[PlannedGroup]:
    """Split entries into connected components, ordered by first appearance.

    Edges are ``subject`` and ``DiagnosticReport.result`` urn:uuid references.
    Because validation already resolved every urn reference, components are
    closed: no group references an entry outside itself.
    """
    n = len(entries)
    parent = list(range(n))

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # Keep the smaller position as representative so ordering is stable.
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    position_by_urn = {
        entry.full_url: pos for pos, entry in enumerate(entries) if entry.full_url
    }
    for pos, entry in enumerate(entries):
        refs: list[str] = []
        if entry.subject_ref and entry.subject_ref.startswith("urn:uuid:"):
            refs.append(entry.subject_ref)
        if entry.resource_type == "DiagnosticReport":
            for ref_obj in entry.resource["result"]:
                ref = ref_obj["reference"].strip()
                if ref.startswith("urn:uuid:"):
                    refs.append(ref)
        for ref in refs:
            target = position_by_urn.get(ref)
            if target is None:  # pragma: no cover - validation guarantees this
                raise bad_request(
                    f"Bundle.entry[{entry.index}]: reference '{ref}' cannot be "
                    f"resolved inside the incoming report."
                )
            union(pos, target)

    buckets: dict[int, list[ParsedEntry]] = {}
    for pos, entry in enumerate(entries):
        buckets.setdefault(find(pos), []).append(entry)

    components = [sorted(bucket, key=lambda e: e.index) for bucket in buckets.values()]
    components.sort(key=lambda bucket: bucket[0].index)

    groups: list[PlannedGroup] = []
    for group_index, bucket in enumerate(components):
        if len(bucket) > settings.MAX_INTAKE_GROUP_ENTRIES:
            raise bad_request(
                f"Linked group starting at Bundle.entry[{bucket[0].index}] contains "
                f"{len(bucket)} entries; each group limit is "
                f"{settings.MAX_INTAKE_GROUP_ENTRIES}. The whole incoming report "
                f"was rejected."
            )
        _assert_unique_group_identifiers(bucket)
        groups.append(
            PlannedGroup(
                group_index=group_index,
                first_entry_index=bucket[0].index,
                entry_indexes=[e.index for e in bucket],
                entries=bucket,
            )
        )
    return groups


def _assert_unique_group_identifiers(group: list[ParsedEntry]) -> None:
    """The same patient identifier may span groups, but not repeat inside one."""
    seen: dict[tuple[str, str], int] = {}
    for entry in group:
        if entry.resource_type != "Patient":
            continue
        for ident in entry.resource.get("identifier") or []:
            pair = (ident.get("system") or "", str(ident.get("value", "")).strip())
            if pair in seen:
                raise bad_request(
                    f"Bundle.entry[{entry.index}]: duplicate patient identifier "
                    f"system='{pair[0]}' value='{pair[1]}' inside the same linked "
                    f"group (also at Bundle.entry[{seen[pair]}])."
                )
            seen[pair] = entry.index


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _register(
    session: Session, accession_number: str, hash_value: str, groups: list[PlannedGroup]
) -> IntakeRecord:
    """Create the ledger rows once; an existing accession number is reloaded."""
    session.add(
        IntakeRecord(
            accession_number=accession_number,
            content_hash=hash_value,
            state=STATUS_PROCESSING,
        )
    )
    for group in groups:
        session.add(
            IntakeGroupRecord(
                accession_number=accession_number,
                group_index=group.group_index,
                first_entry_index=group.first_entry_index,
                entry_indexes=list(group.entry_indexes),
                state=GROUP_PENDING,
            )
        )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.get(IntakeRecord, accession_number)
        if existing is None:  # pragma: no cover - extremely rare race
            raise conflict(
                f"Accession number '{accession_number}' is being registered "
                f"concurrently; retry."
            )
        if existing.content_hash != hash_value:
            raise conflict(
                f"Accession number '{accession_number}' was already submitted with "
                f"different content; reuse the number only for identical retries."
            )
        return existing
    record = session.get(IntakeRecord, accession_number)
    assert record is not None
    return record


def _group_rows(session: Session, accession_number: str) -> list[IntakeGroupRecord]:
    session.expire_all()
    rows = (
        session.execute(
            select(IntakeGroupRecord)
            .where(IntakeGroupRecord.accession_number == accession_number)
            .order_by(IntakeGroupRecord.group_index)
        )
        .scalars()
        .all()
    )
    return list(rows)


def _lock_group(session: Session, group_row_id: str) -> IntakeGroupRecord:
    stmt = (
        select(IntakeGroupRecord)
        .where(IntakeGroupRecord.id == group_row_id)
        .execution_options(populate_existing=True)
    )
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one()


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def process_intake(session: Session, bundle: dict, accession_number: str) -> dict:
    entries = validate_intake_bundle(bundle)
    groups = plan_groups(entries)
    hash_value = content_hash(bundle)

    record = _register(session, accession_number, hash_value, groups)
    if record.content_hash != hash_value:
        raise conflict(
            f"Accession number '{accession_number}' was already submitted with "
            f"different content; reuse the number only for identical retries."
        )

    # A finished report replays its stored receipt byte-for-byte.
    if record.state == STATUS_COMPLETED and record.response is not None:
        return _canonicalize(record.response)

    groups_by_index = {g.group_index: g for g in groups}

    failed = False
    for row in _group_rows(session, accession_number):
        if failed:
            break  # stop at the first failing group
        if row.state == GROUP_COMPLETED:
            continue  # resume: skip groups committed by an earlier attempt
        planned = groups_by_index.get(row.group_index)
        if planned is None:  # pragma: no cover - defensive (same hash)
            continue
        if _process_group(session, accession_number, row.id, planned) == "failed":
            failed = True

    return _canonicalize(_finalize(session, accession_number))


def _process_group(
    session: Session,
    accession_number: str,
    group_row_id: str,
    planned: PlannedGroup,
) -> str:
    """Apply one group; return 'completed', 'skipped' or 'failed'.

    The resource writes and the group's ``completed`` marker commit in the same
    transaction, so a crash can never leave a group applied but unrecorded.
    """
    session.rollback()
    try:
        row = _lock_group(session, group_row_id)
        if row.state == GROUP_COMPLETED:
            session.rollback()
            return "skipped"

        # Clear a previous failure so the resumed attempt is judged afresh.
        row.state = GROUP_PENDING
        row.error_status = None
        row.error_response = None
        row.error_entry_index = None

        apply_entries(session, planned.entries)

        row.state = GROUP_COMPLETED
        row.response = _build_group_receipt(planned.entries)
        session.commit()
        return "completed"
    except FhirError as exc:
        session.rollback()
        _record_group_failure(session, accession_number, group_row_id, planned, exc)
        return "failed"
    except Exception as exc:  # transient infrastructure failure
        session.rollback()
        _record_group_failure(
            session,
            accession_number,
            group_row_id,
            planned,
            _transient_error(exc),
        )
        return "failed"


def _transient_error(exc: Exception) -> FhirError:
    """Wrap an unexpected per-group failure as a retryable OperationOutcome."""
    return FhirError(
        500,
        "transient",
        f"Group processing failed with a transient error ({type(exc).__name__}); "
        f"resubmit the same incoming report to resume.",
    )


def _record_group_failure(
    session: Session,
    accession_number: str,
    group_row_id: str,
    planned: PlannedGroup,
    exc: FhirError,
) -> None:
    row = _lock_group(session, group_row_id)
    if row.state == GROUP_COMPLETED:
        # Another concurrent retry completed the group after our rollback.
        session.rollback()
        return
    row.state = GROUP_FAILED
    row.error_status = exc.status_code
    row.error_response = exc.to_outcome()
    row.error_entry_index = _failure_entry_index(exc, planned)

    record = session.get(IntakeRecord, accession_number)
    if record is not None:
        record.state = STATUS_PARTIAL
        record.updated_at = _now()
    session.commit()


def _failure_entry_index(exc: FhirError, planned: PlannedGroup) -> int:
    match = _ENTRY_INDEX_RE.search(exc.diagnostics or "")
    if match:
        index = int(match.group(1))
        if index in planned.entry_indexes:
            return index
    return planned.first_entry_index


def _build_group_receipt(entries: list[ParsedEntry]) -> dict:
    response_entries = []
    resource_map = []
    for entry in sorted(entries, key=lambda e: e.index):
        response_entry, map_entry = entry_receipt_parts(entry)
        response_entries.append({"requestIndex": entry.index, **response_entry})
        resource_map.append(map_entry)
    return {"entry": response_entries, "resourceMap": resource_map}


# --------------------------------------------------------------------------- #
# Final response
# --------------------------------------------------------------------------- #

def _finalize(session: Session, accession_number: str) -> dict:
    session.rollback()
    record = session.get(IntakeRecord, accession_number)
    rows = _group_rows(session, accession_number)

    all_completed = bool(rows) and all(r.state == GROUP_COMPLETED for r in rows)

    response_entries: dict[int, dict] = {}
    entry_map: dict[int, dict] = {}
    group_summaries = []
    failure = None
    for row in rows:
        group_summaries.append(
            {
                "groupIndex": row.group_index,
                "firstEntryIndex": row.first_entry_index,
                "entryIndexes": list(row.entry_indexes or []),
                "state": row.state,
            }
        )
        if row.state == GROUP_COMPLETED and row.response:
            for item in row.response.get("resourceMap", []):
                entry_map[item["requestIndex"]] = item
            for item in row.response.get("entry", []):
                index = item.get("requestIndex")
                if index is not None:
                    response_entries[index] = {
                        k: v for k, v in item.items() if k != "requestIndex"
                    }
        if row.state == GROUP_FAILED and failure is None:
            failure = {
                "groupIndex": row.group_index,
                "requestIndex": row.error_entry_index,
                "failedGroupEntryIndexes": list(row.entry_indexes or []),
                "status": row.error_status,
                "outcome": row.error_response,
            }

    result = {
        "resourceType": "Bundle",
        "type": "transaction-response",
        "accessionNumber": accession_number,
        "idempotencyKey": accession_number,
        "status": STATUS_COMPLETED if all_completed else STATUS_PARTIAL,
        "entry": [response_entries[i] for i in sorted(response_entries)],
        "resourceMap": [entry_map[i] for i in sorted(entry_map)],
        "groups": group_summaries,
        "failure": failure,
    }

    if record is not None:
        record.state = STATUS_COMPLETED if all_completed else STATUS_PARTIAL
        record.updated_at = _now()
        if all_completed:
            record.response = result
        session.commit()
    return result
