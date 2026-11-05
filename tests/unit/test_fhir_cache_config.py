import pytest

from patient_matching_service.api import _parse_fhir_search_params

SYNTHETIC_FILTER = (
    "_security=https://www.icanbwell.com/codes/data_provenance"
    "|synthetic_test_data_generator"
)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_blank_means_no_filter(raw: str | None) -> None:
    assert _parse_fhir_search_params(raw) is None


def test_security_filter_is_passed_through_unchanged() -> None:
    assert _parse_fhir_search_params(SYNTHETIC_FILTER) == {
        "_security": (
            "https://www.icanbwell.com/codes/data_provenance"
            "|synthetic_test_data_generator"
        )
    }


def test_percent_encoded_value_is_decoded() -> None:
    encoded = (
        "_security=https%3A%2F%2Fwww.icanbwell.com%2Fcodes%2Fdata_provenance"
        "%7Csynthetic_test_data_generator"
    )
    assert _parse_fhir_search_params(encoded) == _parse_fhir_search_params(
        SYNTHETIC_FILTER
    )


def test_several_parameters() -> None:
    assert _parse_fhir_search_params("_security=a|b&_lastUpdated=gt2026-01-01") == {
        "_security": "a|b",
        "_lastUpdated": "gt2026-01-01",
    }


@pytest.mark.parametrize("raw", ["a=1&a=2", "novalue", "=x"])
def test_malformed_or_ambiguous_filter_is_rejected(raw: str) -> None:
    # A filter that silently widens to "everyone" is the failure to avoid, so
    # anything ambiguous fails startup loudly instead.
    with pytest.raises(ValueError):
        _parse_fhir_search_params(raw)
