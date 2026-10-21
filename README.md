# 航运智能账单助手智能体

航运智能账单助手面向航运与货运代理场景，提供多格式单据解析、费用抽取、标准化、校验、已有账单匹配与入账决策能力。系统支持 EML、PDF、图片、Excel、Word 与 TXT 输入，按费用行输出标准 JSON 记录，并为每个字段保留来源证据与审计轨迹。

系统采用 Python 3.11 与 LangChain 实现，遵循规则优先、证据优先、模型受控补充的设计原则。OCR 与 LLM 作为增强能力接入；外部服务不可用时，系统可回退至规则链路继续运行。

## 1. 核心能力

- **多格式解析**：EML 邮件头、正文及附件递归解析，附件按真实格式二次解析并基于 SHA256 去重；PDF 优先使用原生文本，扫描件调用 OCR；支持图片、Excel、Word 与 TXT。
- **费用标准化**：费用别名精确/模糊匹配，保留原始费用名与标准费用名；统一币种、日期与付款方式表示。
- **规则校验**：覆盖 ISO 币种、明细合计、单价与数量一致性、负数与 Credit Note、重复费用、OCR 易混字符及箱号 ISO 6346 校验位；参考匹配后会复核提单号、箱号与单据日期，冲突时转人工。
- **账单匹配**：四层有序匹配规则结合金额、单价、数量与付款方式消歧；候选不唯一时显式转人工。
- **行级决策**：按费用行输出 `CREATE`、`UPDATE`、`NO_CHANGE` 或 `PENDING_REVIEW`。
- **增量更新**：更新采用字段白名单补丁，仅修改具备证据的字段，其余字段继承原账单。
- **AR/AP 判定**：从配置的记账主体视角判断应收/应付，不依据 Debit Note 或 Credit Note 直接推断。
- **审计追踪**：记录字段证据、校验结果、匹配候选、决策原因、更新补丁、继承字段及 OCR/LLM 元信息。
- **批次治理**：支持文件级去重、业务级重复检测、同一目标冲突识别、manifest 与 summary 输出。

## 2. 安装与配置

### 2.1 环境要求

- Python 3.11 或更高版本
- 项目依赖见 `pyproject.toml`

```bash
pip install -e .
```

如需运行完整测试与评测：

```bash
pip install -e ".[test]"
```

### 2.2 LLM 凭据

LLM 凭据通过独立的凭据文件或 `SHIPPING_BILLING_KEY_FILE` 环境变量注入。仓库提供 `template_key.example.txt` 作为字段示例，实际凭据文件应置于版本控制之外。

凭据文件支持两种格式：

- `key: value` 文本，字段包括 `供应商`、`模型名字`、`base_url`、`api_key`；
- JSON 对象，字段包括 `provider`、`model`、`base_url`、`api_key`。

凭据文件查找顺序：

1. `SHIPPING_BILLING_KEY_FILE` 环境变量；
2. `llm.key_file` 配置项；
3. 项目根目录；
4. 上级目录；
5. 当前目录。

未找到凭据时，系统仍可通过规则与 OCR 链路运行，并在 summary 中将 LLM 标记为不可用。凭据不会写入代码、日志或提交内容。

### 2.3 运行方式

> 公开代码仓库不包含题目原始样例文档与参考运行结果；请使用自有文件，并将下例中的 `data/samples` 替换为实际输入目录。

```bash
# 启用 OCR 与 LLM
shipping-billing run data/samples --output-dir outputs/sample_run

# 仅使用规则链路
shipping-billing run data/samples --output-dir outputs/offline --no-llm --no-ocr

# 指定记账主体及别名
shipping-billing run data/samples --entity "Origin Agent Ltd" --entity-alias "PLT"

# 调整非账单文档中有效费用的操作策略
shipping-billing run data/samples --non-bill-operation PENDING_REVIEW
```

辅助命令：

