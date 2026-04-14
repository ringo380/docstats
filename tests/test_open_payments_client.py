"""Tests for Open Payments (Sunshine Act) client."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from docstats.open_payments_client import OpenPaymentsClient, OpenPaymentsError


SAMPLE_PAYMENT_ROW_1 = {
    "covered_recipient_npi": "1003000126",
    "total_amount_of_payment_usdollars": "20.78",
    "applicable_manufacturer_or_applicable_gpo_making_payment_name": "Paratek Pharmaceuticals, Inc.",
    "nature_of_payment_or_transfer_of_value": "Food and Beverage",
    "program_year": "2023",
}

SAMPLE_PAYMENT_ROW_2 = {
    "covered_recipient_npi": "1003000126",
    "total_amount_of_payment_usdollars": "150.00",
    "applicable_manufacturer_or_applicable_gpo_making_payment_name": "Pfizer Inc.",
    "nature_of_payment_or_transfer_of_value": "Food and Beverage",
    "program_year": "2023",
}

SAMPLE_PAYMENT_ROW_3 = {
    "covered_recipient_npi": "1003000126",
    "total_amount_of_payment_usdollars": "75.50",
    "applicable_manufacturer_or_applicable_gpo_making_payment_name": "Paratek Pharmaceuticals, Inc.",
    "nature_of_payment_or_transfer_of_value": "Education",
    "program_year": "2023",
}


class TestOpenPaymentsLookup:
    def test_single_payment(self):
        client = OpenPaymentsClient()
        with patch.object(client, "_query", return_value=[SAMPLE_PAYMENT_ROW_1]):
            result = client.lookup_payments("1003000126")

        assert result is not None
        assert result["total_payments"] == 20.78
        assert result["payment_count"] == 1
        assert result["payment_year"] == 2024  # first year tried
        assert len(result["top_payers"]) == 1
        assert result["top_payers"][0]["name"] == "Paratek Pharmaceuticals, Inc."
        client.close()

    def test_multiple_payments_aggregated(self):
        client = OpenPaymentsClient()
        rows = [SAMPLE_PAYMENT_ROW_1, SAMPLE_PAYMENT_ROW_2, SAMPLE_PAYMENT_ROW_3]
        with patch.object(client, "_query", return_value=rows):
            result = client.lookup_payments("1003000126")

        assert result is not None
        assert result["total_payments"] == 246.28  # 20.78 + 150.00 + 75.50
        assert result["payment_count"] == 3
        # Pfizer should be second; Paratek first (20.78 + 75.50 = 96.28)
        # Actually: Pfizer=150, Paratek=96.28 → Pfizer first
        assert result["top_payers"][0]["name"] == "Pfizer Inc."
        assert result["top_payers"][0]["amount"] == 150.00
        assert result["top_payers"][1]["name"] == "Paratek Pharmaceuticals, Inc."
        assert result["top_payers"][1]["amount"] == 96.28
        client.close()

    def test_no_payments(self):
        client = OpenPaymentsClient()
        with patch.object(client, "_query", return_value=[]):
            result = client.lookup_payments("0000000000")

        assert result is None
        client.close()

    def test_fallback_to_prior_year(self):
        """If 2024 has no results, try 2023."""
        client = OpenPaymentsClient()
        call_count = [0]

        def side_effect(dataset_id, npi):
            call_count[0] += 1
            if call_count[0] == 1:
                return []  # 2024 empty
            return [SAMPLE_PAYMENT_ROW_1]  # 2023 has data

        with patch.object(client, "_query", side_effect=side_effect):
            result = client.lookup_payments("1003000126")

        assert result is not None
        assert result["payment_year"] == 2023
        assert call_count[0] == 2
        client.close()

    def test_invalid_amount_handled(self):
        row = {**SAMPLE_PAYMENT_ROW_1, "total_amount_of_payment_usdollars": "invalid"}
        client = OpenPaymentsClient()
        with patch.object(client, "_query", return_value=[row]):
            result = client.lookup_payments("1003000126")

        assert result is not None
        assert result["total_payments"] == 0.0
        client.close()


class TestOpenPaymentsAggregate:
    def test_payer_deduplication(self):
        """Payments from same payer should be summed."""
        client = OpenPaymentsClient()
        rows = [SAMPLE_PAYMENT_ROW_1, SAMPLE_PAYMENT_ROW_3]  # Both from Paratek
        result = client._aggregate(rows, 2023)

        assert len(result["top_payers"]) == 1
        assert result["top_payers"][0]["amount"] == 96.28  # 20.78 + 75.50
        client.close()

    def test_top_payers_limited_to_10(self):
        """Should only return top 10 payers."""
        client = OpenPaymentsClient()
        rows = [
            {**SAMPLE_PAYMENT_ROW_1, "applicable_manufacturer_or_applicable_gpo_making_payment_name": f"Company {i}"}
            for i in range(15)
        ]
        result = client._aggregate(rows, 2023)
        assert len(result["top_payers"]) == 10
        client.close()


class TestOpenPaymentsRetry:
    def test_retries_on_server_error(self):
        client = OpenPaymentsClient()
        mock_500 = MagicMock()
        mock_500.status_code = 500
        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.json.return_value = {"results": [SAMPLE_PAYMENT_ROW_1]}

        with patch.object(client._http, "post", side_effect=[mock_500, mock_200]):
            with patch("docstats.open_payments_client.time.sleep"):
                result = client._query("test-dataset", "1003000126")

        assert len(result) == 1
        client.close()

    def test_raises_after_max_retries(self):
        client = OpenPaymentsClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch.object(client._http, "post", return_value=mock_resp):
            with patch("docstats.open_payments_client.time.sleep"):
                with pytest.raises(OpenPaymentsError):
                    client._query("test-dataset", "1003000126")
        client.close()
