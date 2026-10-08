"""The logger-argument guard has to be able to fail, and so has the tree (C39).

``make loguru-check`` runs ``scripts/quality/loguru_render_check.py`` inside
``make gate``. Wiring a guard in only proves that it ran: a classifier whose
binding logic had rotted - one that resolved every receiver to ``stdlib``, or
that never looked at ``self.logger`` at all - would also let the gate pass, and
the defect it exists to catch is precisely a call that reads as healthy while
saying nothing. So the rules are asserted here on synthetic modules, the shipped
tree is asserted against them, and both failure directions of both renderers are
measured rather than quoted.

What the guard guards: ``loguru`` renders with ``str.format`` and ``logging``
with ``%-formatting``. Written in the other family's idiom the arguments never
reach the log - silently on the loguru side, loudly on the ``logging`` side - and
every one of the thirteen calls C39 fixed sat on an exception path, where the
argument that went missing was the reason something failed.
"""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from loguru import logger
from starlette.testclient import TestClient

from opendata.data_fetch.providers.akshare_provider import AkshareProvider
from opendata.middleware.request_logging import RequestLoggingMiddleware
from scripts.quality import loguru_render_check as guard

#: Imports that settle a bare ``logger`` name for the classifier.
LOGURU_MODULE = "from loguru import logger\n"
STDLIB_MODULE = "import logging\nlogger = logging.getLogger(__name__)\n"

#: The provider call C39 fixed: the one receiver whose family the definition
#: site cannot settle, so the message had to be interpolated by the caller.
ROW_COUNT_MESSAGE = "Could not get row count for table ods_stock_daily: connection reset by peer"


def judged(source: str) -> list[guard.Site]:
    """Return the calls in ``source`` the classifier resolved a family for."""
    return [site for site in guard.sites_of(source, "synthetic.py") if site.why_unjudged is None]


def dropped(source: str) -> list[guard.Site]:
    """Return the calls in ``source`` whose arguments never reach the log."""
    return [site for site in judged(source) if guard.verdict_of(site) is not None]


def unjudged(source: str) -> list[guard.Site]:
    """Return the calls in ``source`` the guard reports without judging them."""
    return [site for site in guard.sites_of(source, "synthetic.py") if site.why_unjudged]


class TestBindingAwareJudgement:
    """The same template is a defect on one family and idiomatic on the other."""

    def test_loguru_percent_template_loses_its_arguments(self) -> None:
        source = LOGURU_MODULE + 'logger.warning("freshness unavailable for %s: %s", t, exc)\n'
        assert [site.template for site in dropped(source)] == ["freshness unavailable for %s: %s"]

    def test_loguru_brace_template_is_the_idiom(self) -> None:
        source = LOGURU_MODULE + 'logger.warning("freshness unavailable for {}: {}", t, exc)\n'
        assert dropped(source) == []

    def test_stdlib_percent_template_is_not_a_finding(self) -> None:
        """The discriminator: a bare grep for ``%s`` would flag this correct call."""
        source = STDLIB_MODULE + 'logger.warning("freshness unavailable for %s: %s", t, exc)\n'
        assert judged(source)[0].family == guard.STDLIB
        assert dropped(source) == []

    def test_stdlib_brace_template_loses_its_arguments(self) -> None:
        source = STDLIB_MODULE + 'logger.warning("freshness unavailable for {}: {}", t, exc)\n'
        assert [site.template for site in dropped(source)] == ["freshness unavailable for {}: {}"]

    def test_stdlib_control_keywords_do_not_count_as_format_arguments(self) -> None:
        source = (
            STDLIB_MODULE + 'logger.warning("cache safety miss", extra={"cache_key": key}, '
            "exc_info=True, stack_info=False, stacklevel=2)\n"
        )
        assert guard.sites_of(source, "synthetic.py") == []
        assert dropped(source) == []

    def test_stdlib_control_keywords_do_not_hide_a_real_format_error(self) -> None:
        source = (
            STDLIB_MODULE + 'logger.warning("cache safety miss", cache_key, '
            'extra={"cache_key": cache_key}, exc_info=True, stack_info=False, stacklevel=2)\n'
        )
        assert [site.template for site in dropped(source)] == ["cache safety miss"]

    def test_stdlib_control_keywords_allow_a_real_format_argument(self) -> None:
        source = (
            STDLIB_MODULE + 'logger.warning("cache miss for %s", table, extra={"table": table}, '
            "exc_info=True, stack_info=False, stacklevel=2)\n"
        )
        assert judged(source)[0].family == guard.STDLIB
        assert dropped(source) == []

    def test_arguments_with_no_placeholder_are_lost_on_both(self) -> None:
        for module in (LOGURU_MODULE, STDLIB_MODULE):
            source = module + 'logger.info("query finished", domain, exc)\n'
            assert [site.template for site in dropped(source)] == ["query finished"]

    def test_loguru_named_keyword_arguments_remain_format_arguments(self) -> None:
        source = (
            LOGURU_MODULE
            + 'logger.warning("cache miss for {table}", table=table)\n'
            + 'logger.warning("cache safety miss", extra={"cache_key": key})\n'
        )
        rows = judged(source)
        assert [row.template for row in rows] == [
            "cache miss for {table}",
            "cache safety miss",
        ]
        assert [site.template for site in dropped(source)] == ["cache safety miss"]

    def test_log_method_takes_the_level_before_the_template(self) -> None:
        source = LOGURU_MODULE + 'logger.log("warning", "row count failed: %s", exc)\n'
        assert [site.level for site in dropped(source)] == ["log"]

    def test_a_template_built_at_runtime_is_reported_not_judged(self) -> None:
        """The guard's own residual face, so it cannot be quietly widened."""
        source = LOGURU_MODULE + "def go(template, exc):\n    logger.debug(template, exc)\n"
        rows = unjudged(source)
        assert [row.why_unjudged for row in rows] == ["template built at runtime"]
        assert dropped(source) == []


