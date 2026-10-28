# Stock-daily history gap reconciliation

This page describes the offline reconciler in `opendata/pipeline/stock_daily_reconcile.py`.
It joins the existing single-series date evaluator with the alias-pinned request planner so an
operator can compare a frozen publication against dates when one exact security/source pair was
expected to publish a daily bar.

The module accepts only explicit in-memory evidence. It does not read a database or publication
file, call a provider, create records, enable a scheduled job, expose a REST endpoint, or authorize
dispatch. Its candidates are review records containing date windows and pinned source aliases.

## Evidence required

Each input identifies one `dataset_id`, logical `source_id`, and canonical `instrument_id`.
Those three values must match across the coverage request, publication snapshot, expected-observation
evidence, terminal-boundary evidence, source retrieval evidence, and backfill-plan key. A mismatch
is rejected; rows from another source or security cannot be silently counted toward coverage.

The evidence has distinct jobs:

| Evidence | What it establishes | Missing or unresolved result |
| --- | --- | --- |
| Instrument reference start boundary | The independently evidenced first date for the security | `UNKNOWN`; no full-history claim or candidate |
| Calendar identity, version, and coverage | The trusted date calendar spans the requested interval | `UNKNOWN` |
| Instrument/source expected-observation dates | A row is expected for that exact security and source on each listed date; the list accounts for suspensions and lifecycle | Exchange sessions alone remain diagnostic; no confirmed gap or candidate |
| Pinned publication ID and SHA-256 | The observation dates came from one frozen publication with exact dataset/source/security identity | Missing publication remains `UNKNOWN`; mismatched rows or identity are rejected |
| Active-through or last-expected-observation evidence | The independent end boundary covers the requested history | `UNKNOWN` |
| Source retention bounds and evidence | The source's known retention interval includes a missing date | Unknown retention blocks completeness and candidates; excluded dates are source-limited |
| Per-security source capability | The source can query the exact provider symbol and date interval under a bounded window | No candidate is emitted |
| Existing backfill plan | One unambiguous alias-pinned route covers the candidate date | Missing, ambiguous, or blocked routes remain unplanned |

The calendar passed to `HistoryCoverageRequest` must match the instrument-specific evidence's
calendar ID, version, evidence reference, and expected date set. Dates with unresolved suspension,
lifecycle, or source publication semantics are listed separately. They make the series status
`UNKNOWN` and are never treated as expected missing bars.

References are evidence pointers supplied by the caller. The reconciler checks that identities and
references are present and consistent; it does not verify signatures or inspect the referenced
documents. Reviewers must independently inspect the pinned source artifacts before relying on a
`COMPLETE` status or acting on a candidate.

## Input work budget

Each individual evidence date sequence retains its existing 100,000-entry limit, and one call may
contain at most 10,000 series. The reconciler also caps the combined date-entry count at 5,000,000.
It counts every entry in the request's expected and observed dates, the publication's observed dates,
and the instrument expectation's expected and unresolved dates; repeated dates in separate evidence
objects count separately because they consume separate validation and report work. The count is
accumulated as each series is yielded, before the complete input tuple is materialized or series are
sorted. Exceeding the cap rejects the whole call with `ValueError`; no series or dates are silently
truncated. The report records both the applied cap and the accepted entry count. Larger workloads
must be submitted in explicit batches, with batch boundaries and completeness aggregation handled
by the caller; a complete batch does not attest the full A-share universe.

Plans have separate whole-call caps of 50,000 instrument rows and 250,000 chunks. The reconciler
builds each source plan's instrument index once and reuses it for matching series. For each
instrument, chunks must already be ordered by start date and non-overlapping; otherwise the whole
call fails with `ValueError` rather than falling back to a potentially expensive scan. A date lookup
uses binary search over that validated sequence. Total matching work is capped at 10,000,000 units;
one unit is charged for each series-to-instrument index lookup, each provider symbol inserted into
the per-series membership index, each binary-search comparison, each chunk membership/alias
validity check, and each plan blocker inspected for a candidate date. Exceeding any plan or matching
cap raises `ValueError`, so no partial report can be mistaken for complete reconciliation. The
report records plan totals and matching work. These limits apply after plans have been built and
passed in; the backfill plan builder's own instrument/chunk generation cost is a separate follow-up
and is not included in this budget.

