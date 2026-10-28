"""Synthetic offline checks for SZSE delisted-table seed classification."""

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from itertools import repeat

import pytest

from opendata.data.szse_delisted_seed import (
    SZSE_2023_YEARBOOK_PDF_SHA256,
    SZSE_2023_YEARBOOK_SOURCE_ID,
    CallerAshareClassAttestation,
    DateFact,
    DateReconciliation,
    SeedClassification,
    SzseDelistedSeedAudit,
    SzseDelistedSeedError,
    SzseDelistedSeedRow,
    validate_szse_delisted_seed,
)

_SYNTHETIC_REVIEW_SHA256 = "b" * 64


def fact(raw: str, parsed: str | None, locator: str) -> DateFact:
    """Build a clearly synthetic source date field."""
    return DateFact(
        raw_text=raw,
        parsed_date=date.fromisoformat(parsed) if parsed is not None else None,
        source_locator=locator,
    )


def candidate(symbol: str = "999999", **changes: object) -> SzseDelistedSeedRow:
    """Build a synthetic row; its values were not extracted from the pinned PDF."""
    values: dict[str, object] = {
        "symbol": symbol,
        "issuer_name": "Synthetic issuer",
        "source_id": SZSE_2023_YEARBOOK_SOURCE_ID,
        "pdf_sha256": SZSE_2023_YEARBOOK_PDF_SHA256,
        "pdf_page_number": 1000,
        "printed_page_label": "983",
        "row_locator": "synthetic/table/row-1",
        "listing_date": fact("1994-08-08", "1994-08-08", "synthetic/列/上市日期"),
        "last_trade_date": fact("2023-07-11", "2023-07-11", "synthetic/列/最后交易日"),
        "termination_date": fact("2023-07-12", "2023-07-12", "synthetic/列/终止上市日"),
        "security_class_as_printed": None,
    }
    values.update(changes)
    return SzseDelistedSeedRow(**values)  # type: ignore[arg-type]


def attestation(symbol: str = "999999", **changes: object) -> CallerAshareClassAttestation:
    """Build synthetic caller-attested class evidence."""
    values: dict[str, object] = {
        "symbol": symbol,
        "issuer_name": "Synthetic issuer",
        "share_class": "A",
        "market": "CN",
        "exchange": "XSHE",
        "source_id": "szse.delisting_notice.synthetic",
        "pdf_sha256": _SYNTHETIC_REVIEW_SHA256,
        "source_locator": "synthetic/notice/page-1/row-1",
        "attestation_ref": "synthetic/attestation/1",
        "attester_id": "synthetic-attester",
        "attested_on": date(2026, 10, 28),
        "attests_independent_of_yearbook": True,
        "listing_date": fact("1994-08-08", "1994-08-08", "synthetic-notice/listing"),
        "last_trade_date": fact("2023-07-11", "2023-07-11", "synthetic-notice/last-trade"),
        "termination_date": fact("2023-07-12", "2023-07-12", "synthetic-notice/termination"),
    }
    values.update(changes)
    return CallerAshareClassAttestation(**values)  # type: ignore[arg-type]


def validate(
    *rows: SzseDelistedSeedRow,
    attestations: tuple[CallerAshareClassAttestation, ...] = (),
):
    """Run the validator against synthetic rows bound to the fixed profile."""
    return validate_szse_delisted_seed(rows, attestations)


def test_unattested_mixed_table_row_stays_candidate_and_keeps_three_dates() -> None:
    row = candidate()

    result = validate(row)

    assert result.market_completeness == "not_assessed"
    assert result.candidate_count == 1
    assert result.a_share_attested_count == 0
    assert result.rows[0].classification is SeedClassification.CANDIDATE
    assert result.rows[0].date_reconciliation is DateReconciliation.NOT_COMPARED
    assert result.rows[0].candidate.listing_date.parsed_date == date(1994, 8, 8)
    assert result.rows[0].candidate.last_trade_date.parsed_date == date(2023, 7, 11)
    assert result.rows[0].candidate.termination_date.parsed_date == date(2023, 7, 12)


def test_a_share_attestation_does_not_replace_yearbook_date_facts() -> None:
    row = candidate()
    supplied = attestation(
        listing_date=fact("1994-08-09", "1994-08-09", "synthetic-notice/listing")
    )

    result = validate(row, attestations=(supplied,))
    checked = result.rows[0]

    assert checked.classification is SeedClassification.A_SHARE_CLASS_ATTESTED
    assert checked.date_reconciliation is DateReconciliation.DISAGREES
    assert checked.date_conflicts == ("listing_date",)
    assert checked.candidate.listing_date.parsed_date == date(1994, 8, 8)
    assert checked.independent_attestation is supplied
    assert checked.independent_attestation.listing_date is not None
    assert checked.independent_attestation.listing_date.parsed_date == date(1994, 8, 9)
    assert result.market_completeness == "not_assessed"


