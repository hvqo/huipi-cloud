# Rubric 候选评分验证边界

## 指标定义

P3-B 的候选分数不是准确率、教学效果或真实学生评分质量指标。严格协议校验、评分边界校验和引用溯源只说明输出满足可机读约束，不能证明模型正确理解了解题过程或 Rubric。新候选评分中，每个正分 Criterion 必须带有一条经原文和字符范围校验的 quote；零分可以仅引用 `no_evidence`，部分分可以同时引用 quote 与 `no_evidence`。这一规则不判断引用在教学语义上是否足以支持所给分数。

合成闭环使用程序建立的作业、Question、AnswerKey、Rubric、解析/Canonical/Alignment 和人工 `response_present`。真实 Ollama 测试只验证当前一条合成 GradeInput 的模型调用、候选结果协议、PostgreSQL 索引和私有 S3 对象哈希。它不覆盖真实学生笔迹、完整 MinerU 模型解析或评分者一致性。

## 本地真实 Ollama 专项记录

2026-10-11 使用本机 `qwen3.5:4b`，在真实 PostgreSQL 测试库和本地 S3 兼容测试 bucket 上运行 `grade_llm_e2e`。输入是单个合成回答字符 `4`、一条十分制评分细则和人工准备的 GradeInput。最终结果为 `completed_candidate`，唯一 Criterion 得到 10.00 / 10.00；有效引用是 segment 0 的 Unicode 码点半开范围 `[0, 1)`。proposal 已从测试对象存储读回并通过对象大小、SHA-256、严格协议和数据库索引校验；人工复核记录数仍为 1。

该次模型用量为 prompt 562 tokens、completion 197 tokens，报告耗时 3,200 ms。一次样本不具有性能或准确率代表性，也不能因单个 10/10 结果宣称评分准确。该输出含有效原文 quote，符合本轮新增的正分结构门禁；这不验证模型对复杂 Rubric、多步骤过程、部分分、模糊手写或混合多模态内容的评分能力。

2026-10-12 在新增 quote 门禁与 `rubric-candidate-v3` 提示下再次运行同一类合成 PostgreSQL/MinIO/Ollama 闭环，使用 `qwen3.5:4b`。结果仍为 `completed_candidate`，得分 10.00 / 10.00，引用为 segment 0 的 `[0, 1)`，proposal 可由测试验证流程读回。Provider 报告 prompt 663 tokens、completion 195 tokens、耗时 8,450 ms。这个单样本只证明新提示和真实服务链路兼容，不是准确率或性能结论。

早期同模型试跑将提示中的弃权结构包成额外对象，随后又因示例结构和 Unicode 偏移提示不够清楚而产生协议/引用错误。严格协议拒绝了这些输出，没有创建 proposal 索引。提示 v2 明确顶层回复结构，并将示例引用长度与半开范围保持一致后，以上最终闭环通过；验证没有放宽回复 schema 或引用检查。

## 后续评测需要

- 使用经许可、去标识化的真实作业，并由至少两名教师按 Rubric 独立评分与仲裁。
- 按题型、Rubric 复杂度、书写质量、公式和页面质量分层报告模型分数误差、逐项误差、弃权比例和引用有效率。
- 报告每类指标的原始样本数及置信区间；单次本地模型调用只作为连通性和持久化验证。
- 人工检查理由是否对应 Rubric、引用是否构成充分证据，以及部分得分是否符合教研规则。
- 在引入真实教师复核和成绩发布流程前，候选分数不得作为最终成绩使用。

## P3-C1：Rubric 候选评分离线评测

### 数据集与标签边界

`data/evaluation/grading/rubric-cases-v1.json` 是版本 `1.0` 的纯合成数据集，SHA-256 由同目录 `.sha256` 清单固定。数据集中有 26 个案例：24 个参考标签明确、进入统计；2 个因手写内容或 Rubric 不确定而排除。所有纳入标签的来源标记为 `synthetic_curated_reference`，由规则明确的合成案例整理，不是教师独立评分，也不是模型输出。正分参考项必须附能在学生片段中唯一定位的引用；不确定参考不得作为零分样本。

2026-10-13 对26个 v1 案例做了逐项参考标签审计。发现 `equivalent-fraction` 的“最简分数”与“接受等值小数”口径不一致，另有两个过程题给复写题干中的方程计分。v1 数据、SHA、qwen 预测和原始报告全部保留。新建的 v2 将等值小数拆为严格形式与明确允许等值形式，并修订两个过程 Rubric；详见[参考标签审计记录](rubric-reference-audit.md)。v2 有25个纳入案例、2个不确定案例，SHA 为 `0da5948b79a15839d6abb939f134948e8d58dad101b66862ed7d4abaac40b26d`。v2 没有模型调用，运行清单把所有纳入案例明确标为 `not_requested`。

评测器只把题干、AnswerKey、Rubric 和学生片段构造成内存中的 `GradeInputDocument`。参考分数、参考理由、标签来源、难度和参考引用不进入 P3-B Prompt。它不是已保存的 GradeInputArtifact，不写 PostgreSQL 或 S3。预测 JSON 与参考数据分开保存；预测绑定数据集版本和 SHA。离线重算时会重新验证模型回复协议、Rubric 分数和 quote 来源，并拒绝重复、缺失或额外案例、SHA 不匹配和伪造的保存状态。默认 CLI 只验证数据，不发送 Provider 请求。

### 指标定义

