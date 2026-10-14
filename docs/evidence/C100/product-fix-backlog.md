# Product fix backlog: Congress, BLS, FRED, and YFinance

As of 2026-10-13 UTC. This is a read-only backlog triage against frozen evidence, not a provider acceptance, a live-source result, or a change to the fixed-350 ledger. The reference 905-source snapshot is `docs/evidence/C100/model-current-retakes/product-release-20261012T2305Z/product-sources.json` (SHA-256 `a8eda002082e3bad76e3df9bc6bac58a3a9288befa79b24a2a4b40c9c25ee08d`). The relevant current hashes below were recomputed against that manifest.

No product code, tests, prior receipt, patch, or final-status ledger was modified. No product import, pytest, provider execution, database access, live request, OpenBB implementation read, or Intrinio algorithm-comment read was performed. Both proposed diffs were applied only to temporary copies to independently derive hypothetical after-hashes. The detailed machine-readable counterpart is [product-fix-backlog.json](product-fix-backlog.json).

## Recommended first bounded product fix: `CongressAmendments.number`

The pinned public contract requires `CongressAmendments.number` to be an integer. In the authorized offline R1 direct and central observations, both outputs were the string `"10"` while the comparator expected integer `10`; the missing-number case emitted an empty string and was recorded as a product gap. The source conversion uses `str(...)` for the field, which establishes the output-type mismatch independently of whether Congress sends a number or digit string. The source fixture is explicitly hand-authored, not an upstream capture, so this receipt does **not** prove the upstream wire type.

