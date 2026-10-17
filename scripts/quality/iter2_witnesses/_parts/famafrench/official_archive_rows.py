"""Verbatim-line witnesses for the six frozen Fama/French model identities.

Every body here is a contiguous slice of one official archive member captured live from
``mba.tuck.dartmouth.edu`` on 2026-10-17: the archive and member SHA-256s, the member names and
the line ranges are recorded in the acceptance record beside this shard. The lines are
transcribed character for character -- fixed-width padding, alignment spaces and the library's
own ``-99.99`` missing sentinel included -- and re-published inside a one-member ZIP by
:func:`_archive_base64`, because
:func:`opendata.data.providers.famafrench._source.fetch_archive` hands ``zipfile`` the bytes the
governed client returned and :func:`~opendata.data.providers.famafrench._source.archive_text`
accepts only a flat archive with exactly one selected member. The transport is the repository's
fake governed client, patched at the one attribute this provider reads per request
(:data:`_SEAM`); no leg opens a socket -- the runner refuses socket use for the whole process --
and the ZIP, member-selection, size, encoding, header, column-count, period and pivot gates
under test are the shipped ones in ``_source.py`` and ``models/fetchers.py``.

Slicing instead of retyping is what makes each counterfact load-bearing. One tamper edits exactly
one line inside one slice: a percentile cell removed from a breakpoint row, the factor column
header removed, one portfolio return cell removed, one cell printed as prose where the table
publishes a decimal, the ``Mkt High Low ...`` value header removed from a ``.Dat`` section, and a
requirement anchor flipped to the table the caller did not ask for. Each of those is a fact the
source itself publishes, so the guard that has to refuse it is named rather than asserted.

What these six witnesses do not cover: the ``_TXT.zip`` mirror branch of ``_table_archive_text``
(every query here takes ``archive_format="csv"``), the country ``measure="ratios"`` annual table,
and the without-dividends archives ``F-F_International_{Countries,Indices}_Wout_Div.zip``. The
slices are head windows of three to six periods per archive, so period anchors, the two missing
sentinels and ``MAX_OUTPUT_ROWS`` are exercised only as far as these windows reach. The row values
are the source's own, captured on the date above; they prove the reading and the gates, not that
the library publishes them today.
"""

from __future__ import annotations

import base64
import io
import zipfile
from typing import Any, Final

#: The one attribute the provider reads per request; the runner patches it in ``_source`` only.
_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.famafrench._source"],
    "attr": "get_shared_http_client",
}

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/famafrench/_source.py",
    "opendata/data/providers/famafrench/datasets.py",
    "opendata/data/providers/famafrench/models/fetchers.py",
]
_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/famafrench.py",
    "opendata/data/domains.yaml",
]


def _archive_base64(member_name: str, lines: tuple[str, ...]) -> str:
    """Publish one flat ZIP the way the library ships it: a single member, CRLF records."""
    payload = "".join(f"{line}\r\n" for line in lines).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED, strict_timestamps=False) as archive:
        archive.writestr(member_name, payload)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _envelope(archive_name: str, member_name: str, lines: tuple[str, ...]) -> dict[str, Any]:
    """Wrap one archive's verbatim slice as the single response that fetch is served."""
    return {
        "responses": [
            {
                "url_contains": archive_name,
                "content_type": "application/zip",
                "body_b64": _archive_base64(member_name, lines),
            }
        ]
    }


def _line_index(lines: tuple[str, ...], needle: str) -> int:
    """Resolve a tamper target by one exact source line, so an index cannot drift silently."""
    hits = [index for index, line in enumerate(lines) if line == needle]
    if len(hits) != 1:
        raise AssertionError(f"witness slice needle {needle!r} matched {len(hits)} line(s)")
    return hits[0]


def _without_line(lines: tuple[str, ...], needle: str) -> tuple[str, ...]:
    """Return the slice with exactly one line removed."""
    index = _line_index(lines, needle)
    return lines[:index] + lines[index + 1 :]


def _line_replaced(lines: tuple[str, ...], needle: str, value: str) -> tuple[str, ...]:
    """Return the slice with exactly one line rewritten to ``value``."""
    index = _line_index(lines, needle)
    return lines[:index] + (value,) + lines[index + 1 :]


def _dropped_last_field(line: str) -> str:
    """Return the source row with its final published cell removed."""
    head, _sep, _tail = line.rpartition(",")
    return head


def _cell_printed_as(line: str, cell: str, spelling: str) -> str:
    """Return the source row with one published cell reprinted as another spelling."""
    if line.count(cell) != 1:
        raise AssertionError(f"witness cell {cell!r} is not unique in {line!r}")
    return line.replace(cell, spelling, 1)


