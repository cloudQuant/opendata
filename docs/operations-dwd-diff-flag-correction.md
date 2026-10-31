# DWD `_diff_flag` 口径与历史差异

本文按当前代码与《研发设计文档》§8.2–8.3 记录 `_diff_flag`、修订重算和 `dq_diff_report` 的边界。设计文档是产品契约；历史验收档案描述的是各自提交时的实现与读数，不能覆盖当前契约。

## 当前 DWD 口径

- 当前服务的普通合并路径中，单源直通为 `_diff_flag=0`。多源有同一业务键时按权威顺序选值；源值在比较规则下相同，标记为 `0`；值不一致，标记为 `1`，不改变权威值。不能数值化的文本精确比较；可数值化的值（包括数值字符串）按相对阈值比较，当前 `dwd_merge._differs` 使用 `1e-4`。独立传入 `extra_diff_keys` 是显式覆写，另按下文处理。
- 权威源缺少某键而低优先级源有该键时，DWD 使用低优先级行并在 `source` 留下实际来源。当前合并器将其计作降级行，但不会仅因一侧缺键就将该键标为差异。
- `affected_keys` 是重算范围：有当前来源的 mapping 时，将源键规范成契约键；没有 mapping 时保留原键，不猜测转换。随后把键传给各源 reader，以便窗口以外的修订行也能重新合并。它不是差异事实。当前 `DwdMergeService` 将重算结果交给 `merge_source_frames` 时不把 `affected_keys` 传作 `extra_diff_keys`；相同源值被重新计算后仍为 `0`。`merge_source_frames` 将 `extra_diff_keys` 与源值比较得到的差异键取并集，对并集中且本轮有输出的键置 `1`。该参数是独立的显式标记输入，不应拿修订键集合代替它。

上面是当前行为读数，不表示历史行已按此重算。单侧缺键与数值容差规则仍须分别看待：DWD 的降级留痕不等于 `dq_diff_report` 的 `missing` 记录；DWD 当前固定的 `1e-4` 数值阈值也不等于交叉校对使用的映射阈值（缺省 `1e-6`，并可按字段配置）。两处规则若需统一，应作为单独的口径变更评审，不能由本说明推定已经统一。

## `dq_diff_report` 的边界

交叉校对以两源业务键并集为比较范围。只在一侧出现的键记为 `missing`；两侧都有键时逐字段比较。可数值化的值（包括数值字符串）按该域映射中的相对容差判定；不能数值化的文本精确比较。`DiffSummary.deviation_count` 按超容差的字段差异事件计数，`missing_count` 按单侧缺键计数，`diff_rate = (deviation_count + missing_count) / compared_keys`。同一业务键有多个不一致字段时会重复计入，因此该比例可能超过 100%，不能解释成唯一差异键占比。`DiffSummary` 的缺键与字段差异读数、以及报告中按批次保存的抽样明细，描述的是交叉校对结果；它们不是 DWD `_diff_flag` 的逐行副本。报告明细默认最多抽样 100 条，因此报告行数也不能反推 DWD 中差异行数。

## 历史记录说明

历史 C48/C52 档案保留了相反的行为描述：C48 记录过同值的 `affected_keys` 经当时的额外打标路径后被强制置为 `1`；C52 同一档案一处把受影响键传播进 `extra_diff_keys`（这意味着强制标记），另一处却把 `_diff_flag` 描述为仅在源值不一致时置 `1`。两份记录只能作为各自历史版本的实现/验收快照；尤其 C52 内部这两种说法本身不一致，不能拼接成当前规则。当前应以《研发设计文档》§8.3 和现行 `opendata/pipeline/dwd_merge.py` 为准；当前回归用例也覆盖了“同值修订重算保持 `0`”及“单源修订保持 `0`”。

既有错误标记不会因代码或本说明更新而自动修复。后续若要纠正存量数据，需先执行有界的双源重算，核对业务键、源覆盖、重算前后 `_diff_flag`、降级来源及对应校对结果，再确认范围内的写入结果；不得把未验证的历史 `1` 批量清零，也不得由 `dq_diff_report` 抽样行数代替 DWD 全量核对。

## 依据

- `docs/迭代计划/迭代1-重构数据中台/研发设计文档.md` §8.2–8.3。
- `opendata/pipeline/dwd_merge.py`：`merge_source_frames`、`DwdMergeService._run_batch`、`DwdMergeService._contract_keys`、`_disagreeing_keys`、`_differs`。
- `opendata/pipeline/cross_check.py`、`opendata/pipeline/diff_report.py`。
- 历史快照：`docs/evidence/C48/README.md`、`docs/evidence/C52/README.md`。
