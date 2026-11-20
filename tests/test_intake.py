"""Large incoming report (来件) intake: grouping, atomic groups, resume.

All tests run against the configured database (PostgreSQL by default). The
group-locking and concurrent-resubmission tests are PostgreSQL-specific and are
skipped on SQLite.
"""
from __future__ import annotations

import concurrent.futures

import pytest

from app import database
from app.database import SessionLocal
from app.models import DiagnosticReport, IntakeGroupRecord, IntakeRecord, Observation
from tests.conftest import (
    make_bundle,
    observation_entry,
    patient_entry,
    report_entry,
    urn,
)

URL = "/fhir/intake"


def _submit(client, accession, bundle, expect=200):
    resp = client.post(URL, json=bundle, headers={"Accession-Number": accession})
    assert resp.status_code == expect, resp.text
    return resp.json()


def _patient_group(value: str, n_obs: int):
    """One patient + n observations, all mutually connected through subject."""
    p_urn = urn()
    p, (system, value) = patient_entry(p_urn, value=value)
    obs = [observation_entry(urn(), p_urn, code=f"C{i}") for i in range(n_obs)]
    return p, (system, value), obs


def _report_group(value: str, n_obs: int):
    """Independent patient + report + observations, connected as one group."""
    p_urn = urn()
    p, (system, value) = patient_entry(p_urn, value=value)
    obs_urns = [urn() for _ in range(n_obs)]
    obs = [observation_entry(o, p_urn) for o in obs_urns]
    report = report_entry(urn(), p_urn, obs_urns)
    return p, obs, report


def test_intake_accepts_more_than_twenty_entries_in_several_groups(client):
    # Three independent groups; 16 + 17 + 4 entries = 37, over the /fhir limit.
    p1, (system, value1), obs1 = _patient_group("INTAKE-A", 15)
    p2, obs2, report2 = _report_group("INTAKE-B", 15)
    p3, _, obs3 = _patient_group("INTAKE-C", 3)

    entries = [p1, *obs1, p2, *obs2, report2, p3, *obs3]
    assert len(entries) == 37
    # Deliberately shuffle each group: grouping is by references, not adjacency.
    entries = entries[3:19] + entries[19:35] + entries[0:3] + entries[35:]

    receipt = _submit(client, "ACC-BIG-1", make_bundle(entries))
    assert receipt["status"] == "completed"
    assert receipt["accessionNumber"] == "ACC-BIG-1"
    assert receipt["failure"] is None
    assert len(receipt["resourceMap"]) == len(entries)
    # Resource map is returned in original entry order.
    assert [m["requestIndex"] for m in receipt["resourceMap"]] == list(range(len(entries)))
    assert all(m["action"] == "created" for m in receipt["resourceMap"])
    assert [g["state"] for g in receipt["groups"]] == ["completed"] * 3

    patient = client.get("/patients", params={"identifier": f"{system}|{value1}"})
    assert patient.status_code == 200
    pid = patient.json()["id"]
    assert client.get(f"/patients/{pid}/observations").json()["total"] == 15


def test_intake_rejects_more_than_100_entries(client):
    entries = [patient_entry(urn())[0] for _ in range(101)]
    resp = client.post(URL, json=make_bundle(entries), headers={"Accession-Number": "ACC-101"})
    assert resp.status_code == 400
    assert "limit is 100" in resp.json()["issue"][0]["diagnostics"]


def test_intake_rejects_non_post_entries(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn)
    body = {
        "resource": {
            "resourceType": "Observation",
            "id": "x",
            "status": "final",
            "subject": {"reference": p_urn},
        },
        "request": {"method": "PUT", "url": "Observation/x", "ifMatch": 'W/"1"'},
    }
    resp = client.post(URL, json=make_bundle([p, body]), headers={"Accession-Number": "ACC-PUT"})
    assert resp.status_code == 400
    assert "must be POST" in resp.json()["issue"][0]["diagnostics"]


def test_intake_rejects_external_references_before_storing(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="INTAKE-EXT")
    o = observation_entry(urn(), p_urn)
    r = report_entry(urn(), p_urn, ["Observation/external-id"])
    resp = client.post(
        URL, json=make_bundle([p, o, r]), headers={"Accession-Number": "ACC-EXT"}
    )
    assert resp.status_code == 400
    assert "urn:uuid" in resp.json()["issue"][0]["diagnostics"]
    # Nothing was stored, not even the valid patient entry.
    assert client.get(
        "/patients", params={"identifier": "http://hospital/mrn|INTAKE-EXT"}
    ).status_code == 404