```bash
shipping-billing validate outputs/sample_run/results.json
shipping-billing schema --output schema/output.schema.json
```

`--fail-on-pending` 可在存在 `PENDING_REVIEW` 记录时返回退出码 3，适用于 CI 或批处理告警。

## 3. 输出规范

每次运行生成三个文件：

| 文件 | 内容 |
| --- | --- |
| `results.json` | 标准结果数组，每个费用行一条记录 |
| `audit.jsonl` | 每行一条审计记录，支持流式接入 |
| `summary.json` | 运行元信息、文档清单、操作与 AR/AP 统计、OCR/LLM 状态、配置版本及 manifest |

`schema/output.schema.json` 为结果 JSON Schema。

### 3.1 结果结构

```json
{
  "operation": "UPDATE",
  "document_info": {
    "document_type": "Debit Note",
    "reference_no": "DN-202609-001",
    "document_date": "2026-09-20"
  },
  "transaction_party": {
    "issuer": "Destination Agent",
    "recipient": "Origin Agent"
  },
  "charge_details": {
    "charge_item": "Handling Charge",
    "amount": 45.0,
    "currency": "USD",
    "payment_term": "PP"
  },
  "financial_attributes": {
    "accounting_entity": "Origin Agent",
    "posting_type": "AP"
  },
  "audit_trail": {
    "run_id": "run_...",
    "record_id": "run_...-R0001",
    "source_documents": [{"file": "bill.pdf", "location": "page:1"}],
    "field_evidence": [],
    "validation_results": [],
    "match_candidates": [],
    "ai_reasoning": [],
    "review_reasons": [],
    "operation_reason": [],
    "update_patch": {"charge": {"amount": 45.0}, "bill": {}},
    "inherited_fields": ["charge.payment_term"],
    "existing_bill_id": "BILL-9001",
    "raw_charge_name": "HANDLING FEE",
    "charge_confidence": 0.9,
    "rules_version": "1.0.0",
    "prompt_version": "1.0.0"
  }
}
```

输出约定：

- 每个费用行生成一条记录，不合并多费用行；
- `amount` 输出为 JSON number，内部使用 `Decimal` 计算；
- 无法可靠获取的字段为 `null`；
- `PENDING_REVIEW` 的 `financial_attributes.posting_type` 为 `null`，决策原因写入 `review_reasons`；
- `UPDATE` 必须包含 `update_patch`，且仅允许白名单字段；
- `NO_CHANGE` 不产生更新补丁；
- 所有记录包含 `run_id`、`source_documents` 及可获取的证据信息。

## 4. 处理流程

```text
输入文件/目录
  │
  ├─ 文件清单、哈希、类型识别、大小与递归限制
  │
  ├─ 解析
  │   ├─ EML：邮件头 + 正文 + 附件（递归）
  │   ├─ PDF：原生布局文本 + 可选 OCR
  │   ├─ 图片：OCR
  │   ├─ Excel：工作表、表头与明细行
  │   └─ Word：段落与表格
  │
  ├─ 文档分类：BILL / BUSINESS_EMAIL / ARRIVAL_NOTICE / OTHER / UNCERTAIN
  │
  ├─ 规则抽取与证据定位
  │   └─ LLM 受限补充（仅缺失字段，逐字段证据校验）
  │
  ├─ 标准化：费用名、币种、日期、付款方式
  ├─ 校验：阻断性与告警问题
  ├─ 匹配：四层规则与消歧
  ├─ 决策：逐费用行状态机
  ├─ AR/AP：记账主体视角
  ├─ 批次规则：重复与冲突
  └─ 输出：results / audit / summary
```

### 4.1 文档分类

文档分类是费用抽取前的强制关卡。B/L、Sea Waybill 等运输单据被标记为 `OTHER` 并跳过费用抽取，避免提单中的参考金额被误识别为费用。PDF 解析器同时检查原生文本与默认文本，以降低版式抽取丢失标题导致的误判。

