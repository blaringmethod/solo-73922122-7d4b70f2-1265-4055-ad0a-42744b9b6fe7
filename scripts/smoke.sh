#!/usr/bin/env bash
# End-to-end smoke test against a running instance (default http://localhost:8000).
# Usage: BASE_URL=http://localhost:8000 ./scripts/smoke.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
KEY="SMOKE-$(date +%s)"
PATIENT_URN="urn:uuid:11111111-1111-4111-8111-111111111111"
OBS_URN="urn:uuid:22222222-2222-4222-8222-222222222222"
REPORT_URN="urn:uuid:33333333-3333-4333-8333-333333333333"

BUNDLE=$(cat <<JSON
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {"fullUrl": "$REPORT_URN",
     "resource": {"resourceType": "DiagnosticReport", "status": "final",
       "code": {"coding": [{"code": "24323-8"}]},
       "subject": {"reference": "$PATIENT_URN"},
       "result": [{"reference": "$OBS_URN"}]},
     "request": {"method": "POST", "url": "DiagnosticReport"}},
    {"fullUrl": "$OBS_URN",
     "resource": {"resourceType": "Observation", "status": "final",
       "code": {"coding": [{"code": "2345-7"}]},
       "subject": {"reference": "$PATIENT_URN"},
       "valueQuantity": {"value": 5.5, "unit": "mmol/L"}},
     "request": {"method": "POST", "url": "Observation"}},
    {"fullUrl": "$PATIENT_URN",
     "resource": {"resourceType": "Patient",
       "identifier": [{"system": "http://hospital/mrn", "value": "$KEY"}]},
     "request": {"method": "POST", "url": "Patient"}}
  ]
}
JSON
)

echo "1) submit out-of-order bundle"
RECEIPT=$(curl -fsS -X POST "$BASE_URL/fhir" -H "Idempotency-Key: $KEY" \
  -H "Content-Type: application/json" -d "$BUNDLE")
echo "$RECEIPT" | python3 -m json.tool | head -20

echo "2) identical retry must return the same ids"
REPLAY=$(curl -fsS -X POST "$BASE_URL/fhir" -H "Idempotency-Key: $KEY" \
  -H "Content-Type: application/json" -d "$BUNDLE")
canon() { python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['resourceMap'], sort_keys=True))"; }
diff <(echo "$RECEIPT" | canon) <(echo "$REPLAY" | canon) && echo "   receipts identical (canonical comparison)"

PID=$(echo "$RECEIPT" | python3 -c "import json,sys;print(next(m['id'] for m in json.load(sys.stdin)['resourceMap'] if m['resourceType']=='Patient'))")
echo "3) read patient $PID"
curl -fsS "$BASE_URL/patients/$PID" | python3 -m json.tool | head -12

# ---------------------------------------------------------------------------
# 4) Large intake: two dependency groups, the second one fails.
#    Group 1 (patient + observation) must commit and stay readable; group 2 is
#    an internally inconsistent report/observation pair and is rejected.
# ---------------------------------------------------------------------------
LARGE_KEY="SMOKE-LARGE-$(date +%s)"
G1P="urn:uuid:0a000001-1111-4111-8111-000000000001"
G1O="urn:uuid:0a000002-2222-4222-8222-000000000002"
G2PA="urn:uuid:0a000003-3333-4333-8333-000000000003"
G2PB="urn:uuid:0a000004-4444-4444-8444-000000000004"
G2O="urn:uuid:0a000005-5555-4555-8555-000000000005"
G2R="urn:uuid:0a000006-6666-4666-8666-000000000006"

