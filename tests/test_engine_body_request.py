"""Offline proof for the request-body kernel: every rule shown on both sides.

Each guard in :mod:`opendata.data.providers._engine.request_body` gets a
positive arm (the declaration builds, serializes or advances) and a refusal arm
(the same call with the violation, refused by stable code before any byte
exists). No case here performs I/O: the kernel is pure data and these tests
never patch a transport because there is nothing to patch.

The acceptance-critical arms are the security ones: an undeclared or wrongly-
typed parameter must not reach the serialized body, a declared string must not
be interpreted, and a key or paging pointer containing ``.``/``[`` must address
exactly the one literal key the declaration named -- never a structure.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from opendata.data.providers._engine.request_body import (
    CODE_CURSOR_INVALID,
    CODE_FIELD_MISSING,
    CODE_FIELD_UNDECLARED,
    CODE_FORM_SHAPE_INVALID,
    CODE_PAGING_INVALID,
    CODE_PATH_CONFLICT,
    CODE_POINTER_INVALID,
    CODE_TYPE_INVALID,
    BodyFieldSpec,
    BodyPagePlan,
    BodyPagingSpec,
    RequestBodyError,
    RequestBodySpec,
    body_digest,
    build_request_body,
    paging_decision,
    plan_body_page,
    read_declared_total,
    read_response_cursor,
    serialize_body,
)

DEMO = RequestBodySpec(
    fields=(
        BodyFieldSpec(parameter="symbol", kind="str", path=("symbol",), required=True),
        BodyFieldSpec(parameter="count", kind="int", path=("count",), default=100),
        BodyFieldSpec(
            parameter="frequency",
            kind="enum",
            path=("options", "frequency"),
            enum=("daily", "weekly"),
        ),
        BodyFieldSpec(parameter="tags", kind="str_list", path=("criteria", "tags")),
    )
)


class TestFieldDeclarations:
    def test_valid_field_builds_with_leaf_key(self) -> None:
        field = BodyFieldSpec(parameter="symbol", kind="str", path=("data", "symbol"))
        assert field.leaf_key == "symbol"
        assert field.path == ("data", "symbol")

    def test_parameter_must_be_identifier(self) -> None:
        with pytest.raises(ValueError, match="identifier"):
            BodyFieldSpec(parameter="bad key", kind="str", path=("k",))

    def test_unknown_kind_refused(self) -> None:
        with pytest.raises(ValueError, match="unsupported kind"):
            BodyFieldSpec(parameter="x", kind="bytes", path=("k",))  # type: ignore[arg-type]

    def test_enum_without_values_refused(self) -> None:
        with pytest.raises(ValueError, match="declares no values"):
            BodyFieldSpec(parameter="x", kind="enum", path=("k",))

    def test_non_enum_with_values_refused(self) -> None:
        with pytest.raises(ValueError, match="only enum"):
            BodyFieldSpec(parameter="x", kind="str", path=("k",), enum=("a",))

    def test_required_with_default_refused(self) -> None:
        with pytest.raises(ValueError, match="required and defaulted"):
            BodyFieldSpec(parameter="x", kind="str", path=("k",), required=True, default="d")

    def test_default_outside_domain_refused_at_declaration(self) -> None:
        with pytest.raises(RequestBodyError) as refusal:
            BodyFieldSpec(parameter="limit", kind="int", path=("limit",), default="10")
        assert refusal.value.code == CODE_TYPE_INVALID

    def test_dotted_string_path_refused_not_parsed(self) -> None:
        with pytest.raises(ValueError, match="tuple of literal"):
            BodyFieldSpec(parameter="x", kind="str", path="a.b")  # type: ignore[arg-type]

    def test_empty_segment_refused(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            BodyFieldSpec(parameter="x", kind="str", path=("a", ""))

    def test_overdeep_path_refused(self) -> None:
        with pytest.raises(ValueError, match="between 1 and"):
            BodyFieldSpec(parameter="x", kind="str", path=tuple(f"s{i}" for i in range(9)))

    def test_literal_dot_bracket_segment_is_accepted_as_one_key(self) -> None:
        field = BodyFieldSpec(parameter="x", kind="str", path=("query.filter[0]",))
        assert field.path == ("query.filter[0]",)


class TestBodySpecDeclarations:
    def test_json_content_type_header(self) -> None:
        assert DEMO.content_type_header == "application/json"
        assert DEMO.declared_parameters == {"symbol", "count", "frequency", "tags"}

    def test_form_content_type_header(self) -> None:
        spec = RequestBodySpec(
            fields=(BodyFieldSpec(parameter="q", kind="str", path=("q",)),),
            content_type="form",
        )
        assert spec.content_type_header == "application/x-www-form-urlencoded"

    def test_unknown_content_type_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown body content type"):
            RequestBodySpec(
                fields=(BodyFieldSpec(parameter="q", kind="str", path=("q",)),),
                content_type="xml",  # type: ignore[arg-type]
            )

    def test_empty_fields_refused(self) -> None:
        with pytest.raises(ValueError, match="no fields"):
            RequestBodySpec(fields=())

    def test_duplicate_parameter_refused(self) -> None:
        with pytest.raises(ValueError, match="declared twice"):
            RequestBodySpec(
                fields=(
                    BodyFieldSpec(parameter="q", kind="str", path=("a",)),
                    BodyFieldSpec(parameter="q", kind="int", path=("b",)),
                )
            )

    def test_identical_paths_refused(self) -> None:
        with pytest.raises(ValueError, match="collide"):
            RequestBodySpec(
                fields=(
                    BodyFieldSpec(parameter="a", kind="str", path=("x", "k")),
                    BodyFieldSpec(parameter="b", kind="str", path=("x", "k")),
                )
            )

    def test_nested_prefix_path_refused(self) -> None:
        with pytest.raises(ValueError, match="collide"):
            RequestBodySpec(
                fields=(
                    BodyFieldSpec(parameter="a", kind="str", path=("x",)),
                    BodyFieldSpec(parameter="b", kind="str", path=("x", "inner")),
                )
            )

    def test_sibling_paths_under_one_parent_are_fine(self) -> None:
        spec = RequestBodySpec(
            fields=(
                BodyFieldSpec(parameter="a", kind="str", path=("x", "one")),
                BodyFieldSpec(parameter="b", kind="str", path=("x", "two")),
            )
        )
        assert len(spec.fields) == 2

    def test_form_with_nesting_refused(self) -> None:
        with pytest.raises(ValueError, match="no nesting"):
            RequestBodySpec(
                fields=(BodyFieldSpec(parameter="a", kind="str", path=("x", "one")),),
                content_type="form",
            )

    def test_form_with_list_field_refused(self) -> None:
        with pytest.raises(ValueError, match="no list grammar"):
            RequestBodySpec(
                fields=(BodyFieldSpec(parameter="a", kind="str_list", path=("x",)),),
                content_type="form",
            )


class TestBuild:
    def test_flat_and_nested_build(self) -> None:
        body = build_request_body(
            DEMO, {"symbol": "AAPL", "frequency": "daily", "tags": ["tech", "mv"]}
        )
        assert body["symbol"] == "AAPL"
        assert body["count"] == 100
        assert body["options"] == {"frequency": "daily"}
        assert body["criteria"] == {"tags": ["tech", "mv"]}

    def test_undeclared_parameter_refused_not_passed_through(self) -> None:
        with pytest.raises(RequestBodyError) as refusal:
            build_request_body(DEMO, {"symbol": "AAPL", "admin": True})
        assert refusal.value.code == CODE_FIELD_UNDECLARED
        assert refusal.value.rejected == ("admin",)

    def test_attribute_traversal_key_is_refused_as_undeclared(self) -> None:
        with pytest.raises(RequestBodyError) as refusal:
            build_request_body(DEMO, {"symbol": "A", "__class__": "os.system"})
        assert refusal.value.code == CODE_FIELD_UNDECLARED

    def test_refusal_names_never_echo_values(self) -> None:
        secret = "hunter2-secret-value"
        with pytest.raises(RequestBodyError) as refusal:
            build_request_body(DEMO, {"symbol": "A", "leaky": secret})
        assert secret not in str(refusal.value)
        assert "leaky" in str(refusal.value)

    def test_wrongly_typed_int_refused_no_coercion(self) -> None:
        with pytest.raises(RequestBodyError) as refusal:
            build_request_body(DEMO, {"symbol": "A", "count": "300"})
        assert refusal.value.code == CODE_TYPE_INVALID

    def test_bool_cannot_satisfy_int_field(self) -> None:
        with pytest.raises(RequestBodyError) as refusal:
            build_request_body(DEMO, {"symbol": "A", "count": True})
        assert refusal.value.code == CODE_TYPE_INVALID

    def test_int_cannot_satisfy_str_field(self) -> None:
        with pytest.raises(RequestBodyError) as refusal:
            build_request_body(DEMO, {"symbol": 7})
        assert refusal.value.code == CODE_TYPE_INVALID

    def test_non_finite_float_refused(self) -> None:
        spec = RequestBodySpec(
            fields=(BodyFieldSpec(parameter="ratio", kind="float", path=("ratio",)),)
        )
        with pytest.raises(RequestBodyError) as refusal:
            build_request_body(spec, {"ratio": float("nan")})
        assert refusal.value.code == CODE_TYPE_INVALID

    def test_undeclared_enum_value_refused(self) -> None:
        with pytest.raises(RequestBodyError) as refusal:
            build_request_body(DEMO, {"symbol": "A", "frequency": "hourly"})
        assert refusal.value.code == CODE_TYPE_INVALID

    def test_mixed_list_refused_but_str_tuple_accepted(self) -> None:
        with pytest.raises(RequestBodyError):
            build_request_body(DEMO, {"symbol": "A", "tags": ["ok", 3]})
        body = build_request_body(DEMO, {"symbol": "A", "tags": ("ok", "fine")})
        assert body["criteria"]["tags"] == ["ok", "fine"]

    def test_date_renders_iso_and_float_accepts_int(self) -> None:
        spec = RequestBodySpec(
            fields=(
                BodyFieldSpec(parameter="from_date", kind="date", path=("from",)),
                BodyFieldSpec(parameter="weight", kind="float", path=("weight",)),
            )
        )
        body = build_request_body(spec, {"from_date": date(2026, 2, 13), "weight": 3})
        assert body == {"from": "2026-02-13", "weight": 3.0}

    def test_required_missing_refused(self) -> None:
        with pytest.raises(RequestBodyError) as refusal:
            build_request_body(DEMO, {"count": 10})
        assert refusal.value.code == CODE_FIELD_MISSING
        assert refusal.value.rejected == ("symbol",)

    def test_omitted_optional_is_absent_not_null(self) -> None:
        body = build_request_body(DEMO, {"symbol": "A"})
        assert "criteria" not in body
        assert "options" not in body
        serialized = serialize_body(DEMO, body)
        assert b"criteria" not in serialized


class TestNoInterpretation:
    def test_template_shaped_string_stays_literal(self) -> None:
        spec = RequestBodySpec(
            fields=(BodyFieldSpec(parameter="needle", kind="str", path=("needle",)),)
        )
        for evil in ("{symbol}", "%(x)s", "`__import__('os')`", "../../etc/passwd"):
            body = build_request_body(spec, {"needle": evil})
            assert body["needle"] == evil
            assert json.loads(serialize_body(spec, body))["needle"] == evil

    def test_dotted_key_name_is_one_literal_key_not_a_structure(self) -> None:
        spec = RequestBodySpec(
            fields=(BodyFieldSpec(parameter="weird", kind="str", path=("a.b[c]",), required=True),)
        )
        serialized = serialize_body(spec, build_request_body(spec, {"weird": "x"}))
        assert json.loads(serialized) == {"a.b[c]": "x"}


class TestSerialization:
    def test_two_builds_hash_to_identical_bytes(self) -> None:
        first = build_request_body(DEMO, {"symbol": "A", "tags": ["t"], "count": 5})
        second = build_request_body(DEMO, {"tags": ["t"], "count": 5, "symbol": "A"})
        left = serialize_body(DEMO, first)
        right = serialize_body(DEMO, second)
        assert left == right
        assert body_digest(left) == body_digest(right)

    def test_keys_sorted_at_every_level(self) -> None:
        body = build_request_body(
            DEMO, {"symbol": "A", "count": 1, "frequency": "weekly", "tags": ["z"]}
        )
        serialized = serialize_body(DEMO, body)
        assert serialized == json.dumps(
            json.loads(serialized), sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        assert serialized.count(b" ") == 0

    def test_digest_is_sha256_of_serialized_bytes(self) -> None:
        body = build_request_body(DEMO, {"symbol": "A"})
        serialized = serialize_body(DEMO, body)
        assert body_digest(serialized) == hashlib.sha256(serialized).hexdigest()

    def test_form_body_is_sorted_percent_encoded_pairs(self) -> None:
        spec = RequestBodySpec(
            fields=(
                BodyFieldSpec(parameter="beta", kind="str", path=("beta",)),
                BodyFieldSpec(parameter="alpha", kind="str", path=("alpha",)),
                BodyFieldSpec(parameter="on", kind="bool", path=("on",), default=True),
            ),
            content_type="form",
        )
        body = build_request_body(spec, {"beta": "a b&c", "alpha": "1"})
        assert serialize_body(spec, body) == b"alpha=1&beta=a+b%26c&on=true"

    def test_form_serialize_refuses_nested_value(self) -> None:
        spec = RequestBodySpec(
            fields=(BodyFieldSpec(parameter="a", kind="str", path=("a",)),),
            content_type="form",
        )
        with pytest.raises(RequestBodyError) as refusal:
            serialize_body(spec, {"a": {"nested": 1}})
        assert refusal.value.code == CODE_FORM_SHAPE_INVALID


class TestPagingDeclarations:
    def test_offset_paging_requires_pointer(self) -> None:
        with pytest.raises(ValueError, match="offset_path"):
            BodyPagingSpec(kind="offset")

    def test_cursor_paging_requires_both_pointers(self) -> None:
        with pytest.raises(ValueError, match="cursor_path"):
            BodyPagingSpec(kind="cursor", cursor_response_path=("pagination", "next"))
        with pytest.raises(ValueError, match="cursor_response_path"):
            BodyPagingSpec(kind="cursor", cursor_path=("pagination", "token"))

    def test_unknown_kind_and_bad_bounds_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown body paging kind"):
            BodyPagingSpec(kind="limit")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="page_size"):
            BodyPagingSpec(kind="page", offset_path=("page",), page_size=0)
        with pytest.raises(ValueError, match="max_pages"):
            BodyPagingSpec(kind="page", offset_path=("page",), max_pages=0)
        with pytest.raises(ValueError, match="max_rows"):
            BodyPagingSpec(kind="page", offset_path=("page",), max_rows=0)
        with pytest.raises(ValueError, match="step"):
            BodyPagingSpec(kind="offset", offset_path=("offset",), step=-5)
        with pytest.raises(ValueError, match="start"):
            BodyPagingSpec(kind="offset", offset_path=("offset",), start=True)

    def test_effective_step_defaults_to_page_size(self) -> None:
        default = BodyPagingSpec(kind="offset", offset_path=("offset",), page_size=100)
        declared = BodyPagingSpec(kind="offset", offset_path=("offset",), page_size=100, step=25)
        assert default.effective_step == 100
        assert declared.effective_step == 25


class TestOffsetAndPagePlans:
    def test_offset_advances_from_start_by_step(self) -> None:
        paging = BodyPagingSpec(
            kind="offset",
            offset_path=("search", "offset"),
            limit_path=("search", "count"),
            page_size=100,
            start=50,
            step=150,
        )
        values = {"symbol": "AAPL"}
        page_one = plan_body_page(DEMO, paging, values, 1)
        page_three = plan_body_page(DEMO, paging, values, 3)
        assert page_one.body["search"]["offset"] == 50
        assert page_three.body["search"]["offset"] == 350
        assert page_one.offset_value == 50 and page_three.offset_value == 350

    def test_mutations_name_the_moved_pointers_with_previous_values(self) -> None:
        paging = BodyPagingSpec(
            kind="offset", offset_path=("offset",), limit_path=("count",), page_size=25
        )
        page_two = plan_body_page(DEMO, paging, {"symbol": "A"}, 2)
        moved = {mutation.path: mutation for mutation in page_two.mutations}
        assert moved[("offset",)].value == 25
        assert moved[("offset",)].previous is None
        # count is a declared body field defaulting to 100; the paging limit
        # moves it, and the plan says so from the page-1 value.
        assert moved[("count",)].previous == 100
        assert moved[("count",)].value == 25

    def test_page_number_kind_writes_the_page_itself(self) -> None:
        paging = BodyPagingSpec(kind="page", offset_path=("page",), page_size=100)
        plan = plan_body_page(DEMO, paging, {"symbol": "A"}, 4)
        assert plan.body["page"] == 4

    def test_bad_page_numbers_refused(self) -> None:
        paging = BodyPagingSpec(kind="page", offset_path=("page",))
        for bad in (0, -1, "2", True):
            with pytest.raises(RequestBodyError) as refusal:
                plan_body_page(DEMO, paging, {"symbol": "A"}, bad)  # type: ignore[arg-type]
            assert refusal.value.code == CODE_PAGING_INVALID

    def test_plan_digest_reproduces_from_its_own_bytes(self) -> None:
        paging = BodyPagingSpec(kind="offset", offset_path=("offset",))
        plan = plan_body_page(DEMO, paging, {"symbol": "A"}, 2)
        assert plan.digest == body_digest(plan.serialized)
        again = plan_body_page(DEMO, paging, {"symbol": "A"}, 2)
        assert again.serialized == plan.serialized

    def test_paging_pointer_colliding_with_scalar_field_is_refused(self) -> None:
        paging = BodyPagingSpec(kind="offset", offset_path=("symbol", "offset"))
        with pytest.raises(RequestBodyError) as refusal:
            plan_body_page(DEMO, paging, {"symbol": "A"}, 1)
        assert refusal.value.code == CODE_PATH_CONFLICT

    def test_plan_does_not_mutate_the_baseline_body(self) -> None:
        paging = BodyPagingSpec(kind="offset", offset_path=("offset",))
        baseline = build_request_body(DEMO, {"symbol": "A"})
        snapshot = json.dumps(baseline, sort_keys=True)
        plan_body_page(DEMO, paging, {"symbol": "A"}, 7)
        assert json.dumps(baseline, sort_keys=True) == snapshot


class TestCursorPlans:
    PAGING = BodyPagingSpec(
        kind="cursor",
        cursor_path=("pagination", "next_page"),
        cursor_response_path=("pagination", "next_page"),
        page_size=1000,
    )

    def test_first_page_may_carry_no_cursor_and_omits_the_key(self) -> None:
        spec = RequestBodySpec(
            fields=(BodyFieldSpec(parameter="q", kind="str", path=("query",), required=True),)
        )
        plan = plan_body_page(spec, self.PAGING, {"q": "etfs"}, 1)
        assert plan.cursor is None
        assert "pagination" not in plan.body

    def test_response_cursor_is_written_verbatim_into_the_next_body(self) -> None:
        spec = RequestBodySpec(
            fields=(BodyFieldSpec(parameter="q", kind="str", path=("query",), required=True),)
        )
        echoed = read_response_cursor(
            {"pagination": {"next_page": "abc123"}, "data": []}, self.PAGING
        )
        assert echoed == "abc123"
        plan = plan_body_page(spec, self.PAGING, {"q": "etfs"}, 2, cursor=echoed)
        assert plan.body["pagination"]["next_page"] == "abc123"
        mutation = plan.mutations[-1]
        assert mutation.path == ("pagination", "next_page")
        assert mutation.previous is None

    def test_page_past_first_without_a_cursor_is_refused(self) -> None:
        spec = RequestBodySpec(
            fields=(BodyFieldSpec(parameter="q", kind="str", path=("query",), required=True),)
        )
        with pytest.raises(RequestBodyError) as refusal:
            plan_body_page(spec, self.PAGING, {"q": "etfs"}, 2)
        assert refusal.value.code == CODE_PAGING_INVALID

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            17,
            "x" * 1025,
            "line1\nline2",
            "tab\there",
            "\x00nul",
        ],
    )
    def test_malformed_cursor_never_reaches_the_body(self, bad: object) -> None:
        spec = RequestBodySpec(
            fields=(BodyFieldSpec(parameter="q", kind="str", path=("query",), required=True),)
        )
        with pytest.raises(RequestBodyError) as refusal:
            plan_body_page(spec, self.PAGING, {"q": "etfs"}, 2, cursor=bad)
        assert refusal.value.code == CODE_CURSOR_INVALID

    def test_path_shaped_cursor_is_data_not_a_pointer(self) -> None:
        spec = RequestBodySpec(
            fields=(BodyFieldSpec(parameter="q", kind="str", path=("query",), required=True),)
        )
        plan = plan_body_page(spec, self.PAGING, {"q": "e"}, 2, cursor="../../etc/passwd")
        assert plan.body["pagination"]["next_page"] == "../../etc/passwd"

    def test_read_response_cursor_arms(self) -> None:
        assert read_response_cursor({}, self.PAGING) is None
        assert read_response_cursor({"pagination": {"next_page": ""}}, self.PAGING) is None
        with pytest.raises(RequestBodyError) as refusal:
            read_response_cursor({"pagination": {"next_page": {"deep": 1}}}, self.PAGING)
        assert refusal.value.code == CODE_CURSOR_INVALID

    def test_read_cursor_without_a_declared_path_is_none(self) -> None:
        offset = BodyPagingSpec(kind="offset", offset_path=("offset",))
        assert read_response_cursor({"next_page": "tok"}, offset) is None


class TestDeclaredTotals:
    PAGING = BodyPagingSpec(
        kind="offset",
        offset_path=("offset",),
        total_response_path=("metadata", "total_results"),
    )

    def test_int_and_numeric_string_totals_read(self) -> None:
        assert read_declared_total({"metadata": {"total_results": 251}}, self.PAGING) == 251
        assert read_declared_total({"metadata": {"total_results": "40"}}, self.PAGING) == 40

    def test_missing_total_is_none_absent_not_a_failure(self) -> None:
        assert read_declared_total({"metadata": {}}, self.PAGING) is None
        assert read_declared_total({"metadata": {"total_results": None}}, self.PAGING) is None

    def test_unusable_total_is_refused(self) -> None:
        paging = BodyPagingSpec(kind="page", offset_path=("page",), total_response_path=("total",))
        for bad in (True, [1], {"a": 1}, "many"):
            with pytest.raises(RequestBodyError) as refusal:
                read_declared_total({"total": bad}, paging)
            assert refusal.value.code == CODE_POINTER_INVALID

    def test_undeclared_total_path_reads_no_total(self) -> None:
        plain = BodyPagingSpec(kind="page", offset_path=("page",))
        assert read_declared_total({"total": 9}, plain) is None


class TestStopCondition:
    PAGING = BodyPagingSpec(
        kind="offset",
        offset_path=("offset",),
        page_size=100,
        max_pages=3,
        max_rows=1000,
    )

    def test_short_page_completes(self) -> None:
        assert (
            paging_decision(self.PAGING, page_number=1, rows_returned=40, collected=40)
            == "complete"
        )

    def test_empty_page_completes_even_when_full_size_expected(self) -> None:
        no_short = BodyPagingSpec(
            kind="offset", offset_path=("offset",), page_size=100, stop_on_short_page=False
        )
        assert (
            paging_decision(no_short, page_number=2, rows_returned=0, collected=200) == "complete"
        )

    def test_full_page_continues(self) -> None:
        assert (
            paging_decision(self.PAGING, page_number=1, rows_returned=100, collected=100)
            == "continue"
        )

    def test_declared_total_completes_mid_page_size(self) -> None:
        decision = paging_decision(
            self.PAGING, page_number=1, rows_returned=100, collected=250, total=250
        )
        assert decision == "complete"

    def test_page_bound_is_incomplete_not_a_quiet_stop(self) -> None:
        assert (
            paging_decision(self.PAGING, page_number=3, rows_returned=100, collected=300)
            == "bound_reached"
        )
        assert (
            paging_decision(self.PAGING, page_number=2, rows_returned=100, collected=1100)
            == "bound_reached"
        )

    def test_source_exhausted_on_the_bound_page_is_complete(self) -> None:
        assert (
            paging_decision(self.PAGING, page_number=3, rows_returned=20, collected=220)
            == "complete"
        )

    def test_cursor_stop_follows_the_echoed_token(self) -> None:
        paging = BodyPagingSpec(
            kind="cursor",
            cursor_path=("next",),
            cursor_response_path=("next",),
            page_size=100,
        )
        assert (
            paging_decision(
                paging, page_number=1, rows_returned=100, collected=100, next_cursor="t"
            )
            == "continue"
        )
        assert (
            paging_decision(
                paging, page_number=1, rows_returned=100, collected=100, next_cursor=None
            )
            == "complete"
        )


class TestWholeRowShapes:
    """The twelve census rows this seam unlocks, expressed as declarations."""

    def test_yfinance_screener_offset_body(self) -> None:
        # EquityScreener / EquityGainers: POST criteria body, offset paging,
        # stop on total_results.
        spec = RequestBodySpec(
            fields=(
                BodyFieldSpec(parameter="query", kind="str", path=("query",), required=True),
                BodyFieldSpec(parameter="sector", kind="str", path=("criteria", "sector")),
                BodyFieldSpec(parameter="min_price", kind="float", path=("criteria", "min")),
            )
        )
        paging = BodyPagingSpec(
            kind="offset",
            offset_path=("offset",),
            limit_path=("count",),
            total_response_path=("total_results",),
            page_size=100,
        )
        page_two = plan_body_page(spec, paging, {"query": "gainers"}, 2)
        assert page_two.body["offset"] == 100
        assert page_two.body["count"] == 100
        assert page_two.body["query"] == "gainers"
        assert "criteria" not in page_two.body
        total = read_declared_total({"total_results": 150}, paging)
        assert total == 150

    def test_benzinga_news_page_number_body(self) -> None:
        # CompanyNews / WorldNews: page-offset driving with a page-size field.
        spec = RequestBodySpec(
            fields=(
                BodyFieldSpec(parameter="tickers", kind="str", path=("tickers",), required=True),
                BodyFieldSpec(parameter="from_date", kind="date", path=("created_date_greater",)),
            )
        )
        paging = BodyPagingSpec(
            kind="page", offset_path=("page",), limit_path=("items_per_page",), page_size=100
        )
        plan = plan_body_page(spec, paging, {"tickers": "AAPL", "from_date": date(2026, 1, 5)}, 12)
        assert isinstance(plan, BodyPagePlan)
        assert plan.body["page"] == 12
        assert plan.body["created_date_greater"] == "2026-01-05"

    def test_intrinio_next_page_cursor_body(self) -> None:
        # EtfSearch / WorldNews / FredSeries: a server next_page token echoed
        # from the response into the next request document.
        spec = RequestBodySpec(
            fields=(BodyFieldSpec(parameter="query", kind="str", path=("filter", "keyword")),)
        )
        paging = BodyPagingSpec(
            kind="cursor",
            cursor_path=("pagination", "next_page"),
            cursor_response_path=("pagination", "next_page"),
            limit_path=("pagination", "page_size"),
            page_size=10000,
        )
        token = read_response_cursor({"pagination": {"next_page": "aG9uZXN0"}}, paging)
        plan = plan_body_page(spec, paging, {"query": "bank"}, 2, cursor=token)
        assert plan.body["pagination"]["next_page"] == "aG9uZXN0"
        assert plan.body["pagination"]["page_size"] == 10000

    def test_tmx_graphql_envelope_body(self) -> None:
        # EquityHistorical / EtfHistorical: POST GraphQL document; the query
        # text is a declared literal, variables are typed slots.
        spec = RequestBodySpec(
            fields=(
                BodyFieldSpec(parameter="query_text", kind="str", path=("query",), required=True),
                BodyFieldSpec(parameter="symbol", kind="str", path=("variables", "symbol")),
            )
        )
        body = build_request_body(
            spec,
            {"query_text": "query { ... }", "symbol": "SHOP.TO"},
        )
        assert serialize_body(spec, body) == (
            b'{"query":"query { ... }","variables":{"symbol":"SHOP.TO"}}'
        )

    def test_government_us_post_body_with_form_alternative(self) -> None:
        # TreasuryPrices / CommodityPsdData: POST payload from validated
        # parameters; form flavor keeps the same field rules.
        spec = RequestBodySpec(
            fields=(
                BodyFieldSpec(parameter="security", kind="str", path=("Security",), required=True),
                BodyFieldSpec(parameter="start_date", kind="date", path=("FromDt",)),
                BodyFieldSpec(parameter="position", kind="int", path=("PositionTyp",), default=0),
            ),
            content_type="form",
        )
        serialized = serialize_body(
            spec, build_request_body(spec, {"security": "CUA_2Y", "start_date": date(2025, 1, 1)})
        )
        assert serialized == b"FromDt=2025-01-01&PositionTyp=0&Security=CUA_2Y"
