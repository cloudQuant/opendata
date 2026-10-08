# C65 搬运层安全审阅与保留债务

日期：2026-09-30。状态：**用户委托 Codex 主智能体的当前技术审阅已完成；安全债务保留，未宣称漏洞已修复。**

下文 §1–5 保留前期审阅输入（Bandit 1.9.2 / 1036项），当前结论以 §6、`ported-bandit-scan.json` 与 `ported-security-triage.json` 为准。空白签名格是历史材料，不再作为等待人工审阅的阻塞；本次 AI 审阅不是 cloudQuant 个人签名，也不是供应方授权。

范围是 C64 草稿中尚未 triage 的五个 Bandit finding，以及当前新增 `opendata_http/futures_derivative/` 子树的所有 Bandit finding。另记录这 12 个文件的 Ruff 债务。本文不包含源代码片段、不更改搬运代码、扫描阈值、lint 基线、验收判据或台账。

## 1. 扫描输入与计数边界

- 当前 `opendata_http/manifest.json` 有 325 个 Python 文件和 2 个资源；`opendata_http/upstream.lock` 与 `docs/port-report.md` 各有 327 条记录。327 是总记录数，325 才是 Python 文件数。`futures_derivative` 的 12 个 Python 文件占当前搬运 Python 文件的 12/325 = **3.69%**。
- 当前快照的只读静态扫描使用 Anaconda `base`（Python 3.11.8）：Bandit 1.9.2，命令为 `conda run -n base bandit -q -r opendata_http -f json -o /tmp/c65-bandit.json`；Ruff 0.16.2，命令为 `conda run -n base ruff check opendata_http --output-format json --output-file /tmp/c65-ruff.json`。Bandit 未传 `bandit.yaml`，因为该文件排除整个 `opendata_http/`，本次需扫描搬运树。两条命令因发现项返回非零；Bandit 报告 0 个扫描错误。没有跑测试、数据库或 provider。
- `/tmp/c65-bandit.json` 与 `/tmp/c65-ruff.json` 是本机私有原始输出；原始扫描负载可能包含代码上下文，不复制进本仓库。本文仅保留 finding 元数据、定位和人工审阅摘要。
- 当前 Bandit 扫完整 325 个 Python 文件，记录 **1,036 findings / 15 条规则**；其中 `futures_derivative` 有 27 条：B113=22、B314=2、B405=2、B112=1。当前全树 B113=929，新增子树占 22/929 = **2.37%**。
- 对照 C61 原始扫描：当时为 313 个 Python 文件、1,021 条 finding、Bandit 1.9.4，其中 B113=907；当前扫描器是 1.9.2，且源码面增加了 12 个文件，不能用总 finding 数的净差直接判断回归。B113 的子树外当前数为 907，当前新增子树有 22 条 B113。A2 最初 131 个文件的 raw scan 是 328 条 finding，其中 B113=267；这些分母不可混作一个版本。
- 当前 Ruff 全树有 9,878 条诊断；新增 12 文件有 **144** 条：S113=22、S314=2、S112=1、其余 119 条（D205=28、D212=28、D415=28、E501=21、I001=1、UP009=12、B905=1）。S113/S314/S112 与上表 Bandit 项是重叠提示，不另算三类风险。Ruff 144 是当前选择规则下的描述性债务，不等于 AC-17|05 的 E/F 单项读数，也没有据此调整其门槛或快照。

## 2. C64 未 triage 的五项

当前重扫仍在原位置触发。C64 摘要与当前判定可由 [C64 草稿](../C64/ported-security-review-draft.md)、[C61 原始扫描](../C61/ported-bandit-scan.json) 和当前源文件对读。

