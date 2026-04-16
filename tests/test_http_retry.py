"""Tests for the shared HTTP retry helper and timeout/retry env config."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from docstats import http_retry
from docstats.http_retry import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    get_default_max_retries,
    get_default_timeout,
    request_with_retry,
)


class _Err(Exception):
    """Test-only error class so we can assert on exhaustion."""


def _resp(status: int, headers: dict | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.headers = headers or {}
    return r


class TestEnvConfig:
    def test_default_timeout_without_env(self, monkeypatch):
        monkeypatch.delenv("DOCSTATS_HTTP_TIMEOUT", raising=False)
        assert get_default_timeout() == DEFAULT_TIMEOUT_SECONDS

    def test_default_timeout_honors_env(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_TIMEOUT", "12.5")
        assert get_default_timeout() == 12.5

    def test_default_timeout_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_TIMEOUT", "not-a-number")
        assert get_default_timeout() == DEFAULT_TIMEOUT_SECONDS

    def test_default_timeout_non_positive_falls_back(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_TIMEOUT", "0")
        assert get_default_timeout() == DEFAULT_TIMEOUT_SECONDS

    def test_default_max_retries_without_env(self, monkeypatch):
        monkeypatch.delenv("DOCSTATS_HTTP_MAX_RETRIES", raising=False)
        assert get_default_max_retries() == DEFAULT_MAX_RETRIES

    def test_default_max_retries_honors_env(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_MAX_RETRIES", "5")
        assert get_default_max_retries() == 5

    def test_default_max_retries_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_MAX_RETRIES", "")
        assert get_default_max_retries() == DEFAULT_MAX_RETRIES

    def test_default_max_retries_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_MAX_RETRIES", "-1")
        assert get_default_max_retries() == DEFAULT_MAX_RETRIES


class TestRetryBehavior:
    def test_returns_on_first_success(self):
        http = MagicMock()
        http.request.return_value = _resp(200)
        with patch.object(http_retry.time, "sleep") as mock_sleep:
            resp = request_with_retry(http, "GET", "https://x", error_class=_Err)
        assert resp.status_code == 200
        assert http.request.call_count == 1
        assert mock_sleep.call_count == 0

    def test_retries_on_500_then_succeeds(self):
        http = MagicMock()
        http.request.side_effect = [_resp(500), _resp(500), _resp(200)]
        with patch.object(http_retry.time, "sleep") as mock_sleep:
            resp = request_with_retry(http, "GET", "https://x", error_class=_Err)
        assert resp.status_code == 200
        assert http.request.call_count == 3
        # default backoff_base=2.0 → delays 2**0=1s, 2**1=2s
        assert [c.args[0] for c in mock_sleep.call_args_list] == [1.0, 2.0]

    def test_exhaustion_raises_error_class(self):
        http = MagicMock()
        http.request.return_value = _resp(503)
        with patch.object(http_retry.time, "sleep"):
            with pytest.raises(_Err, match="returned 503"):
                request_with_retry(http, "GET", "https://x", error_class=_Err)
        # 1 initial + 3 retries with default max_retries=3
        assert http.request.call_count == 4

    def test_non_retryable_status_raises_immediately(self):
        http = MagicMock()
        http.request.return_value = _resp(400)
        with patch.object(http_retry.time, "sleep") as mock_sleep:
            with pytest.raises(_Err, match="returned 400"):
                request_with_retry(http, "GET", "https://x", error_class=_Err)
        assert http.request.call_count == 1
        assert mock_sleep.call_count == 0

    def test_timeout_retries_then_exhausts(self):
        http = MagicMock()
        http.request.side_effect = httpx.ReadTimeout("slow")
        with patch.object(http_retry.time, "sleep"):
            with pytest.raises(_Err) as excinfo:
                request_with_retry(http, "GET", "https://x", error_class=_Err)
        assert isinstance(excinfo.value.__cause__, httpx.TimeoutException)
        assert http.request.call_count == 4

    def test_request_error_retries_then_exhausts(self):
        http = MagicMock()
        http.request.side_effect = httpx.ConnectError("nope")
        with patch.object(http_retry.time, "sleep"):
            with pytest.raises(_Err) as excinfo:
                request_with_retry(http, "GET", "https://x", error_class=_Err)
        assert isinstance(excinfo.value.__cause__, httpx.ConnectError)

    def test_env_overrides_retry_count(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_MAX_RETRIES", "1")
        http = MagicMock()
        http.request.return_value = _resp(500)
        with patch.object(http_retry.time, "sleep"):
            with pytest.raises(_Err):
                request_with_retry(http, "GET", "https://x", error_class=_Err)
        # 1 initial + 1 retry
        assert http.request.call_count == 2

    def test_explicit_max_retries_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_MAX_RETRIES", "10")
        http = MagicMock()
        http.request.return_value = _resp(500)
        with patch.object(http_retry.time, "sleep"):
            with pytest.raises(_Err):
                request_with_retry(http, "GET", "https://x", error_class=_Err, max_retries=0)
        assert http.request.call_count == 1


class TestRetryAfter:
    def test_honors_integer_retry_after(self):
        http = MagicMock()
        http.request.side_effect = [_resp(429, {"retry-after": "7"}), _resp(200)]
        with patch.object(http_retry.time, "sleep") as mock_sleep:
            request_with_retry(http, "GET", "https://x", error_class=_Err)
        mock_sleep.assert_called_once_with(7.0)

    def test_ignores_non_numeric_retry_after(self):
        http = MagicMock()
        http.request.side_effect = [
            _resp(429, {"retry-after": "tomorrow"}),
            _resp(200),
        ]
        with patch.object(http_retry.time, "sleep") as mock_sleep:
            request_with_retry(http, "GET", "https://x", error_class=_Err)
        # Falls through to computed backoff (1.0 on first retry, base=1.0)
        mock_sleep.assert_called_once_with(1.0)

    def test_ignores_sub_half_second_retry_after(self):
        http = MagicMock()
        http.request.side_effect = [_resp(429, {"retry-after": "0.1"}), _resp(200)]
        with patch.object(http_retry.time, "sleep") as mock_sleep:
            request_with_retry(http, "GET", "https://x", error_class=_Err)
        mock_sleep.assert_called_once_with(1.0)
