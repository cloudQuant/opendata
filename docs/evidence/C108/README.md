# C108 — 声明式 engine 的五相冒烟（benzinga::PriceTarget）

日期：2026-10-22 入档（捕获本体产出于 engine smoke 轮，早于本仓迭代 2 的落地波次）。

这一包只有 11 个文件，全部是可复算产物，没有叙述档，所以 `evidence_traceability` 把它记成
`docs/evidence/C108/#narrative`。本文件就是那份叙述，内容逐条取自包内已提交的载体。

## 人口与构成

| 目录 | 内容 |
| --- | --- |
| `captures/benzinga/PriceTarget/` | 五相 stdout+stderr 落盘：`contract-exit0.txt`、`direct-exit0.txt`、`service-exit0.txt`、`implementation-exit0.txt`、`counterfact-exit0.txt` |
| `fixtures/benzinga/PriceTarget.json` | 该行的 wire 形状 fixture（见证读取的对象，不是构造的返回值） |
| `receipts/benzinga/PriceTarget/` | 五相 receipt 各一份 |

## 载体上写着的判定

`captures/benzinga/PriceTarget/service-exit0.txt` 末三行逐字为：

```
service: query_provider_model executed for real (registry + reviewed domain semantics), rows=1 first_row_sha256=1b0527dc703e94f7
VERDICT=PASS
--- stderr ---
```

即：这一相不是静态断言，而是真的走了一次 `query_provider_model`（经 registry 与已审阅的域语义），
返回 1 行，行的 sha256 前缀 `1b0527dc703e94f7` 落在那份 fixture 上；`VERDICT=PASS`。

## 复算

```bash
python scripts/quality/iter2_claims_probe_runner.py --provider benzinga --evidence-root <本包外的工作根>
```

`counterfact-exit0.txt` 是同一行的拒绝臂：篡改 fixture 后 runner 必须拒，且拒绝码与见证声明一致。
本包不主张该行已在平面 1 验收——平面 1 的逐行状态以 `docs/evidence/C101/judge/claims.merged.report.json`
为准（现读 349/350）。