- Current source pins: [`congress_cboe_iter2.py`](../../../opendata/data/models/congress_cboe_iter2.py) `50a89ed72de77db1a66be0e936dff11dd497d23b9578fbd4b6651bd42727a98e`; `congress_gov/models/iter2.py` `d25992ef5a911a87f154a4169e86a96682313b560a3682d0ab84c780108bffb0`.
- Public-contract pins: `congress-eight-spec/spec.json` `7bd39be04287e6d1575ee046ab6b05fbce5b8f47fafb88aafb5395e125ab5fd0` and `acceptance-congress-cftc-eia/contracts.json` `97df59a9d628350e94e3c0a567b2c3efa3aea0dddbca257ab06d566f19bb4f78`. The official Amendment endpoint page gives numeric examples: [Library of Congress Amendment Endpoint documentation](https://github.com/LibraryOfCongress/api.congress.gov/blob/main/Documentation/AmendmentEndpoint.md).
- Existing R1 evidence: receipt SHA `aaf50c2ddb1fcc3c57bad7242cf9b0e2962ac39f4ae4972c35afc678028d9473`, cases SHA `89fbad1429c2f1ff6f365e10615cf7afb29d921ec452f1f33d9d0f195c5d8215`, fixture SHA `fc4cbf149ecf87220f339aa569c969fa770a729f2f48af225b06472805edde61`. The run had 35 total cases; `CongressAmendments` had 15, with 12 pass, 2 output-type failures, and 1 missing-value product gap. Run: `primary-authorized-diagnostic-r1-20261013t015247z`, 2026-10-13 01:53:31.685588Z–01:53:34.214541Z, exit 1, live `NOT_RUN`.
- Unapplied candidate: `docs/evidence/C100/quality-source-repairs/congress-amendment-number-type-proposal-r1-20261013T0203Z/congress-amendment-number.patch` SHA `e77b36d5673c825c58d623ce6d61fabe7891843c684bbe7c97d7cf17af903a50`. It changes the public field to `StrictInt`, parses the source field through the existing integer parser, and rejects absent/null instead of emitting an empty string. Applying the exact patch to temporary copies yielded model hash `71152ab3a24391db80cd0418f8505741c6356c05256a655f58b18d32f5e97829` and provider hash `c6c6d352f1ae1e1894ae830f60bfb3fcfea694b7a4180379632b553448fb8c55`; these are proposed hashes, not current product files.

A bounded follow-up should test, on the real Fetcher and central service path, integer `10` serialization, digit-string conversion if retained, missing/null rejection, and rejection of bool, float, and non-decimal text. Assert the returned JSON contains a numeric value, not a quoted string. Keep amendment-type casing as a separate finding. After any authorized change, create a successor 905/source binding and independent receipt; retain the R1 failure as history.

## BLS: clarification proposal, not a demonstrated runtime bug

The agreed compatibility rule is that date aliases select the inclusive calendar years containing the bounds; month and day do not clip returned observations. The BLS API documents `startyear`/`endyear`, while the cited API references do not establish day-level filtering. The BLS time-period page is survey-specific and cannot establish a universal date anchor. `M13` and `A01` are source period labels, not evidenced exact dates.

The existing offline `BlsSeries` receipt (`3beeeb7c277675588856a630c4a1f9c7e2c01bd0e6456a7e2ed50b14cd00907c`) maps a December-only 2013 request to `startyear=endyear=2013` and returns a hand-authored synthetic 2013/M11 row. That supports whole-year selection; it does not prove exact-window semantics or establish a product bug.

The unapplied proposal `clarify-bls-year-alias.patch` has SHA `245dc7bb4e1a0a406cc1ad78215e73e4a960e31709ad17fa0ddc190a4dc32a14`. It updates query/observation descriptions and the progress note. It should be treated as a contract clarification, not a behavior repair. Because Pydantic field descriptions affect generated schema metadata, review that intended metadata delta if the patch is approved. Temporary-copy after-hashes are recorded in the JSON. Exact day/month filtering remains `UNRESOLVED`.

## FRED calendar and release table: unresolved discrepancy, no patch basis

The V4 public-contract review says `EconomicCalendar.date` is a release/publication date and fields missing from FRED releases/dates remain nullable. It says `FredReleaseTable.release_id` is a string and flattened tree values/dates must only be populated where the nested source provides them; do not invent an observation date for a non-series node. See `field-contract-review.md` SHA `cb22c20e98959b99dae1822146092d9a7bd8b8af6cd91275f231d56e14a798c6` and the linked [EconomicCalendar](https://docs.openbb.co/odp/v4/python/data_models/EconomicCalendar) / [FredReleaseTable](https://docs.openbb.co/odp/v4/python/data_models/FredReleaseTable) V4 pages.

Historical synthetic evidence is mixed but not enough to justify a current-source fix:

- Direct run `results/run-state.json` SHA `3f41411a859d1b3649b13c54c7741394cbf3f7c1586c4b3336543d59eac4c816`, at `2026-10-12T08:24:29.509584Z`, exited 2 because central was not ready; both identities individually passed direct probes at source hash `0dbc2d088331a9a08138631e3076a456ee0f5b73b06d5143c676bcc39662ab4b`.
- Central run `central/run-state.json` SHA `d9b543594a40ef12fe48780fdb4d52755ec688bb35848ea4b51ec8aceef3d5b4`, at `2026-10-12T12:02:49.529686Z`, exited 2/overall `PARTIAL` because a different model was unsupported. `EconomicCalendar` and `FredReleaseTable` each passed all seven model cases, including invalid query, at source hash `0dbc2d...`; fixtures were synthetic and live was `NOT_RUN`.
- A later FRED36 receipt reports `EconomicCalendar` assertion `calendar latest-vintage selector differs from fixture date` and `FredReleaseTable` assertion `central invalid query was accepted`. Its frozen failure index (`864e32bb9e2b83715b24b27abbc230322b057b4e5de46d275b1c87108925934e`) has no exact failing query or detailed observed/expected values in those nested receipts; both outer wrappers are `IsolationViolation`, `source_verified=false`. A prior central invalid-query case explicitly used `release_id="53x"`, returned `ProviderModelQueryValidationError`, and recorded zero transport sends. The two runs cannot be reconciled from their surviving receipts.
- Current 905 source hash for `opendata/data/providers/fred/models/iter2_auxiliary.py` is `1850d4c83f1dc18c2ddc86dd49db43d48cd65ba2082927133e97d30bd222deb4`, different from the historical direct/central source pin.

Disposition: `UNKNOWN`, not a confirmed source bug and not a clean current-source pass. Do not patch from the unqualified assertion. If necessary, do a small current-hash retake that persists the exact query, expected and observed values, and transport count; keep old receipts unchanged.

## FRED Manufacturing Outlook NY/Texas: historical focused pass, current hash not rebound

The focused two-model probe at `2026-10-12T12:08:23.727336Z` exited 0. Each model had 14/14 case statuses pass, including direct and central public examples, topic trimming/deduplication, invalid query, missing grant, principal denial, and domain semantics. The receipts are `e3f9001f4b07881bee544009bb61f12a69154f295de6dce87ca1b1ddf2f284f6` (NY) and `6a50b8b6abbf18a72ab42b208a6af9ca22d5bd1b989a0a2f5f01536ee4d837b8` (Texas), using synthetic fixtures. They pin `iter2_macro.py` `e2751a4f621154f31679854b2ba776507e3e01a333f711f0e347b6b45719cfa5` and typed model `746b500ad736c564237f061de673114d6160d77e6696ae6b51f64a3271dc4e98`.

The frozen 905 current fetcher hash is `6619326930a71a4652b0f6eb2c9ab4cc92d690754a8e8f6c910d5dd102b70dba`; it differs from the historical pass. An earlier broad attempt was partial, while the later focused slice passed at the earlier hash. Do not treat either as a current-source defect or pass. No patch candidate is supported; only rebind the two-model offline probe if current-source verification is needed. Live FRED remains `NOT_RUN`.

## YFinance zero-quota SDK boundary: historical pass, current hashes differ

The offline run `20261012T103058Z` reports `OFFLINE_ACCEPTANCE_PASS_WITH_2_GAPS_LIVE_NOT_RUN` for a 29-model denominator. Six zero-quota cases—`BalanceSheet`, `CashFlowStatement`, `HistoricalDividends`, `IncomeStatement`, `KeyExecutives`, and `OptionsChains`—each raised `ProviderModelQueryBudgetError` before any logical SDK-boundary method call, with zero budget attempts and zero source attempts. The run began at `2026-10-12T10:30:58Z` and ended at `10:30:59.694867Z`; receipts are in `case-receipts/zero-quota-sdk-boundary-*.json`.

This only verifies the logical SDK method-call seam. The run explicitly leaves SDK-internal HTTP retries/hosts `NOT_VERIFIED`, with live provider calls and database use `NOT_RUN`/`NOT_USED`. As-run hashes for the query model, `_budget.py`, `_sdk.py`, and `models/iter2.py` were `c3573f7f…`, `99dd1088…`, `e5a2ef3e…`, and `e21e18ff…`; current 905 pins are `7f7091eb…`, `d1e2a313…`, `08b915e3…`, and `c7341d4a…`. Therefore this is historical seam evidence, not current-source acceptance. No product fix is indicated; if needed, rerun only the six bounded zero-quota cases against current pins and keep the HTTP-level boundary explicitly unverified.

## Suggested ordering

1. Ask the root to authorize the narrow Congress patch; its output-type discrepancy is the only clear product-code fix supported by this review.
2. Keep BLS as a separate, optional contract/schema-description decision. Do not claim exact-day semantics from the year-only API.
3. Do not spend a code-fix slot on FRED or YFinance from the records here. If current confidence is required, authorize small source-hash-rebound offline retakes with precise inputs and provenance.

All listed provider probes are offline synthetic or historical and `live=NOT_RUN`. This backlog does not mark any fixed-350 identity accepted, source-verified, or final.