class TestAttributeFormFace:
    """The receiver shape the C38 census could not see at all."""

    INJECTED = (
        "import logging\n"
        "from loguru import logger as _default_logger\n"
        "\n"
        "class Provider:\n"
        "    def __init__(self, logger: logging.Logger | None = None) -> None:\n"
        "        self.logger = logger or _default_logger\n"
        "\n"
        "    def count(self, table: str) -> None:\n"
        "        pass\n"
    )

    def test_injected_logger_defaulting_to_loguru_is_neither_family(self) -> None:
        source = self.INJECTED.replace(
            "        pass\n",
            '        self.logger.debug("Could not get row count for table %s: %s", t, e)\n',
        )
        assert [site.family for site in dropped(source)] == [guard.AMBIGUOUS]

    def test_interpolated_message_passes_on_an_ambiguous_receiver(self) -> None:
        """What C39 changed the provider call to: readable on both renderers."""
        source = self.INJECTED.replace(
            "        pass\n",
            '        self.logger.debug(f"Could not get row count for table {table}: {e}")\n',
        )
        assert dropped(source) == []
        assert unjudged(source) == []

    def test_finished_message_that_also_passes_arguments_is_disclosed(self) -> None:
        """The face this file cannot read: an interpolated message that also passes args.

        Nothing in the tree writes this form today, and on a stdlib receiver it would
        render nothing at all, so the row is disclosed as unjudged rather than being
        waved through as safe.
        """
        source = LOGURU_MODULE + 'logger.debug(f"freshness unavailable for {table}", exc)\n'
        assert dropped(source) == []
        assert [row.why_unjudged for row in unjudged(source)] == ["template built at runtime"]

    def test_class_attribute_bound_to_stdlib_is_judged_by_that_family(self) -> None:
        source = (
            "import logging\n"
            "\n"
            "class Runner:\n"
            "    def __init__(self) -> None:\n"
            "        self.logger = logging.getLogger(__name__)\n"
            "\n"
            "    def run(self) -> None:\n"
            '        self.logger.error("tick failed: %s", exc)\n'
        )
        assert judged(source)[0].family == guard.STDLIB
        assert dropped(source) == []

    def test_receiver_this_file_cannot_resolve_is_reported_not_judged(self) -> None:
        source = LOGURU_MODULE + 'other.logger.debug("tick failed: %s", exc)\n'
        assert [row.family for row in unjudged(source)] == [guard.UNRESOLVED]
        assert dropped(source) == []

    def test_control_keyword_is_not_exempt_for_ambiguous_or_unresolved_receivers(self) -> None:
        ambiguous_source = self.INJECTED.replace(
            "        pass\n",
            '        self.logger.warning("cache safety miss", extra={"cache_key": key})\n',
        )
        unresolved_source = (
            LOGURU_MODULE + 'other.logger.warning("cache safety miss", extra={"cache_key": key})\n'
        )
        assert [site.family for site in dropped(ambiguous_source)] == [guard.AMBIGUOUS]
        assert [row.family for row in unjudged(unresolved_source)] == [guard.UNRESOLVED]

    def test_function_local_binding_shadows_the_module_import(self) -> None:
        """A local ``getLogger`` is the classic stdlib idiom inside a loguru module."""
        source = (
            LOGURU_MODULE + "\n"
            "def handler() -> None:\n"
            "    import logging\n"
            "    logger = logging.getLogger(__name__)\n"
            '    logger.error("handler failed: %s", exc)\n'
        )
        assert judged(source)[0].family == guard.STDLIB
        assert dropped(source) == []