### 4.2 抽取优先级

1. 规则抽取：表格行、PDF 布局行、邮件金额行与 OCR 结构化结果；
2. 原生 PDF 费用行优先于 OCR 费用行；两者不一致时采用原生结果，并保留 OCR 审计信息；
3. 规则不足时，LLM 仅补充缺失字段，且每个值必须可在原文或 OCR 文本中找到证据；
4. 仍无法满足要求时，低置信度或关键字段缺失的记录进入 `PENDING_REVIEW`。

## 5. 决策规则

### 5.1 操作类型

| 情况 | 结果 |
| --- | --- |
| 存在阻断性校验问题 | `PENDING_REVIEW` |
| 匹配歧义或冲突 | `PENDING_REVIEW` |
| 已匹配，且白名单字段无差异 | `NO_CHANGE` |
| 已匹配，且存在有明确证据的差异 | `UPDATE` |
| 未匹配，费用有效且置信度达标 | `CREATE` |
| AR/AP 无法判断 | `PENDING_REVIEW`（非账单文档可按配置例外） |
| 批次内重复或同一目标冲突 | `PENDING_REVIEW` |

### 5.2 匹配顺序

1. 单据编号 + 费用名称；
2. 提单号 + 费用名称 + 币种；
3. 订舱号 + 费用名称 + 币种；
4. 箱号 + 交易双方 + 日期 + 金额。

候选记录多于一条时，使用金额、单价、数量与付款方式消歧；仍不唯一时输出全部候选并转人工处理。

参考匹配成功后，系统仍会复核主/分提单号、箱号与单据日期。任一标识冲突将阻断自动入账并转人工审核，避免单号复用或 OCR 误读导致错改。箱号在进入匹配前需通过 ISO 6346 校验位，发票号等 11 位字符不会被误识别为箱号。

### 5.3 更新白名单

费用字段：

`charge_item`、`amount`、`currency`、`payment_term`、`quantity`、`unit_price`

单据字段：

`document_type`、`reference_no`、`document_date`、`issuer`、`recipient`

所有补丁字段均要求对应 `FieldEvidence`；金额、单价与数量还必须在原始文本中可定位，或可由已证据化的字段计算得出。未出现在补丁中的字段继承已有账单值，并记录于 `inherited_fields`。

### 5.4 AR/AP 矩阵

| 记账主体匹配 | posting_type | rule_id |
| --- | --- | --- |
| 仅为签发方 | `AR` | `entity_is_issuer` |
| 仅为接收方 | `AP` | `entity_is_recipient` |
| 同时为签发方与接收方 | `null` + PENDING | `entity_is_both` |
| 均不匹配 | `null` + PENDING | `entity_not_party` |

说明：

- 记账主体通过名称与别名模糊匹配，默认阈值为 0.82，可通过 `--entity` 与 `--entity-alias` 覆盖；
- 系统不依据 Debit Note 或 Credit Note 直接决定 AR/AP；
- Credit Note 保持同一主体视角，金额为负；应收/应付增减由下游会计逻辑处理；
- 非账单文档的 AR/AP 无法判断时，可通过 `non_bill_operation` 选择 `CREATE` 或 `PENDING_REVIEW`。

### 5.5 非账单费用的处理策略

账单识别是系统的重要目标，但费用信息最早可能出现在 PRE-ALERT、到港通知或邮件正文中。若直接丢弃非账单文档，将造成应收应付遗漏。系统默认策略如下：

- 非账单文档中的有效费用仍生成记录；
- 默认执行 `CREATE`（`non_bill_operation: CREATE`）；
- `audit_trail.document_kind` 保留真实来源类型；
- AR/AP 可由交易主体判断时填写，否则保持 `null` 并提示人工确认。

该策略并非将通知类文档等同于账单，而是将非正式来源纳入审核范围。若财务政策要求先经人工确认，可将策略配置为 `PENDING_REVIEW`。