- 总分误差只统计同时满足 P3-B 确定性验证的候选。MAE 的分子是绝对误差 Decimal 总和，分母是有效候选数；精确计数为总分完全相等的候选数/有效候选数。`within_tolerance` 使用绝对误差不超过 0.50 分。over/under 以候选总分相对参考总分的方向计数。
- Criterion 指标逐项比较，不因总分抵消而隐藏错误。报告整体 MAE/Exact/Over/Under、每个 `case_id/criterion_key` 的统计，以及总分正确但逐项不一致的案例。
- 覆盖率分母同时给出已请求案例数和纳入参考数据集案例数。弃权、严格输出协议失败、Rubric 失败、证据失败、Provider 失败与超时分别统计；没有有效候选的案例不进入分数 MAE 分母，但保留在逐例失败报告中。
- Quote 检查只验证结构、原文字符区间和参考标注区间重叠。引用确实来自答案，不证明它在教学语义上足以支持该 Criterion 分数；该判断仍需人工检查。
- 离线指标不更改参考标签，也不根据模型输出来筛除失败案例。每个指标提供原始分子/分母或误差总和/样本数。

### 命令

默认运行是无模型请求的 v2 dry-run；历史 v1 复算必须显式指定数据集：

```bash
uv run python scripts/evaluate_grade_proposals.py --dry-run
```

只有显式加 `--run-model` 才调用现有 P3-B Provider。每个纳入案例最多一次请求，支持最多 30 个案例、每次 1–180 秒超时；本地基线关闭格式重试，以便一次输出对应一条可重算预测。示例：

```bash
uv run python scripts/evaluate_grade_proposals.py \
  --dataset data/evaluation/grading/rubric-cases-v1.json \
  --run-model --model qwen3.5:4b --max-requests 24 --timeout-seconds 90 \
  --output data/evaluation/grading/predictions-qwen3.5-4b-v1-reproduction.json \
  --report-output data/evaluation/grading/report-qwen3.5-4b-v1-reproduction.json

uv run python scripts/evaluate_grade_proposals.py \
  --dataset data/evaluation/grading/rubric-cases-v1.json \
  --evaluate-predictions data/evaluation/grading/predictions-qwen3.5-4b-v1.json \
  --report-output /tmp/huipi-v1-audited-new.json
```

不要用离线重算覆盖 `report-qwen3.5-4b-v1.json`。对真实模型重跑也必须使用新的预测文件名；当前 v1 预测文件是不可变历史基线。

模型名必须已在本机安装；CLI 不下载模型。预测文件仅包含合成作答的原始模型回复、状态、usage、延迟和生成配置摘要，不包含真实学生内容或 Provider 密钥。

### 本机合成基线

2026-10-12 使用本机 `ollama_native / qwen3.5:4b`、`rubric-candidate-v3`，对 24 个纳入样本各发出一次请求；2 个不确定标签案例未发送。请求均在一次调用内返回，无 Provider 超时、请求故障或模型弃权。

| 项目 | 原始结果 |
| --- | ---: |
| 严格 GradeModelReply 协议通过 | 3 / 24 |
| 通过 Rubric 与证据校验的候选 | 2 / 24 |
| 模型弃权 | 0 / 24 |
| 协议失败 | 21 / 24 |
| 证据门禁失败 | 1 / 24 |
| 候选覆盖率 | 2 / 24 = 0.0833 |
| 总分完全相等 | 2 / 2 个有效候选 |
| 总分 MAE | 0.00 / 2 = 0.0000 |
| Criterion 完全相等 | 2 / 2 个有效 Criterion |
| Criterion MAE | 0.00 / 2 = 0.0000 |
| 合计模型耗时 | 137,496 ms / 24，均值 5,729 ms |
| Token 用量 | prompt 17,951；completion 9,172 |

两个通过的案例是 `equivalent-fraction` 与 `ratio-simplification`，两例各一项分数均与合成参考相同。由于仅 2 个有效候选且覆盖率很低，不能把 2/2 或零 MAE 称为评分准确率。

其余失败逐例列在版本化报告和预测文件中。21 个协议失败均使用 `model_evidence_protocol_invalid`：保存的模型回复包含 33 处 Unicode quote 长度与半开偏移不一致、2 处空 quote、1 处 `no_evidence` 多带 `segment_index`。`off-topic-zero` 的回复本身通过协议，但给了正分且只提供 `no_evidence`，因此被 P3-B 以 `positive_score_without_quote` 拒绝。没有通过修补偏移、改写输出或调整人工参考来改善结果。

新增审计报告将案例数和发生次数分开：19个协议失败案例有33处 quote 长度/跨度问题；11个案例有21处声明位置不匹配，但精确文本在同一片段其他位置存在；2个案例有2处文本无法在片段找到；空 quote 为2例2处；`no_evidence` 多字段为1例1处。这些类别可以在同一案例重叠。匹配诊断只比较原始字符串并记录次数/范围是否吻合；它不会修正偏移、改写引用或放宽 Quote 门禁。

审计报告还分开展示请求响应、JSON解析、严格回复协议、Rubric/分数约束、Quote来源、可用候选和候选分数比较。原始 qwen 输出中有12个案例的未验证分项分数与 v1 标签存在差异，其中包括错误答案或未满足 Criterion 时仍给分的情况。它们仍是未验证的模型输出，不进入 MAE/Exact 分母；报告保留案例供人工判断，且不包含原文 Quote 或理由。v1原始报告中的分数指标没有变化。评分理由的教学语义仍需人工判断。

预测、指标和逐例报告位于 `data/evaluation/grading/`。该测试使用合成题目和答案，不包含真实 MinerU、真实教师标注或学生作业；它只展示当前 Prompt/模型的协议可用度与严格验证后的候选覆盖，不能推出真实教学作业的分数质量。
