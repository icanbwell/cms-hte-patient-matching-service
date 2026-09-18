"""HTTP endpoints backing the /testing-ui page.

Gated behind ENABLE_TESTING_UI, checked per-request (not at router-
registration time -- see _require_testing_ui_enabled) -- these endpoints
decode arbitrary IAL2 tokens and run patient matching on arbitrary pasted
FHIR Patients with no auth of their own, so they must never be reachable in
a deployment that isn't already trusted/internal.
"""

from __future__ import annotations

import dataclasses
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


def _normalized_field_summary(fields: PatientFields) -> dict[str, list[str]]:
    """Every value FieldExtractor pulled out, normalized and sorted.

    For troubleshooting: an empty list here for a field the caller clearly
    supplied (e.g. "phones": [] despite a telecom entry in the pasted
    Patient) means NormalizationManager dropped it -- an invalid-looking
    number, a detected placeholder value -- *before* matching ever saw it.
    field_outcomes/field_values (below) only show what happened once a
    rule was evaluated; this shows what was available to evaluate at all.
    """
    return {f.name: sorted(getattr(fields, f.name)) for f in dataclasses.fields(fields)}


def _build_rules_report(
    result: Any, fields_a: PatientFields, fields_b: PatientFields
) -> list[dict[str, Any]]:
    """Shared by match_pair and match_bundle: one row per Table 2 rule,
    evaluated or not, for the troubleshooting table.

    `field_values`/`candidates_retrieved`/`blocking_criteria`/`step` come
    from cms-hte-patient-matching's RuleEvaluation -- guarded with getattr
    since older installed versions of that package predate them.

    A rule can now appear in `result.rule_evaluations` even when blocking
    retrieved zero candidates (candidates_retrieved == 0, field_outcomes
    empty) -- that's still "not evaluated" in the sense this table means
    (no field-by-field comparison ran), just with a real reason instead of
    the `_not_evaluated_reason` guess below.
    """
    evaluated_rule_ids: set[str] = set()
    rules_report: list[dict[str, Any]] = []
    for ev in result.rule_evaluations:
        evaluated_rule_ids.add(ev.rule_id)
        candidates_retrieved = getattr(ev, "candidates_retrieved", None)
        if candidates_retrieved == 0:
            rules_report.append(
                {
                    "rule_id": ev.rule_id,
                    "description": _RULE_DESCRIPTIONS.get(ev.rule_id, ""),
                    "evaluated": False,
                    "matched": False,
                    "reason": _zero_candidates_reason(ev),
                }
            )
            continue
        rules_report.append(
            {
                "rule_id": ev.rule_id,
                "description": _RULE_DESCRIPTIONS.get(ev.rule_id, ""),
                "evaluated": True,
                "matched": ev.matched,
                "match_type": ev.match_type,
                "fuzzy_fields": ev.fuzzy_fields,
                "negated_by_suffix": ev.negated_by_suffix,
                "field_outcomes": ev.field_outcomes,
                "field_values": getattr(ev, "field_values", {}),
            }
        )

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
    return rules_report


def _zero_candidates_reason(ev: Any) -> str:
    """Explain a rule/step whose blocking search() retrieved nothing --
    real data (which value blocking used) rather than _not_evaluated_reason's
    guess, since we now know blocking ran and came back empty."""
    blocking_criteria = getattr(ev, "blocking_criteria", {}) or {}
    criteria_desc = ", ".join(
        f"{k}={v!r}" for k, v in sorted(blocking_criteria.items())
    )
    step = getattr(ev, "step", "flat")
    step_desc = f" ({step} tier)" if step != "flat" else ""
    return (
        f"Both patients have the required fields, but blocking{step_desc} found no "
        f"candidate matching on {criteria_desc or 'the blocked field(s)'}"
    )


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

    return {
        "outcome": result.outcome.value,
        "matched": result.outcome == MatchOutcome.MATCH,
        "matched_rule_id": result.matched_rule_id,
        "match_type": result.match_type,
        "normalized_fields": {
            "a": _normalized_field_summary(fields_a),
            "b": _normalized_field_summary(fields_b),
        },
        "rules": _build_rules_report(result, fields_a, fields_b),
    }


# Pairwise-matching a bundle is O(n^2) MatchingEngine.match() calls (see
# match_bundle below) -- capped so a large pasted bundle can't turn this
# debug endpoint into an accidental load test.
_MAX_BUNDLE_PATIENTS = 50


def _patient_label(patient: dict[str, Any], index: int) -> str:
    """Human-readable label for a bundle Patient, for the results table.

    Best-effort, display-only -- falls back to the resource id, then a
    positional placeholder, so a patient with no name still gets a row.
    """
    for name in patient.get("name") or []:
        given = " ".join(name.get("given") or [])
        family = name.get("family") or ""
        full = f"{given} {family}".strip()
        if full:
            return full
    return patient.get("id") or f"Patient {index}"


@router.post(
    "/testing-ui-api/match-bundle",
    dependencies=[Depends(_require_testing_ui_enabled)],
)
async def match_bundle(request: Request) -> dict[str, Any]:
    """Pairwise-match every Patient in a pasted FHIR Bundle against every other.

    Runs the same NormalizationManager + MatchingEngine as /match-pair once
    per unordered pair (i, j with i < j), each against a single-candidate
    InMemoryBackend -- so each pair's outcome/rule id reads exactly like a
    two-patient /match-pair call would, rather than introducing a second,
    N-candidate matching semantics (escalate/ambiguous) into this endpoint.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400, detail="Request body must be a JSON object"
        )
    bundle = body.get("bundle")
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        raise HTTPException(
            status_code=400, detail="Request body must be {'bundle': <FHIR Bundle>}"
        )

    patients: list[dict[str, Any]] = []
    for entry in bundle.get("entry") or []:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if isinstance(resource, dict) and resource.get("resourceType") == "Patient":
            patients.append(resource)

    if len(patients) < 2:
        raise HTTPException(
            status_code=400,
            detail="Bundle must contain at least 2 Patient resources",
        )
    if len(patients) > _MAX_BUNDLE_PATIENTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Bundle contains {len(patients)} Patients; this debug tool "
                f"supports at most {_MAX_BUNDLE_PATIENTS} (pairwise matching "
                "is O(n^2))"
            ),
        )

    normalizer = NormalizationManager()
    normalized = [normalizer.normalize(p) for p in patients]

    extractor = FieldExtractor()
    fields = [extractor.extract(p) for p in normalized]

    pairs: list[dict[str, Any]] = []
    for i in range(len(normalized)):
        for j in range(i + 1, len(normalized)):
            backend = InMemoryBackend([normalized[j]])
            engine = MatchingEngine(backend=backend)
            result = await engine.match(normalized[i])
            pairs.append(
                {
                    "a_index": i,
                    "b_index": j,
                    "outcome": result.outcome.value,
                    "matched": result.outcome == MatchOutcome.MATCH,
                    "matched_rule_id": result.matched_rule_id,
                    "match_type": result.match_type,
                    "rules": _build_rules_report(result, fields[i], fields[j]),
                }
            )

    return {
        "patients": [
            {
                "index": i,
                "label": _patient_label(p, i),
                "normalized_fields": _normalized_field_summary(fields[i]),
            }
            for i, p in enumerate(patients)
        ],
        "pairs": pairs,
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