## 6. OCR

OCR 配置位于 `config/settings.yaml` 的 `ocr` 段。

| 模式 | 行为 |
| --- | --- |
| `tunnel` | 通过临时公网 URL 暴露待识别文件，适用于开发与验证环境 |
| `static` | 将文件写入 `public_dir`，按 `public_base_url` 生成访问 URL，适用于对象存储或静态站点 |
| `none` | 禁用 OCR；需要 OCR 的文档将因字段缺失按校验结果转审核 |

实现要点：

- OCR 原始响应保存至 `outputs/ocr_raw/`，结构化缓存保存至 `outputs/ocr_cache/`；
- 相同 SHA256 文件直接命中缓存，避免重复调用；
- 连接超时、读取超时与重试次数均可配置；
- OCR 仅上传 PDF 与图片，不执行附件，不访问任意 URL；
- 运输单据（B/L、Sea Waybill）跳过 OCR；
- OCR 返回 `currency`、`currencyCode`、`ccy` 或 `cur` 时保留币种；未返回时保持 `null`，不根据图片内容猜测；
- OCR 金额中的 `0/O`、`1/I` 修正在证据中保留原始值与修正说明。

生产环境建议采用 `static` 模式配合对象存储，或将 OCR 服务部署于同一内网，以降低临时公网链路的稳定性风险。

## 7. LLM 与 LangChain

- 使用 `langchain-anthropic` 或 `langchain-openai` 的 Chat 模型；
- 提示词由 `ChatPromptTemplate` 组织，系统消息中的 JSON 示例按字面量处理；
- 抽取提示词要求模型仅输出 JSON，并为每个字段提供原文证据；
- 证据校验失败、字段无法在原文中定位、日期或币种格式异常时，拒绝该字段；
- LLM 超时、报错或未配置凭据时，系统回退至规则结果，不阻塞批次；
- 相同输入的 LLM 响应缓存至 `outputs/llm_cache/`；
- 模型名、供应商与提示词版本写入 summary 与审计记录。

凭据仅通过环境变量或版本控制之外的凭据文件注入，不写入代码、日志或提交内容。

## 8. Harness 与质量保障

质量保障体系详见 [docs/harness.md](docs/harness.md)。

```bash
pytest                          # 单元、契约与场景测试
python evals/run_evals.py       # Golden Set 评测
```

参考评测结果：Golden Set 7/7 用例通过，结构、字段、操作、AR/AP 与证据完整率均为 100%，不安全自动入账率为 0%。

测试分层：

- **单元测试**：标准化、校验、匹配、决策、输出脱敏、Schema 与解析器；
- **契约测试**：LLM JSON 解析、凭据加载、无凭据降级、系统提示词花括号处理、OCR 响应映射、缓存与禁用；
- **场景测试**：多格式批次、AR/AP 覆盖、非账单策略、重复 `CREATE`、同一目标冲突与无费用账单；
- **Golden Set**：逐用例校验业务期望并生成指标报告。

## 9. 项目结构

```text
config/                     settings、费用别名、付款方式、匹配规则、AR/AP 规则
data/                       existing_bills.json（题目样例仅随完整交付包提供）
docs/                       harness、thoughts
evals/                      golden_set.json、run_evals.py、report.json
schema/                     output.schema.json
src/shipping_billing/
  parsers/                  pdf、image、eml、excel、word、text、registry
  extraction/               rules、llm_extract、engine
  classification.py         文档分类
  normalization.py          费用、币种与付款方式标准化
  validation.py             校验
  matching.py               已有账单匹配
  decision.py               操作与 AR/AP 决策、更新补丁
  output.py                 结果、审计、脱敏与文件写入
  pipeline.py               批次编排
  llm.py                    LangChain 客户端、JSON 解析与缓存
  ocr.py                    OCR 客户端、托管模式与缓存
  cli.py                    run / validate / schema
tests/                      unit / contract / scenarios / fixtures
outputs/sample_run/         参考运行结果
```