class TestRenderersMeasuredNotQuoted:
    """Both failure directions, on the real renderers, without the probe's sinks."""

    @staticmethod
    def render_on_loguru(template: str) -> str:
        """Return the single line a loguru handler receives for ``template``."""
        captured: list[str] = []
        handler_id = logger.add(
            lambda message: captured.append(str(message.record["message"])),
            level="DEBUG",
            format="{message}",
        )
        try:
            logger.error(template, 7, RuntimeError("boom"))
        finally:
            logger.remove(handler_id)
        assert len(captured) == 1, "the handler must receive exactly one record"
        return captured[0]

    def test_loguru_drops_percent_arguments_in_silence(self) -> None:
        """The defect, reproduced: the record arrives, and it says nothing."""
        line = self.render_on_loguru("Could not get row count for table %s: %s")
        assert line == "Could not get row count for table %s: %s"
        assert "boom" not in line

    def test_loguru_substitutes_brace_arguments(self) -> None:
        line = self.render_on_loguru("Could not get row count for table {}: {}")
        assert line == "Could not get row count for table 7: boom"

    @staticmethod
    def render_on_logging(template: str) -> tuple[str, str]:
        """Return ``(at the handler, on stderr)`` for ``template`` plus two arguments."""
        stream = io.StringIO()
        broken = io.StringIO()
        # A private logger keeps pytest's caplog handler out of the failing
        # formatting path while the real StreamHandler still reports TypeError.
        box = logging.Logger("c39-stdlib-probe")
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        box.addHandler(handler)
        box.setLevel(logging.DEBUG)
        box.propagate = False
        real_stderr, real_raise = sys.stderr, logging.raiseExceptions
        logging.raiseExceptions = True
        try:
            sys.stderr = broken
            box.error(template, 7, RuntimeError("boom"))
        finally:
            sys.stderr = real_stderr
            logging.raiseExceptions = real_raise
            box.removeHandler(handler)
        return stream.getvalue(), broken.getvalue()

    def test_logging_substitutes_percent_arguments(self) -> None:
        """Why the thirteen fixes could not be a blanket ban on ``%s``."""
        reached, reported = self.render_on_logging("Could not get row count for table %s: %s")
        assert reached == "Could not get row count for table 7: boom\n"
        assert reported == ""

    def test_logging_extra_field_and_message_reach_the_handler(self) -> None:
        class RecordHandler(logging.Handler):
            """Capture the actual record before a formatter changes it."""

            def __init__(self) -> None:
                super().__init__()
                self.records: list[logging.LogRecord] = []

            def emit(self, record: logging.LogRecord) -> None:
                self.records.append(record)

        box = logging.getLogger("loguru-render-check-extra")
        handler = RecordHandler()
        previous_level, previous_propagate = box.level, box.propagate
        box.addHandler(handler)
        box.setLevel(logging.DEBUG)
        box.propagate = False
        try:
            box.warning("cache safety miss", extra={"cache_key": "daily:600519"})
        finally:
            box.removeHandler(handler)
            box.setLevel(previous_level)
            box.propagate = previous_propagate
            handler.close()

        assert len(handler.records) == 1
        record = handler.records[0]
        assert record.getMessage() == "cache safety miss"
        assert record.__dict__["cache_key"] == "daily:600519"

    def test_logging_reports_a_brace_template_and_renders_nothing(self) -> None:
        """The mirror image: loud on stderr, and the message never reaches the sink."""
        reached, reported = self.render_on_logging("Could not get row count for table {}: {}")
        assert reached == "", "logging must not emit the template either"
        assert "TypeError" in reported, "and it must fail loudly, not silently"


