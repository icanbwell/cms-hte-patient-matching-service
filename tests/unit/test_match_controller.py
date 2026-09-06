from typing import Any
from unittest.mock import AsyncMock

import pytest
from patient_matching.api.service import MatchResponse

from patient_matching_service.service.match_controller import (
    InvalidMatchRequest,
    MatchController,
)


def _parameters(patient: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Parameters",
        "parameter": [{"name": "resource", "resource": patient}],
    }


class TestExtractPatient:
    def test_extracts_patient_resource(self) -> None:
        patient = {"resourceType": "Patient", "id": "p1"}
        assert MatchController._extract_patient(_parameters(patient)) == patient

    @pytest.mark.parametrize(
        "body",
        [
            "not a dict",
            {"resourceType": "Patient"},
            {"resourceType": "Parameters", "parameter": []},
            {"resourceType": "Parameters", "parameter": [{"name": "other"}]},
            {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "resource", "resource": {"resourceType": "Observation"}}
                ],
            },
        ],
    )
    def test_rejects_invalid_payloads(self, body: object) -> None:
        with pytest.raises(InvalidMatchRequest):
            MatchController._extract_patient(body)  # type: ignore[arg-type]


class TestMatch:
    async def test_calls_service_and_builds_bundle(self) -> None:
        service = AsyncMock()
        service.match_patient.return_value = MatchResponse(
            outcome="match",
            matched_patient_ids=["p1"],
            matched_patients=[{"resourceType": "Patient", "id": "p1"}],
            matched_rule_id="01",
            match_type="exact",
            confidence_score=0.999999999999,
            candidate_count=1,
            rule_evaluations_summary=[],
        )
        controller = MatchController(service=service)

        patient = {"resourceType": "Patient", "id": "query"}
        bundle = await controller.match(_parameters(patient))

        service.match_patient.assert_called_once_with(patient)
        assert bundle["resourceType"] == "Bundle"
        assert bundle["type"] == "searchset"
        assert bundle["total"] == 1
        entry = bundle["entry"][0]
        assert entry["fullUrl"] == "Patient/p1"
        assert entry["search"]["score"] == 0.999999999999
        extensions = {e["url"]: e["valueString"] for e in entry["search"]["extension"]}
        assert extensions["https://icanbwell.com/patient_match/outcome"] == "match"
        assert extensions["https://icanbwell.com/patient_match/matched_rule_id"] == "01"

    async def test_no_match_builds_empty_bundle(self) -> None:
        service = AsyncMock()
        service.match_patient.return_value = MatchResponse(outcome="no_match")
        controller = MatchController(service=service)

        bundle = await controller.match(_parameters({"resourceType": "Patient"}))

        assert bundle["total"] == 0
        assert "entry" not in bundle

    async def test_invalid_request_never_calls_service(self) -> None:
        service = AsyncMock()
        controller = MatchController(service=service)

        with pytest.raises(InvalidMatchRequest):
            await controller.match({"resourceType": "Patient"})

        service.match_patient.assert_not_called()
