"""HTTP endpoints backing the /testing-ui page.

Gated behind ENABLE_TESTING_UI, checked per-request (not at router-
registration time -- see _require_testing_ui_enabled) -- these endpoints
decode arbitrary IAL2 tokens and run patient matching on arbitrary pasted
FHIR Patients with no auth of their own, so they must never be reachable in
a deployment that isn't already trusted/internal.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import jwt
from cmshteial2reader import IAL2Extractor, TokenVerificationError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from patient_matching.matching import (
    APPROVED_RULES,
    FieldExtractor,
    InMemoryBackend,
    MatchingEngine,
    MatchOutcome,
    PatientFields,
)
from patient_matching.matching.household_rules import CATEGORY_2_RULES
from patient_matching.normalization import NormalizationManager
from pydantic import ValidationError

logger = logging.getLogger(__name__)

router = APIRouter()

_INDEX_HTML_PATH = Path(__file__).parent / "static" / "index.html"


def is_testing_ui_enabled() -> bool:
    """Whether ENABLE_TESTING_UI opts into this module's routes."""
    return os.getenv("ENABLE_TESTING_UI", "").strip().lower() in ("1", "true", "yes")


def _require_testing_ui_enabled() -> None:
    """Route dependency gating every endpoint in this module.

    Checked per-request (reads the env var fresh each call) rather than at
    router-registration/import time, so: (a) this module's routes can
    always be registered on `app` unconditionally, keeping route
    registration order independent of configuration, and (b) tests can
    toggle ENABLE_TESTING_UI per-test without needing to reimport api.py.
    404, not 403: a disabled debug UI shouldn't reveal that it exists.
    """
    if not is_testing_ui_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


@router.get(
    "/testing-ui",
    include_in_schema=False,
    dependencies=[Depends(_require_testing_ui_enabled)],
)
async def testing_ui_page() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML_PATH.read_text())


# (rule_id, description, required field names) for every Table 2 rule --
# flat (Category 1) and household/individual (Category 2) -- used to explain
# rules the engine never got far enough to evaluate for a given pair (see
# match_pair's "evaluated": False entries below).
_ALL_RULES: list[tuple[str, str, set[str]]] = [
    (rule.rule_id, rule.description, {rf.name for rf in rule.fields})
    for rule in APPROVED_RULES
] + [
    (
        rule.rule_id,
        rule.description,
        {rf.name for rf in rule.household_row.fields}
        | {rf.name for rf in rule.individual_row.fields},
    )
    for rule in CATEGORY_2_RULES
]

_RULE_DESCRIPTIONS: dict[str, str] = {
    rule_id: description for rule_id, description, _ in _ALL_RULES
}


def _get_ial2_extractor(request: Request) -> IAL2Extractor:
    extractor: IAL2Extractor | None = getattr(request.app.state, "ial2_extractor", None)
    if extractor is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "IAL2 support is not configured on this service (set "
                "IAL2_ALLOWED_JWKS_URLS and IAL2_AUDIENCE)"
            ),
        )
    return extractor