## Result semantics

`diagnostic_gap_dates` are the dates missing from the expected-date list supplied to the generic
coverage evaluator. They are not a finding that the security should have traded. Only
`confirmed_expected_gap_dates` are supported by both a pinned publication and the exact
instrument/source expected-observation evidence. If either of those evidence inputs is absent,
diagnostic dates stay in `diagnostic_gap_dates` and are not copied to `unplanned_gap_dates` or
candidate plans. `unplanned_gap_dates` is reserved for confirmed gaps that could not be safely
planned. Expected-observation evidence that places a date before the independently evidenced
`known_start_date` is rejected as a boundary conflict; it is never filtered into a complete result.

`head`, `interior`, and `tail` gaps retain their evaluator meanings. Duplicate and unexpected
publication dates remain visible in the nested coverage report. A `COMPLETE` per-series result
requires all expected dates exactly once, a complete calendar, a pinned publication, independently
evidenced start and end boundaries, no unresolved expected-observation dates, and bounded source
retention evidence. `SOURCE_LIMITED`, `PARTIAL`, `UNKNOWN`, and `NOT_APPLICABLE` are preserved where
their conditions apply.

The reconciler emits a candidate only if the gap date also lies inside the declared source retention
and query windows, the provider symbol is named in the exact source capability evidence, the current
backfill plan has one alias-pinned chunk for that date, and no applicable lifecycle or route blocker
remains. Candidate windows are grouped only within the same exact plan chunk, even if adjacent chunks
reuse the same alias metadata. The full inclusive query interval must fit inside both that chunk's
query bounds and its alias-validity interval. Candidate windows contain the exact missing expected
dates. Their inclusive query interval can span a weekend or a known exchange closure; no bar is
inferred for dates inside that interval. Window count and length are bounded, and windows are
generated incrementally without first materializing an unbounded intermediate list. Dates without a
candidate remain listed with a blocking issue when they are confirmed gaps.

`all_requested_series_complete` applies only to the exact caller-supplied series. The report always
sets `all_a_universe_complete` and `universe_census_attested` to false because a list of rows or a
partial registry cannot prove that every listed, delisted, and newly added A-share instrument is
present. This module also does not establish price adjustment bases, source contract rights, or
point-in-time semantics.

## Safe use checklist

- [ ] Build the publication and calendar inputs from frozen, independently reviewed evidence; do
  not derive a listing date, delisting boundary, or expected-bar date from the observed bars.
- [ ] Confirm any absent bar is expected for that instrument/source date. A market-open day alone is
  insufficient; resolve suspension and provider-specific omission semantics first.
- [ ] Keep each source separate. Do not combine observations from AkShare, Fuyao, or another vendor
  into one publication identity.
- [ ] Verify the exact plan, alias evidence, source retention, source capability, and source terms
  before using a candidate operationally. A SHA-256-shaped reference is a pin, not proof of source
  authenticity or license.
- [ ] Treat candidates as non-dispatching review artifacts. This module contains no scheduler or
  job activation path.
- [ ] Preserve `UNKNOWN` when evidence is missing or conflicting. Never interpolate, forward-fill,
  or fabricate a bar to make coverage appear complete.
- [ ] Do not use this per-series report as proof that the all-A universe is complete. Reconcile the
  instrument denominator and every source independently before making that claim.
- [ ] For verification, use the isolated test procedure approved by the integration owner and run
  only `tests/test_stock_daily_reconcile.py`. No test was executed while this implementation was
  being prepared.