| Finding（文件:行） | 严重度/置信度 | 实际输入、可控性与影响 | 最小处置建议 |
|---|---|---|---|
| B403 `opendata_http/futures/cons.py:13` | LOW / HIGH | 这是 `pickle` 的导入告警；直接导入不反序列化数据。实际危险调用是同文件的 B301。AST 只读检索未发现仓库内 `get_pk_data` 调用，当前仓库也无 `.pkl` 资源；外部 Python 使用者是否调用未知。 | 与 B301 一起决策；不要把单纯 import 记作独立可利用路径，也不要因仓库内无调用就自动标成误报。 |
| B301 `opendata_http/futures/cons.py:576` | MEDIUM / HIGH | `get_pk_data` 在 :569 接收 `file_name`，传入仅做路径拼接的 `get_pk_path`（:557、:575），随后直接 `pickle.load`。该 helper 内无 allowlist 或路径 containment 检查；绝对路径或 `..` 可脱离资源目录。仓库内无调用点和随包 pickle 文件，因此没有证据表明存在 HTTP 入口利用；若外部调用者能传入路径，恶意 pickle 可在加载时执行代码。 | 由 cloudQuant 确认该 helper 是否属于需兼容的外部 API。若保留并允许调用，按有限资源名 allowlist 并将规范化路径限制在固定资源根目录；可用 JSON 资源替换时先确认行为兼容。签认前维持现状、保持 finding。 |
| B324 `opendata_http/stock_feature/stock_a_indicator.py:50` | HIGH / HIGH | :49–:52 以当前日期字符串更新 MD5 对象并生成请求 token；源代码路径没有秘密输入。是否只是上游兼容字段、是否参与授权/防重放，远端协议未知，不能仅凭无密钥就认定无安全用途。 | 取得该接口 token 的协议说明或人工确认。若确认只用于非安全兼容字段，再考虑以 `usedforsecurity=False` 显式说明用途（保持摘要算法与输出）；否则需要接口级方案。不得先改算法或仅加抑制标记。 |
| B324 `opendata_http/stock_feature/stock_info.py:36` | HIGH / HIGH | `_cls_signed_params` 对查询串计算 SHA-1；C64 源码审阅记录该串为当前固定查询参数，symbol 后续用于筛选。调用输出进入远端请求参数；未见密钥，但“签名”字段在对端的安全语义未证实。 | 与 :37 成对向 cloudQuant 确认 CLS 协议及服务端用途；在确认前不换算法、不消除 finding。 |
| B324 `opendata_http/stock_feature/stock_info.py:37` | HIGH / HIGH | 对上一行 SHA-1 十六进制结果再计算 MD5，输出到 `sign` 参数；本地无秘密密钥。攻击者能否控制待签值及服务端是否据此授信，都没有协议证据。 | 同 :36；如果是纯兼容校验，留下有依据的非安全用途说明；如果承担授权/完整性，需确定兼容的新签名方案后另行授权实施。 |

## 3. 新增 12 文件的 Bandit findings

27 条逐项列出；相同规则的行仍是独立 finding。对所有 B113 的 AST 定位显示请求使用文件内常量端点或固定 host 模板；date/symbol/contract 等调用参数进入路径或查询参数，没有发现参数直接控制 URL authority 的路径。固定 endpoint 不表示上游可信：延迟、断连、服务端被攻陷或网络异常仍可触发以下风险。

