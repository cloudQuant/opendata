"""
Retry utility tests.

Tests for the retry decorators used in data fetch operations.
"""

from unittest.mock import Mock

import pytest


class TestRetryOnException:
    """Test synchronous retry decorator."""

    def test_retry_on_exception_success_first_try(self):
        """Test function succeeds on first try."""
        from opendata.data_fetch.utils.retry import retry_on_exception

        @retry_on_exception(max_retries=3)
        def successful_function():
            return "success"

        result = successful_function()
        assert result == "success"

    def test_retry_on_exception_success_after_retry(self):
        """Test function succeeds after initial failure."""
        from opendata.data_fetch.utils.retry import retry_on_exception

        call_count = 0

        @retry_on_exception(max_retries=3, retry_delay=0)
        def flaky_function():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("Temporary failure")
            return "success"

        result = flaky_function()
        assert result == "success"
        assert call_count == 2

    def test_retry_on_exception_max_retries_exceeded(self):
        """Test function fails after max retries."""
        from opendata.data_fetch.utils.retry import retry_on_exception

        @retry_on_exception(max_retries=2, retry_delay=0)
        def failing_function():
            raise ValueError("Permanent failure")

        with pytest.raises(ValueError, match="Permanent failure"):
            failing_function()

    def test_retry_on_exception_custom_allowed_exceptions(self):
        """Test retry with custom exception types."""
        from opendata.data_fetch.utils.retry import retry_on_exception

        @retry_on_exception(max_retries=2, retry_delay=0, allowed_exceptions=(ValueError,))
        def selective_function():
            raise TypeError("Different error")

        # Should not retry for TypeError
        with pytest.raises(TypeError, match="Different error"):
            selective_function()

    def test_retry_on_exception_default_parameters(self):
        """Test decorator with default parameters."""
        from opendata.data_fetch.utils.retry import retry_on_exception

        @retry_on_exception()
        def test_func():
            return "done"

        assert test_func() == "done"

    def test_retry_on_exception_with_logger(self):
        """Test decorator with custom logger."""
        from opendata.data_fetch.utils.retry import retry_on_exception

        mock_logger = Mock()

        @retry_on_exception(max_retries=2, retry_delay=0, logger=mock_logger)
        def failing_function():
            raise ValueError("Test error")

        with pytest.raises(ValueError):
            failing_function()

        # Should have logged warning and error
        assert mock_logger.warning.called
        assert mock_logger.error.called

    def test_retry_on_exception_exponential_backoff(self):
        """Test exponential backoff in retry delays."""
        import time

        from opendata.data_fetch.utils.retry import retry_on_exception

        call_times = []

        @retry_on_exception(max_retries=3, retry_delay=0.1)
        def delayed_function():
            call_times.append(time.time())
            if len(call_times) < 3:
                raise ValueError("Fail")
            return "success"

        start_time = time.time()
        result = delayed_function()
        elapsed = time.time() - start_time

        assert result == "success"
        assert len(call_times) == 3
        # Should have delays: 0.1s, 0.2s (approximately)
        assert elapsed >= 0.25  # At least 0.1 + 0.2 = 0.3 seconds

    def test_retry_on_exception_preserves_function_name(self):
        """Test that decorator preserves function name."""
        from opendata.data_fetch.utils.retry import retry_on_exception

        @retry_on_exception()
        def my_function():
            pass

        assert my_function.__name__ == "my_function"

    def test_retry_on_exception_with_args(self):
        """Test that function arguments are passed through."""
        from opendata.data_fetch.utils.retry import retry_on_exception

        @retry_on_exception(max_retries=2, retry_delay=0)
        def function_with_args(a, b, c=None):
            return f"{a}-{b}-{c}"

        result = function_with_args(1, 2, c=3)
        assert result == "1-2-3"

    def test_retry_on_exception_class_method(self):
        """Test decorator works with class methods."""
        from opendata.data_fetch.utils.retry import retry_on_exception

        class TestClass:
            def __init__(self):
                self.attempts = 0

            @retry_on_exception(max_retries=3, retry_delay=0)
            def method(self):
                self.attempts += 1
                if self.attempts < 2:
                    raise ValueError("Fail")
                return "success"

        obj = TestClass()
        result = obj.method()
        assert result == "success"
        assert obj.attempts == 2


class TestAsyncRetryOnException:
    """Test asynchronous retry decorator exists."""

    def test_async_retry_decorator_exists(self):
        """Test that async retry decorator exists."""
        from opendata.data_fetch.utils.retry import async_retry_on_exception

        assert async_retry_on_exception is not None
        # It's a regular function (decorator factory) that returns a decorator
        assert callable(async_retry_on_exception)


class TestRetryIntegration:
    """Integration tests for retry utilities."""

    def test_both_decorators_exist(self):
        """Test both sync and async decorators are available."""
        from opendata.data_fetch.utils import retry

        assert hasattr(retry, "retry_on_exception")
        assert hasattr(retry, "async_retry_on_exception")

    def test_decorator_chaining(self):
        """Test that decorators can be chained."""
        from opendata.data_fetch.utils.retry import retry_on_exception

        def other_decorator(func):
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)

            return wrapper

        @other_decorator
        @retry_on_exception(max_retries=2, retry_delay=0)
        def chained_function():
            return "done"

        assert chained_function() == "done"
