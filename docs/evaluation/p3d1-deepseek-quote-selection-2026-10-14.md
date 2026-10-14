# P3-D1：DeepSeek 严格 Quote 与确定性定位对照

## 目的与数据边界

本次使用 v2 版本的 27 个合成评分案例。25 个有明确合成参考标签，2 个不确定案例排除。数据文件 SHA-256 为
`0da5948b79a15839d6abb939f134948e8d58dad101b66862ed7d4abaac40b26d`。模型输入只含评分所需的题干、标准答案、Rubric 和合成学生片段；参考分、参考理由和标签出处不进入模型 Prompt。未发送真实学生作业，也没有修改 v1/v2 数据集、参考标签或 qwen v1 预测。

DeepSeek 的 OpenAI-compatible 模型名为 `deepseek-flash`，官方文档将该别名映射至 **DeepSeek-V4.1-Flash**。A、B 两组都通过同一个 Provider 运行，使用 Thinking disabled、配置 reasoning effort=max（禁用思考时不发送该参数）、4096 输出 Token、120 秒超时、0 次格式重试。实际响应模型 ID 为 `deepseek-flash`。A 使用正式 `rubric-candidate-v3` 和原 P3-B 严格偏移协议；B 使用实验 Prompt `rubric-quote-selection-v1`，只选 `segment_index` 和逐字 Quote，再由程序做唯一精确定位。两组 Prompt 不同，因此这是一次配对观测，不是证明差异由单个协议变量造成的随机对照实验。

本次在 A 组后先执行了 19 项确定性合成协议单测，再发送 B 组请求；没有另做单独的少量付费模型试跑。两组均一次一请求，共 25 次，不重试。整个预算预检使用官方当前页面的 Flash 峰时未缓存输入价 `$0.30 / 1M tokens`、输出价 `$1.20 / 1M tokens`；价格会变动，金额只是上界估算，不是账单或账户限额。[DeepSeek 官方模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)

## A 组：严格字符偏移基线

预测文件：[predictions-deepseek-flash-v2-20261014.json](../../data/evaluation/grading/predictions-deepseek-flash-v2-20261014.json)，SHA-256：`1cc769df63c477b5a12e3145349141e1e958b15dac3dfb111912aaa6ba7293d0`。重算报告：[report-deepseek-flash-v2-20261014.json](../../data/evaluation/grading/report-deepseek-flash-v2-20261014.json)。结构化运行清单及校验和见[data目录](../../data/evaluation/grading/deepseek-flash-v2-20261014-run-manifest.json)。

| 阶段 | 结果 |
| --- | ---: |
| Provider 响应 | 25 / 25 |
| JSON 可解析 | 25 / 25 |
| 严格 `GradeModelReply` 有效 | 14 / 25 |
| Rubric 结构/算术有效 | 14 / 14 个协议有效候选输出 |
| Quote 来源校验有效 | 11 / 14 个候选输出；16 / 19 条引用 |
| 最终候选 | 11 / 25（44%） |
| 弃权 / 协议失败 / 证据失败 | 0 / 11 / 3 |
| 总分 MAE / 完全相等 | `3.00 / 11 = 0.2727`；10 / 11 |
| Criterion MAE / 完全相等 | `3.00 / 17 = 0.1765`；16 / 17 |

11 个严格协议失败案例及诊断如下。类别可在同一案例重叠，不能把发生次数当作失败案例数。

| 案例 | 诊断 |
| --- | --- |
| `biology-incomplete` | `no_evidence` 结构包含多余字段 |
| `chemistry-balanced` | Quote 长度与声明偏移不一致 |
| `corrected-redundant-work` | Quote 长度错误 |
| `fraction-partial` | Quote 长度与偏移错误 |
| `geometry-cross-page` | Quote 长度错误 |
| `literature-two-criteria` | Quote 长度错误 |
| `photosynthesis-omission` | `no_evidence` 结构包含多余字段 |
| `physics-missing-unit` | `no_evidence` 结构与 Quote 长度错误 |
| `physics-substitution` | Quote 长度与偏移错误 |
| `pythagorean-proof` | Quote 长度错误 |
| `zero-evidence-absence` | `no_evidence` 结构包含多余字段 |

