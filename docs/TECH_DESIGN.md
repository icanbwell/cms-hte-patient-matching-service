# Tech Design: cms-hte-patient-matching-service

**Status:** Draft
**Author:** Imran Qureshi (with Claude Code)
**Date:** 2026-09-04
**Reviewers:** TBD
**Ticket(s):** TBD (default board: BAI)

## Summary

This service will wrap the `patient_matching` Python package (repo: `cms-hte-patient-matching`, upstream name `patient-matching-reference-implementation`) as an HTTP microservice, structurally following the pattern already established by `person-matching-service`, which wraps `helix.personmatching`. The two wrapped libraries solve different problems (deterministic CMS-rules-based record linkage vs. probabilistic FHIR resource matching), so the wrapping pattern carries over at the **service-conventions level** (API shape, auth, config, observability, deployment) but not at the **candidate-retrieval level** — `patient_matching` ships its own cache/normalization/matching pipeline, so this service does not need a bespoke MongoDB blocking layer the way `person-matching-service` does.

The current `cms-hte-patient-matching-service` repo is not empty: it is a CI/CD-hardened FastAPI+GraphQL+MCP skeleton cloned from Provider Search Service (PSS) with zero patient-matching logic. Part of this design is therefore a migration plan, not just a greenfield build.

## Background

### What "cms-hte-patient-matching" actually wraps

The wrapped package implements the **CMS Patient Matching Proposal** (versions v3.2.2–v3.3.1) — an ONC/CMS-defined deterministic algorithm for linking patient identity records across systems using approved field-combination rules with bounded collision probability. It is explicitly **not** a fuzzy/ML matcher like `helix.personmatching`; matching decisions are rule-based and auditable (`rule_evaluations_summary` on every response).

`__NOTE__`: grepping the package for "HTE" (Health Technology Ecosystem) returns no hits beyond incidental substrings. Nothing in the wrapped package or its docs corresponds to "HTE." This is flagged as an open question below — confirm what HTE refers to before this name propagates further (JIRA epic, Confluence page, k8s namespace, etc.), since it may be a naming artifact rather than a real distinguishing concept.

### Current repo state (audit finding)

`cms-hte-patient-matching-service` (module name internally: `patient_matching_service`) already has:
- A working FastAPI app (`patient_matching_service/api.py`) with CORS, Prometheus instrumentation, a composite lifespan, and a GraphQL endpoint (Ariadne) at `/graphql` serving a stub `Query.providers` resolver that returns hardcoded fake results.
- An MCP server demo (`mcp_servers/math_server/`) exposing `add`/`multiply` tools — an explicit worked example of the MCP pattern, not business logic.
- Real, reusable JWT/OIDC bearer-auth scaffolding (`mcp_servers/auth/bearer_auth_manager.py`, `jwt_verifier_with_logging.py`) — JWKS discovery, issuer/audience/expiry validation.
- Full CI/CD: multi-stage `Dockerfile` (uv + JFrog + Root.io hardened base images), `.github/workflows/{build_and_test,docker-publish,deploy,codeql}.yml`, and Helm values for `dev-ue1`/`staging-ue1`/`prod-ue1`/`client-sandbox-ue1`.
- **Zero references to `cms-hte-patient-matching`/`patient_matching`** anywhere — no dependency declaration, no imports.
- Multiple un-cleaned leftovers from being cloned off Provider Search Service and, further back, off a service called `complaint-parser`:
  - `README.md` still documents PSS's GraphQL playground and links `helix.providersearch` as the reference example.
  - `utilities/config_loader.py` docstring: `"""load different configurations for PSS"""`.
  - `Makefile` targets still hardcode the pre-rename path `patient_matching` instead of `patient_matching_service`, and reference `setup.py`/Pipenv/`twine` targets that don't exist in this uv-based repo.
  - Helm IAM role ARNs for **prod/staging/client-sandbox** still say `irsa-complaint-parser` (only `dev-ue1` was renamed).
  - `sonar-project.properties` still has the literal placeholder `<REPOSITORY NAME>` and JS/lcov settings on a pure-Python repo.
  - `.pre-commit-config.yaml` excludes files (`docker-compose-openwebui*.yml`) that don't exist in this repo.

These need to be fixed as part of adopting this design, not left in place (see Migration Plan).

## Goals