class TestShippedTree:
    """The face the gate judges, and the calls C39 changed."""

    def test_no_call_in_the_tree_loses_its_arguments(self) -> None:
        sweep = guard.scan()
        listing = [f"{site.where} {site.receiver}.{site.level}" for site in sweep.findings]
        assert listing == []

    def test_every_declared_root_sweeps_files(self) -> None:
        """A root that sweeps nothing shrinks the face in silence (C35, C36)."""
        per_root = guard.scan().per_root
        assert all(files > 0 for files in per_root.values()), per_root
        assert sum(per_root.values()) > 600, per_root

    def test_the_sweep_matches_an_independent_file_count(self) -> None:
        """``find`` counts what the walker saw, so an exclusion cannot hide files."""
        for root, swept in guard.scan().per_root.items():
            listed = subprocess.run(  # noqa: S603  # nosec B603
                ["find", root, "-name", "*.py"],  # noqa: S607  # nosec B607
                capture_output=True,
                text=True,
                check=True,
                cwd=guard.REPO_ROOT,
            ).stdout.splitlines()
            assert swept == len(listed), f"{root}: swept {swept}, find listed {len(listed)}"

    def test_the_unjudged_face_is_only_the_guards_own_instrumentation(self) -> None:
        """The residual face stays where a reader can see it, not everywhere."""
        sweep = guard.scan()
        assert sweep.unjudged, "the probe's runtime templates must stay disclosed"
        assert {site.file for site in sweep.unjudged} <= {
            "scripts/quality/loguru_render_check.py",
            "tests/test_loguru_render_check.py",
        }

    def test_the_middleware_500_line_names_the_request_and_the_reason(self) -> None:
        """``%.1f`` became ``{:.1f}``: a dropped arg here hid every 500."""
        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)

        @app.get("/boom")
        async def boom() -> None:
            raise RuntimeError("warehouse table missing")

        lines: list[str] = []
        handler_id = logger.add(
            lambda message: lines.append(str(message.record["message"])),
            level="DEBUG",
            format="{message}",
        )
        try:
            response = TestClient(app, raise_server_exceptions=False).get("/boom")
        finally:
            logger.remove(handler_id)
        assert response.status_code == 500
        error_lines = [line for line in lines if " 500 (" in line]
        assert len(error_lines) == 1, f"expected one 500 line, got {lines!r}"
        line = error_lines[0]
        assert line.startswith("GET /boom 500 (")
        assert "exception=warehouse table missing" in line
        duration = line[line.index("(") + 1 : line.index("ms)")]
        assert duration.replace(".", "", 1).isdigit(), f"duration did not render: {line!r}"
        assert "%s" not in line and "{}" not in line

    @pytest.mark.asyncio
    async def test_the_provider_line_is_readable_on_either_family(self) -> None:
        """The one call whose logger the definition site cannot settle."""
        captured: list[str] = []
        handler_id = logger.add(
            lambda message: captured.append(str(message.record["message"])),
            level="DEBUG",
            format="{message}",
        )
        stdlib_box = io.StringIO()
        stdlib = logging.getLogger("c39-provider")
        stdlib_handler = logging.StreamHandler(stdlib_box)
        stdlib_handler.setFormatter(logging.Formatter("%(message)s"))
        stdlib.addHandler(stdlib_handler)
        stdlib.setLevel(logging.DEBUG)
        stdlib.propagate = False
        session = AsyncMock()
        session.execute.side_effect = RuntimeError("connection reset by peer")
        try:
            provider = AkshareProvider(db_url="mysql://u:p@h/db")
            assert await provider.get_table_row_count_async("ods_stock_daily", session) is None
            injected = AkshareProvider(db_url="mysql://u:p@h/db", logger=stdlib)
            assert await injected.get_table_row_count_async("ods_stock_daily", session) is None
        finally:
            logger.remove(handler_id)
            stdlib.removeHandler(stdlib_handler)
        assert captured == [ROW_COUNT_MESSAGE], "the loguru default must render it"
        assert stdlib_box.getvalue().strip() == ROW_COUNT_MESSAGE, "and an injected stdlib one"


class TestEnforcementWiring:
    """A guard that cannot fail is a reading, not a gate."""

    FINDING = guard.Site(
        "scripts/quality/loguru_render_check.py", 1, "logger", "debug", guard.LOGURU, "x: %s"
    )

    @staticmethod
    def stub_scan(monkeypatch: pytest.MonkeyPatch, sweep: guard.Sweep) -> None:
        """Point the sweep at a synthetic result, keeping the roots honest."""
        monkeypatch.setattr(guard, "scan", lambda *args, **kwargs: sweep)

    def test_gate_mode_fails_on_a_finding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sweep = guard.Sweep([self.FINDING], [], 1, dict.fromkeys(guard.CHECK_ROOTS, 1))
        self.stub_scan(monkeypatch, sweep)
        assert guard.run([]) == 1

    def test_report_mode_reports_without_failing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sweep = guard.Sweep([self.FINDING], [], 1, dict.fromkeys(guard.CHECK_ROOTS, 1))
        self.stub_scan(monkeypatch, sweep)
        assert guard.run(["--report"]) == 0

    def test_an_empty_root_fails_even_with_nothing_to_judge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        per_root = {root: (0 if root == "opendata" else 1) for root in guard.CHECK_ROOTS}
        self.stub_scan(monkeypatch, guard.Sweep([], [], 0, per_root))
        assert guard.run([]) == 1

    def test_a_clean_sweep_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.stub_scan(monkeypatch, guard.Sweep([], [], 0, dict.fromkeys(guard.CHECK_ROOTS, 1)))
        assert guard.run([]) == 0
