"""
Direct tests for data_fetch retry utility to maximize coverage.
"""

from unittest.mock import patch

import pytest

from opendata.data_fetch.utils.retry import retry_on_exception


class TestRetryOnException:
    def test_success_no_retry(self):
        call_count = 0

        @retry_on_exception(max_retries=3, retry_delay=0)
        def succeed():
            nonlocal call_count
            call_count += 1
            return "ok"

        assert succeed() == "ok"
        assert call_count == 1

    def test_fails_then_succeeds(self):
        call_count = 0

        @retry_on_exception(max_retries=3, retry_delay=0)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "ok"

        with patch("time.sleep"):
            assert flaky() == "ok"
        assert call_count == 3

    def test_all_retries_exhausted(self):
        @retry_on_exception(max_retries=2, retry_delay=0)
        def always_fail():
            raise RuntimeError("always")

        with patch("time.sleep"), pytest.raises(RuntimeError, match="always"):
            always_fail()

    def test_logger_from_self(self):
        """Test that logger is taken from self if available."""
        import logging

        mock_logger = logging.getLogger("test_self")

        class MyClass:
            logger = mock_logger

            @retry_on_exception(max_retries=1, retry_delay=0)
            def do_thing(self):
                return "done"

        obj = MyClass()
        assert obj.do_thing() == "done"

    def test_custom_logger(self):
        import logging

        custom = logging.getLogger("custom_test")

        @retry_on_exception(max_retries=1, retry_delay=0, logger=custom)
        def succeed():
            return "ok"

        assert succeed() == "ok"

    def test_allowed_exceptions(self):
        call_count = 0

        @retry_on_exception(max_retries=3, retry_delay=0, allowed_exceptions=(ValueError,))
        def specific_fail():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("retry this")
            return "ok"

        with patch("time.sleep"):
            assert specific_fail() == "ok"

    def test_non_allowed_exception_not_caught(self):
        @retry_on_exception(max_retries=3, retry_delay=0, allowed_exceptions=(ValueError,))
        def type_error():
            raise TypeError("not caught")

        with pytest.raises(TypeError):
            type_error()