def _peek_issuer(token: str) -> str | None:
    """Read the token's `iss` claim without verifying its signature.

    Purely informational, for display -- this is the same unverified peek
    MultiIssuerTokenVerifier itself does internally to pick a JWKS URL to
    check, done here independently (via pyjwt directly, not by reaching
    into that private method) so the UI can show which issuer/JWKS was
    being checked even when verification fails before ever reaching a
    trust decision (e.g. an unparseable token).
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None
    issuer = unverified.get("iss")
    return str(issuer) if issuer else None


@router.post(
    "/testing-ui-api/decode-ial2-token",
    dependencies=[Depends(_require_testing_ui_enabled)],
)
async def decode_ial2_token(request: Request) -> JSONResponse:
    """Verify a pasted IAL2 token and return the FHIR Patient it decodes to.

    Unlike POST /Patient/$match's generic 401/422 (which deliberately hides
    verification detail from untrusted callers -- see api.py's
    TokenVerificationError/ValidationError handlers), this debug endpoint
    surfaces the real failure detail -- including which JWKS URL/issuer was
    checked and whether it was accepted -- since the whole point of this UI
    is diagnosing *why* a token was rejected.
    """
    extractor = _get_ial2_extractor(request)
    body = await request.json()
    token = body.get("token") if isinstance(body, dict) else None
    if not token or not isinstance(token, str):
        raise HTTPException(
            status_code=400, detail="Request body must be {'token': '<jwt>'}"
        )

    issuer = _peek_issuer(token)

    try:
        patient = await extractor.extract(token)
    except TokenVerificationError as exc:
        return JSONResponse(
            {
                "accepted": False,
                "issuer": issuer,
                "error_type": type(exc).__name__,
                "detail": str(exc),
            },
            status_code=401,
        )
    except ValidationError as exc:
        return JSONResponse(
            {
                "accepted": False,
                "issuer": issuer,
                "error_type": "ValidationError",
                "detail": (
                    "Token verified and its JWKS URL was accepted, but its "
                    f"claims don't build a valid FHIR Patient: {exc}"
                ),
            },
            status_code=422,
        )

    return JSONResponse({"accepted": True, "issuer": issuer, "patient": patient})


@router.post(
    "/testing-ui-api/match-pair",
    dependencies=[Depends(_require_testing_ui_enabled)],
)
async def match_pair(request: Request) -> dict[str, Any]:
    """Run the real matching engine on two pasted FHIR Patients.

    Uses the same NormalizationManager + MatchingEngine + Table 2 rules as
    production matching (see PatientMatcherService), just against a single
    in-memory candidate instead of the FHIR-backed cache -- so a match/
    no-match here reflects what production would actually decide, not a
    simplified approximation.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400, detail="Request body must be a JSON object"
        )
    patient_a = body.get("patient_a")
    patient_b = body.get("patient_b")
    if not isinstance(patient_a, dict) or not isinstance(patient_b, dict):
        raise HTTPException(
            status_code=400,
            detail=(
                "Request body must be {'patient_a': <FHIR Patient>, "
                "'patient_b': <FHIR Patient>}"
            ),
        )

    normalizer = NormalizationManager()
    normalized_a = normalizer.normalize(patient_a)
    normalized_b = normalizer.normalize(patient_b)

    backend = InMemoryBackend([normalized_b])
    engine = MatchingEngine(backend=backend)
    result = await engine.match(normalized_a)

    extractor = FieldExtractor()
    fields_a = extractor.extract(normalized_a)
    fields_b = extractor.extract(normalized_b)

    evaluated_rule_ids = {ev.rule_id for ev in result.rule_evaluations}
    rules_report: list[dict[str, Any]] = [
        {
            "rule_id": ev.rule_id,
            "description": _RULE_DESCRIPTIONS.get(ev.rule_id, ""),
            "evaluated": True,
            "matched": ev.matched,
            "match_type": ev.match_type,
            "fuzzy_fields": ev.fuzzy_fields,
            "negated_by_suffix": ev.negated_by_suffix,
            "field_outcomes": ev.field_outcomes,
        }
        for ev in result.rule_evaluations
    ]

    for rule_id, description, required_fields in _ALL_RULES:
        if rule_id in evaluated_rule_ids:
            continue
        rules_report.append(
            {
                "rule_id": rule_id,
                "description": description,
                "evaluated": False,
                "matched": False,
                "reason": _not_evaluated_reason(required_fields, fields_a, fields_b),
            }
        )

    rules_report.sort(key=lambda r: r["rule_id"])

    return {
        "outcome": result.outcome.value,
        "matched": result.outcome == MatchOutcome.MATCH,
        "matched_rule_id": result.matched_rule_id,
        "match_type": result.match_type,
        "rules": rules_report,
    }


def _not_evaluated_reason(
    required_fields: set[str], fields_a: PatientFields, fields_b: PatientFields
) -> str:
    """Explain why a rule never reached field-by-field verification.

    Either one side is missing a required field outright, or both sides
    have every required field but the engine's candidate-retrieval
    (blocking) step didn't select this pair for this rule -- e.g. the
    blocked-on field's values don't align closely enough to be retrieved
    as candidates for each other.
    """
    missing_in_a = sorted(f for f in required_fields if not fields_a.has_field(f))
    missing_in_b = sorted(f for f in required_fields if not fields_b.has_field(f))
    if not missing_in_a and not missing_in_b:
        return (
            "Both patients have all required fields, but this rule's "
            "candidate-retrieval step didn't select this pair -- the "
            "blocked-on field values likely don't align closely enough"
        )
    parts = []
    if missing_in_a:
        parts.append(f"Patient A is missing {', '.join(missing_in_a)}")
    if missing_in_b:
        parts.append(f"Patient B is missing {', '.join(missing_in_b)}")
    return "Missing required field(s): " + "; ".join(parts)
