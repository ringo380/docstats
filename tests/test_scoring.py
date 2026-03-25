"""Tests for result scoring and ranking."""

from docstats.models import NPIResult
from docstats.scoring import SearchQuery, rank_results, score_result
from tests.conftest import SAMPLE_NPI1_RESULT, SAMPLE_NPI2_RESULT


def _make_result(**overrides) -> NPIResult:
    """Create an NPIResult with overrides to the sample data."""
    data = {**SAMPLE_NPI1_RESULT, **overrides}
    if "basic" not in overrides and any(
        k in overrides for k in ("status", "first_name", "last_name", "middle_name")
    ):
        data["basic"] = {**SAMPLE_NPI1_RESULT["basic"]}
        for key in ("status", "first_name", "last_name", "middle_name"):
            if key in overrides:
                data["basic"][key] = overrides.pop(key)
    return NPIResult.model_validate(data)


class TestScoring:
    def test_active_provider_scores_higher(self):
        query = SearchQuery(last_name="SMITH")
        active = _make_result(basic={**SAMPLE_NPI1_RESULT["basic"], "status": "A"})
        inactive = _make_result(basic={**SAMPLE_NPI1_RESULT["basic"], "status": "D"})
        assert score_result(active, query) > score_result(inactive, query)

    def test_exact_last_name_scores_higher_than_partial(self):
        query = SearchQuery(last_name="SMITH")
        exact = _make_result(basic={**SAMPLE_NPI1_RESULT["basic"], "last_name": "SMITH"})
        partial = _make_result(basic={**SAMPLE_NPI1_RESULT["basic"], "last_name": "SMITHSON"})
        assert score_result(exact, query) > score_result(partial, query)

    def test_first_name_match_adds_score(self):
        query = SearchQuery(last_name="SMITH", first_name="JOHN")
        with_first = _make_result(
            basic={**SAMPLE_NPI1_RESULT["basic"], "last_name": "SMITH", "first_name": "JOHN"}
        )
        without_first = _make_result(
            basic={**SAMPLE_NPI1_RESULT["basic"], "last_name": "SMITH", "first_name": "JANE"}
        )
        assert score_result(with_first, query) > score_result(without_first, query)

    def test_middle_name_exact_match(self):
        query = SearchQuery(last_name="SMITH", middle_name="ROBERT")
        with_mid = _make_result(
            basic={**SAMPLE_NPI1_RESULT["basic"], "middle_name": "ROBERT"}
        )
        without_mid = _make_result(
            basic={**SAMPLE_NPI1_RESULT["basic"], "middle_name": "JAMES"}
        )
        assert score_result(with_mid, query) > score_result(without_mid, query)

    def test_middle_initial_match(self):
        query = SearchQuery(last_name="SMITH", middle_name="R")
        with_r = _make_result(
            basic={**SAMPLE_NPI1_RESULT["basic"], "middle_name": "ROBERT"}
        )
        with_j = _make_result(
            basic={**SAMPLE_NPI1_RESULT["basic"], "middle_name": "JAMES"}
        )
        assert score_result(with_r, query) > score_result(with_j, query)

    def test_zip_match_beats_city_state(self):
        query = SearchQuery(postal_code="94110", state="CA")
        zip_match = NPIResult.model_validate(SAMPLE_NPI1_RESULT)  # has 94110 ZIP
        # Create one with different ZIP -- only gets state match (+10) not ZIP match (+20)
        other_data = {**SAMPLE_NPI1_RESULT}
        other_data["addresses"] = [{
            **SAMPLE_NPI1_RESULT["addresses"][0],
            "postal_code": "90210",
            "city": "BEVERLY HILLS",
        }]
        other = NPIResult.model_validate(other_data)
        assert score_result(zip_match, query) > score_result(other, query)

    def test_org_name_match(self):
        query = SearchQuery(organization_name="KAISER")
        result = NPIResult.model_validate(SAMPLE_NPI2_RESULT)
        score = score_result(result, query)
        # Should get org name partial match bonus
        assert score > 50  # at least active + org match


class TestRanking:
    def test_rank_results_sorts_by_score(self):
        query = SearchQuery(last_name="SMITH")
        active = _make_result(
            number="1111111111",
            basic={**SAMPLE_NPI1_RESULT["basic"], "status": "A", "last_name": "SMITH"},
        )
        inactive = _make_result(
            number="2222222222",
            basic={**SAMPLE_NPI1_RESULT["basic"], "status": "D", "last_name": "SMITH"},
        )
        ranked = rank_results([inactive, active], query)
        assert ranked[0].number == "1111111111"
        assert ranked[1].number == "2222222222"

    def test_empty_results(self):
        query = SearchQuery(last_name="SMITH")
        assert rank_results([], query) == []