def test_intake_rejects_unresolvable_urn_before_storing(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="INTAKE-GHOST")
    ghost = urn()
    r = report_entry(urn(), p_urn, [ghost])
    resp = client.post(
        URL, json=make_bundle([p, r]), headers={"Accession-Number": "ACC-GHOST"}
    )
    assert resp.status_code == 422
    assert "cannot be resolved" in resp.json()["issue"][0]["diagnostics"]
    assert client.get(
        "/patients", params={"identifier": "http://hospital/mrn|INTAKE-GHOST"}
    ).status_code == 404


def test_intake_rejects_oversized_linked_group_and_stores_nothing(client):
    # A single patient with 20 observations = 21 connected entries, over the
    # per-group limit, even though the total is under 100.
    p_urn = urn()
    p, (system, value) = patient_entry(p_urn, value="GROUP-TOO-BIG")
    obs = [observation_entry(urn(), p_urn, code=f"G{i}") for i in range(20)]
    resp = client.post(
        URL,
        json=make_bundle([p, *obs]),
        headers={"Accession-Number": "ACC-GROUP-TOO-BIG"},
    )
    assert resp.status_code == 400
    diagnostics = resp.json()["issue"][0]["diagnostics"]
    assert "group limit is 20" in diagnostics
    assert client.get(
        "/patients", params={"identifier": f"{system}|{value}"}
    ).status_code == 404


def test_intake_accepts_group_of_exactly_twenty(client):
    p_urn = urn()
    p, (system, value) = patient_entry(p_urn, value="GROUP-20")
    obs = [observation_entry(urn(), p_urn, code=f"E{i}") for i in range(19)]
    receipt = _submit(client, "ACC-GROUP-20", make_bundle([p, *obs]))
    assert receipt["status"] == "completed"
    pid = client.get("/patients", params={"identifier": f"{system}|{value}"}).json()["id"]
    assert client.get(f"/patients/{pid}/observations").json()["total"] == 19


def test_intake_failed_group_stops_later_groups_and_keeps_earlier_ones(client):
    # Group 1 is valid and independent.
    p1, (system, value1), obs1 = _patient_group("INTAKE-OK", 2)

    # Group 2 is internally inconsistent: report belongs to patient B while its
    # observation belongs to patient A. References resolve, so this fails only
    # when the group is applied.
    a_urn, b_urn = urn(), urn()
    pa, (_, _) = patient_entry(a_urn, value="INTAKE-MIX-A")
    pb, _ = patient_entry(b_urn, value="INTAKE-MIX-B")
    o_urn = urn()
    o = observation_entry(o_urn, a_urn)
    bad_report = report_entry(urn(), b_urn, [o_urn])

    # Group 3 is valid but must not be submitted after group 2 fails.
    p3, (system, value3), obs3 = _patient_group("INTAKE-AFTER", 1)

    entries = [p1, *obs1, pa, pb, o, bad_report, p3, *obs3]
    receipt = _submit(client, "ACC-PARTIAL", make_bundle(entries))

    assert receipt["status"] == "partial"
    assert [g["state"] for g in receipt["groups"]] == ["completed", "failed", "pending"]
    assert receipt["failure"]["groupIndex"] == 1
    assert receipt["failure"]["status"] == 422
    # Group 1 resources are mapped and readable.
    mapped = {m["requestIndex"] for m in receipt["resourceMap"]}
    assert mapped == {0, 1, 2}  # patient + two observations
    pid1 = client.get("/patients", params={"identifier": f"{system}|{value1}"}).json()["id"]
    assert client.get(f"/patients/{pid1}/observations").json()["total"] == 2
    # Group 2 rolled back entirely, group 3 never started.
    for name in ("INTAKE-MIX-A", "INTAKE-MIX-B", f"{system}|{value3}"):
        assert client.get("/patients", params={"identifier": name}).status_code == 404


def test_intake_retry_is_idempotent_and_resumes_after_failure(client, monkeypatch):
    p1, (system, value1), obs1 = _patient_group("RESUME-OK", 2)
    p2, (system2, value2), obs2 = _patient_group("RESUME-TRANSIENT", 2)
    entries = [p1, *obs1, p2, *obs2]
    bundle = make_bundle(entries)

    # Simulate a transient infrastructure failure on the second group only.
    from app.services import intake as intake_service

    real_apply = intake_service.apply_entries
    calls = {"n": 0}

    def flaky_apply(session, group_entries):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated process/database failure")
        return real_apply(session, group_entries)

    monkeypatch.setattr(intake_service, "apply_entries", flaky_apply)
    first = _submit(client, "ACC-RESUME", bundle)
    assert first["status"] == "partial"
    assert [g["state"] for g in first["groups"]] == ["completed", "failed"]
    assert first["failure"]["status"] == 500
    pid1 = client.get("/patients", params={"identifier": f"{system}|{value1}"}).json()["id"]
    assert client.get(f"/patients/{pid1}/observations").json()["total"] == 2
    assert client.get(
        "/patients", params={"identifier": f"{system2}|{value2}"}
    ).status_code == 404

    # Retry with the same number and content: completed group 1 is skipped,
    # group 2 is applied. No duplicate resources for group 1.
    monkeypatch.setattr(intake_service, "apply_entries", real_apply)
    second = _submit(client, "ACC-RESUME", bundle)
    assert second["status"] == "completed"
    assert len(second["resourceMap"]) == len(entries)
    assert [m["requestIndex"] for m in second["resourceMap"]] == list(range(len(entries)))
    assert len(client.get(f"/patients/{pid1}/observations").json()["entry"]) == 2

    # A further identical retry is a pure replay.
    third = _submit(client, "ACC-RESUME", bundle)
    assert third["resourceMap"] == second["resourceMap"]


