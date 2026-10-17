r"""Which of plane-1's 90 residue rows is a producer run able to reach, and which is not?

Wave 22 split the residue by *whether a provider had ever been produced* (83 UNREVIEWED rows in
10 never-produced providers, 7 INCOMPLETE rows in produced ones). That answers "has anyone run the
instrument here", not "can the instrument pass once run". A row can be produced and still refuse,
and the reason is a property of the binding, not of the evidence tree:

``bound``
    ``ProviderRegistry.resolve_model(provider, model)`` returns a fetcher at all.
``declared``
    the fetcher publishes ``row_model`` that is a ``ContractModel`` subclass -- the same predicate
    the C125 predicate used, and deliberately not ``row_model is not None``.
``frozen_module``
    ``row_model.__module__`` starts with ``opendata.data.models.``, which is the gate at
    ``iter2_claims_probe_runner.py:537``.
``is_domain_contract``
    ``row_model is contract_model(fetcher.capability.domain)``. This is the gate the *service* leg
    enforces through ``provider_model_query._serialize_contract_row`` and the one
    ``tests/test_federal_reserve_iter2_models.py:392-413`` guards: a fetcher that builds a
    look-alike class passes its direct leg and refuses on the service leg, so declaring that class
    would move the row from INCOMPLETE to REJECT-shaped rather than to accepted.
``witness_entry``
    the provider's shard on disk carries a validated model entry for this row.

Three arms, each one able to disagree with the census:

``A_controls``
    a landed row (``intrinio::ForwardSalesEstimates``) must read all-green on the five columns, and
    a row the cboe shard says is unbound must read ``bound=False`` with a named ``resolve_error``.
    An instrument whose positive control is red is measuring nothing.
``B_forced_declared``
    force ``declared`` for every bound row, both off and on. Off must land all ``produced`` rows in
    ``NEED_DECLARE`` with nothing left in a later bucket; on must empty ``NEED_DECLARE``. The arm
    first measures that at least one direction actually moves a row -- movability is compared
    against the unperturbed bucketing, not assumed -- because a control that cannot differ is a
    second false gate. On this residue the column does not vary (0 of 58 bound rows declare a
    class), so only the ``on`` direction can move, and that is what the arm proves.
``C_forced_domain_contract``
    reported, not gated: the service-leg column only has a denominator among rows that already
    declare a class, and the print says whether it could differ at all on the current residue.

The first draft of this probe ordered the buckets with ``witness_entry`` before the declaration
columns, and on this residue that swallowed every bound row into ``NEED_SHARD_ENTRY``: its
``is_domain_contract`` arm moved 0 rows and its own gate read ``0 == 0`` and passed. That is the
false-gate shape this instrument was built to avoid, so the bucket order now asks the binding
questions first and arm B is gated on movability.

Run from the repository root::

    cd /Users/yunjinqi/Documents/new_projects/opendata
    /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python \\
        docs/evidence/C128/plane1-residue-readiness-20261017/probes/readiness_census.py

It is a census: it exits 0 unless one of its own two arms fails.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))

import iter2_claims_fixtures as witnesses  # noqa: E402  # the repo's own shard loader

from opendata.data.domains import contract_model  # noqa: E402
from opendata.data.models.base import ContractModel  # noqa: E402
from opendata.data.providers.catalog import register_provider  # noqa: E402
from opendata.data.registry import ProviderRegistry  # noqa: E402

REPORT: Final[str] = "docs/evidence/C101/judge/claims.merged.report.json"
IDENTITY_SET_SHA256: Final[str] = "5168e1e723999312b0978bf9b1a8999c0b4960cdb12013e43e1e59e349e21e65"
LANDED_CONTROL: Final[str] = "intrinio::ForwardSalesEstimates"
UNBOUND_CONTROL: Final[str] = "cboe::FuturesCurve"
COLUMNS: Final[tuple[str, ...]] = (
    "bound",
    "declared",
    "frozen_module",
    "is_domain_contract",
    "witness_entry",
)


def _residue(report: dict[str, Any]) -> dict[str, str]:
    """Return the non-accepted identities with their judged status, straight off the report."""
    out = {}
    for row in report["models"]:
        if row["status"] != "OFFLINE_ACCEPTED":
            out[row["model_identity"]] = row["status"]
    return out


def _readiness(provider: str, model: str, domain_ok: dict) -> dict[str, Any]:
    """Measure the five columns for one identity without asserting anything about them."""
    face: dict[str, Any] = dict.fromkeys(COLUMNS, False)
    registry = ProviderRegistry()
    register_provider(provider, registry)
    try:
        fetcher: Any = registry.resolve_model(provider, model)
    except Exception as exc:  # the census reports the class, never re-raises
        face["resolve_error"] = type(exc).__name__
        return face
    face["bound"] = True
    row_model = getattr(fetcher, "row_model", None)
    face["row_model"] = getattr(row_model, "__name__", None)
    face["declared"] = isinstance(row_model, type) and issubclass(row_model, ContractModel)
    if face["declared"]:
        face["frozen_module"] = str(row_model.__module__).startswith("opendata.data.models.")
        domain = fetcher.capability.domain
        try:
            expected = contract_model(domain)
        except LookupError:
            face["domain"] = f"UNKNOWN_DOMAIN:{domain}"
            return face
        domain_ok.setdefault(domain, expected)
        face["is_domain_contract"] = row_model is expected
        face["domain"] = domain
    try:
        entry = dict(witnesses.model_entry(provider, model))
        face["witness_entry"] = bool(entry)
    except KeyError:
        face["witness_entry"] = False
    return face


def _bucket(face: dict[str, Any]) -> str:
    """Name the first job this row still has, in pipeline order.

    Binding quality is judged before shard presence: a row whose fetcher publishes no contract
    class has that job whether or not a shard entry exists yet, and a census that asked for the
    shard first could not see the declaration columns at all (the first draft of this probe did
    exactly that, and its ``is_domain_contract`` arm then had no row to move).
    """
    if not face["bound"]:
        return "NEED_BINDING"
    if not face["declared"]:
        return "NEED_DECLARE"
    if not face["frozen_module"]:
        return "DECLARE_NOT_FROZEN"
    if not face["is_domain_contract"]:
        return "LOOK_ALIKE"
    if not face["witness_entry"]:
        return "NEED_SHARD_ENTRY"
    return "PRODUCIBLE_NOW"


def _pair(identity: str) -> tuple[str, str]:
    """Split one ``provider::Model`` identity into the two arguments ``_readiness`` takes."""
    provider, _sep, model = identity.partition("::")
    return provider, model


def _faces(residue: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Measure every residue row once; the arms re-bucket these faces, they do not re-resolve."""
    domain_ok: dict = {}
    return {identity: _readiness(*_pair(identity), domain_ok) for identity in residue}