LARGE=$(cat <<JSON
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {"fullUrl": "$G1P",
     "resource": {"resourceType": "Patient",
       "identifier": [{"system": "http://hospital/mrn", "value": "$LARGE_KEY"}]},
     "request": {"method": "POST", "url": "Patient"}},
    {"fullUrl": "$G1O",
     "resource": {"resourceType": "Observation", "status": "final",
       "code": {"coding": [{"code": "2345-7"}]},
       "subject": {"reference": "$G1P"}},
     "request": {"method": "POST", "url": "Observation"}},
    {"fullUrl": "$G2PA",
     "resource": {"resourceType": "Patient",
       "identifier": [{"system": "http://hospital/mrn", "value": "$LARGE_KEY-A"}]},
     "request": {"method": "POST", "url": "Patient"}},
    {"fullUrl": "$G2PB",
     "resource": {"resourceType": "Patient",
       "identifier": [{"system": "http://hospital/mrn", "value": "$LARGE_KEY-B"}]},
     "request": {"method": "POST", "url": "Patient"}},
    {"fullUrl": "$G2O",
     "resource": {"resourceType": "Observation", "status": "final",
       "code": {"coding": [{"code": "NA"}]},
       "subject": {"reference": "$G2PA"}},
     "request": {"method": "POST", "url": "Observation"}},
    {"fullUrl": "$G2R",
     "resource": {"resourceType": "DiagnosticReport", "status": "final",
       "code": {"coding": [{"code": "24323-8"}]},
       "subject": {"reference": "$G2PB"},
       "result": [{"reference": "$G2O"}]},
     "request": {"method": "POST", "url": "DiagnosticReport"}}
  ]
}
JSON
)

echo "4) large intake: first group must commit, second must fail"
LARGE1=$(curl -fsS -X POST "$BASE_URL/fhir/intake" \
  -H "Accession-Number: $LARGE_KEY" -H "Content-Type: application/json" -d "$LARGE")
python3 - "$LARGE1" <<'PY'
import json, sys
r = json.loads(sys.argv[1])
assert r["status"] == "partial", r["status"]
assert [g["state"] for g in r["groups"]] == ["completed", "failed"], r["groups"]
assert r["failure"]["requestIndex"] == 5, r["failure"]
assert [m["requestIndex"] for m in r["resourceMap"]] == [0, 1], r["resourceMap"]
print("   partial receipt: ", r["status"], r["failure"]["status"], "at entry",
      r["failure"]["requestIndex"])
PY
LARGE_G1_PID=$(echo "$LARGE1" | python3 -c "import json,sys;print(next(m['id'] for m in json.load(sys.stdin)['resourceMap'] if m['resourceType']=='Patient'))")
curl -fsS "$BASE_URL/patients/$LARGE_G1_PID/observations" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);assert d['total']==1;print('   committed group 1 readable, observations =',d['total'])"

echo "5) identical resubmit: same accession, same mapping, no duplicate resources"
LARGE2=$(curl -fsS -X POST "$BASE_URL/fhir/intake" \
  -H "Accession-Number: $LARGE_KEY" -H "Content-Type: application/json" -d "$LARGE")
python3 - "$LARGE1" "$LARGE2" <<'PY'
import json, sys
a, b = (json.loads(x) for x in sys.argv[1:3])
assert a["resourceMap"] == b["resourceMap"], (a["resourceMap"], b["resourceMap"])
assert b["status"] == "partial" and b["failure"]["requestIndex"] == 5, b
print("   identical retry reproduced the same partial mapping")
PY
curl -fsS "$BASE_URL/patients/$LARGE_G1_PID/observations" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);assert d['total']==1;print('   group 1 still has exactly 1 observation (no duplication)')"

echo "6) same accession number with different content must be rejected"
CODE=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/fhir/intake" \
  -H "Accession-Number: $LARGE_KEY" -H "Content-Type: application/json" \
  -d "{\"resourceType\":\"Bundle\",\"type\":\"transaction\",\"entry\":[{\"resource\":{\"resourceType\":\"Patient\",\"identifier\":[{\"system\":\"http://hospital/mrn\",\"value\":\"$LARGE_KEY-OTHER\"}]},\"request\":{\"method\":\"POST\",\"url\":\"Patient\"}}]}")
[ "$CODE" = "409" ] && echo "   rejected with 409 as expected"
echo "SMOKE OK"