def test_intake_same_number_different_content_rejected(client):
    p, (system, value) = patient_entry(urn(), value="INTAKE-CONFLICT-A")
    _submit(client, "ACC-CONFLICT", make_bundle([p]))
    p2, (system2, value2) = patient_entry(urn(), value="INTAKE-CONFLICT-B")
    resp = client.post(
        URL, json=make_bundle([p2]), headers={"Accession-Number": "ACC-CONFLICT"}
    )
    assert resp.status_code == 409
    assert "different content" in resp.json()["issue"][0]["diagnostics"]
    assert client.get(
        "/patients", params={"identifier": f"{system2}|{value2}"}
    ).status_code == 404


def test_intake_accepts_exactly_one_hundred_entries_in_five_groups(client):
    entries: list[dict] = []
    identifiers = []
    for g in range(5):
        p_urn = urn()
        p, ident = patient_entry(p_urn, value=f"MAX100-{g}")
        identifiers.append((p_urn, ident))
        entries.append(p)
        for i in range(19):  # group of exactly 20 connected entries
            entries.append(observation_entry(urn(), p_urn, code=f"G{g}-{i}"))
    assert len(entries) == 100

    receipt = _submit(client, "ACC-100", make_bundle(entries))
    assert receipt["status"] == "completed"
    assert len(receipt["resourceMap"]) == 100
    assert [g["state"] for g in receipt["groups"]] == ["completed"] * 5
    assert [g["entryIndexes"][0] for g in receipt["groups"]] == [0, 20, 40, 60, 80]


def test_intake_completed_receipt_replays_identically(client):
    p, (system, value), obs = _patient_group("REPLAY-1", 2)
    bundle = make_bundle([p, *obs])
    raw1 = client.post(
        URL, json=bundle, headers={"Accession-Number": "ACC-REPLAY"}
    ).content
    raw2 = client.post(
        URL, json=bundle, headers={"Accession-Number": "ACC-REPLAY"}
    ).content
    assert raw1 == raw2


def test_intake_missing_accession_number_rejected(client):
    p, _ = patient_entry(urn())
    assert client.post(URL, json=make_bundle([p])).status_code == 400


def test_intake_accepts_number_in_header_case_insensitively(client):
    p, _ = patient_entry(urn(), value="HDR-CASE")
    resp = client.post(
        URL, json=make_bundle([p]), headers={"accession-number": "ACC-HDR-CASE"}
    )
    assert resp.status_code == 200
    assert resp.headers["x-intake-status"] == "completed"