- Expose `patient_matching`'s matching capability over HTTP, following b.well's established microservice conventions (as demonstrated by `person-matching-service`): JWT-bearer auth scoped to protected routes, structured/redacted JSON logging with trace correlation, OTel auto-instrumentation via Helm, uv/JFrog-based dependency management, multi-stage Docker build, Helm-values-only deployment via `icanbwell/cie.gha-deploy`.
- Provide a FHIR-native match endpoint consistent with the FHIR `$match` operation convention `person-matching-service` uses, so downstream FHIR-aware callers have one consistent contract shape across matching services.
- Remove the PSS-derived dead weight (GraphQL/Ariadne layer, MCP math demo, stub resolvers) that has nothing to do with patient matching.
- Fix the leftover cross-service naming cruft before first production deploy.

## Non-Goals

- Reimplementing or forking any matching/normalization/collision-probability logic — that all lives in `patient_matching` and stays there.
- Building a general-purpose fuzzy-search service (the package's `patient_matching.fuzzy` toolkit is out of scope; it's not wired into the actual matching path).
- IAL2 JWT-based matching (`match_from_token`, `/match/ial2`) in the first release — see Open Questions; can be added later behind the same `PatientMatcherService` instance without an architecture change.
- Publishing `patient_matching` to the JFrog index if it isn't already there — that's a prerequisite tracked separately (see Open Questions), not this service's work.

## Reference Architecture: the person-matching-service pattern

Summarized from the actual `person-matching-service` codebase, since this is the template being followed:

- **API layer**: FastAPI, single unversioned `POST /$match` route on a `protected_router`, `GET /health` on the base app (unauthenticated, used for k8s probes). No per-field Pydantic request model — the wire format *is* a FHIR `Parameters` resource, validated via `fhir.resources.R4B`. Auth is a FastAPI dependency (`Depends(verify_jwt)`) applied at `include_router(..., dependencies=[...])` — scoped to the router, not global middleware, so `/health` bypasses it by construction.
- **DI pattern**: no framework DI container. A `lifespan` async context manager builds the object graph once at startup (Mongo connection, service instance) and stores it on `app.state`; a small `get_match_service(request: Request)` function retrieves it via `Depends`.
- **Service/adapter layer**: one class (`MatchService`) does FHIR-Parameters parsing, candidate resolution, the actual library call (`Matcher().match_resources(...)`), and FHIR-Bundle response building — not split into separate classes. A `Protocol`-based `BlockingStrategy`/`HybridBlockingOrchestrator` sits in front of it for candidate retrieval from MongoDB (Atlas `$search` → basic `find()` fallback), since `helix.personmatching` itself only scores pairs and doesn't retrieve candidates.
- **Config**: no centralized settings class — ad-hoc `os.environ.get(...)` at point of use, with per-environment values split across `.helm/{common,dev-ue1,staging-ue1,...}.values.yaml`.
- **Error handling**: a known gap — no global exception handler; malformed input becomes an unstructured 500. Deliberately not being replicated here (see Proposed Architecture, Error Handling).
- **Observability**: structured JSON logs via a `RedactingJsonFormatter` (redacts secrets/tokens, stamps OTel `trace_id`/`span_id` for log↔trace correlation); tracing/metrics come from OTel auto-instrumentation injected by a k8s operator, declared via Helm flags — no tracer-provider code in the app itself. Groundcover is the approved observability backend per `policies/approved-tech.yaml`.
- **Testing**: pytest + pytest-asyncio, `testcontainers` for MongoDB/Keycloak, unit tests mock the library's scoring objects, e2e tests snapshot full request→response fixtures against real containerized dependencies.
- **Deployment**: multi-stage Dockerfile (uv-managed, JFrog-authenticated build secrets, non-root `appuser`, `EXPOSE 5000`, `uvicorn ... --workers 4 --timeout-graceful-shutdown 30`), Helm **values only** (chart templates live in the separate `icanbwell/cie.gha-deploy` repo), a 3-stage shutdown ladder (`preStop sleep 10s` < `uvicorn graceful timeout 30s` < `k8s terminationGracePeriodSeconds 100`), HPA 2–10 replicas at 75% CPU.
- **Dependency pinning**: the library is a normal (non-git, non-path) dependency resolved through bWell's JFrog virtual-pypi index, loosely lower-bound pinned in `pyproject.toml`, exact-pinned in `uv.lock` (regenerated inside a throwaway Docker build, not on the host).

## Proposed Architecture: cms-hte-patient-matching-service

