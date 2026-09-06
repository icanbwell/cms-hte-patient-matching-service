"""Adapter between the FHIR $match wire format and cms-hte-patient-matching's
PatientMatcherService.

Owns FHIR Parameters parsing and FHIR Bundle response building around the
library call, mirroring person-matching-service's MatchService (one class
owns parse -> library call -> response build) rather than splitting into
separate parser/adapter/builder classes -- see docs/TECH_DESIGN.md.

Unlike person-matching-service, this controller does not do its own
candidate-retrieval/blocking: PatientMatcherService already owns that
end-to-end via its configured CacheBackend.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from patient_matching.api.service import MatchResponse, PatientMatcherService

logger = logging.getLogger(__name__)


class InvalidMatchRequest(ValueError):
    """The request body isn't a valid FHIR $match Parameters payload."""


class MatchController:
    """Wraps a PatientMatcherService instance with FHIR (de)serialization."""

    def __init__(self, *, service: PatientMatcherService) -> None:
        self._service = service

    def match(self, parameter_json: dict[str, Any]) -> dict[str, Any]:
        """Run a FHIR $match request and return a FHIR searchset Bundle.

        Raises:
            InvalidMatchRequest: if parameter_json isn't a valid $match
                Parameters payload containing a Patient resource.
        """
        patient = self._extract_patient(parameter_json)
        result = self._service.match_patient(patient)
        return self._build_bundle(result)

    @staticmethod
    def _extract_patient(parameter_json: dict[str, Any]) -> dict[str, Any]:
        """Extract the query Patient resource from a FHIR Parameters payload.

        Expects the standard FHIR $match request shape:
        {"resourceType": "Parameters", "parameter": [{"name": "resource",
        "resource": {"resourceType": "Patient", ...}}]}
        """
        if not isinstance(parameter_json, dict):
            raise InvalidMatchRequest("Request body must be a JSON object")

        if parameter_json.get("resourceType") != "Parameters":
            raise InvalidMatchRequest(
                "Request body must be a FHIR Parameters resource "
                f"(got resourceType={parameter_json.get('resourceType')!r})"
            )

        for param in parameter_json.get("parameter", []) or []:
            if param.get("name") == "resource":
                resource = param.get("resource")
                if (
                    isinstance(resource, dict)
                    and resource.get("resourceType") == "Patient"
                ):
                    return resource

        raise InvalidMatchRequest(
            "Parameters resource must contain a 'resource' parameter "
            "with a Patient resource"
        )

    @staticmethod
    def _build_bundle(result: MatchResponse) -> dict[str, Any]:
        """Build a FHIR searchset Bundle from a MatchResponse.

        Each entry carries the match outcome/rule/confidence as a
        b.well-namespaced extension on the search mode, so a caller can see
        *why* a candidate matched (CMS's audit-trail requirement) without
        parsing rule_evaluations_summary separately.
        """
        entries: list[dict[str, Any]] = []
        for patient_dict in result.matched_patients:
            patient_id = patient_dict.get("id", "")
            entry: dict[str, Any] = {
                "resource": patient_dict,
                "search": {
                    "mode": "match",
                    "score": result.confidence_score,
                    "extension": [
                        {
                            "url": "https://icanbwell.com/patient_match/outcome",
                            "valueString": result.outcome,
                        },
                        {
                            "url": "https://icanbwell.com/patient_match/matched_rule_id",
                            "valueString": result.matched_rule_id or "",
                        },
                    ],
                },
            }
            if patient_id:
                entry["fullUrl"] = f"Patient/{patient_id}"
            entries.append(entry)

        bundle: dict[str, Any] = {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(entries),
        }
        if entries:
            bundle["entry"] = entries
        return bundle


def get_match_controller(request: Request) -> MatchController:
    """FastAPI dependency: retrieve the MatchController from app.state."""
    controller: MatchController | None = getattr(
        request.app.state, "match_controller", None
    )
    if controller is None:
        raise RuntimeError("MatchController is not configured on app.state")
    return controller