def test_caller_attested_non_a_class_is_not_promoted_to_a_share() -> None:
    result = validate(candidate(), attestations=(attestation(share_class="B"),))

    assert result.non_a_share_attested_count == 1
    assert result.a_share_attested_count == 0
    assert result.rows[0].classification is SeedClassification.NON_A_SHARE_CLASS_ATTESTED


def test_unknown_class_attestation_remains_unresolved_and_dates_can_be_incomplete() -> None:
    result = validate(
        candidate(),
        attestations=(attestation(share_class="UNRESOLVED", termination_date=None),),
    )

    assert result.unresolved_class_count == 1
    assert result.rows[0].classification is SeedClassification.CLASS_UNRESOLVED
    assert result.rows[0].date_reconciliation is DateReconciliation.INCOMPLETE


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"attests_independent_of_yearbook": False}, "declare independence"),
        ({"source_id": SZSE_2023_YEARBOOK_SOURCE_ID}, "differ from yearbook"),
        ({"pdf_sha256": SZSE_2023_YEARBOOK_PDF_SHA256}, "differ from yearbook"),
        ({"issuer_name": "Different issuer"}, "does not exactly match"),
    ],
)
def test_attestation_must_be_independent_and_match_candidate(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(SzseDelistedSeedError, match=message):
        validate(candidate(), attestations=(attestation(**changes),))


def test_fixed_profile_binds_source_id_and_digest_without_overrides() -> None:
    with pytest.raises(SzseDelistedSeedError, match="no controlled profile"):
        validate(candidate(source_id="unregistered.yearbook"))

    with pytest.raises(SzseDelistedSeedError, match="does not match controlled profile"):
        validate(candidate(pdf_sha256="a" * 64))

    with pytest.raises(SzseDelistedSeedError, match="duplicate candidate symbol 999999"):
        validate(
            candidate(),
            candidate("999999", row_locator="synthetic/table/row-2", pdf_page_number=1001),
        )


def test_rejects_duplicate_source_row_locator_with_conflict_diagnostic() -> None:
    with pytest.raises(
        SzseDelistedSeedError,
        match="duplicate source row locator.*symbols 999999 and 999998 conflict",
    ):
        validate(candidate(), candidate("999998"))


def test_rejects_orphan_and_duplicate_attestation_rows() -> None:
    with pytest.raises(SzseDelistedSeedError, match="attestation has no matching"):
        validate(candidate(), attestations=(attestation("000001"),))

    with pytest.raises(SzseDelistedSeedError, match="attestation symbols must be unique"):
        validate(candidate(), attestations=(attestation(), attestation()))


def test_rejects_dates_that_reverse_listing_and_trading_lifecycle() -> None:
    with pytest.raises(ValueError, match="listing_date cannot follow last_trade_date"):
        candidate(
            listing_date=fact("2024-01-02", "2024-01-02", "synthetic/listing"),
            last_trade_date=fact("2024-01-01", "2024-01-01", "synthetic/last-trade"),
        )


def test_attestation_date_comparison_requires_all_three_comparable_dates() -> None:
    supplied = attestation(last_trade_date=None, termination_date=None)

    result = validate(candidate(), attestations=(supplied,))

    assert result.rows[0].classification is SeedClassification.A_SHARE_CLASS_ATTESTED
    assert result.rows[0].date_reconciliation is DateReconciliation.INCOMPLETE
    assert result.rows[0].date_conflicts == ()


def test_rows_are_sorted_by_code_without_asserting_completeness() -> None:
    result = validate(
        candidate("999998"),
        candidate("999997", row_locator="synthetic/table/row-2", pdf_page_number=1001),
    )

    assert tuple(row.candidate.symbol for row in result.rows) == ("999997", "999998")
    assert result.market_completeness == "not_assessed"


def test_market_completeness_cannot_be_overridden_by_normal_dataclass_apis() -> None:
    result = validate(candidate())

    with pytest.raises(TypeError):
        SzseDelistedSeedAudit(
            source_id=result.source_id,
            source_pdf_sha256=result.source_pdf_sha256,
            rows=result.rows,
            market_completeness="complete",
        )
    with pytest.raises((ValueError, TypeError), match="market_completeness.*init=False"):
        replace(result, market_completeness="complete")
    with pytest.raises(FrozenInstanceError):
        result.market_completeness = "complete"  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"raw_text": "bad\nvalue"}, "raw_text must be text"),
        ({"raw_text": "x" * 513}, "raw_text must be text"),
        ({"parsed_date": datetime(2023, 1, 2)}, "parsed_date must be datetime.date or None"),
        ({"raw_text": "", "parsed_date": date(2023, 1, 2)}, "requires nonempty raw_text"),
        ({"source_locator": ""}, "source_locator must be nonempty text"),
    ],
)
def test_date_fact_rejects_invalid_source_text_and_date_types(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "raw_text": "2023-01-02",
        "parsed_date": date(2023, 1, 2),
        "source_locator": "synthetic/date-cell",
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        DateFact(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"symbol": "12345"}, "six decimal digits"),
        ({"issuer_name": ""}, "issuer_name must be nonempty text"),
        ({"source_id": "../yearbook"}, "stable identifier"),
        ({"pdf_sha256": "A" * 64}, "lowercase SHA-256"),
        ({"pdf_page_number": True}, "integer from 1 through 10000"),
        ({"printed_page_label": "bad\nlabel"}, "printed_page_label must be nonempty text"),
        ({"row_locator": ""}, "row_locator must be nonempty text"),
        ({"listing_date": object()}, "listing_date must be a DateFact"),
        (
            {"security_class_as_printed": "bad\nclass"},
            "security_class_as_printed must be nonempty text",
        ),
    ],
)
def test_candidate_rejects_malformed_identity_and_source_fields(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        candidate(**changes)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"symbol": "AB12"}, "six decimal digits"),
        ({"share_class": "C"}, "share_class must be A, B, OTHER, or UNRESOLVED"),
        ({"market": "HK"}, "market CN and exchange XSHE"),
        ({"exchange": "XSHG"}, "market CN and exchange XSHE"),
        ({"source_id": "bad source"}, "stable identifier"),
        ({"pdf_sha256": "not-a-digest"}, "lowercase SHA-256"),
        ({"source_locator": ""}, "source_locator must be nonempty text"),
        ({"attestation_ref": ""}, "attestation_ref must be nonempty text"),
        ({"attester_id": "bad id"}, "stable identifier"),
        ({"attested_on": datetime(2026, 10, 28)}, "attested_on must be datetime.date"),
        ({"attests_independent_of_yearbook": 1}, "must be a bool"),
        ({"listing_date": object()}, "listing_date must be a DateFact or None"),
    ],
)
def test_attestation_rejects_malformed_class_scope_and_evidence_fields(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        attestation(**changes)


def test_unknown_source_dates_remain_unknown_instead_of_being_inferred() -> None:
    unknown_listing = fact("", None, "synthetic/blank-listing-cell")

    result = validate(candidate(listing_date=unknown_listing))

    assert result.rows[0].candidate.listing_date.raw_text == ""
    assert result.rows[0].candidate.listing_date.parsed_date is None
    assert result.market_completeness == "not_assessed"


def test_all_three_matching_attested_dates_report_agreement() -> None:
    result = validate(candidate(), attestations=(attestation(),))

    assert result.rows[0].date_reconciliation is DateReconciliation.AGREES
    assert result.rows[0].date_conflicts == ()


def test_validator_rejects_unbounded_wrong_type_and_oversized_inputs() -> None:
    with pytest.raises(SzseDelistedSeedError, match="candidates must be a bounded iterable"):
        validate_szse_delisted_seed("not candidate rows")  # type: ignore[arg-type]

    with pytest.raises(SzseDelistedSeedError, match="candidates must contain"):
        validate_szse_delisted_seed((object(),))  # type: ignore[arg-type]

    with pytest.raises(SzseDelistedSeedError, match="attestations must contain"):
        validate_szse_delisted_seed((candidate(),), (object(),))  # type: ignore[arg-type]

    with pytest.raises(SzseDelistedSeedError, match="exceeds the 20,000-row limit"):
        validate_szse_delisted_seed(repeat(candidate(), 20_001))


def test_validator_wraps_iterable_type_error_and_preserves_domain_error() -> None:
    class BrokenIterable:
        """Represent an input whose iteration protocol raises TypeError."""

        def __iter__(self):
            raise TypeError("synthetic iterator failure")

    class DomainErrorIterable:
        """Represent a parser that already raised the module's domain error."""

        def __iter__(self):
            raise SzseDelistedSeedError("synthetic extraction error")

    with pytest.raises(SzseDelistedSeedError, match="candidates must be a bounded iterable"):
        validate_szse_delisted_seed(BrokenIterable())  # type: ignore[arg-type]

    with pytest.raises(SzseDelistedSeedError, match="synthetic extraction error"):
        validate_szse_delisted_seed(DomainErrorIterable())  # type: ignore[arg-type]
