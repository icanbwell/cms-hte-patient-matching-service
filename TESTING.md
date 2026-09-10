# Manual testing with curl

Assumes the service is running locally via `make up` (listens on `http://localhost:5050`,
mapped from the container's port 5000 -- see `docker-compose.yml`).

## Health check (no auth required)

```bash
curl -s http://localhost:5050/health
```

Expected:

```json
{"status": "ok"}
```

## POST /Patient/$match

This route requires a bearer JWT (see `deps.py`/`jwt_validator.py`) verified against the JWKS
endpoint(s) in `AUTH_JWK_URLS`. If `AUTH_JWK_URLS` is unset, the route fails closed -- every
request gets 401, by design (see README.md's Configuration section).

### 1. Without a token -- confirms auth is actually enforced

```bash
curl -s -i -X POST http://localhost:5050/Patient/\$match \
  -H "Content-Type: application/json" \
  -d '{"resourceType": "Parameters", "parameter": []}'
```

Expected: `401 Unauthorized` (either "Missing or invalid Authorization header" if no token was
sent, or "JWT authentication is not configured" if `AUTH_JWK_URLS` isn't set at all).

### 2. With a token

Get a bearer token from whatever IdP `AUTH_JWK_URLS` points at, then:

```bash
TOKEN="<your-jwt>"

curl -s -X POST "http://localhost:5050/Patient/\$match" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Content-Type: application/fhir+json" \
  -d '{
    "resourceType": "Parameters",
    "parameter": [
      {
        "name": "resource",
        "resource": {
          "resourceType": "Patient",
          "name": [{"family": "smith", "given": ["john"]}],
          "birthDate": "1990-01-15",
          "telecom": [
            {"system": "phone", "value": "+12125551234"},
            {"system": "email", "value": "john@gmail.com"}
          ],
          "address": [{"line": ["123 main st"]}],
          "identifier": [
            {"system": "http://hl7.org/fhir/sid/us-ssn", "value": "xxx-xx-6789"}
          ]
        }
      }
    ]
  }'
```

Expected shape (a FHIR `searchset` `Bundle`; empty `total`/no `entry` if the query patient isn't
in the candidate cache -- see "Nothing ever matches" below):

```json
{
  "resourceType": "Bundle",
  "type": "searchset",
  "total": 1,
  "entry": [
    {
      "resource": { "resourceType": "Patient", "id": "...", "...": "..." },
      "fullUrl": "Patient/...",
      "search": {
        "mode": "match",
        "score": 0.999999999999,
        "extension": [
          {"url": "https://icanbwell.com/patient_match/outcome", "valueString": "match"},
          {"url": "https://icanbwell.com/patient_match/matched_rule_id", "valueString": "01"}
        ]
      }
    }
  ]
}
```

### 3. Malformed request -- confirms the 400 path (not an unstructured 500)

```bash
curl -s -i -X POST "http://localhost:5050/Patient/\$match" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"resourceType": "Patient"}'
```

Expected: `400 Bad Request` with a `detail` explaining the payload isn't a valid `$match`
`Parameters` resource.

## Nothing ever matches (empty `total`, no `entry`)

This is expected, not a bug, if `FHIR_BASE_URL` isn't set -- the candidate cache starts empty
and stays empty (see README.md's Configuration table and `docs/TECH_DESIGN.md` Open Question
4). Check the service logs at startup for:

```
FHIR_BASE_URL is not set -- the candidate cache will stay empty and every $match request will return no_match.
```

## Testing without a real IdP/FHIR server

Standing up a real JWKS endpoint and FHIR server just to exercise the matching logic manually
is unnecessary -- the automated test suite already does this without either:

```bash
uv run pytest tests -q
```

- `tests/unit/` mocks `PatientMatcherService` directly (fast, no auth/cache infra).
- `tests/end_to_end/test_match.py` runs the real matching engine against an in-memory
  `DuckDBCache` seeded with the same fixture patient used in the curl example above, and bypasses
  auth via FastAPI's `dependency_overrides` (its documented mechanism for testing auth-gated
  routes) -- read that file if you want to see a known-good request/response pair without
  needing a running server at all.
