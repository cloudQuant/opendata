"""
Direct tests for CLI commands to maximize coverage.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from opendata.cli import cli


class TestGenerateSecret:
    def test_generate(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["generate-secret"])
        assert result.exit_code == 0
        assert "SECRET_KEY=" in result.output


class TestExportConfig:
    def test_stdout(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["export-config"])
        assert result.exit_code == 0
        assert "app_name" in result.output

    def test_file(self, tmp_path):
        runner = CliRunner()
        outfile = str(tmp_path / "config.json")
        result = runner.invoke(cli, ["export-config", "--output", outfile])
        assert result.exit_code == 0
        import json
        from pathlib import Path

        with Path(outfile).open() as f:
            data = json.load(f)
        assert "app_name" in data


class TestCheckHealth:
    def test_healthy(self):
        runner = CliRunner()
        with patch(
            "opendata.core.database.check_db_connection", new_callable=AsyncMock, return_value=True
        ):
            result = runner.invoke(cli, ["check-health"])
        assert "healthy" in result.output

    def test_unhealthy(self):
        runner = CliRunner()
        with patch(
            "opendata.core.database.check_db_connection", new_callable=AsyncMock, return_value=False
        ):
            result = runner.invoke(cli, ["check-health"])
        assert result.exit_code == 1
        assert "unhealthy" in result.output


class TestInitDbCmd:
    def test_init(self):
        runner = CliRunner()
        with (
            patch("opendata.cli.create_tables", new_callable=AsyncMock),
            patch("opendata.cli.init_db", new_callable=AsyncMock),
        ):
            result = runner.invoke(cli, ["init-db"])
        assert result.exit_code == 0


class TestLoadInterfaces:
    def test_load(self):
        runner = CliRunner()
        mock_loader = MagicMock()
        mock_loader.load_interfaces = AsyncMock(return_value=10)
        with patch("opendata.cli.InterfaceLoader", return_value=mock_loader):
            result = runner.invoke(cli, ["load-interfaces"])
        assert result.exit_code == 0
