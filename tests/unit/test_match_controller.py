from typing import Any
from unittest.mock import AsyncMock

import pytest
from cmshteial2reader import TokenVerificationError
from patient_matching.api.service import MatchResponse
from pydantic import ValidationError

from patient_matching_service.service.match_controller import (
    Ial2PatientValidationError,
    InvalidMatchRequest,
    MatchController,
)


def _parameters(patient: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Parameters",
        "parameter": [{"name": "resource", "resource": patient}],
    }


def _cms_smart_claims(id_token: str) -> dict[str, Any]:
    return {"extensions": {"cms_smart": {"version": "1", "id_token": id_token}}}


def _make_validation_error() -> ValidationError:
    from pydantic import BaseModel

    class _Model(BaseModel):
        x: int

    try:
        _Model(x="not-an-int")  # type: ignore[arg-type]
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")  # pragma: no cover


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
        bundle = await controller.match(_parameters(patient), jwt_claims={})

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

        bundle = await controller.match(
            _parameters({"resourceType": "Patient"}), jwt_claims={}
        )

        assert bundle["total"] == 0
        assert "entry" not in bundle

    async def test_invalid_request_never_calls_service(self) -> None:
        service = AsyncMock()
        controller = MatchController(service=service)

        with pytest.raises(InvalidMatchRequest):
            await controller.match({"resourceType": "Patient"}, jwt_claims={})

        service.match_patient.assert_not_called()

    async def test_missing_body_without_cms_smart_claim_rejected(self) -> None:
        service = AsyncMock()
        controller = MatchController(service=service)

        with pytest.raises(InvalidMatchRequest):
            await controller.match(None, jwt_claims={})

        service.match_patient.assert_not_called()


class TestMatchWithIal2:
    async def test_cms_smart_claim_extracts_patient_and_skips_body(self) -> None:
        service = AsyncMock()
        service.match_patient.return_value = MatchResponse(outcome="no_match")
        ial2_extractor = AsyncMock()
        extracted_patient = {"resourceType": "Patient", "id": "from-token"}
        ial2_extractor.extract.return_value = extracted_patient
        controller = MatchController(service=service, ial2_extractor=ial2_extractor)

        bundle = await controller.match(
            None, jwt_claims=_cms_smart_claims("the-nested-jwt")
        )

        ial2_extractor.extract.assert_called_once_with("the-nested-jwt")
        service.match_patient.assert_called_once_with(extracted_patient)
        assert bundle["total"] == 0

    async def test_cms_smart_claim_ignores_body(self) -> None:
        service = AsyncMock()
        service.match_patient.return_value = MatchResponse(outcome="no_match")
        ial2_extractor = AsyncMock()
        extracted_patient = {"resourceType": "Patient", "id": "from-token"}
        ial2_extractor.extract.return_value = extracted_patient
        controller = MatchController(service=service, ial2_extractor=ial2_extractor)

        body_patient = {"resourceType": "Patient", "id": "from-body"}
        await controller.match(
            _parameters(body_patient),
            jwt_claims=_cms_smart_claims("the-nested-jwt"),
        )

        service.match_patient.assert_called_once_with(extracted_patient)

    async def test_cms_smart_claim_without_configured_extractor_rejected(self) -> None:
        service = AsyncMock()
        controller = MatchController(service=service, ial2_extractor=None)

        with pytest.raises(InvalidMatchRequest):
            await controller.match(None, jwt_claims=_cms_smart_claims("the-nested-jwt"))

        service.match_patient.assert_not_called()

    async def test_cms_smart_claim_propagates_token_verification_error(self) -> None:
        service = AsyncMock()
        ial2_extractor = AsyncMock()
        ial2_extractor.extract.side_effect = TokenVerificationError("rejected")
        controller = MatchController(service=service, ial2_extractor=ial2_extractor)

        with pytest.raises(TokenVerificationError):
            await controller.match(None, jwt_claims=_cms_smart_claims("the-nested-jwt"))

        service.match_patient.assert_not_called()

    async def test_cms_smart_claim_wraps_validation_error(self) -> None:
        """MatchController wraps the extractor's raw pydantic ValidationError
        in Ial2PatientValidationError, not letting it propagate directly --
        see Ial2PatientValidationError's docstring for why: api.py's global
        exception handler needs a type it can trust always means "an IAL2
        token produced a bad Patient", not any pydantic model in the app."""
        service = AsyncMock()
        ial2_extractor = AsyncMock()
        original = _make_validation_error()
        ial2_extractor.extract.side_effect = original
        controller = MatchController(service=service, ial2_extractor=ial2_extractor)

        with pytest.raises(Ial2PatientValidationError) as exc_info:
            await controller.match(None, jwt_claims=_cms_smart_claims("the-nested-jwt"))

        assert exc_info.value.original is original
        service.match_patient.assert_not_called()

    @pytest.mark.parametrize(
        "cms_smart_extension",
        [
            {"version": "1", "id_token": ""},
            {"version": "1", "id_token": "   "},
            {"version": "1", "id_token": 12345},
            {"version": "1"},
        ],
    )
    async def test_malformed_cms_smart_extension_rejected_not_fallen_back_to_body(
        self, cms_smart_extension: dict[str, Any]
    ) -> None:
        """A cms_smart block that's present but yields no usable id_token
        must be rejected, not silently treated the same as "no cms_smart
        identity at all" (which would fall through to the body-Patient
        path below and let a malformed/emptied claim downgrade an
        IAL2-intended request into an unverified caller-supplied one)."""
        service = AsyncMock()
        ial2_extractor = AsyncMock()
        controller = MatchController(service=service, ial2_extractor=ial2_extractor)

        body_patient = {"resourceType": "Patient", "id": "from-body"}
        with pytest.raises(InvalidMatchRequest):
            await controller.match(
                _parameters(body_patient),
                jwt_claims={"extensions": {"cms_smart": cms_smart_extension}},
            )

        ial2_extractor.extract.assert_not_called()
        service.match_patient.assert_not_called()

    async def test_no_cms_smart_claim_uses_body_path_even_when_ial2_configured(
        self,
    ) -> None:
        service = AsyncMock()
        service.match_patient.return_value = MatchResponse(outcome="no_match")
        ial2_extractor = AsyncMock()
        controller = MatchController(service=service, ial2_extractor=ial2_extractor)

        patient = {"resourceType": "Patient", "id": "query"}
        await controller.match(_parameters(patient), jwt_claims={})

        ial2_extractor.extract.assert_not_called()
        service.match_patient.assert_called_once_with(patient)
