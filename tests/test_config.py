"""
Configuration module tests.

Tests for application settings and configuration.
"""


class TestSettings:
    """Test application settings."""

    def test_settings_attributes(self):
        """Test that settings has required attributes."""
        from opendata.core.config import settings

        assert hasattr(settings, "app_name")
        assert hasattr(settings, "app_version")
        assert hasattr(settings, "app_env")
        assert hasattr(settings, "secret_key")
        assert hasattr(settings, "database_url")

    def test_settings_values(self):
        """Test settings have expected values."""
        from opendata.core.config import settings

        assert isinstance(settings.app_version, str)
        assert settings.app_env in ["development", "testing", "production"]
        assert isinstance(settings.secret_key, str)
        assert len(settings.secret_key) >= 10

    def test_app_name_is_the_name_the_service_shows_the_operator(self):
        """``app_name`` 的行为面：它写进 FastAPI 的对外标题，标题按字面量钉住。

        断言 ``settings.app_name == "opendata"`` 只是把 config 的定义抄一遍（§5.1 第 2
        类）；这里断言设置真的接到了服务自描述上 —— 改设置这里红，接线断掉也红。
        """
        from opendata.main import app

        assert app.openapi()["info"]["title"] == "opendata API v1"

    def test_cors_origins(self):
        """Test CORS origins configuration."""
        from opendata.core.config import settings

        assert hasattr(settings, "cors_origins")
        assert isinstance(settings.cors_origins, list)

    def test_jwt_settings(self):
        """Test JWT configuration."""
        from opendata.core.config import settings

        assert hasattr(settings, "access_token_expire_minutes")
        assert hasattr(settings, "refresh_token_expire_days")
        assert settings.access_token_expire_minutes > 0
        assert settings.refresh_token_expire_days > 0

    def test_database_settings(self):
        """Test database configuration."""
        from opendata.core.config import settings

        assert hasattr(settings, "database_url")
        assert "mysql" in settings.database_url or "sqlite" in settings.database_url

    def test_testing_mode(self):
        """Test that testing mode is properly set."""
        from opendata.core.config import settings

        # In tests, environment should be testing or development
        assert settings.app_env in ["development", "testing"]

    def test_algorithm_is_the_one_the_signer_uses(self):
        """``algorithm`` 的行为面：签出去的 token 头部就写着这个算法。

        抄一遍 config 里的 "HS256" 不算测试；读 token 自己的 header 才证明设置真的
        进了签名调用 —— 签名代码写死别的算法、或设置改了没生效，这里都会红。
        """
        import jwt

        from opendata.core.config import settings
        from opendata.core.security import create_access_token, verify_token

        token = create_access_token({"sub": "config-algorithm"})

        assert jwt.get_unverified_header(token)["alg"] == settings.algorithm
        assert verify_token(token)["sub"] == "config-algorithm"

    def test_host_and_port(self):
        """Test server host and port settings."""
        from opendata.core.config import settings

        assert hasattr(settings, "host")
        assert hasattr(settings, "port")
        assert settings.host in ["0.0.0.0", "127.0.0.1", "localhost"]
        assert isinstance(settings.port, int)
        assert 1024 < settings.port < 65536


class TestSecretGeneration:
    """Test secret key generation utilities."""

    def test_generate_random_string(self):
        """Test random string generation for secrets."""
        import secrets

        secret = secrets.token_urlsafe(32)

        assert isinstance(secret, str)
        assert len(secret) >= 40  # Base64 encoded 32 bytes should be at least 40 chars

    def test_secrets_are_unique(self):
        """Test that generated secrets are unique."""
        import secrets

        secret1 = secrets.token_urlsafe(32)
        secret2 = secrets.token_urlsafe(32)

        assert secret1 != secret2