_ME_BREAKPOINT_LINES: Final[tuple[str, ...]] = (
    (
        "This file was created using the 202608 CRSP database.  It contains every 5th NYSE ME"
        " percentile (divided by 1000000)."
    ),
    "",
    (
        "192512,"
        "   488,"
        "        1.40,"
        "        2.38,"
        "        3.37,"
        "        4.96,"
        "        5.95,"
        "        7.40,"
        "        8.92,"
        "       10.81,"
        "       13.01,"
        "       15.56,"
        "       18.50,"
        "       22.88,"
        "       29.55,"
        "       38.41,"
        "       49.93,"
        "       65.40,"
        "       92.72,"
        "      142.48,"
        "      238.78,"
        "     1319.00"
    ),
    (
        "192601,"
        "   492,"
        "        1.38,"
        "        2.54,"
        "        3.76,"
        "        4.84,"
        "        5.94,"
        "        7.50,"
        "        9.28,"
        "       10.94,"
        "       12.69,"
        "       15.81,"
        "       17.75,"
        "       23.20,"
        "       30.40,"
        "       38.99,"
        "       50.85,"
        "       66.82,"
        "       94.70,"
        "      139.18,"
        "      235.20,"
        "     1331.71"
    ),
    (
        "192602,"
        "   500,"
        "        1.29,"
        "        2.34,"
        "        3.57,"
        "        4.69,"
        "        5.61,"
        "        7.15,"
        "        8.45,"
        "       10.41,"
        "       12.06,"
        "       14.62,"
        "       17.03,"
        "       21.18,"
        "       27.62,"
        "       36.55,"
        "       46.49,"
        "       61.42,"
        "       87.51,"
        "      138.04,"
        "      223.02,"
        "     1366.39"
    ),
)

_FACTOR_LINES: Final[tuple[str, ...]] = (
    "This file was created using the 202608 CRSP database.",
    (
        "The 1-month TBill rate data until 202405 are from Ibbotson Associates. Starting from"
        " 202406,"
        " the 1-month TBill rate is from ICE BofA US 1-Month Treasury Bill Index. "
    ),
    (
        "The annual TBill return is compounded from the monthly T-bill rates from January to "
        "December. "
    ),
    "",
    ",Mkt-RF,SMB,HML,RF",
    "192607,   2.89,  -2.42,  -2.75,   0.22",
    "192608,   2.64,  -1.44,   4.13,   0.25",
    "192609,   0.38,  -1.36,   0.05,   0.23",
    "192610,  -3.27,  -0.18,   0.78,   0.32",
)

_US_PORTFOLIO_LINES: Final[tuple[str, ...]] = (
    "  Average Value Weighted Returns -- Monthly",
    ",SMALL LoBM,ME1 BM2,SMALL HiBM,BIG LoBM,ME2 BM2,BIG HiBM",
    "192607,   1.0866,   0.9068,  -0.1275,   5.7178,   1.9709,   1.4225",
    "192608,   0.7831,   1.1829,   5.4422,   2.7268,   2.6766,   6.3196",
    "192609,  -2.8045,  -0.0518,  -0.4399,   1.4777,   0.0850,  -0.7906",
    "192610,  -4.0289,  -4.5905,  -2.0128,  -3.6327,  -2.3475,  -4.0981",
)

_REGIONAL_PORTFOLIO_LINES: Final[tuple[str, ...]] = (
    "  Average Value Weighted Returns -- Monthly",
    ",SMALL LoBM,ME1 BM2,SMALL HiBM,BIG LoBM,ME2 BM2,BIG HiBM",
    "199007   ,2.42   ,1.91   ,0.96   ,0.64   ,1.68   ,1.38",
    "199008 ,-12.21 ,-11.48 ,-10.55  ,-9.50 ,-10.00 ,-10.20",
    "199009 ,-10.82 ,-10.00  ,-9.38 ,-11.77 ,-10.78 ,-11.60",
    "199010   ,5.19   ,3.49   ,0.53  ,13.04  ,10.20   ,8.74",
)

_COUNTRY_UK_LINES: Final[tuple[str, ...]] = (
    "     Value-Weight Dollar Returns      All 4 Data Items Required",
    "                -- BE/ME --   --- E/P ---   --- CE/P --   ------  Yld  -----",
    "          Mkt   High    Low   High    Low   High    Low   High    Low   Zero",
    "197501  54.56  47.27  55.59  54.79  52.33  57.33  53.27  47.16  56.87 -99.99",
    "197502  26.32  32.61  24.23  26.89  24.96  26.58  25.34  29.75  25.75 -99.99",
    "197503  -8.16  -9.37  -6.04 -10.69  -7.49  -9.99  -7.48  -9.34  -6.05 -99.99",
)

