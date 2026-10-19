# Ledger-ruling audit: the 11 `ledger_ruled_out` residue rows

Instrument: `probes/w34_ledger_ruling_face.py` (wave 34), carrier `runs/w34_ledger_ruling_face.txt`.
Face line: `ledger_ruling_face arms=8 arms_passed=8 arms_failed=0 failed=[] residue=18
ledger_ruled_out=11 class_counts={'a': 2, 'c': 6, 'd': 3} residual_unexplained=0
contradicted_rulings=3 two_sided_controls=52 PASS=True` / `w34_ledger_ruling_face_exit=0`.

Nothing in `opendata/**`, `scripts/quality/**`, `docs/quality/**`, `tests/**` or any existing bundle
was touched. The ruled-out set is not typed here: it is the set the shipped axis measurer
(`incomplete_row_guard_check._ruled_out`) returns over the judged residue, and the carrier's `W`
line shows it equals the shipped census's `action=ledger_ruled_out` rows row-for-row
(`agrees_with_in_process=True`, `census_ledger_ruled_out=11`, `only_in_census=[]`, `only_here=[]`;
the census's own non-zero exit is its pre-existing red `B_landed_negative` arm).

## Classes, as the face decides them

* **(a) upstream/access fact** — the row's measured canonical need set contains a blocker the
  shipped taxonomy itself declares outside engine work (`outside.*`).
* **(c) in-repo structural choice** — blocked only by engine-work capabilities the registry reads
  as absent right now.
* **(d) prose only** — nothing blocks the row: the recomputable face reads *declarable* while the
  dict rules it out.
* **(b) rights/licensing** — 0 of the 11. No reason string in these five dicts is a rights claim.

| row | dict (path:line) | dict size | reason the dict records | class | measured blocker (canonical) |
|---|---|---|---|---|---|
| cboe::FuturesCurve | `opendata/data/providers/cboe/specs.py:316` (key :317) | 2 | "reviewed official All Access docs do not establish … protocol" | a | `outside.bespoke` + `flow.fan_out`, `flow.join` |
| cboe::IndexSnapshots | same dict (:319) | 2 | "No official endpoint/response mapping … evidenced in this slice" | c | `flow.branch` |
| econdb::CountryProfile | `…/econdb/specs.py:192` (:193) | 2 | documented v1 API has no country-profile mapping/units | c | `flow.fan_out`, `flow.join` |
| econdb::ExportDestinations | same dict (:200) | 2 | TEU volumes ≠ export values (metric semantics) | c | `columns.compose`, `flow.fan_out`, `path.assemble_single_row` |
| intrinio::OptionsSnapshots | `…/intrinio/specs.py:773` (:774) | 1 | names its mechanism: `flow.preflight_lookup + shape.columnar` + upstream file:line | c | `flow.preflight_lookup`, `shape.columnar` |
| seeking_alpha::CalendarEarnings | `…/seeking_alpha/specs.py:8` (:9) | 3 | client-side render, no machine-response schema | a | `outside.anti_bot` + `flow.fan_out` |
| seeking_alpha::ForwardEpsEstimates | same dict (:13) | 3 | "HTTP 403 without credentials" | c | `flow.fan_out`, `flow.preflight_lookup`, `path.object_keys` |
| seeking_alpha::ForwardSalesEstimates | same dict (:17) | 3 | "HTTP 403 without credentials" | c | same set as the EPS row |
| wsj::ETFActive | `…/wsj/specs.py:8` (:9) | 3 | "HTTP 401; no official response schema" — **one string shared by all 3 keys** (`dict.fromkeys`) | **d** | none: `request.serialised_param`, `rows.order_limit` both read **present** |
| wsj::ETFGainers | same dict (:9) | 3 | same string | **d** | none |
| wsj::ETFLosers | same dict (:9) | 3 | same string | **d** | none |

## Denominators (nothing here is an empty-input zero)

`Q_dict_denominators`: 11 ledger dicts across the tree; sizes
`cboe.NOT_DECLARABLE=2, congress_gov=8, econdb.NOT_DECLARED=2, federal_reserve=0,
intrinio=1, multpl=0, oecd=9, sec=24, seeking_alpha=3, tiingo=0, wsj=3`.
`ruled_out_rows_through_an_empty_dict=0`, `ruled_out_with_a_no_ledger_cause=0`,
`ruled_out_keyed_in_a_ledger_dict=11/11`, `census_keyed=11/11`, `residual_unexplained=0`.
The three empty dicts (`federal_reserve`, `multpl`, `tiingo`) rule out nothing, and the arm proves
the shape: `_ruled_out` applied through them returns `("", "")`, so an empty dict cannot fabricate a
refusal. 52 same-provider rows in those five dicts read **not** ruled out, and a forged model name
reads `("", "")`.

`Q2_provider_coverage` is where the *fabricated* risk actually lives: `seeking_alpha` and `wsj`
have `ruled_out=3/inventory_rows=3` — each dict enumerates the provider's entire row set, and
`wsj`'s three entries share **one** reason string (`distinct_reasons_in_dict=1`), so one prose
claim is doing the work of three decisions. `cboe` (2/11), `econdb` (2/8), `intrinio` (1/38) are
genuine subsets.

## The ruling the face contradicts

`declarable_face_says_otherwise=3: ['wsj::ETFActive', 'wsj::ETFGainers', 'wsj::ETFLosers']`. All
three carry a measured `census-*.json` row (the same 279-row population the roadmap instrument
loads), whose
whole need set — `request.serialised_param`, `rows.order_limit` — the registry reads **present**
with anchors on disk. So the engine is not the blocker for the wsj rows; the only thing between
them and a binding is an un-recomputed HTTP-401 claim about a live page. The same predicate reads
blocking sets non-empty for the other 8 rows, which is what makes the 3 a reading rather than an
artifact.

## The upstream half of every ruling is verifiable and does not say "absent"

`S_upstream_declared: rows_with_pinned_shape_sha_match=11/11
rows_whose_cited_line_names_the_model=11/11 rows_with_a_defined_upstream_fetcher=11/11
forged_model_reading=False/False`. Each row's `upstream_shape_file:upstream_shape_line` (pinned at
`upstream_commit=3e071fcc…`, the checkout HEAD) is the upstream provider's own registry line, e.g.
`"FuturesCurve": CboeFuturesCurveFetcher`, and the named fetcher class is defined in that package.
So no exclusion here can rest on "the endpoint/model does not exist upstream". Where the reasons
say "no official docs/protocol established", the honest reading is *vendor documentation*, not
*upstream implementation*: the upstream implementations exist and are readable, and the measured
blockers are the engine's declarable vocabulary (class c) or access/anti-bot (class a).

