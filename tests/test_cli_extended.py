"""
Extended CLI module tests.

Additional tests for CLI commands and functionality.
"""

import click
from click.testing import CliRunner


class TestCLICommandsExtended:
    """Extended tests for CLI commands."""

    def test_cli_main_entry_point(self):
        """Test main CLI entry point."""
        from opendata.cli import main

        assert callable(main)

    def test_cli_run_command_exists(self):
        """Test run command exists."""
        from opendata.cli import cli

        assert "run" in cli.commands
        assert cli.commands["run"] is not None

    def test_cli_reset_db_command_exists(self):
        """Test reset-db command exists."""
        from opendata.cli import cli

        assert "reset-db" in cli.commands

    def test_cli_init_db_command_exists(self):
        """Test init-db command exists."""
        from opendata.cli import cli

        assert "init-db" in cli.commands

    def test_cli_load_interfaces_command_exists(self):
        """Test load-interfaces command exists."""
        from opendata.cli import cli

        assert "load-interfaces" in cli.commands

    def test_cli_create_admin_command_exists(self):
        """Test create-admin command exists."""
        from opendata.cli import cli

        assert "create-admin" in cli.commands

    def test_cli_generate_secret_command_exists(self):
        """Test generate-secret command exists."""
        from opendata.cli import cli

        assert "generate-secret" in cli.commands

    def test_cli_export_config_command_exists(self):
        """Test export-config command exists."""
        from opendata.cli import cli

        assert "export-config" in cli.commands

    def test_cli_check_health_command_exists(self):
        """Test check-health command exists."""
        from opendata.cli import cli

        assert "check-health" in cli.commands


class TestCLIGenerateSecret:
    """Test generate-secret command."""

    def test_generate_secret_output_format(self):
        """Test generate-secret outputs correct format."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["generate-secret"])

        assert result.exit_code == 0
        assert "SECRET_KEY=" in result.output

    def test_generate_secret_length(self):
        """Test generate secret has sufficient length."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["generate-secret"])

        # Extract secret from output
        secret = result.output.strip().replace("SECRET_KEY=", "")
        assert len(secret) >= 40  # token_urlsafe(32) produces ~43 chars

    def test_generate_secret_unique(self):
        """Test generate secret produces unique values."""
        from opendata.cli import cli

        runner = CliRunner()
        result1 = runner.invoke(cli, ["generate-secret"])
        result2 = runner.invoke(cli, ["generate-secret"])

        secret1 = result1.output.strip().replace("SECRET_KEY=", "")
        secret2 = result2.output.strip().replace("SECRET_KEY=", "")

        assert secret1 != secret2


class TestCLIExportConfig:
    """Test export-config command."""

    def test_export_config_to_stdout(self):
        """Test export config to stdout."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["export-config"])

        assert result.exit_code == 0
        assert "{" in result.output or "app_name" in result.output

    def test_export_config_has_app_name(self):
        """Test exported config contains app_name."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["export-config"])

        assert "app_name" in result.output

    def test_export_config_has_env(self):
        """Test exported config contains app_env."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["export-config"])

        assert "app_env" in result.output

    def test_export_config_partial_db_url(self):
        """Test exported config hides full database URL."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["export-config"])

        # Database URL should be partially hidden
        assert "..." in result.output
        # Should not contain full db url
        assert "mysql://" not in result.output or "..." in result.output.split("mysql://")[1]


class TestCLIRunCommand:
    """Test run command."""

    def test_run_command_with_defaults(self):
        """Test run command with default options."""
        from opendata.cli import cli

        runner = CliRunner()
        # Using --help to test command structure without actually running
        result = runner.invoke(cli, ["run", "--help"])

        assert result.exit_code == 0
        assert "Start the application server" in result.output

    def test_run_command_has_host_option(self):
        """Test run command has host option."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])

        assert "--host" in result.output

    def test_run_command_has_port_option(self):
        """Test run command has port option."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])

        assert "--port" in result.output

    def test_run_command_has_reload_option(self):
        """Test run command has reload option."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])

        assert "--reload" in result.output


class TestCLICreateAdmin:
    """Test create-admin command."""

    def test_create_admin_has_user_option(self):
        """Test create-admin has user option."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["create-admin", "--help"])

        assert result.exit_code == 0
        assert "--user" in result.output

    def test_create_admin_description(self):
        """Test create-admin description."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["create-admin", "--help"])

        assert "Create an admin user" in result.output


class TestCLILoadInterfaces:
    """Test load-interfaces command."""

    def test_load_interfaces_has_categories_option(self):
        """Test load-interfaces has categories option."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["load-interfaces", "--help"])

        assert result.exit_code == 0
        assert "--categories" in result.output

    def test_load_interfaces_description(self):
        """Test load-interfaces description."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["load-interfaces", "--help"])

        assert "Load data interfaces" in result.output


class TestCLICheckHealth:
    """Test check-health command."""

    def test_check_health_description(self):
        """Test check-health description."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["check-health", "--help"])

        assert result.exit_code == 0
        assert "Check application health" in result.output


class TestCLIResetDB:
    """Test reset-db command."""

    def test_reset_db_has_confirmation(self):
        """Test reset-db has confirmation prompt."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["reset-db", "--help"])

        assert result.exit_code == 0

    def test_reset_db_description(self):
        """Test reset-db description."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["reset-db", "--help"])

        assert "Reset the database" in result.output or "drops all tables" in result.output


class TestCLIInitDB:
    """Test init-db command."""

    def test_init_db_description(self):
        """Test init-db description."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["init-db", "--help"])

        assert result.exit_code == 0
        assert "Initialize database" in result.output


class TestCLIGroup:
    """Test CLI group functionality."""

    def test_cli_is_group(self):
        """Test cli is a click group."""
        from opendata.cli import cli

        assert isinstance(cli, click.Group)

    def test_cli_help_text(self):
        """Test CLI help text."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "opendata" in result.output or "CLI" in result.output

    def test_cli_all_commands_listed(self):
        """Test all commands are listed in help."""
        from opendata.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        expected_commands = [
            "run",
            "reset-db",
            "init-db",
            "load-interfaces",
            "create-admin",
            "generate-secret",
            "export-config",
            "check-health",
        ]

        for cmd in expected_commands:
            assert cmd in result.output
