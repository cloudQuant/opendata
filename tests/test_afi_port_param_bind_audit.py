"""Tests for the static akshare task parameter binder audit.

The checker must classify from source alone: these fixtures are written to a tmp tree
and never imported, and no test touches the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from opendata.afi_port.services.param_bind_audit import (
    CLASS_ACCEPTS_VAR_KWARGS,
    CLASS_BINDS,
    CLASS_MISSING_REQUIRED_PARAM,
    CLASS_SCRIPT_FILE_ABSENT,
    CLASS_UNEXPECTED_PARAM,
    CLASS_UNRESOLVABLE,
    PythonTreeSource,
    TaskRecord,
    audit_task,
)

if TYPE_CHECKING:
    from pathlib import Path

SCRIPT_PACKAGE = "app.data_fetch.scripts"

# Fixed fixture sources. Written verbatim to disk so the line numbers asserted below
# are stable: keep them in sync if a fixture is edited.
_VAR_KWARGS_SCRIPT = '''"""One generated collection script shape."""


class CollectorBase:
    def fetch_data(self, **kwargs):
        """Accept anything and pass it to the upstream call."""
        return kwargs
'''

_BINDS_SCRIPT = '''"""Module-level ``main`` shape."""


def main(symbol=None, date=None):
    """Bind both keys."""
    return symbol, date
'''

_UNEXPECTED_SCRIPT = '''"""Legacy class shape reproducing the live ``source`` failure."""

DEFAULT_DB_CONFIG = {"host": "unused"}


class EtfFundHistEmLike:
    def __init__(self, db_config=DEFAULT_DB_CONFIG, logger=None):
        self.db_config = db_config

    def run(self, fund_codes=None, start_date=None, end_date=None, max_codes=20):
        """Only these four keys bind."""
        return fund_codes, start_date, end_date, max_codes
'''

_MISSING_REQUIRED_SCRIPT = '''"""A required keyword that the task never supplies."""


def fetch_data(symbol, date=None):
    """``symbol`` is mandatory."""
    return symbol, date
'''

_ALIAS_SCRIPT = '''"""The entrypoint name is an import, so its signature is outside this file."""

from akshare import stock_zh_a_hist as fetch_data
'''

_BASE_MODULE = '''"""A local base class the checker can read without importing."""


class ProviderBase:
    def fetch_data(self, *, symbol, region="cn"):
        """Keyword-only required ``symbol``."""
        return symbol, region
'''

_INHERITING_SCRIPT = '''"""Class whose entrypoint signature lives in the base module."""

from app.data_fetch.providers.base import ProviderBase


class Inherited(ProviderBase):
    def __init__(self):
        self.name = "inherited"
'''


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A tmp package root holding only the fixtures above."""
    _write(tmp_path, "app/data_fetch/providers/base.py", _BASE_MODULE)
    _write(tmp_path, "app/data_fetch/scripts/var.py", _VAR_KWARGS_SCRIPT)
    _write(tmp_path, "app/data_fetch/scripts/binds.py", _BINDS_SCRIPT)
    _write(tmp_path, "app/data_fetch/scripts/unexpected.py", _UNEXPECTED_SCRIPT)
    _write(tmp_path, "app/data_fetch/scripts/required.py", _MISSING_REQUIRED_SCRIPT)
    _write(tmp_path, "app/data_fetch/scripts/alias.py", _ALIAS_SCRIPT)
    _write(tmp_path, "app/data_fetch/scripts/inheriting.py", _INHERITING_SCRIPT)
    return tmp_path


def _task(
    script_id: str,
    function_name: str,
    parameters: object,
    *,
    module_tail: str | None = None,
) -> TaskRecord:
    module_name = f"{SCRIPT_PACKAGE}.{module_tail or script_id}"
    return TaskRecord(
        task_id=1,
        script_id=script_id,
        module_path=module_name,
        function_name=function_name,
        source="akshare",
        is_custom=False,
        parameters=parameters,
    )