| Finding（文件:行） | 严重度/置信度 | 实际输入与影响摘要 | 建议/保留依据 |
|---|---|---|---|
| B405 `opendata_http/futures_derivative/futures_contract_info_cffex.py:11` | LOW / HIGH | 导入 `xml.etree.ElementTree`；同文件 :34 将 HTTP 响应文本送入该解析器（见 B314）。这是 parser 选择提示，不单独证明利用。 | 与 :34 合并审阅；source-fidelity 阶段保留并登记。批准修改后可评估 `defusedxml` 与响应体大小限制。 |
| B314 `opendata_http/futures_derivative/futures_contract_info_cffex.py:34` | MEDIUM / HIGH | `ET.fromstring` 输入来自 HTTP 响应文本，非本地固定可信文件；用户不能直接传任意 XML，但上游载荷可异常或被污染，解析资源消耗/拒绝服务影响未实证。该请求有显式 timeout。 | 依 FR4 保留当前搬运源；登记远端 XML 输入。若获准调整，使用防护型解析器、响应体上限并加恶意/超大 XML 用例。 |
| B405 `opendata_http/futures_derivative/futures_contract_info_czce.py:11` | LOW / HIGH | 导入 `xml.etree.ElementTree`；同文件 :37 解析 HTTP 响应文本。 | 与 :37 合并；当前依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_contract_info_czce.py:34` | MEDIUM / LOW | 同步 GET 没有显式 timeout；URL 模板仅把 date 放入固定端点。网络/服务端迟滞可长期占用调用线程；date 不控制 host。 | 按 FR4 暂不改直连接口；登记 debt。未来如获准收口，走共享 HTTP client 或采用有限 connect/read timeout，并覆盖超时/取消行为。 |
| B314 `opendata_http/futures_derivative/futures_contract_info_czce.py:37` | MEDIUM / HIGH | `ET.fromstring` 解析网络响应文本，来源与可控性边界同 CFFEX；该请求同时缺少显式 timeout。 | 依 FR4 保留并登记；获得批准后同时处理 parser 防护、输入大小和 HTTP deadline。 |
| B113 `opendata_http/futures_derivative/futures_contract_info_ine.py:30` | MEDIUM / LOW | 同步 GET 无显式 timeout；date 仅拼入固定 host 模板。慢/不响应的远端可占用调用线程。 | 依 FR4 保留并登记；未来经共享 client 或批准的有限 timeout 修复。 |
| B113 `opendata_http/futures_derivative/futures_contract_info_shfe.py:31` | MEDIUM / LOW | 同步 GET 无显式 timeout；date 仅拼入固定 host 模板。慢/不响应的远端可占用调用线程。 | 依 FR4 保留并登记；未来经共享 client 或批准的有限 timeout 修复。 |
| B113 `opendata_http/futures_derivative/futures_cot_sina.py:35` | MEDIUM / LOW | 同步 GET 无显式 timeout；端点为本地固定值，symbol/contract/date 属查询输入。无 URL authority 注入证据，网络迟滞仍可占用线程。 | 依 FR4 保留并登记；未来经共享 client 或批准的有限 timeout 修复。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:27` | MEDIUM / LOW | 同步 POST 无显式 timeout；请求 endpoint 为函数内常量，symbol 属数据参数。远端无响应可占用线程。 | 依 FR4 保留并登记；未来经共享 client 或批准的有限 timeout 修复。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:38` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:49` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:71` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:82` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:96` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:109` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:131` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:142` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:152` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:179` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:192` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:206` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:216` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_hog.py:227` | MEDIUM / LOW | 同步 POST 无显式 timeout；固定 endpoint，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B113 `opendata_http/futures_derivative/futures_index_sina.py:33` | MEDIUM / LOW | 同步 GET 无显式 timeout；端点为模块常量，输入参数不是 URL host。上游迟滞可占用线程。 | 依 FR4 保留并登记；未来经共享 client 或批准的有限 timeout 修复。 |
| B113 `opendata_http/futures_derivative/futures_index_sina.py:69` | MEDIUM / LOW | 同步 GET 无显式 timeout；端点为模块常量，迟滞风险同上。 | 依 FR4 保留并登记。 |
| B112 `opendata_http/futures_derivative/futures_index_sina.py:83` | LOW / HIGH | `match_main_contract` 的裸 `except` 后继续下一项；它处理远端匹配结果的转换失败，可把坏行静默变成缺失合约，也会吞掉非解析异常。未发现任意代码执行路径。 | 依 FR4 先保留并登记；未来若批准改造，应限缩到预期解析异常并增加跳过计数/可观察错误。 |
| B113 `opendata_http/futures_derivative/futures_index_sina.py:125` | MEDIUM / LOW | 同步 GET 无显式 timeout；固定 host 模板中带 symbol/date 数据参数，不由参数设置 host。上游迟滞可占用线程。 | 依 FR4 保留并登记；未来经共享 client 或批准的有限 timeout 修复。 |

**未列出的新增文件 finding 数为零**：以上 27 条覆盖当前 `futures_derivative` 的完整 Bandit 输出，不代表 12 个文件无 Ruff 债务。

## 4. 新增 12 文件 Ruff 债务清单

下表按文件汇总 144 条当前 Ruff finding。S113/S314/S112 已在 §3 逐项提供文件行和实际输入审查；其余列为 Ruff 返回条数，不是新增安全漏洞判定。