## Rights: what enforces `NOT_REGISTERED`, and what would have to change

Reading (`D_rights_gate`): registry §1 names cboe, econdb, intrinio; it does **not** name
seeking_alpha, wsj, biztoc, stockgrid. `provider-inventory.yaml` carries `rights_rows:` for cboe,
econdb, intrinio and `ABSENT_FIELD` for seeking_alpha, wsj, biztoc, stockgrid. All 11 ruled rows,
and biztoc::WorldNews / stockgrid::ShortVolume, carry `rights_status=NOT_REGISTERED` with an empty
`rights_evidence_ref` in `模型级任务清单.csv`.

Two mechanisms, neither of which is what excludes the 11:

1. `scripts/quality/openbb_inventory_plane.py` `RIGHTS LINK` (findings built only under
   `if served:`) fires only for a source that already serves registered capabilities. biztoc and
   stockgrid serve none (no package on disk), so their stop is the package/registry plane
   (`NO_PACKAGE_ON_DISK` → `new_provider_package`), not a rights gate.
2. `scripts/quality/acceptance_item_probe.py::measure_ac1_08 / judge_ac1_08` (AC-1|08) is the face
   that reads §1 of `docs/data-rights-registry.md`: row count, dated, undecided, unlinked
   (`条款链接` must be a single http(s) URL), responsible party, and the `rights_rows:` citations
   from the YAML that name no §1 source (`uncovered`).

Registration is **not a code change**. It is three documentation/data edits plus one owner
decision: (i) a new numbered row in §1 of `docs/data-rights-registry.md` with 数据源 / 接入方式 /
条款链接 / the three 允许… columns / 复核日期 (ISO) / 责任人 / 状态; (ii) `rights_rows: [<source name>]`
on the biztoc and stockgrid rows of `docs/proposals/openbb-migration/provider-inventory.yaml`;
(iii) `rights_status` flipped from `NOT_REGISTERED` on the `OBB2-biztoc-WorldNews` and
`OBB2-stockgrid-ShortVolume` rows of `docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv`,
with `rights_evidence_ref` pointing at the new §1 row. The decision owner is the named 责任人
(cloudQuant) with the review routed through the standing decision rows `RIGHTS2-biztoc` /
`RIGHTS2-stockgrid` in `docs/迭代计划/…/前置条件与决策清单.md` ("接入前逐产品三项结论；WP2-00/主线程审阅").
Nothing was registered by this audit.

## Bootstrap defect, one sentence

The shipped census and its sibling probes never put the repo root on `sys.path` (only their own
directory and `scripts/quality`), which is why they need `PYTHONPATH=<repo root>` — on this machine
the missing-`PYTHONPATH` run happens to still resolve `opendata` from the interpreter, so the face
prints the structural reading (`shipped_probes_insert_repo_root=False`,
`this_probe_inserts_repo_root=True`) instead of asserting the crash.

## What the owner should decide

* 6 class-c + 2 class-a rows: the ruling is evidenced but it is an *engine vocabulary* ruling, so
  the decision is whether to build `flow.fan_out` / `flow.join` / `flow.branch` /
  `flow.preflight_lookup` / `shape.columnar` / `columns.compose` / `path.object_keys` etc., or to
  keep the refusal.
* 3 class-d wsj rows: the ledger should not keep excluding them on a 2026 prose line — either a
  recomputable face (a captured 401 receipt plus an anti-bot/`outside.*` classification) or the
  ruling changes. `dict.fromkeys` over three identities should be replaced by three separate,
  separately-cited entries.
* biztoc / stockgrid: owner-side rights registration (above) plus a package decision; no code
  path can accept them today.