def _bucket_all(
    faces: dict[str, dict[str, Any]],
    *,
    force: str | None = None,
    value: bool = False,
) -> Counter:
    """Bucket pre-measured faces; ``force`` pins one boolean for every bound row."""
    buckets: Counter = Counter()
    for face in faces.values():
        view = dict(face)
        if force is not None and view["bound"]:
            view[force] = value
        buckets[_bucket(view)] += 1
    return buckets


def _sums_to(buckets: Counter, total: int) -> bool:
    """Refuse a bucketing that loses or invents a row."""
    return sum(buckets.values()) == total


def main() -> int:
    """Print the residue's readiness face, then gate the instrument on its own two arms."""
    report = json.loads((REPO_ROOT / REPORT).read_text(encoding="utf-8"))
    residue = _residue(report)
    landed = _readiness(*_pair(LANDED_CONTROL), {})
    unbound = _readiness(*_pair(UNBOUND_CONTROL), {})

    arm_a = all(landed[c] for c in COLUMNS) and not unbound["bound"]
    print(
        "A_controls: "
        f"landed={ {c: landed[c] for c in COLUMNS} } row_model={landed.get('row_model')} "
        f"unbound={ {c: unbound[c] for c in COLUMNS} } "
        f"resolve_error={unbound.get('resolve_error')} PASS={arm_a}"
    )

    total = len(residue)
    faces = _faces(residue)
    bound_faces = [face for face in faces.values() if face["bound"]]
    produced = len(bound_faces)
    col_counts = {c: sum(1 for face in bound_faces if face[c]) for c in COLUMNS}
    base = _bucket_all(faces)

    # Arm B forces ``declared`` in both directions. The arm is only a control if at least one
    # direction moves a row, so movability is measured against the unperturbed bucketing instead
    # of assumed from a column the population might not vary on.
    forced_declared_off = _bucket_all(faces, force="declared", value=False)
    forced_declared_on = _bucket_all(faces, force="declared", value=True)
    movable_declared = forced_declared_on != base or forced_declared_off != base
    arm_b = (
        movable_declared
        and _sums_to(forced_declared_off, total)
        and _sums_to(forced_declared_on, total)
        and forced_declared_off["NEED_DECLARE"] == produced
        and forced_declared_off["PRODUCIBLE_NOW"] == 0
        and forced_declared_on["NEED_DECLARE"] == 0
    )
    print(
        "B_forced_declared: "
        f"movable={movable_declared} base_need_declare={base['NEED_DECLARE']} "
        f"forced_off_need_declare={forced_declared_off['NEED_DECLARE']} "
        f"forced_on_need_declare={forced_declared_on['NEED_DECLARE']} "
        f"forced_on_buckets={dict(sorted(forced_declared_on.items()))} PASS={arm_b}"
    )

    # Arm C asks the same of the service-leg column. It can only differ among rows that already
    # declare a contract class, so its denominator is ``declared``, not the residue.
    movable_contract = 0 < col_counts["is_domain_contract"] < col_counts["declared"]
    forced_contract_off = _bucket_all(faces, force="is_domain_contract", value=False)
    print(
        "C_forced_domain_contract: "
        f"movable={movable_contract} base_look_alike={base['LOOK_ALIKE']} "
        f"forced_off_look_alike={forced_contract_off['LOOK_ALIKE']} "
        f"forced_off_producible={forced_contract_off['PRODUCIBLE_NOW']} "
        f"sums={_sums_to(forced_contract_off, total)}"
    )

    arm_a2 = _sums_to(base, total) and base["NEED_BINDING"] == total - produced
    expect_line = (
        f"residue={total} bound={produced} columns={col_counts} "
        f"buckets={dict(sorted(base.items()))}"
    )
    print(f"column_census: {expect_line} sums_to_residue={arm_a2}")
    arms = [arm_a, arm_b, arm_a2]

    per_provider: dict[str, Counter] = defaultdict(Counter)
    per_provider_buckets: dict[str, Counter] = defaultdict(Counter)
    for identity, status in residue.items():
        provider = identity.split("::", 1)[0]
        per_provider[provider][status] += 1
        per_provider_buckets[provider][_bucket(faces[identity])] += 1
    print(
        "residue_by_provider="
        + json.dumps({p: dict(c) for p, c in sorted(per_provider.items())}, ensure_ascii=False)
    )
    bucket_face = json.dumps(
        {p: dict(c) for p, c in sorted(per_provider_buckets.items())},
        ensure_ascii=False,
    )
    print(f"buckets_by_provider={bucket_face}")
    inventory = report.get("inventory", {})
    face_sha = str(inventory.get("expected_identity_set_sha256"))
    print(
        f"identity_set={face_sha[:16]} expected_match={face_sha == IDENTITY_SET_SHA256} "
        f"inventory_keys={sorted(inventory)[:8]}"
    )
    passed = sum(1 for ok in arms if ok)
    print(
        f"readiness_arms={len(arms)} readiness_passed={passed} "
        f"readiness_failed={len(arms) - passed}"
    )
    return 0 if passed == len(arms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