| 文件 | S113 | S314 | S112 | 其他 Ruff | 合计 |
|---|---:|---:|---:|---:|---:|
| `opendata_http/futures_derivative/__init__.py` | 0 | 0 | 0 | 5 | 5 |
| `opendata_http/futures_derivative/cons.py` | 0 | 0 | 0 | 8 | 8 |
| `opendata_http/futures_derivative/futures_contract_info_cffex.py` | 0 | 1 | 0 | 9 | 10 |
| `opendata_http/futures_derivative/futures_contract_info_czce.py` | 1 | 1 | 0 | 9 | 11 |
| `opendata_http/futures_derivative/futures_contract_info_dce.py` | 0 | 0 | 0 | 9 | 9 |
| `opendata_http/futures_derivative/futures_contract_info_gfex.py` | 0 | 0 | 0 | 9 | 9 |
| `opendata_http/futures_derivative/futures_contract_info_ine.py` | 1 | 0 | 0 | 9 | 10 |
| `opendata_http/futures_derivative/futures_contract_info_shfe.py` | 1 | 0 | 0 | 9 | 10 |
| `opendata_http/futures_derivative/futures_cot_sina.py` | 1 | 0 | 0 | 8 | 9 |
| `opendata_http/futures_derivative/futures_hog.py` | 15 | 0 | 0 | 14 | 29 |
| `opendata_http/futures_derivative/futures_index_sina.py` | 3 | 0 | 1 | 18 | 22 |
| `opendata_http/futures_derivative/futures_spot_sys.py` | 0 | 0 | 0 | 12 | 12 |
| **合计** | **22** | **2** | **1** | **119** | **144** |

其他 119 条具体规则为 D205=28、D212=28、D415=28、E501=21、I001=1、UP009=12 和 B905=1。B905 位于 `opendata_http/futures_derivative/futures_spot_sys.py:33`，提示 `zip()` 两侧长度不同时会按较短输入截断；当前来源长度不变量未由本次静态检查证明，应作为数据完整性问题单独审阅。其余 118 条是文档/行长/import/编码声明规则，不作为本材料的安全漏洞。

依 FR4，以上搬运源保持原样并登记债务；不加 `noqa`、不从扫描中排除、不抬高 Ruff ratchet 快照。若后续授权修复，先确认逐文件重放和源码保真边界，再另做最小变更及针对性验证。

## 5. cloudQuant 待签认

审阅责任人由用户指定为 cloudQuant；此处**未签名、未发送外部消息，也未把负责人名称视为风险接受**。请签认人逐项补充依据和决定：

- `futures/cons.py`：确认 `get_pk_data` 是否属于受支持的外部 Python API；选择受限资源加载/迁移格式，或有依据地保留未使用 helper。
- 三条 B324：提供/确认 Legu 与 CLS 字段的安全语义；判断是否为非安全兼容值，或需要另行设计服务端兼容签名。
- 新增 B113/B314/B405/B112：确认在 FR4 期间以源码保真为由保留并登记；如授权调整，明确由谁批准接口、解析器或超时变更及兼容测试。
- 144 条 Ruff：确认当前逐源债务摘要准确；不改 `ruff_ported` 快照/阈值。B905 的数据长度不变量需另附证据或指定后续处理人。
- 签认人/组织：____________________；签认日期：____________________；逐项凭证/决定记录位置：____________________。

**当前决定：待签认。** 本文件提供了可核对的人工审阅输入，不等于风险接受、授权改码或 AC-5|07 通过。

## 6. 用户委托的当前完整审阅结论

审阅人：Codex 主智能体（AI）；授权：用户于本聊天直接回复“你帮我直接审阅”。依据为当前源码、固定环境的完整扫描和已注册模型调用链；未运行危险载荷或供应方请求。

Bandit 1.9.4 / Python 3.11.8 实测 325 个 Python 文件、1048 项、15 条规则、0 扫描错误；HIGH 31 / MEDIUM 967 / LOW 50。扫描源码摘要 `1869a81cb20c509f971a1e5322a7c7a650b359be096506056afe3b032bca8564`，逐文件摘要和无源码/字面凭据的定位在 `ported-bandit-scan.json`。扫描命令返回1表示发现项存在，不能按执行失败或零风险解释。与前期1.9.2扫描的12项差异全为B105 matcher变化，未改历史计数。

