"""
Comprehensive tests for AkshareProvider.

Covers initialization, method existence, and mocked execution paths.
"""

from unittest.mock import patch

import pandas as pd
import pytest


class TestAkshareProviderInit:
    """Test AkshareProvider initialization."""

    def test_init(self):
        """Test provider initialization."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        provider = AkshareProvider()
        assert provider is not None
        assert provider.batch_size == 1000
        assert provider.max_retries == 3

    def test_has_fetch_ak_data(self):
        """Test provider has fetch_ak_data method."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        provider = AkshareProvider()
        assert hasattr(provider, "fetch_ak_data")

    def test_has_connect_db(self):
        """Test provider has connect_db method."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        provider = AkshareProvider()
        assert hasattr(provider, "connect_db")

    def test_has_disconnect_db(self):
        """Test provider has disconnect_db method."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        provider = AkshareProvider()
        assert hasattr(provider, "disconnect_db")

    def test_connect_db_non_mysql(self):
        """Test connect_db returns False for non-MySQL URL."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        provider = AkshareProvider(db_url="sqlite:///test.db")
        result = provider.connect_db()
        assert result is False

    def test_disconnect_db_no_connection(self):
        """Test disconnect_db when no connection exists."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        provider = AkshareProvider()
        provider.disconnect_db()  # Should not raise


class TestAkshareProviderFetchData:
    """Test AkshareProvider fetch_ak_data method."""

    def test_fetch_ak_data_success(self):
        """Test fetching data with mocked akshare function."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        provider = AkshareProvider()

        mock_df = pd.DataFrame({"col1": [1, 2], "col2": ["a", "b"]})

        with (
            patch.object(pd.DataFrame, "__init__", return_value=None),
            patch("akshare.stock_zh_a_spot_em", return_value=mock_df, create=True),
        ):
            result = provider.fetch_ak_data("stock_zh_a_spot_em")
            assert isinstance(result, pd.DataFrame)

    def test_fetch_ak_data_nonexistent_function(self):
        """Test fetching data with non-existent function."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        provider = AkshareProvider()

        with pytest.raises(AttributeError):
            provider.fetch_ak_data("nonexistent_function_xyz")


class TestFuncThread:
    """Test FuncThread helper class."""

    def test_func_thread_success(self):
        """Test FuncThread with successful function."""
        from opendata.data_fetch.providers.akshare_provider import FuncThread

        def add(a, b):
            return a + b

        thread = FuncThread(add, 1, 2)
        thread.start()
        status, result = thread.get_result(timeout=5)
        assert status == "success"
        assert result == 3

    def test_func_thread_error(self):
        """Test FuncThread with failing function."""
        from opendata.data_fetch.providers.akshare_provider import FuncThread

        def fail():
            raise ValueError("test error")

        thread = FuncThread(fail)
        thread.start()
        status, result = thread.get_result(timeout=5)
        assert status == "error"

    def test_func_thread_timeout(self):
        """Test FuncThread with timeout."""
        import time

        from opendata.data_fetch.providers.akshare_provider import FuncThread

        def slow():
            time.sleep(10)
            return "done"

        thread = FuncThread(slow)
        thread.start()
        status, result = thread.get_result(timeout=0.1)
        assert status == "timeout"
        assert result is None