class TestAuditTaskClassifications:
    def test_task_parameters_that_match_the_signature_bind(self, tree: Path) -> None:
        result = audit_task(
            _task("binds", "main", {"symbol": "600519", "date": "20260101"}),
            PythonTreeSource(tree),
            script_file=tree / "app/data_fetch/scripts/binds.py",
        )

        assert result.classification == CLASS_BINDS
        assert result.family == CLASS_BINDS
        assert result.signature_callable == "main"
        assert result.signature_line == 4

    def test_unregistered_task_key_is_reported_as_unexpected_param(self, tree: Path) -> None:
        result = audit_task(
            _task(
                "unexpected",
                "run",
                {"source": "auto", "fund_codes": ["510300"], "start_date": "19000101"},
            ),
            PythonTreeSource(tree),
            script_file=tree / "app/data_fetch/scripts/unexpected.py",
        )

        assert result.classification == "unexpected_param:source"
        assert result.family == CLASS_UNEXPECTED_PARAM
        assert result.detail_key == "source"
        assert result.offending_keys == ("source",)
        assert result.signature_callable == "EtfFundHistEmLike.run"
        assert result.signature_line == 10
        assert result.signature_file.endswith("app/data_fetch/scripts/unexpected.py")

    def test_json_string_parameters_are_normalized(self, tree: Path) -> None:
        """A ``str`` column value is decoded exactly once, like the driver does."""
        result = audit_task(
            _task("unexpected", "run", '{"source": "auto", "max_codes": 1}'),
            PythonTreeSource(tree),
            script_file=tree / "app/data_fetch/scripts/unexpected.py",
        )

        assert result.classification == "unexpected_param:source"
        assert result.task_param_keys == ("max_codes", "source")

    def test_double_encoded_json_string_is_decoded_twice_like_the_driver(self, tree: Path) -> None:
        """The driver's loop allows two decode passes; so does the audit."""
        result = audit_task(
            _task("unexpected", "run", '"{\\"source\\": \\"auto\\"}"'),
            PythonTreeSource(tree),
            script_file=tree / "app/data_fetch/scripts/unexpected.py",
        )

        assert result.classification == "unexpected_param:source"
        assert result.task_param_keys == ("source",)

    def test_var_kwargs_entrypoint_is_a_class_of_its_own(self, tree: Path) -> None:
        result = audit_task(
            _task("var", "fetch_data", {"symbol": "204001", "source": "auto"}),
            PythonTreeSource(tree),
            script_file=tree / "app/data_fetch/scripts/var.py",
        )

        assert result.classification == CLASS_ACCEPTS_VAR_KWARGS
        assert result.family == CLASS_ACCEPTS_VAR_KWARGS
        assert result.detail_key == ""
        assert result.signature_callable == "CollectorBase.fetch_data"
        assert result.signature_line == 5

    def test_missing_required_parameter_outranks_var_kwargs(self, tree: Path) -> None:
        result = audit_task(
            _task("required", "fetch_data", {"date": "20260101"}),
            PythonTreeSource(tree),
            script_file=tree / "app/data_fetch/scripts/required.py",
        )

        assert result.classification == "missing_required_param:symbol"
        assert result.family == CLASS_MISSING_REQUIRED_PARAM
        assert result.detail_key == "symbol"

    def test_registered_script_without_a_file_is_script_file_absent(self, tree: Path) -> None:
        result = audit_task(
            _task("forex_hist_sina", "fetch_data", {"symbol": "USDCNH"}),
            PythonTreeSource(tree),
            script_file=tree / "app/data_fetch/scripts/forex_hist_sina.py",
        )

        assert result.classification == CLASS_SCRIPT_FILE_ABSENT
        assert result.family == CLASS_SCRIPT_FILE_ABSENT
        assert result.signature_file == ""

    def test_entrypoint_imported_from_another_package_is_unresolvable(self, tree: Path) -> None:
        result = audit_task(
            _task("alias", "fetch_data", {"symbol": "600519"}),
            PythonTreeSource(tree),
            script_file=tree / "app/data_fetch/scripts/alias.py",
        )

        assert result.family == CLASS_UNRESOLVABLE
        assert result.classification == "unresolvable"
        assert result.reason == "entrypoint_is_imported_or_aliased"

    def test_signature_inherited_from_a_local_base_is_followed(self, tree: Path) -> None:
        source = PythonTreeSource(tree)
        script_file = tree / "app/data_fetch/scripts/inheriting.py"
        unexpected = audit_task(
            _task("inheriting", "fetch_data", {"symbol": "AU", "zone": "shfe"}),
            source,
            script_file=script_file,
        )
        missing = audit_task(
            _task("inheriting", "fetch_data", {"region": "cn"}),
            source,
            script_file=script_file,
        )

        assert unexpected.classification == "unexpected_param:zone"
        assert unexpected.signature_callable == "Inherited.fetch_data"
        # The signature is read where it is defined, which is the base file.
        assert unexpected.signature_file.endswith("app/data_fetch/providers/base.py")
        assert unexpected.signature_line == 5
        assert missing.classification == "missing_required_param:symbol"


class TestDriverParameterMerge:
    def test_driver_injected_default_key_is_audited_too(self, tree: Path) -> None:
        """``_apply_safe_default_parameters`` injects keys the entrypoint must accept."""
        result = audit_task(
            _task("binds", "main", {"symbol": "600519"}),
            PythonTreeSource(tree),
            script_file=tree / "app/data_fetch/scripts/binds.py",
            default_keys={"binds": ("_call_timeout",)},
        )

        assert result.classification == "unexpected_param:_call_timeout"
        assert result.injected_default_keys == ("_call_timeout",)
        assert result.runtime_param_keys == ("_call_timeout", "symbol")

    def test_injected_key_can_be_overridden_by_the_task(self, tree: Path) -> None:
        result = audit_task(
            _task("binds", "main", {"symbol": "600519", "date": "20260101"}),
            PythonTreeSource(tree),
            script_file=tree / "app/data_fetch/scripts/binds.py",
            default_keys={"binds": ("date",)},
        )

        assert result.classification == CLASS_BINDS
        assert result.runtime_param_keys == ("date", "symbol")


class TestNonRuntimeObjectParameters:
    def test_parameters_that_are_not_a_json_object_are_unresolvable(self, tree: Path) -> None:
        result = audit_task(
            _task("binds", "main", "{symbol: 600519}"),
            PythonTreeSource(tree),
            script_file=tree / "app/data_fetch/scripts/binds.py",
        )

        assert result.family == CLASS_UNRESOLVABLE
        assert result.reason.startswith("parameters_not_json_object")
