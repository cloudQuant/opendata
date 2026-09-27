"""口径映射表的三类域级口径读数仪：导出面与加载器的拒绝面（AC-9|01, C53）.

判据问的是「六类口径由这张表承载」，所以读数必须来自这张表本身：`adjust`/
`suspension`/`denominator` 三个域级声明经 `mapping_as_json` 导出后逐个域打印，
再逐类删掉一个声明看加载器是否拒绝加载。后者是判据的反面：一个漏声明的域必须
fail closed，否则「表里写了才算」就成了空话。删掉的键还会额外补一个表不认的键
（`adjust_kind`），用来证明未知键同样不让过。

只读本机的 ``opendata/data/mappings/*.yaml``：不连库、不写仓库文件（探针文件写进
``TemporaryDirectory``，退出即删）。用法（PYTHONPATH=仓库根）：

    python docs/evidence/C53/caliber_face.py
"""

from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

import opendata.data.mapping as mapping_module
from opendata.data.mapping import DomainMapping, load_mapping, mapping_as_json, mapping_sources

CALIBERS = ("adjust", "suspension", "denominator")


def exported_calibers() -> None:
    """Print the three domain-level calibers of every shipped domain, as exported."""
    print("--- 1. mapping_as_json 导出的每域口径（表字段，非代码常量）---")
    for source in sorted(mapping_sources()):
        for domain, entry in sorted(load_mapping(source).domains.items()):
            payload = loads(mapping_as_json(entry))
            stated = " ".join(f"{name}={payload.get(name)!r}" for name in CALIBERS)
            print(f"  {domain:<20} {source:<8} {stated} fields={len(payload['fields'])}")


def exported_field_calibers() -> None:
    """Print one column's caliber (from/scale/normalize) on both legs."""
    print()
    print("--- 2. 逐字段口径导出样子（stock_daily.volume：ths 无换算 / akshare 手->股）---")
    for source in ("ths", "akshare"):
        entry: DomainMapping = load_mapping(source).domains["stock_daily"]
        volume = loads(mapping_as_json(entry))["fields"]["volume"]
        print(f"  {source}: {dumps(volume, ensure_ascii=False, sort_keys=True)}")


def minimal_domain_payload() -> dict:
    """Build a legal ``stock_daily`` payload from the shipped ths mapping.

    Returns:
        A dict of the domain's keys as the loader would accept them.
    """
    base = load_mapping("ths").domains["stock_daily"]
    fields = {
        name: {"from": spec.source_column}
        if spec.scale == 1.0
        else {"from": spec.source_column, "scale": spec.scale}
        for name, spec in base.fields.items()
    }
    declared = {name: getattr(base, name) for name in CALIBERS}
    return {"key": list(base.key), "fields": fields, **declared}


def refusal_face() -> None:
    """Drop one caliber at a time (and add an unknown key) and show the loader refuses."""
    print()
    print("--- 3. 加载器对缺一类口径声明的拒绝面（fail closed，现场调用）---")
    # Built before the mapping directory is swapped: the probe dir holds only the
    # probe file, so reading the shipped ths mapping after the swap would 404.
    minimal = minimal_domain_payload()
    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        original = mapping_module._MAPPINGS_DIR
        mapping_module._MAPPINGS_DIR = directory
        try:
            for dropped in (*CALIBERS, "unknown-key"):
                domain = dict(minimal)
                if dropped == "unknown-key":
                    domain["adjust_kind"] = "unadjusted"
                else:
                    domain.pop(dropped)
                payload = {"source": "probe", "domains": {"stock_daily": domain}}
                (directory / "probe.yaml").write_text(
                    yaml.safe_dump(payload, allow_unicode=True, sort_keys=True), encoding="utf-8"
                )
                load_mapping.cache_clear()
                label = {"unknown-key": "多一个表不认的键"}.get(dropped, f"去掉 {dropped}")
                try:
                    loaded = load_mapping("probe").domains["stock_daily"]
                    print(f"  {label}：未拒绝（缺陷）-> {loaded}")
                except (RuntimeError, ValueError) as exc:
                    print(f"  {label}：{type(exc).__name__} -> {exc}")
        finally:
            mapping_module._MAPPINGS_DIR = original
            load_mapping.cache_clear()


def main() -> int:
    """Run the three faces and report success.

    Returns:
        ``0`` when every face printed (a face that found a defect prints too).
    """
    exported_calibers()
    exported_field_calibers()
    refusal_face()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
