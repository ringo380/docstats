"""Tests for the concurrency helpers."""

from __future__ import annotations

import asyncio

import pytest

from docstats.concurrency import (
    DEFAULT_CONCURRENCY,
    async_limiter,
    get_default_concurrency,
)


class TestDefault:
    def test_without_env(self, monkeypatch):
        monkeypatch.delenv("DOCSTATS_HTTP_CONCURRENCY", raising=False)
        assert get_default_concurrency() == DEFAULT_CONCURRENCY

    def test_honors_env(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_CONCURRENCY", "12")
        assert get_default_concurrency() == 12

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_CONCURRENCY", "three")
        assert get_default_concurrency() == DEFAULT_CONCURRENCY

    def test_below_minimum_falls_back(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_CONCURRENCY", "0")
        assert get_default_concurrency() == DEFAULT_CONCURRENCY


class TestAsyncLimiter:
    @pytest.mark.asyncio
    async def test_explicit_size_overrides_env(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_CONCURRENCY", "99")
        sem = async_limiter(3)
        assert isinstance(sem, asyncio.Semaphore)
        assert sem._value == 3  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_uses_env_when_no_arg(self, monkeypatch):
        monkeypatch.setenv("DOCSTATS_HTTP_CONCURRENCY", "4")
        sem = async_limiter()
        assert sem._value == 4  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_caps_in_flight(self):
        sem = async_limiter(2)
        in_flight = 0
        max_in_flight = 0

        async def worker() -> None:
            nonlocal in_flight, max_in_flight
            async with sem:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0)
                in_flight -= 1

        await asyncio.gather(*(worker() for _ in range(10)))
        assert max_in_flight <= 2
