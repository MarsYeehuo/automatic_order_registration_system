# Harness 工程说明

本项目的 Harness 不是单一的 pytest 文件，而是一套“可重复输入 + 确定性断言 + 指标报告 + 生产回归”的分层验证体系。目标不是证明代码能跑，而是持续回答三个问题：

1. 解析/抽取/匹配/决策的结果是否稳定？
2. 哪些错误会导致错误入账，而不是仅仅少一条记录？
3. 模型、OCR、词典或规则变更后，哪些历史行为被破坏了？

## 1. 分层结构

| 层级 | 位置 | 关注点 | 是否依赖网络 |
| --- | --- | --- | --- |
| 单元测试 | `tests/unit/` | 标准化、校验、匹配、决策、输出、Schema、解析器 | 否 |
| 契约测试 | `tests/contract/` | LLM JSON 解析与密钥解析、OCR 响应映射、缓存、禁用路径 | 否 |
| 场景测试 | `tests/scenarios/` | 完整 pipeline：多格式批次、AR/AP 覆盖、重复建账、无费用账单 | 否（OCR/LLM 关闭） |
| Golden Set | `evals/golden_set.json` + `evals/run_evals.py` | 业务结果与质量指标 | 否 |
| 人工对抗集 | 建议在 `evals/adversarial/` 扩展 | 低质量扫描件、混排表格、错误币种、跨文档冲突 | 可选 |
| 生产影子回归 | 未随题交付 | 对真实流量只读重放，比较新旧版本 | 视环境 |

## 2. 测试替身与确定性

- OCR：契约测试直接构造 OCR 响应字典，验证 `chargeList`、`ctrList`、`0/O` 修正、失败与缓存路径，不调用远端。
- LLM：`LLMClient` 可注入任意 LangChain `Runnable`；测试用固定 `AIMessage` 验证 JSON 围栏、系统提示词中的花括号、无密钥降级。
- 配置：`tests/conftest.py` 将记账主体固定为 `Origin Agent Ltd`，避免本机 `template_key.txt` 或真实主体影响断言。
- 时间/随机：场景断言只比较操作、金额、币种、付款方式和 AR/AP，不比较 `run_id` 与时间戳。
- 文件：所有断言输入来自 `tests/fixtures/`，不依赖 `data/samples/` 的 OCR 结果。

## 3. Golden Set 设计

`evals/golden_set.json` 每个 case 包含：

- `inputs`：一个或多个文件；
- `expected`：每个费用行期望的 `charge_item`、`amount`、`currency`、`payment_term`、`operation`、`posting_type`；
- `description`：该 case 保护的业务规则。

覆盖的规则：

| Case | 保护点 |
| --- | --- |
| `arrival_notice` | 非账单含有效费用时按配置 CREATE，且 AR/AP 仍按主体判断 |
| `text_invoice` | 费用别名标准化、逐费用行拆分 |
| `update_bill` | 单据号 + 费用名匹配；金额变化 UPDATE，金额一致 NO_CHANGE；继承 PP |
| `excel_bill` | 表头识别、THC 别名、金额消歧、CC 继承 |
| `word_bill` | Word 段落 + 表格抽取 |
| `email_prealert` | 邮件正文费用抽取与非账单 CREATE |
| `batch_duplicate_create` | 跨文档重复费用只保留一条 CREATE，其余 PENDING_REVIEW |

运行：

```bash
python evals/run_evals.py
```

当前结果：7/7 case 通过，结构、字段、操作、AR/AP、证据完整率均为 100%，不安全自动入账率 0%。报告写入 `evals/report.json`。

## 4. 指标定义

| 指标 | 定义 | 为什么重要 |
| --- | --- | --- |
| `structure_valid_rate` | `validate_records` 无错误的 case 占比 | 结构错误会让下游整批失败 |
| `field_accuracy` | 期望费用行四元组（名称/金额/币种/付款方式）的多重集交集 / 期望数 | 防止“行数对了但字段错” |
| `operation_accuracy` | CREATE/UPDATE/NO_CHANGE/PENDING 的多重集匹配率 | 直接决定是否写账 |
| `posting_accuracy` | AR/AP/null 多重集匹配率 | 方向错误的财务影响最大 |
| `evidence_completeness` | 自动入账记录中带 `field_evidence` 的比例 | 可追溯性 |
| `unsafe_auto_posting_rate` | 自动入账但缺少证据或 AR/AP 为 null 的记录 / 总记录 | 核心安全指标，优先于召回率 |

> 评测时应把“少建一条账”和“错误自动入账”分开统计。前者是可修复的召回问题，后者是资金/合规风险。

## 5. 断言策略

### 5.1 结果断言，而非实现断言

场景测试断言输出记录，而不是内部函数调用次数。这样重构 parser 或 matcher 时测试仍然有意义。

### 5.2 状态机的每个出口都有测试

- CREATE：无匹配 + 校验通过 + 置信度达标；
- UPDATE：有匹配 + 白名单字段存在差异 + 证据充分；
- NO_CHANGE：有匹配 + 关键字段无差异；
- PENDING_REVIEW：阻断校验、AR/AP 无法判断、匹配歧义、低置信度、无有效费用、批次冲突。

### 5.3 失败要可解释

每个 PENDING_REVIEW 必须带 `review_reasons`；每个自动操作必须带 `operation_reason`；每个字段尽量带 `field_evidence`。测试同时检查这些不变量。

## 6. 在 CI 中的建议

```text
pytest tests/unit tests/contract        # 快速门禁，秒级
pytest tests/scenarios                  # 确定性批次，秒级
python evals/run_evals.py               # Golden Set 指标，失败即阻断
shipping-billing validate outputs/...   # 产物结构校验
```

建议门禁：

- 单元/契约/场景测试必须全绿；
- Golden Set 的 `unsafe_auto_posting_rate` 必须为 0；
- `field_accuracy`、`operation_accuracy`、`posting_accuracy` 不得低于上一版本；
- Schema 文件变更必须伴随版本号与兼容性说明。

## 7. 扩展方式

### 7.1 新增业务规则

1. 在 `evals/golden_set.json` 增加一个最小 case；
2. 运行 eval，确认失败（规则尚未实现）；
3. 实现规则；
4. 运行全部测试与 eval，确认无回归。

### 7.2 新增文档格式

1. 在 `tests/fixtures/` 放一个最小样本；
2. 在 `tests/unit/test_parsers.py` 断言标准化文本块；
3. 在场景测试中断言费用行；
4. 如果格式需要外部服务，补契约测试替身，避免 CI 依赖网络。

### 7.3 OCR/LLM 变更

- 先跑契约测试，确认响应映射与降级路径；
- 再跑场景测试，确认不改变确定性结果；
- 最后跑 Golden Set，观察字段/操作/AR-AP 指标；
- 真实服务联调使用 `outputs/ocr_cache/` 与 `outputs/llm_cache/`，避免重复计费与不稳定网络。

## 8. 当前不覆盖的部分

- 真实财务系统写入、并发锁、幂等键落库；
- 超大规模（百万行）性能；
- 多语言单据（当前以中英文关键词为主）；
- OCR 图片质量分布与真实扫描件长尾；
- 人工审核界面的交互测试。

这些属于接入生产前的下一阶段 Harness 范围。
