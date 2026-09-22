"""
Comprehensive tests for validators utility module.

Covers email, schedule, username, password, search, JSON parameter validation.
"""

from opendata.utils.validators import (
    sanitize_search_term,
    validate_email,
    validate_json_parameters,
    validate_password,
    validate_schedule_expression,
    validate_username,
)


class TestValidateEmail:
    """Test email validation."""

    def test_valid_email(self):
        assert validate_email("user@example.com") is True

    def test_valid_email_with_dots(self):
        assert validate_email("user.name@example.com") is True

    def test_valid_email_with_plus(self):
        assert validate_email("user+tag@example.com") is True

    def test_invalid_email_no_at(self):
        assert validate_email("userexample.com") is False

    def test_invalid_email_no_domain(self):
        assert validate_email("user@") is False

    def test_invalid_email_empty(self):
        assert validate_email("") is False


class TestValidateScheduleExpression:
    """Test schedule expression validation."""

    def test_valid_cron(self):
        assert validate_schedule_expression("0 8 * * *", "cron") is True

    def test_valid_cron_with_ranges(self):
        assert validate_schedule_expression("*/5 * * * *", "cron") is True

    def test_invalid_cron_wrong_parts(self):
        assert validate_schedule_expression("0 8 *", "cron") is False

    def test_valid_daily(self):
        assert validate_schedule_expression("08:30", "daily") is True

    def test_invalid_daily(self):
        assert validate_schedule_expression("25:00", "daily") is False

    def test_valid_weekly(self):
        assert validate_schedule_expression("MON 08:30", "weekly") is True

    def test_valid_weekly_numeric(self):
        assert validate_schedule_expression("1 08:30", "weekly") is True

    def test_invalid_weekly(self):
        assert validate_schedule_expression("INVALID 08:30", "weekly") is False

    def test_invalid_weekly_no_time(self):
        assert validate_schedule_expression("MON", "weekly") is False

    def test_valid_monthly(self):
        assert validate_schedule_expression("15 08:30", "monthly") is True

    def test_invalid_monthly(self):
        assert validate_schedule_expression("32 08:30", "monthly") is False

    def test_empty_expression(self):
        assert validate_schedule_expression("", "cron") is False

    def test_unknown_type(self):
        assert validate_schedule_expression("something", "unknown") is False

    def test_cron_invalid_part(self):
        assert validate_schedule_expression("0 8 * * abc", "cron") is False


class TestValidateUsername:
    """Test username validation."""

    def test_valid_username(self):
        valid, msg = validate_username("user123")
        assert valid is True
        assert msg is None

    def test_empty_username(self):
        valid, msg = validate_username("")
        assert valid is False

    def test_short_username(self):
        valid, msg = validate_username("ab")
        assert valid is False

    def test_long_username(self):
        valid, msg = validate_username("a" * 51)
        assert valid is False

    def test_invalid_chars(self):
        valid, msg = validate_username("user@name")
        assert valid is False

    def test_underscore_allowed(self):
        valid, msg = validate_username("user_name")
        assert valid is True


class TestValidatePassword:
    """Test password validation."""

    def test_valid_password(self):
        valid, msg = validate_password("Password123!")
        assert valid is True
        assert msg is None

    def test_empty_password(self):
        valid, msg = validate_password("")
        assert valid is False

    def test_short_password(self):
        valid, msg = validate_password("12345")
        assert valid is False

    def test_password_without_letter(self):
        valid, msg = validate_password("12345678")
        assert valid is False

    def test_password_without_digit(self):
        valid, msg = validate_password("abcdefgh")
        assert valid is False

    def test_long_password(self):
        valid, msg = validate_password("a" * 101)
        assert valid is False


class TestSanitizeSearchTerm:
    """Test search term sanitization."""

    def test_normal_term(self):
        assert sanitize_search_term("stock data") == "stock data"

    def test_sql_injection(self):
        result = sanitize_search_term("'; DROP TABLE users; --")
        assert "'" not in result
        assert ";" not in result

    def test_long_term_truncated(self):
        result = sanitize_search_term("a" * 150)
        assert len(result) == 100

    def test_strips_whitespace(self):
        result = sanitize_search_term("  hello  ")
        assert result == "hello"


class TestValidateJsonParameters:
    """Test JSON parameter validation."""

    def test_valid_params(self):
        params = {"count": 10}
        schema = {"count": {"type": int}}
        valid, msg = validate_json_parameters(params, schema)
        assert valid is True

    def test_invalid_type(self):
        params = {"count": "ten"}
        schema = {"count": {"type": int}}
        valid, msg = validate_json_parameters(params, schema)
        assert valid is False

    def test_min_constraint(self):
        params = {"count": -1}
        schema = {"count": {"type": int, "min": 0}}
        valid, msg = validate_json_parameters(params, schema)
        assert valid is False

    def test_max_constraint(self):
        params = {"count": 200}
        schema = {"count": {"type": int, "max": 100}}
        valid, msg = validate_json_parameters(params, schema)
        assert valid is False

    def test_extra_params_allowed(self):
        params = {"extra": "value"}
        schema = {}
        valid, msg = validate_json_parameters(params, schema)
        assert valid is True

    def test_not_dict(self):
        valid, msg = validate_json_parameters("not a dict", {})
        assert valid is False