_INDEX_ALL_LINES: Final[tuple[str, ...]] = (
    "     Value-Weight Dollar Returns      All 4 Data Items Required",
    "                -- BE/ME --   --- E/P ---   --- CE/P --   ------  Yld  -----",
    "          Mkt   High    Low   High    Low   High    Low   High    Low   Zero",
    "197501  17.39  16.03  17.63  17.12  17.46  16.48  18.42  15.00  18.24   6.73",
    "197502  13.59  15.63  12.56  15.92  11.77  15.20  12.67  14.53  12.74   6.28",
    "197503  -0.78  -1.07   0.17  -0.42  -1.66  -1.34  -0.36  -1.46   0.11   1.70",
)

#: The other requirement anchor the same member publishes, verbatim from line 2 of Ind_all.Dat.
_INDEX_NOT_REQD_ANCHOR: Final[str] = (
    "     Value-Weight Dollar Returns      All 4 Data Items Not Reqd"
)

#: Named from the slice so a tamper targets the line the parser actually compares.
_COUNTRY_VALUE_HEADER: Final[str] = _COUNTRY_UK_LINES[2]
_INDEX_REQUIRED_ANCHOR: Final[str] = _INDEX_ALL_LINES[0]


MODELS: Final[dict[str, Any]] = {
    "FamaFrenchBreakpoints": {
        "fixture_document": lambda: _envelope(
            "ME_Breakpoints_CSV.zip", "ME_Breakpoints.csv", _ME_BREAKPOINT_LINES
        ),
        "client_seam": _SEAM,
        "query": {"breakpoint_type": "me", "frequency": "monthly"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body_b64"],
            "value": _archive_base64(
                "ME_Breakpoints.csv",
                _line_replaced(
                    _ME_BREAKPOINT_LINES,
                    _ME_BREAKPOINT_LINES[4],
                    _dropped_last_field(_ME_BREAKPOINT_LINES[4]),
                ),
            ),
        },
        "refusal_class": "FrenchProviderError",
        "refusal_code": "FRENCH_BREAKPOINT_COLUMN_COUNT",
        "tamper_reason": (
            "the 192602 row publishes the sample count and twenty percentile cells, and "
            "parse_breakpoint_table maps field position to percentile by requiring "
            "1 + count_columns + 20 fields; removing the 100th percentile cell keeps the date and "
            "the count legitimate but shifts nothing on disk -- it leaves twenty-one fields, so "
            "the guard must refuse the row rather than publish nineteen percentiles and let "
            "transform_data fill percentile_100 from a value the source never printed for it"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "FamaFrenchFactors": {
        "fixture_document": lambda: _envelope(
            "F-F_Research_Data_Factors_CSV.zip", "F-F_Research_Data_Factors.csv", _FACTOR_LINES
        ),
        "client_seam": _SEAM,
        "query": {"region": "america", "factor": "3_factors", "frequency": "monthly"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body_b64"],
            "value": _archive_base64(
                "F-F_Research_Data_Factors.csv", _without_line(_FACTOR_LINES, ",Mkt-RF,SMB,HML,RF")
            ),
        },
        "refusal_class": "FrenchProviderError",
        "refusal_code": "FRENCH_FACTOR_HEADER_MISSING",
        "tamper_reason": (
            "the monthly block is four numbers per period and the only thing that says which is "
            "Mkt-RF and which is RF is the header line the archive prints above them; "
            "parse_factor_table searches for a line whose first field is empty and whose rest are "
            "named, and with that line gone transform_data could not map any key through "
            "_FACTOR_FIELDS -- so the guard refuses FRENCH_FACTOR_HEADER_MISSING instead of "
            "attaching four period returns to factor names it has to invent"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "FamaFrenchUSPortfolioReturns": {
        "fixture_document": lambda: _envelope(
            "6_Portfolios_2x3_CSV.zip", "6_Portfolios_2x3.csv", _US_PORTFOLIO_LINES
        ),
        "client_seam": _SEAM,
        "query": {"portfolio": "6_portfolios_2x3", "measure": "value"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body_b64"],
            "value": _archive_base64(
                "6_Portfolios_2x3.csv",
                _line_replaced(
                    _US_PORTFOLIO_LINES,
                    _US_PORTFOLIO_LINES[2],
                    _dropped_last_field(_US_PORTFOLIO_LINES[2]),
                ),
            ),
        },
        "refusal_class": "FrenchProviderError",
        "refusal_code": "FRENCH_PORTFOLIO_COLUMN_COUNT",
        "tamper_reason": (
            "the six_portfolio table publishes six value-weighted returns per month under six "
            "column names, and parse_portfolio_table checks every row against "
            "len(header) + 1 before pairing header to fields; a row printed with five cells keeps "
            "a valid date and valid numbers but drops BIG HiBM, so the guard must refuse rather "
            "than zip four names onto five values and publish a 192607 SMALL HiBM return taken "
            "from the BIG LoBM cell"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "FamaFrenchRegionalPortfolioReturns": {
        "fixture_document": lambda: _envelope(
            "Developed_6_Portfolios_ME_BE-ME_CSV.zip",
            "Developed_6_Portfolios_ME_BE-ME.csv",
            _REGIONAL_PORTFOLIO_LINES,
        ),
        "client_seam": _SEAM,
        "query": {"portfolio": "developed_6_portfolios_me_be-me", "measure": "value"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body_b64"],
            "value": _archive_base64(
                "Developed_6_Portfolios_ME_BE-ME.csv",
                _line_replaced(
                    _REGIONAL_PORTFOLIO_LINES,
                    _REGIONAL_PORTFOLIO_LINES[2],
                    _cell_printed_as(_REGIONAL_PORTFOLIO_LINES[2], "1.91", "n/a "),
                ),
            ),
        },
        "refusal_class": "FrenchProviderError",
        "refusal_code": "FRENCH_BAD_NUMERIC_VALUE",
        "tamper_reason": (
            "the archive documents its own missing-value spelling (-99.99, and -999 for counts) "
            "and parse_number accepts only a decimal or exactly those two sentinels; printing the "
            "199007 ME1 BM2 cell as prose 'n/a' keeps the seven-field row and every other value, "
            "so the guard has to refuse FRENCH_BAD_NUMERIC_VALUE instead of quietly turning an "
            "unpublished developed-market return into a null the caller cannot distinguish from "
            "the source's own -99.99"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "FamaFrenchCountryPortfolioReturns": {
        "fixture_document": lambda: _envelope(
            "F-F_International_Countries.zip", "UK.Dat", _COUNTRY_UK_LINES
        ),
        "client_seam": _SEAM,
        "query": {"country": "united_kingdom", "frequency": "monthly", "measure": "usd"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body_b64"],
            "value": _archive_base64(
                "UK.Dat", _without_line(_COUNTRY_UK_LINES, _COUNTRY_VALUE_HEADER)
            ),
        },
        "refusal_class": "FrenchProviderError",
        "refusal_code": "FRENCH_DAT_HEADER_MISMATCH",
        "tamper_reason": (
            "a .Dat section carries no column names in its rows: the ten values per period are "
            "assigned to (sort, portfolio) pairs only after the parser matches the "
            "'Mkt High Low ... Zero' line against _DAT_VALUE_HEADER. Removing that anchor leaves "
            "the BE/ME group banner, the date and all ten numbers intact, so "
            "parse_dat_returns raises FRENCH_DAT_HEADER_MISMATCH on the first row rather than "
            "assuming the published order and mapping the high BE/ME return onto the low E/P cell"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "FamaFrenchInternationalIndexReturns": {
        "fixture_document": lambda: _envelope(
            "F-F_International_Indices.zip", "Ind_all.Dat", _INDEX_ALL_LINES
        ),
        "client_seam": _SEAM,
        "query": {"index": "all", "frequency": "monthly", "measure": "usd"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body_b64"],
            "value": _archive_base64(
                "Ind_all.Dat",
                _line_replaced(_INDEX_ALL_LINES, _INDEX_REQUIRED_ANCHOR, _INDEX_NOT_REQD_ANCHOR),
            ),
        },
        "refusal_class": "FrenchProviderError",
        "refusal_code": "FRENCH_DAT_TABLE_NOT_FOUND",
        "tamper_reason": (
            "the same member publishes the dollar return series twice, once for the all-four-data-"
            "items-required series and once for the not-reqd series, and the query's "
            "all_data_items_required=True selects between those anchors; retagging the section "
            "with the other anchor keeps every number and both headers, so parse_dat_returns must "
            "find no candidate section and raise FRENCH_DAT_TABLE_NOT_FOUND rather than publish "
            "the not-reqd index history under the required-data reading the caller asked for"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
}