合计为 8 个案例 / 13 次 Quote 长度问题、3 个案例 / 4 次偏移问题、4 个案例 / 4 次 `no_evidence` 结构问题。另有 3 个候选因证据门禁失败：`linear-sign-error`、`prompt-injection-data` 和 `quadratic-factorization`。在 20 条可检查的 Quote 中，13 条文本虽然在指定片段内出现，但不在模型给出的偏移；5 条位于声明偏移；2 条偏移无法检查。没有自动改偏移或搜索其他片段替代原引用。

有效候选的分数不是无风险：`neutralization-products` 获得 8 分，对应合成参考 5 分，其中“balanced”项多给 3 分；该案例进入候选分数指标并形成唯一高估。这只是合成标签上的语义风险，必须由人工评阅，不能从数据自动断言教学解释正确或错误。

## B 组：模型选文、程序定位

新预测及所有报告均在[独立运行目录](../../data/evaluation/grading/deepseek-flash-quote-selection-v2-20261014/)中。原始预测 SHA-256：`f83a4cb9ca481728331e677aed542b6824d3d670742edce1541cfcd7a67c9e44`。运行清单绑定 A、B 两组预测、报告、数据集 SHA 及成本预检。

| 阶段 | 结果 |
| --- | ---: |
| Provider 响应 / JSON 可解析 | 25 / 25；25 / 25 |
| 实验 `GradeQuoteSelectionReply` 协议有效 | 25 / 25 |
| Rubric 结构/分数约束有效 | 25 / 25 |
| 模型选择的 Quote | 38 条 |
| 指定 segment 内唯一精确定位 | 38 / 38 |
| 最终候选 | 24 / 25（96%） |
| 弃权 / 协议失败 / 证据失败 | 0 / 0 / 1 |
| 候选总分完全相等 / 条件 MAE | 24 / 24；`0.00 / 24 = 0.0000` |
| Criterion 完全相等 / 条件 MAE | 46 / 46；`0.00 / 46 = 0.0000` |

唯一被拒绝的案例是 `neutralization-products`：模型的一个正分 Criterion 只有 `no_evidence`，没有可定位 Quote，仍由既有 `positive_score_without_quote` 规则拒绝。其未经验证的数值意图是 8 分、合成参考是 5 分；因为证据不合格，它不进入候选 MAE/精确率分母。B 组没有放宽 P3-B 的评分或证据校验。

## 配对结果与解释

两组都尝试了同一组 25 个纳入案例，且 `generation_config_digest` 相同。

| 配对结果 | 案例数 | 案例 |
| --- | ---: | --- |
| A、B 都成为候选 | 10 | `answer-only-no-method`、`correct-transform-wrong-result-v2`、`equivalent-fraction-equivalent-form-allowed`、`equivalent-fraction-strict-format`、`linear-correct`、`linear-missing-step-v2`、`linear-wrong-answer`、`off-topic-zero`、`ratio-simplification`、`two-segment-evidence` |
| 仅 A 成为候选 | 1 | `neutralization-products` |
| 仅 B 成为候选 | 14 | `biology-incomplete`、`chemistry-balanced`、`corrected-redundant-work`、`fraction-partial`、`geometry-cross-page`、`linear-sign-error`、`literature-two-criteria`、`photosynthesis-omission`、`physics-missing-unit`、`physics-substitution`、`prompt-injection-data`、`pythagorean-proof`、`quadratic-factorization`、`zero-evidence-absence` |
| 两组都未成为候选 | 0 | — |