## 10. 假设与边界

1. 记账主体由部署方配置，系统不从单据内容推断固定主体。
2. `data/existing_bills.json` 为示例账务数据；生产环境需替换为实际账务系统查询适配器。
3. 金额内部使用 `Decimal` 计算并输出为 JSON number；极端大额或高精度场景建议改用字符串金额。
4. 日期默认按 US 格式解析，可通过 `date_locale` 调整；其他区域格式需扩展解析规则。
5. OCR 服务为外部依赖；网络不可用时依赖缓存或转人工，不保证全部文档可识别。
6. 当前主要支持中英文单据；其他语种需扩充费用别名词典。
7. 非账单默认 `CREATE` 属于业务策略而非技术必然，可通过配置切换为审核。
8. 批次重复检测基于业务键，不替代数据库唯一约束；生产写入仍应使用幂等键。
9. 当前版本未实现财务系统实际写入、并发控制、权限管理与审核工作台。
10. LLM 仅提供补充能力；在无 LLM 环境下，规则可覆盖的场景仍可完成处理。

## 11. 设计思考

完整内容见 [docs/thoughts.md](docs/thoughts.md)。

1. AR/AP 是记账主体视角的函数，主体缺失时应显式表达不确定性。
2. 非账单文档不应被静默丢弃，但必须保留来源类型与不确定性。
3. OCR 是召回工具而非事实来源，原生文本、OCR 结果与证据需交叉校验。
4. 更新应是增量补丁而非整单覆盖，无证据的字段不允许修改。
5. 文件级重复与业务级重复应分开处理。
6. 匹配歧义是正常状态，应保留候选并转人工，而非直接选取第一条。
7. LLM 仅做受限补充，每个字段必须可回溯至原文。
8. 幂等与批次重放应作为一等需求设计。
9. 人工审核是产品的一部分，质量指标应包含不安全自动入账率。
10. 隐私与外部调用应默认最小化，凭据与敏感文本不进入日志和仓库。
11. 币种缺失属于证据缺口，应阻断自动入账并转人工审核，不能用默认币种或历史账单替代源单据证据。

## 12. 参考运行结果

完整交付包内附有一次多格式参考运行结果，位于 `outputs/sample_run/`；公开代码仓库不包含样例数据与输出。该次运行覆盖 6 个文档（含 EML 附件），生成 7 条费用记录，操作分布为 `CREATE=2`、`UPDATE=1`、`NO_CHANGE=2`、`PENDING_REVIEW=2`，AR/AP 分布为 `AP=4`、`UNKNOWN=3`。

两处保守处理值得说明：

- 图片样例的 OCR 返回了发票号、提单号、箱号与金额，但未返回币种、日期和交易方；记录虽匹配到已有账单 `BILL-1002`，仍因 `MISSING_CURRENCY` 阻断性校验进入 `PENDING_REVIEW`，系统未猜测币种自动入账；同时其箱号 `JXLU4504463` 与已有账单箱号无交集，触发 `MATCH_CONTAINER_CONFLICT`，进一步确认不应自动改账；
- `PDF示例单1.pdf` 为 `General Notice`（到港通知类），其 `On Carriage Additional` 费用有效，签发方为 `NEPTUNE SHIPPING LIMITED`，与配置的记账主体均不匹配，因此按非账单策略执行 `CREATE`，`posting_type` 保持 `null`，并在 `operation_reason` 中提示人工确认 AR/AP；
- `R0002` 的 `UPDATE` 补丁同时包含 `amount` 与 `unit_price`，两者均可在原文本中定位；数量与付款方式无差异，按继承处理。

参考输出包含：

- `results.json`：逐费用行标准记录；
- `audit.jsonl`：字段证据与决策轨迹；
- `summary.json`：文档分类、OCR/LLM 状态、操作与 AR/AP 统计及 manifest。