def test_intake_invalid_body_rejected_with_operation_outcome(client):
    resp = client.post(
        URL,
        content=b"[1,2,3]",
        headers={"Accession-Number": "ACC-BADBODY", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["resourceType"] == "OperationOutcome"

    resp2 = client.post(
        URL,
        content=b"{not json",
        headers={"Accession-Number": "ACC-BADJSON", "Content-Type": "application/json"},
    )
    assert resp2.status_code == 400
    assert resp2.json()["resourceType"] == "OperationOutcome"


def test_intake_partial_receipt_exposes_failure_headers(client):
    p1, _, obs1 = _patient_group("HDR-OK", 1)
    # Second group references resolve, but report patient B links an observation
    # of patient A, so it only fails when the group is applied.
    a_urn, b_urn = urn(), urn()
    pa, _ = patient_entry(a_urn, value="HDR-BAD-A")
    pb, _ = patient_entry(b_urn, value="HDR-BAD-B")
    o = observation_entry(urn(), a_urn)
    report = report_entry(urn(), b_urn, [o["fullUrl"]])
    entries = [p1, *obs1, pa, pb, o, report]
    resp = client.post(
        URL, json=make_bundle(entries), headers={"Accession-Number": "ACC-HDR-PARTIAL"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "partial"
    assert resp.headers["x-intake-status"] == "partial"
    assert resp.headers["x-intake-failed-group"] == "1"
    assert resp.headers["x-intake-failed-entry"] == "5"
    assert body["failure"]["groupIndex"] == 1
    assert body["failure"]["requestIndex"] == 5  # the report entry position
    assert body["failure"]["outcome"]["resourceType"] == "OperationOutcome"
    assert body["failure"]["outcome"]["issue"][0]["diagnostics"]


def test_intake_failure_position_uses_original_entry_index(client):
    # Group 2's report sits at position 5 in the submitted order even though the
    # report is listed before its patient/observation by fullUrl order.
    p1, _, obs1 = _patient_group("POS-OK", 1)
    a_urn, b_urn = urn(), urn()
    pa, _ = patient_entry(a_urn, value="POS-A")
    pb, _ = patient_entry(b_urn, value="POS-B")
    o = observation_entry(urn(), a_urn)
    r = report_entry(urn(), b_urn, [o["fullUrl"]])
    entries = [p1, *obs1, pa, o, pb]  # report appended last at index 5
    entries.append(r)
    body = _submit(client, "ACC-POS", make_bundle(entries))
    assert body["failure"]["requestIndex"] == 5
    assert body["failure"]["groupIndex"] == 1
    assert body["groups"][1]["entryIndexes"] == [2, 3, 4, 5]


def test_intake_resumes_after_process_restart(client, monkeypatch):
    """Ledger progress survives the process; a fresh client resumes at group 2.

    The first attempt is cut off *after* group 1 commits, as if the process
    died before group 2 was attempted (no failure recorded for group 2).
    """
    from app.services import intake as intake_service

    p1, (system1, value1), obs1 = _patient_group("RESTART-1", 1)
    p2, (system2, value2), obs2 = _patient_group("RESTART-2", 1)
    entries = [p1, *obs1, p2, *obs2]
    bundle = make_bundle(entries)

    real_process_group = intake_service._process_group
    seen = {"n": 0}

    class _ProcessDied(BaseException):
        pass

    def die_after_first(session, accession_number, group_row_id, planned):
        seen["n"] += 1
        if seen["n"] > 1:
            # The first group already committed; abort the process abruptly.
            raise _ProcessDied()
        return real_process_group(session, accession_number, group_row_id, planned)

    monkeypatch.setattr(intake_service, "_process_group", die_after_first)
    # Drive the service directly so the simulated crash is not mistaken for an
    # HTTP error and no response is ever produced.
    with SessionLocal() as session:
        with pytest.raises(_ProcessDied):
            intake_service.process_intake(session, bundle, "ACC-RESTART")

    monkeypatch.setattr(intake_service, "_process_group", real_process_group)

    # The committed progress of group 1 is visible in the ledger.
    with SessionLocal() as session:
        record = session.get(IntakeRecord, "ACC-RESTART")
        assert record is not None and record.state == "processing"
        states = [
            row.state
            for row in session.query(IntakeGroupRecord)
            .filter_by(accession_number="ACC-RESTART")
            .order_by(IntakeGroupRecord.group_index)
        ]
        assert states == ["completed", "pending"]

    # A brand new client (fresh app process) resumes the remaining group.
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as fresh:
        receipt = _submit(fresh, "ACC-RESTART", bundle)
    assert receipt["status"] == "completed"
    assert len(receipt["resourceMap"]) == len(entries)

    pid1 = client.get("/patients", params={"identifier": f"{system1}|{value1}"}).json()["id"]
    pid2 = client.get("/patients", params={"identifier": f"{system2}|{value2}"}).json()["id"]
    assert client.get(f"/patients/{pid1}/observations").json()["total"] == 1
    assert client.get(f"/patients/{pid2}/observations").json()["total"] == 1

    with SessionLocal() as session:
        assert session.query(DiagnosticReport).count() == 0
        assert session.query(Observation).count() == 2


def test_intake_concurrent_identical_resubmits_do_not_duplicate(client):
    if database.engine.dialect.name == "sqlite":
        pytest.skip("concurrency test requires PostgreSQL")

    p1, (system, value), obs1 = _patient_group("CONCURRENT-INTAKE", 3)
    p2, _, obs2 = _patient_group("CONCURRENT-INTAKE-2", 3)
    bundle = make_bundle([p1, *obs1, p2, *obs2])

    def submit(_):
        return client.post(
            URL, json=bundle, headers={"Accession-Number": "ACC-CONCURRENT"}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(submit, range(6)))

    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    receipts = [r.json() for r in responses]
    assert all(r["status"] == "completed" for r in receipts)
    # Every response maps the same ids: no duplicated resources.
    maps = {tuple(m["id"] for m in r["resourceMap"]) for r in receipts}
    assert len(maps) == 1

    pid = client.get("/patients", params={"identifier": f"{system}|{value}"}).json()["id"]
    assert client.get(f"/patients/{pid}/observations").json()["total"] == 3
    with SessionLocal() as session:
        assert session.query(Observation).count() == 6
        assert (
            session.query(IntakeGroupRecord)
            .filter_by(accession_number="ACC-CONCURRENT", state="completed")
            .count()
            == 2
        )