### Why this diverges from person-matching-service's candidate-retrieval layer

`helix.personmatching`'s `Matcher` only scores a source resource against a supplied target/candidate set — it has no candidate-retrieval concept at all. `person-matching-service` therefore had to build a blocking layer itself, entirely outside the library. `patient_matching` does not have this gap: blocking is a first-class concept *inside* the library (`patient_matching/matching/backend.py`'s docstring literally names it: "the backend may cast a wide net, e.g. blocking on DOB + last name initial"). The two blocking pipelines differ in more than just "where the code lives" — they differ in granularity, resilience pattern, storage, and who populates the candidate store:

| | person-matching-service | cms-hte-patient-matching-service (this design) |
|---|---|---|
| Blocking logic owned by | the wrapping service (`service/blocking/*`, built for that repo) | the library itself (`MatchingBackend`/`CacheMatchingBackend`) |
| Granularity | once per request → one candidate set (≤`MAX_BLOCKING_CANDIDATES`) → one `Matcher().match_resources()` call scores source against the whole set | once **per CMS Table 2 rule** (~38 calls) — each rule supplies its own field criteria (e.g. exact SSN, or DOB + fuzzy last name), so each rule *is* its own blocking key plus verification step |
| Resilience pattern | `HybridBlockingOrchestrator`: explicit fallback chain — try Atlas `$search`, catch failure, fall back to plain `find()`, catch failure, return empty candidates rather than raise | none — a single backend is configured per deployment; the alternative implementations (`InMemoryBackend` vs. `CacheMatchingBackend`/`DuckDBCache`) are deployment choices, not a runtime fallback chain |
| Storage | MongoDB (Atlas `$search` / `find()`) | pluggable via `MatchingBackend` ABC: in-memory Python list (`InMemoryBackend`, AND-filter scan), or `DuckDBCache` (exact criteria = indexed SQL; fuzzy criteria = O(n) Python loop over `rapidfuzz` Damerau-Levenshtein) |
| Candidate-store population | unowned by this codebase — nothing in `person-matching-service` writes to the Mongo collection (confirmed: no `insert`/`upsert`/`bulk_write` calls anywhere in the repo); it's maintained by some other, external pipeline | owned by the library itself: `CacheManager` (ETL: `FhirClient` → `NormalizationManager` → `FieldExtractor` → cache) with `apscheduler`-backed `start_scheduled_refresh()` |
| Sync/async | async (`pymongo.AsyncMongoClient`, per ADR-0002) — specifically because a stalled Atlas index can block ~30s and would otherwise freeze the uvicorn event loop | sync — no network I/O in the per-request match path; network I/O (via `FhirClient`/httpx) only happens in the periodic cache-refresh job, off the request path |

Net effect: this service's job is narrower than person-matching-service's. There is no `service/blocking/*`-equivalent subsystem to build — `PatientMatcherService`/`MatchingEngine`/`CacheManager` already own candidate retrieval, normalization, and cache population end-to-end. This service's work is to instantiate and operate that pipeline correctly (choose/configure a `MatchingBackend`, wire the cache-refresh job, point `FhirClient` at the right environment) and wrap it in b.well's standard service conventions — not to replicate person-matching-service's Mongo-blocking architecture. See Open Question 6 for the one resilience gap this comparison surfaces: `patient_matching`'s per-rule backend calls have no documented failure-isolation equivalent to `HybridBlockingOrchestrator`'s catch-and-fall-back — worth confirming behavior on backend failure (e.g. a transient DuckDB error mid-request) before relying on it in production.

Reference: the package even ships its own example FastAPI wiring (`patient_matching.api.app.create_app`) exposing `POST /Patient/$match`, `POST /match/ial2`, `GET /health`. This is explicitly a bare reference app with no auth, no structured logging, no OTel wiring, no Helm-shaped health/shutdown behavior — useful as a guide for the request/response shaping logic (`_extract_patient_from_parameters`, `_build_match_bundle`), but not something to mount directly in production. This service reimplements that wiring using person-matching-service's conventions.

### API layer

