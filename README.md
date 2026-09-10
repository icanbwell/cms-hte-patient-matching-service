# patient_matching_service

FastAPI service wrapping [`cms-hte-patient-matching`](https://pypi.org/project/cms-hte-patient-matching/)
(the CMS Patient Matching Proposal's deterministic Table 2 rules engine) as a FHIR `$match` HTTP
endpoint. See [`docs/TECH_DESIGN.md`](docs/TECH_DESIGN.md) for the full architecture and open
questions.

## Prerequisites

- Python 3.12+
- Docker (for containerized development)
- [uv](https://github.com/astral-sh/uv) (Python package manager)

## Setup

Private/proxied packages resolve through bWell's JFrog index. Set `JFROG_READ_TOKEN` in your
environment before building:

```bash
export JFROG_READ_USER="you@bwell.zone"
export JFROG_READ_TOKEN="<your-jfrog-token>"
```

Add these to `~/.zshrc` or `~/.bashrc` to persist across sessions.

```bash
git clone <this-repo>
make init
make up
```

The service listens on `http://localhost:5050`.

## Configuration

| Variable | Purpose |
|---|---|
| `FHIR_BASE_URL` | FHIR server the candidate cache is built from. **Unset by default** -- the service still starts, but the cache stays empty and every `$match` request returns `no_match` until this is set. |
| `FHIR_TOKEN_URL`, `FHIR_CLIENT_ID`, `FHIR_CLIENT_SECRET` | OAuth2 client-credentials auth for the FHIR server, if required. |
| `CACHE_DATABASE_PATH` | DuckDB cache path. Defaults to `:memory:`. |
| `CACHE_REFRESH_INTERVAL_MINUTES` | How often the cache re-syncs from the FHIR server. Defaults to `60`. |
| `AUTH_JWK_URLS` | Comma-separated JWKS endpoint(s) for bearer-token auth on `/Patient/$match`. **Unset by default** -- fails closed (every request to that route returns 401), not open. |
| `AUTH_EXPECTED_CIDS`, `AUTH_CID_CHECK_ISSUER`, `AUTH_JWKS_CACHE_TTL_SECONDS` | Optional client-id allow-list and JWKS cache tuning. |
| `LOG_LEVEL` | Root logger level. Defaults to `INFO`. |

## API

- `POST /Patient/$match` -- FHIR `$match` operation. Accepts a `Parameters` resource containing a
  query `Patient`; returns a `searchset` `Bundle` of matched candidates, each entry carrying the
  match outcome and matched rule ID as extensions. Requires a bearer JWT (see `AUTH_JWK_URLS`
  above).
- `GET /health` -- unauthenticated liveness/readiness probe target.

## Running tests

```bash
make tests
```

Unit tests (`tests/unit/`) mock `PatientMatcherService` and exercise only this service's
FHIR-parsing/response-building logic. End-to-end tests (`tests/end_to_end/`) run the real
matching engine against an in-memory `DuckDBCache` seeded with fixture patients.