在 10 个共同候选上，两组总分都与合成参考相等，条件 MAE 都为 `0.00 / 10`。B 的可用覆盖从 11/25 增至 24/25；B 的 24/24 分数相等率以“通过严格门禁的候选”为条件，不能替代 24/25 覆盖率，更不能称作真实教学准确率。相反，A 的唯一独有候选就是上面记录的高估风险，而 B 的同案例结果因缺少正向 Quote 被挡下。

B 的全 38 条精确定位说明本轮指定片段中的引用字符位置可靠；它不能证明模型选择了语义充分的 Quote，也不能证明 Rubric 解释正确。协议与 Prompt 同时改变，单轮结果有提示行为差异、样本规模小和运行顺序影响，不构成严格因果结论。现有 P3-B 生产 Quote 协议保持原样，B 仅保存在文件中用于比较，不创建 GradeProposal、GradeReview 或数据库记录。

## 用量、延迟和成本

| 组别 | Prompt / Completion Token | 平均 / P50 / P95 延迟 | 峰价 Token 估算 | 预检上限 |
| --- | ---: | ---: | ---: | ---: |
| A | 19,088 / 5,519 | 1,757 / 1,767 / 2,295 ms | `$0.0123492` | `$0.1475436` |
| B | 20,613 / 5,583 | 1,751 / 1,665 / 2,816 ms | `$0.0128835` | `$0.1495986` |

A 的 Provider 报告 6,400 cache-hit、12,688 cache-miss Token。B 的首轮预测文件没有保存 cache-hit/miss 和逐条 `finish_reason`；不能把未记录的缓存量报告为 0。Provider 只在正常、完整结束时返回成功响应，并拒绝 length、缺失或其他不完整结束；本轮 B 没有 Provider 失败。后续代码会保留这些响应元数据，但不为补字段重复进行收费调用。表中 Token 费用按官方 Flash 峰时未缓存输入价和输出价估算，B 也按未缓存输入计费；估算不代表 DeepSeek 账户账单。

## 可复现产物

- 数据集和清单：[rubric-cases-v2.json](../../data/evaluation/grading/rubric-cases-v2.json)、[SHA 清单](../../data/evaluation/grading/rubric-cases-v2.json.sha256)
- A 组预测和报告：[predictions](../../data/evaluation/grading/predictions-deepseek-flash-v2-20261014.json)、[report](../../data/evaluation/grading/report-deepseek-flash-v2-20261014.json)、[run manifest](../../data/evaluation/grading/deepseek-flash-v2-20261014-run-manifest.json)
- B 组预测、报告、配对比较和运行清单：[quote-selection run](../../data/evaluation/grading/deepseek-flash-quote-selection-v2-20261014/)
- 实验协议及只读离线计算：[quote_selection.py](../../src/huipi_cloud/modules/grading/quote_selection.py)、[quote_evaluation.py](../../src/huipi_cloud/modules/grading/quote_evaluation.py)、[CLI](../../scripts/evaluate_quote_selection.py)
- 单测：[test_quote_selection.py](../../tests/unit/test_quote_selection.py)

本地协议测试使用合成数据，不访问模型、PostgreSQL 或 S3。真实模型运行只在显式设置 DeepSeek Provider、API Key 和本次合成数据远程传输授权后执行。原始预测不覆盖；离线报告可从保存预测重新生成。

## P3-D2 前置判断

现有证据足以支持进入下一轮**受控的离线评分质量验证**：严格偏移确实大量拒绝候选，确定性定位消除了这些偏移故障，并明显提高合成候选覆盖；B 组还保留了正分必须有 Quote 的安全门禁。

现有证据不足以宣称评分质量已达到教学或生产标准，也不足以默认启用 Quote-selection 协议。下一阶段需要扩大独立合成集和失败样本、审阅模型的 Rubric 语义判断、针对错误答案/缺步/无关答案报告高估，并取得有许可且去标识化的真实作业与教师双评/仲裁后再评估真实质量。P3-D2 的目标、标签方案和接受阈值仍需单独定义；本报告不开始该阶段。