- FastAPI, single unversioned route: `POST /Patient/$match` on a `protected_router`, mirroring person-matching-service's FHIR `$match`-operation convention (path segment differs only because the FHIR op is resource-scoped: `Patient/$match` vs. person-matching-service's resource-agnostic `/$match`).
- `GET /health` on the base (unauthenticated) app — required for k8s liveness/readiness/startup probes, same as the template.
- Input: FHIR `Parameters` resource (a `resource` parameter containing the query `Patient`), parsed the same way person-matching-service parses its `Parameters` payload — reuse that parsing shape rather than `patient_matching.api.app`'s simpler dict-based parsing, for consistency across matching services.
- Output: a FHIR `searchset` `Bundle`, one `BundleEntry` per matched candidate, built from `MatchResponse` (`outcome`, `matched_patients`, `confidence_score`, `matched_rule_id`, `rule_evaluations_summary`). Port `patient_matching.api.app._build_match_bundle` as the starting point and extend it with match-grade extensions in the style of person-matching-service's `get_bundle_entry_with_score`, so `rule_evaluations_summary` (the audit trail unique to this package) is preserved in the response rather than dropped.
- Remove entirely: the Ariadne/GraphQL layer, `schema.graphql`, `providers/`, `mutations/`, and the MCP math-server demo. None of it is part of either template's pattern and all of it is stub/fake logic.

### Service/adapter layer

- One adapter class, e.g. `PatientMatchController` (in `patient_matching_service/service/match_controller.py`), analogous to person-matching-service's `MatchService`: owns FHIR-Parameters parsing, the call into `PatientMatcherService.match_patient(...)`, and FHIR-Bundle response building.
- `PatientMatcherService` itself (with its `DuckDBCache`/`CacheMatchingBackend`) is constructed once in the FastAPI `lifespan` and stored on `app.state`, exactly like person-matching-service stores its `MatchService` on `app.state` — same `get_match_controller(request: Request)` `Depends`-based retrieval pattern.
- Cache lifecycle: at startup, build the `DuckDBCache` via `CacheManager` (`FhirClient` pointed at the b.well FHIR server → `NormalizationManager` → cache). Use `CacheManager.start_scheduled_refresh(...)` (backed by `apscheduler`, already a package dependency) instead of building a bespoke refresh job — this replaces the role person-matching-service's Mongo `BlockingService`/`HybridBlockingOrchestrator` plays, but is refresh-based rather than per-request-query-based.
- The matching call itself (`MatchingEngine.match`) is synchronous, in-memory, CPU-bound — no need for person-matching-service's async-Mongo-client treatment (ADR-0002 there exists specifically because a stalled Atlas index could block the event loop for ~30s; nothing analogous exists here since there's no per-request network I/O in the match path). Run it directly in the async handler; only the cache-refresh job needs to be mindful of its own execution cadence (see Open Questions on DuckDB fuzzy-search scaling).

### Configuration

- Follow the same pattern as person-matching-service (ad-hoc `os.environ.get(...)`, per-environment values split across `.helm/*.values.yaml`) for consistency across the two sibling services, rather than introducing `pydantic-settings` unilaterally. Key variables:

| Variable | Purpose |
|---|---|
| `FHIR_BASE_URL`, `FHIR_AUTH_*` | `FhirClientConfig` — source of truth for cache population |
| `CACHE_REFRESH_INTERVAL_MINUTES` | `CacheManagerConfig.refresh_interval_minutes` |
| `CACHE_DATABASE_PATH` | `DuckDBCache(database=...)` — `:memory:` vs. file-backed |
| `AUTH_JWK_URLS`, `AUTH_EXPECTED_CIDS`, `AUTH_CID_CHECK_ISSUER`, `AUTH_JWKS_CACHE_TTL_SECONDS` | same JWT/JWKS auth knobs as person-matching-service |
| `LOG_LEVEL` | root logger level |

- `ServiceConfig(rules=...)` is not exposed as an env var initially — default to `APPROVED_RULES`/`CATEGORY_2_RULES` (the full CMS rule set) unless a concrete need to restrict rules emerges.

### Auth

- Adapt this repo's existing JWT/OIDC scaffolding (`mcp_servers/auth/bearer_auth_manager.py`, `jwt_verifier_with_logging.py`) into a `deps.py`/`jwt_validator.py` pair matching person-matching-service's structure — JWKS fetch/cache with TTL, fail-closed if `AUTH_JWK_URLS` is unset, applied only to the protected router via `include_router(..., dependencies=[Depends(verify_jwt)])` so `/health` stays open.

### Error handling — explicit improvement over the template