| 规则 | 数量 | 当前技术判定 | 后续处理 |
|---|---:|---|---|
| B105 | 17 | 12 token字面量候选共2种值，未证实授权属性；其余空哨兵/错误常量不得视为真实凭据。 | 确认底层端点字段用途；禁止归档字面量。 |
| B107 | 1 | 默认参数短静态标记，扫描命中保留；不能仅靠规则名称认定真实密码。 | 协议字段变动时复核。 |
| B110 | 7 | 异常pass可能掩盖解析/请求失败，影响数据完整性及可诊断性。 | 后续适配器应记录受控错误且区分空数据。 |
| B112 | 14 | 异常continue可能跳过坏行或步骤，不能证明零丢失。 | 后续明确失败行计数及整批失败策略。 |
| B113 | 929 | 929无timeout调用；已注册6类模型存在直接可达路径，可能永久阻塞worker；固定host不消除风险。 | 自研调用边界增加明确时间预算需独立兼容验证，FR4本轮不改源。 |
| B301 | 1 | futures/cons.py:576调用pickle.load，file_name只有join无allowlist/containment；外部调用可能路径越界及代码执行，无仓内调用不等于安全。 | 确认公共helper兼容责任；设计受限资源名或迁JSON后单独修复。 |
| B307 | 35 | 35 Python eval输入来自HTTP/表格内容，完整builtins；恶意供应方响应可执行进程代码。 | 设计非执行式解析并独立重放验证；禁止把原样搬运称为误报。 |
| B311 | 4 | 随机值用于抓取兼容/辅助选择，未见作为服务身份/密钥；不提供密码学保证。 | 如改用于认证，另选secrets并验证协议。 |
| B314 | 2 | 2远端XML ET.fromstring，非固定可信资源，解析资源耗尽风险保留。 | 未来限制响应大小并评估安全parser。 |
| B324 | 3 | 3无密钥MD5/SHA1输出进入远端token/sign参数；不能确认服务端安全用途，也不能直接换算法保持兼容。 | 确认Legu/CLS协议；若仅兼容字段明确非安全属性，否则设计新方案。 |
| B403 | 1 | 导入pickle本身不执行载荷；与B301真实load链一起保留。 | 随B301处置，不能以无pkl资源消项。 |
| B404 | 1 | import subprocess本身非执行；实际3个B603见固定curl argv调用。 | 与B603边界共同复核。 |
| B405 | 2 | 导入ElementTree本身非漏洞利用；与2个B314输入链一起保留。 | 随B314处置。 |
| B501 | 28 | 28 verify=False，TLS不校验对端身份，中间人可污染响应；未见经当前注册模型直接调用，模块API仍暴露。 | 未来保持供应端兼容下恢复验证，原搬运树不能称安全。 |
| B603 | 3 | utils/request.py:149/196/249调用固定curl可执行文件+argv list，shell=False默认，显式timeout；参数仍需按curl选项语义限制，不能推导任意输入无风险。 | 确认调用者对URL及argv的控制；不把静态规则作为shell=True命令注入。 |

31个HIGH逐项位置以triage JSON中HIGH记录为准：28个B501均保留TLS身份风险；3个B324保留服务端签名语义缺证。B301及35个Python eval不能判为无害；没有仓内调用或固定host不等于不存在可利用链。额外远端JS eval、固定源码JS和调用方V8执行边界分别留档。

已注册AKShare模型的无timeout直接路径包括 bond_daily→bond_zh_hs_cov_daily、financial_indicator→stock_financial_analysis_indicator_em、financial_statement→stock_financial_report_sina、futures_daily→futures_zh_daily_sina、option_daily→option_sse_daily_sina、stock_action→stock_history_dividend_detail。它们真实可达；没有发起请求。B501/B307未发现经当前十个模型直接调用，但仍存在模块公开调用边界。

全部1048条扫描定位均关联一个已审阅规则组和明确处置分类；规则组审阅及高危链核对不冒充1048处逐行代码审查。FR-4要求本轮保真，故在本轮保持源实现、登记债务；未对风险接受作个人签名，未扩大生产准入。AC-5|07仅可据“完整扫描与当前技术triage完成”评定，不能据此宣称安全缺陷已消除。
