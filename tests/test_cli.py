"""
CLI module tests.

Tests for command-line interface functions.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner


class TestCLI:
    """Test CLI commands."""

    def test_cli_exists(self):
        """Test that CLI module exists and has commands."""
        from opendata.cli import cli

        assert cli is not None

    def test_cli_help(self):
        """Test CLI help command."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "Usage:" in result.output

    def test_cli_has_commands(self):
        """Test that CLI has expected commands."""
        from opendata.cli import cli

        assert "run" in cli.commands
        assert "reset-db" in cli.commands
        assert "init-db" in cli.commands
        assert "load-interfaces" in cli.commands
        assert "create-admin" in cli.commands
        assert "generate-secret" in cli.commands
        assert "export-config" in cli.commands
        assert "check-health" in cli.commands

    def test_cli_generate_secret(self):
        """Test generate-secret command."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["generate-secret"])

        assert result.exit_code == 0
        assert "SECRET_KEY=" in result.output
        assert len(result.output.strip()) > 20  # Secret should be reasonably long

    def test_cli_export_config(self):
        """Test export-config command."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["export-config"])

        assert result.exit_code == 0
        # Should output JSON
        assert "{" in result.output or "{" in result.output

    def test_cli_check_health(self):
        """Test check-health command."""
        from opendata.cli import cli

        runner = CliRunner()
        with patch(
            "opendata.core.database.check_db_connection",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = runner.invoke(cli, ["check-health"])

        assert result.exit_code == 0
        assert "Status: healthy" in result.output

    def test_cli_init_db_command(self):
        """Test init-db command."""
        from opendata.cli import cli

        runner = CliRunner()
        with (
            patch("opendata.cli.create_tables", new_callable=AsyncMock) as create_tables,
            patch("opendata.cli.init_db", new_callable=AsyncMock) as init_db,
        ):
            result = runner.invoke(cli, ["init-db"])

        assert result.exit_code == 0
        create_tables.assert_awaited_once()
        init_db.assert_awaited_once()

    def test_cli_load_interfaces_command(self):
        """Test load-interfaces command."""
        from opendata.cli import cli

        loader = MagicMock()
        loader.load_interfaces = AsyncMock(return_value=10)

        runner = CliRunner()
        with patch("opendata.cli.InterfaceLoader", return_value=loader):
            result = runner.invoke(cli, ["load-interfaces"])

        assert result.exit_code == 0
        loader.load_interfaces.assert_awaited_once()

    def test_cli_create_admin_command(self):
        """Test create-admin command."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["create-admin", "--user", "testuser"])

        # Will fail because no password is provided in non-interactive mode
        assert result.exit_code != 0