Person-matching-service has a known gap: no global exception handler, so malformed input (bad `Parameters` JSON, invalid `resourceType`) leaks as an unstructured 500. This service should close that gap rather than copy it:
- `@app.exception_handler` for `pydantic.ValidationError`/`fhir.resources` validation errors → structured 400.
- Catch `patient_matching.ial2_extraction.TokenVerificationError` and `ValueError` (e.g. "IAL2 extractor not configured") explicitly if/when IAL2 support is added.
- Catch `httpx.HTTPStatusError`/connection errors from `FhirClient`/`ClientCredentialsAuth` during cache refresh and log+alert rather than crash the refresh job.
- Note: `MatchingEngine`/`NormalizationManager` themselves fail closed on bad field data (e.g. unparseable dates just don't match, they don't raise) — so the handler surface needed here is smaller than it would be for a library that throws on bad input.

### Observability

- Port person-matching-service's `RedactingJsonFormatter` logging setup verbatim (structured JSON, secret/token redaction, OTel `trace_id`/`span_id` correlation) — this is generic, not FHIR- or library-specific, and there's no reason to reinvent it.
- OTel auto-instrumentation via the same Helm flags (`otel.enabled`, `autoInstrumentation.language: python`) — no tracer code in the app.
- Groundcover as the observability backend, consistent with `policies/approved-tech.yaml`.

### Testing

