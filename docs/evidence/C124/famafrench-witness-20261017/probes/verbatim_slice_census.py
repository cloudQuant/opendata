r"""Recompute every famafrench witness slice against the live captures it claims to be cut from.

The shard at ``scripts/quality/iter2_witnesses/_parts/famafrench/official_archive_rows.py``
publishes archive bodies as verbatim source lines. That claim is only auditable if each line tuple
is a contiguous window of the member the provider actually downloads, so this probe re-derives it
from the retained capture bytes: it pins each archive's SHA-256, decompresses the named member,
searches for the slice's first line, and requires the whole window -- padding and ``-99.99``
sentinels included -- to match at that offset. It then decodes the ``body_b64`` the producer
materializes and requires those same lines back, so the fixture bytes, the shard literals and the
official capture are one fact rather than three claims.

Run it against the stage directory the captures were downloaded into::

    PYTHONPATH=. python \
        docs/evidence/C124/famafrench-witness-20261017/probes/verbatim_slice_census.py \
        --stage /tmp/french_stage

Exits 0 only when every model prints ``archive_sha256_ok=True``, ``slice_verbatim_match=True``,
``carrier_member_match=True`` and ``url_contains_match=True``; a missing archive prints
``capture_absent`` and exits 1, because an unmeasured slice is not a passing slice.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[5]
SHARD_REL: Final = "scripts/quality/iter2_witnesses/_parts/famafrench/official_archive_rows.py"

#: provider model -> (archive file, member inside it, archive SHA-256 measured at capture time).
CAPTURES: Final[dict[str, tuple[str, str, str]]] = {
    "FamaFrenchBreakpoints": (
        "ME_Breakpoints_CSV.zip",
        "ME_Breakpoints.csv",
        "bec083a0d5c4ee4f508e2dbd715d09c57d979961d0ad939e6a2202b33986f657",
    ),
    "FamaFrenchFactors": (
        "F-F_Research_Data_Factors_CSV.zip",
        "F-F_Research_Data_Factors.csv",
        "593f4fbef03181bc0b22ff6292f217689dd1fb79355049ca905d95b262040a66",
    ),
    "FamaFrenchUSPortfolioReturns": (
        "6_Portfolios_2x3_CSV.zip",
        "6_Portfolios_2x3.csv",
        "82b66ccd6a4dff103eba51b60e1f2310eaa94bbebbb843550173b972c64cdbff",
    ),
    "FamaFrenchRegionalPortfolioReturns": (
        "Developed_6_Portfolios_ME_BE-ME_CSV.zip",
        "Developed_6_Portfolios_ME_BE-ME.csv",
        "a0871d77732fcb80cd75b640c375a40557e51777c4497db0570ddac9476e6f4a",
    ),
    "FamaFrenchCountryPortfolioReturns": (
        "F-F_International_Countries.zip",
        "UK.Dat",
        "6041ff104726d6aadfea76c4ef25af35594e799b90064b76b6bb83bd4acb0091",
    ),
    "FamaFrenchInternationalIndexReturns": (
        "F-F_International_Indices.zip",
        "Ind_all.Dat",
        "6292f7eadfbd7f7dbe9a3fd08d4181f59509973ee4a222040c34b0e4223a687b",
    ),
}


def _shard_models() -> dict[str, Any]:
    """Import the witness shard by location so this probe never re-declares a body."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("french_shard", REPO_ROOT / SHARD_REL)
    if spec is None or spec.loader is None:
        raise SystemExit(f"{SHARD_REL}: no importable spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules["french_shard"] = module
    spec.loader.exec_module(module)
    models: dict[str, Any] = module.MODELS
    return models


def _strip_newlines(raw: bytes) -> list[str]:
    return [line.rstrip("\r\n") for line in raw.decode("utf-8-sig").splitlines(keepends=True)]


def _member_lines(stage: Path, archive_name: str, member_name: str) -> tuple[list[str], str]:
    payload = (stage / archive_name).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        raw = archive.read(member_name)
    return _strip_newlines(raw), digest


def _carrier_lines(document: dict[str, Any]) -> tuple[list[str], str]:
    """Decode the fixture's base64 ZIP and return the member's lines plus the member name."""
    spec = document["responses"][0]
    payload = base64.b64decode(spec["body_b64"], validate=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        raw = archive.read(names[0])
    return _strip_newlines(raw), names[0]


def main(argv: list[str] | None = None) -> int:
    """Census every pinned slice and exit 0 only when all four verbatim faces hold."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, help="directory holding the captured archives")
    parser.add_argument("--fixtures", default="docs/evidence/C101/fixtures/famafrench")
    args = parser.parse_args(argv)
    stage = Path(args.stage)
    if not stage.is_dir():
        print(f"stage_dir_absent={stage}")
        return 1

    models = _shard_models()
    if sorted(models) != sorted(CAPTURES):
        print(f"model_set_mismatch shard={sorted(models)} pinned={sorted(CAPTURES)}")
        return 1

    all_match = True
    for model in sorted(CAPTURES):
        archive_name, member_name, expected_digest = CAPTURES[model]
        path = stage / archive_name
        if not path.is_file():
            print(f"{model:40s} capture_absent={archive_name}")
            all_match = False
            continue
        lines, digest = _member_lines(stage, archive_name, member_name)
        digest_ok = digest == expected_digest
        declared = models[model]["fixture_document"]()["responses"][0]["url_contains"]
        carrier_path = REPO_ROOT / args.fixtures / f"{model}.json"
        carrier: list[str] = []
        carrier_note = "carrier_file_absent"
        slice_ok = member_ok = url_ok = False
        offset = -1
        if carrier_path.is_file():
            document = json.loads(carrier_path.read_text(encoding="utf-8"))
            carrier, carrier_member = _carrier_lines(document)
            member_ok = carrier_member == member_name
            url_ok = declared in archive_name
            if carrier and carrier[0] in lines:
                offset = lines.index(carrier[0])
                slice_ok = len(carrier) > 1 and lines[offset : offset + len(carrier)] == carrier
            carrier_note = f"carrier_file={carrier_path.relative_to(REPO_ROOT)}"
        else:
            carrier = []
        print(
            f"{model:40s} archive_sha256_ok={digest_ok} member={member_name} "
            f"member_total_lines={len(lines)} carrier_lines={len(carrier)} "
            f"slice_offset={offset} slice_verbatim_match={slice_ok} "
            f"carrier_member_match={member_ok} url_contains_match={url_ok} {carrier_note}"
        )
        all_match = all_match and digest_ok and slice_ok and member_ok and url_ok and bool(carrier)
    print(f"models_censused={len(CAPTURES)} VERBATIM_ALL_MATCH={all_match}")
    return 0 if all_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
