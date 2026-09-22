"""
Tests for async_retry_on_exception decorator in retry.py (lines 70-99).
"""

from unittest.mock import MagicMock

import pytest

from opendata.data_fetch.utils.retry import async_retry_on_exception


class TestAsyncRetryOnException:
    @pytest.mark.asyncio
    async def test_success_first_try(self):
        @async_retry_on_exception(max_retries=3, retry_delay=0.01)
        async def good_func():
            return "ok"

        result = await good_func()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retry_then_success(self):
        call_count = 0

        @async_retry_on_exception(max_retries=3, retry_delay=0.01)
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "ok"

        result = await flaky_func()
        assert result == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exhaust_retries(self):
        @async_retry_on_exception(max_retries=2, retry_delay=0.01)
        async def always_fail():
            raise RuntimeError("always fails")

        with pytest.raises(RuntimeError, match="always fails"):
            await always_fail()

    @pytest.mark.asyncio
    async def test_custom_logger(self):
        mock_logger = MagicMock()

        @async_retry_on_exception(max_retries=2, retry_delay=0.01, logger=mock_logger)
        async def fail_func():
            raise ValueError("test error")

        with pytest.raises(ValueError):
            await fail_func()

        assert mock_logger.warning.called
        assert mock_logger.error.called

    @pytest.mark.asyncio
    async def test_allowed_exceptions(self):
        @async_retry_on_exception(max_retries=3, retry_delay=0.01, allowed_exceptions=(ValueError,))
        async def type_error_func():
            raise TypeError("not allowed")

        with pytest.raises(TypeError):
            await type_error_func()

    @pytest.mark.asyncio
    async def test_logger_from_self(self):
        """Test that logger is taken from args[0].logger if available."""
        mock_logger = MagicMock()

        class MyClass:
            def __init__(self):
                self.logger = mock_logger

        obj = MyClass()

        @async_retry_on_exception(max_retries=2, retry_delay=0.01)
        async def method(self_arg):
            raise ValueError("fail")

        with pytest.raises(ValueError):
            await method(obj)

        assert mock_logger.warning.called

    @pytest.mark.asyncio
    async def test_default_logger(self):
        """Test fallback to default logger when no logger provided."""

        @async_retry_on_exception(max_retries=2, retry_delay=0.01)
        async def no_logger_func():
            raise ValueError("fail")

        with pytest.raises(ValueError):
            await no_logger_func()