- pytest + pytest-asyncio, mirroring the template's split:
  - `tests/unit/` — mock `PatientMatcherService`/`MatchResponse` directly to test the FHIR parse/build logic in `PatientMatchController`, without invoking the real rules engine (same reasoning as person-matching-service's `test_match_service.py`).
  - `tests/matching/` — exercise `PatientMatcherService`/`MatchingEngine` directly against an in-memory `DuckDBCache` (`:memory:`) seeded with fixture patients — no `testcontainers` needed here since the package's own cache is in-process, unlike person-matching-service's MongoDB dependency.
  - `tests/end_to_end/` — full-stack `TestClient` tests with a seeded in-memory cache and, if JWT auth is exercised, a `testcontainers`-based Keycloak (reuse `tests/containers/keycloak.py`-equivalent setup from the template if this repo doesn't already have one worth keeping).
  - Fixture-file pattern (`query.json`/`expected.json` pairs) for exercising real CMS Table 2 rules end-to-end, analogous to person-matching-service's `tests/end_to_end/match/`.

### Deployment & CI/CD

- Reuse this repo's existing multi-stage Dockerfile structure (uv + JFrog + Root.io hardened base images) — it's already correctly modeled on the same pattern person-matching-service uses. Confirm whether `patient_matching`'s dependencies (`duckdb`, `rapidfuzz`, `usaddress-scourgify`, etc.) need any additional system build tools analogous to `helix.personmatching`'s `python-crfsuite` C-extension requirement; add an equivalent import smoke-test step in the Dockerfile if so.
- Reuse the existing GitHub Actions workflows (`build_and_test.yml`, `docker-publish.yml`, `deploy.yml`, `codeql.yml`) — they're already infra-correct per the earlier repo audit; no template deviation needed there.
- Helm: keep the values-only pattern (chart templates centralized in `icanbwell/cie.gha-deploy`), but this is where the cleanup from the audit is mandatory before first prod deploy (see Migration Plan) — the prod/staging/client-sandbox IAM role ARNs currently point at `irsa-complaint-parser`, which is a different service's role.

## Migration Plan (current PSS-derived skeleton → this design)

1. **Strip dead scaffolding**: remove `api_schema.py`, `schema.graphql`, `providers/`, `mutations/`, `mcp_servers/math_server/` (keep `mcp_servers/auth/` — it's real, reusable). Remove the Ariadne/GraphQL dependency from `pyproject.toml` if nothing else needs it.
2. **Add the package dependency**: declare `patient_matching` in `pyproject.toml` against the bWell JFrog index (same index config as `helix.personmatching`). **Blocking check**: confirm `patient_matching` is actually published there — its `Makefile` has `build`/`testpackage`/`package` targets but none obviously push to bWell's JFrog index, and its version is still `0.0.1`. If unpublished, this is a prerequisite for the sibling repo, not something to solve by adding a git dependency as a permanent workaround.
3. **Build the API/service/auth layers** described above (`api.py`, `service/match_controller.py`, `deps.py`, `jwt_validator.py`, `observability/logging.py`), reusing `mcp_servers/auth/*` for the JWT/JWKS mechanics.
4. **Fix leftover naming cruft**: correct `Makefile` module paths, retarget prod/staging/client-sandbox Helm IAM role ARNs off `irsa-complaint-parser` onto this service's own role, fix `sonar-project.properties`' placeholder project key and JS-oriented settings, prune the stale `.pre-commit-config.yaml` secret-scan exclusions, and rewrite `README.md` to describe patient matching instead of the PSS GraphQL playground.
5. **Wire cache bootstrap**: implement `FhirClientConfig`/`CacheManager` startup in `lifespan`, pointed at the appropriate b.well FHIR server per environment.
6. **Tests**: add the unit/matching/e2e layers described above; retire the PSS-stub tests (`tests/end_to_end/simple/`, `tests/mcp_servers/math_server/`).

## Open Questions

| # | Question | Needed From | Impact |
|---|---|---|---|
| 1 | What does "HTE" refer to? Nothing in the wrapped package corresponds to it. | Whoever named the repo/ticket | May indicate a naming correction is needed before this propagates into more docs/infra |
| 2 | Is `patient_matching` actually published to bWell's JFrog virtual-pypi index yet? | Package owner / platform team | Blocks step 2 of the migration plan if not |
| 3 | Is IAL2-JWT-based matching (`/match/ial2`) in scope for v1, or a later phase? | Product/consumers of this service | Affects whether `IAL2Extractor`/`TokenVerifier` wiring and its error handling are built now |
| 4 | What FHIR server/environment should back `FhirClient`'s candidate source per environment? | Platform/FHIR team | Needed to set `FHIR_BASE_URL`/auth per Helm env file |
| 5 | Expected candidate-pool size per query, given `DuckDBCache`'s fuzzy search is an O(n) Python loop (not indexed) | Product / data volume estimate | Determines whether a blocking/scoping strategy is needed on top of `patient_matching`'s own cache before this becomes a latency problem at scale |
| 6 | Cache refresh cadence and whether a single in-process DuckDB cache (vs. a shared external store) is sufficient given HPA will run 2–10 pod replicas each with its own independent cache | Whoever owns capacity/consistency requirements | Each replica rebuilding/refreshing its own cache independently may cause consistency drift and duplicated FHIR-server load across replicas — worth resolving before enabling autoscaling |
| 7 | Does `MatchingBackend.search()` have any failure-isolation equivalent to person-matching-service's `HybridBlockingOrchestrator` (catch-and-fall-back per strategy), or does a transient backend error (e.g. DuckDB I/O error) during one rule's lookup propagate and fail the whole request? | Package owner / direct code read of `CacheMatchingBackend`/`DuckDBCache` error paths | Determines whether this service needs its own try/except-per-rule wrapper around backend calls, or can rely on the library's own resilience |

## Non-Goals (recap)

- No reimplementation of matching/normalization/collision logic.
- No general fuzzy-search service.
- No IAL2 support in v1 unless Open Question 3 resolves otherwise.
- No new dependency-publishing pipeline for `patient_matching` (tracked as a blocking prerequisite, not this service's scope).

## Appendix: file-mapping cheat sheet (person-matching-service → cms-hte-patient-matching-service)

| person-matching-service | cms-hte-patient-matching-service (proposed) |
|---|---|
| `personmatching/api.py` | `patient_matching_service/api.py` |
| `personmatching/service/match_service.py` | `patient_matching_service/service/match_controller.py` (wraps `PatientMatcherService` instead of `Matcher`) |
| `personmatching/service/blocking/*` | *(not needed — `patient_matching`'s own `CacheManager`/`DuckDBCache`/`MatchingBackend` play this role)* |
| `personmatching/deps.py`, `jwt_validator.py`, `auth.py` | reuse/adapt existing `mcp_servers/auth/bearer_auth_manager.py`, `jwt_verifier_with_logging.py` |
| `personmatching/observability/logging.py` | port directly (generic, reusable as-is) |
| `.helm/*.values.yaml` | keep, but fix IAM role ARNs and env-specific `FHIR_BASE_URL`/cache config |
| `Dockerfile` | keep existing structure; verify no extra system build deps needed for `patient_matching`'s C-extension-bearing dependencies |
| `tests/unit/`, `tests/end_to_end/match/` | analogous `tests/unit/`, `tests/end_to_end/` against `patient_matching`'s in-process cache instead of testcontainers-Mongo |
