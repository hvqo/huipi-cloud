# P3-C1 面试复盘：Rubric 候选评分质量评测

本评测只对合成参考数据和 P3-B 候选结果做离线分析。它不生成正式成绩，也不声称代表真实学生作业准确率。

## 1. 如何避免模型看到参考标签？

**简洁解释**：评测参考和模型输入分开建模。Prompt 只从 GradeInput 字段构造。

**约 30 秒回答**：数据集里的标准分、逐项理由、难度和参考证据只由评测器读取。模型调用前，代码把题干、教师答案、Rubric 和学生片段复制到一个内存中的 GradeInputDocument，再调用已有 P3-B Prompt。参考字段不属于 GradeInput 协议，也不写进 Prompt。预测文件另存模型回复，并绑定数据集文件 SHA，离线重算时要求 SHA 完全一致。

**约 2 分钟深入回答**：评测有三类对象。版本化数据集持有题目输入与 `synthetic_curated_reference` 标签；内存 GradeInput 只含候选评分需要的数据；预测文件只含 Provider 回复、状态、usage、延迟和配置摘要。`build_grade_input()` 显式选择输入字段，而不是把整个 Case 序列化发送。数据集和预测文件通过版本和文件 SHA 关联，重算时检查完整 case_id 集合，拒绝重复、缺失、额外案例或 SHA 漂移。这样一次修改标签不能静默套用旧预测。

**连续追问**：AnswerKey 是不是标签泄漏？不是。它是 P3-B 评分时本来就允许使用的教师参考答案；人工参考得分、标注理由和案例标签才是离线评测真值，不能进入模型输入。

**代码与演示**：[evaluation.py](../../src/huipi_cloud/modules/grading/evaluation.py) `build_grade_input()`、`load_dataset()`；[prompts.py](../../src/huipi_cloud/modules/grading/prompts.py) `build_user_prompt()`；单测 `test_prompt_and_transient_grade_input_exclude_gold_rationales_and_labels`、`test_dataset_and_prediction_case_sets_and_hash_must_match`。

## 2. 为什么参考标签要标为 synthetic_curated_reference？

**简洁解释**：案例是人工整理的合成题目，不是教师独立双评或真实学生作业。

**约 30 秒回答**：这些标签用于覆盖正确、错误、缺步骤、单位遗漏、跨片段、提示注入文本等边界。它们来自明确规则的合成案例，不代表教师评分共识。两个参考标准不清楚的案例明确排除，不作为零分。报告因此只说明当前模型在这批合成样本上的候选覆盖和误差。

**约 2 分钟深入回答**：评测标签若由模型输出或反复调到模型更容易通过，会形成循环验证。数据集在运行模型前固定 SHA，每项 ReferenceCriterionScore 必须与 Rubric 项一一对应、Decimal 总分一致，正分需要唯一可定位的参考引用。难以判定的案例使用 `excluded_uncertain`，保留排除原因但没有分数标签。即使通过这些检查，规则标签也不是教师之间的一致性结论。真实评分质量评测还需要有授权的作业、至少两名教师独立按 Rubric 评分、分歧仲裁和按题型分层分析。

**连续追问**：是否可以把 2/2 的有效候选说成 100% 准确？不能。有效覆盖仅 2/24，且两个都是简单合成案例，无法推断真实作业表现。

**代码与演示**：[rubric-cases-v1.json](../../data/evaluation/grading/rubric-cases-v1.json)、[SHA-256 清单](../../data/evaluation/grading/rubric-cases-v1.json.sha256)；执行 `uv run python scripts/evaluate_grade_proposals.py --dry-run` 可查看固定 SHA 和纳入/排除计数。

## 3. 为什么只对验证通过的候选计算 MAE？

**简洁解释**：只有结构合法、Criterion 集合完整、分值有效且证据通过检查的结果才有可比较的分数。

**约 30 秒回答**：MAE 的样本是有效候选数，分子是每题候选总分与参考总分的绝对差之和。协议错误、引用错误、Provider 错误和模型弃权不被转换成0分，而是单独作为覆盖或失败指标报告。因此 MAE 和覆盖率要一起看，不能只看有效候选上的分数误差。

**约 2 分钟深入回答**：P3-B 的 `GradeModelReply` 检查输出合同，`_validate_candidate_scores()` 使用可信 Rubric 检查每项分值、总分、quote 和 Criterion 完整性。评测器重复调用同一验证路径，避免离线 JSON 仅凭 `status="ok"` 进入分母。MAE、exact 和 within-tolerance 的分母都是有效候选数；协议通过率、Candidate coverage、Abstention rate、Provider failure 和 invalid evidence 各自使用请求数或明确的覆盖分母。报告也包含逐 Criterion 的错误，防止总分误差抵消。

**连续追问**：协议失败是否等于模型没有评分能力？不能单凭一次协议输出判断语义能力，但在当前严格接口下它不能作为有效候选使用，因此必须计入操作覆盖失败。

**代码与演示**：[evaluation.py](../../src/huipi_cloud/modules/grading/evaluation.py) `evaluate_predictions()`、`_derive_prediction()`；[proposal_service.py](../../src/huipi_cloud/modules/grading/proposal_service.py) `_validate_candidate_scores()`；测试 `test_invalid_json_and_invalid_quote_are_not_candidate_coverage`。

## 4. 为什么既算总分，又算 Criterion 分数？

**简洁解释**：不同评分项的错误可能互相抵消，总分正确不表示评分细则应用正确。

**约 30 秒回答**：如果模型把 Criterion A 多给2分、Criterion B 少给2分，总分仍然相同。报告会分别检查总分和每个 Criterion，记录逐项 MAE、exact、over/under，并统计总分完全相同但逐项错误的案例。

**约 2 分钟深入回答**：数据集对每个案例保存 Criterion ID 对应的参考分数。评测器在通过 P3-B Criterion 集合检查后，逐项计算 Decimal delta，再累计绝对误差和方向计数。总分是逐项分数之和，而不是独立解释模型 total。测试用 6/4 参考与 5/3 预测验证了总分同为10但两项都错，报告必须同时记录 total exact 和 criterion error。

**连续追问**：逐 Criterion 指标能证明 Rubric 理解正确吗？不能。它只比较数值标签；理由和 quote 的教学充分性仍需教师审查。

**代码与演示**：[evaluation.py](../../src/huipi_cloud/modules/grading/evaluation.py) 中 `criterion_deltas` 和 `offsetting_case_ids`；单测 `test_offsetting_criterion_errors_do_not_disappear_in_exact_total`。

## 5. Quote 区域重叠能说明什么？

**简洁解释**：它检查模型引用的字符区间与合成参考区间是否重叠，不证明引用语义充分。

**约 30 秒回答**：模型引用仍先经过 P3-B 原文与 Unicode 半开偏移校验。通过后评测器按案例、Criterion 和 segment 计算区间并集、交集、Precision、Recall 和 IoU。该指标只看字符位置；模型可能引用了确实存在、但不能支持分数的内容，所以语义支持标为需要人工审查。

**约 2 分钟深入回答**：数据集的参考 quote 必须能在指定 segment 唯一定位，允许显式 offset 消除重复片段歧义。模型 quote 必须通过现有 `_validate_quote_reference()`。区间使用 Python Unicode 码点半开 `[start,end)`；先在同一案例、Criterion 和 segment 内归并范围，再累计 gold、predicted 和交集字符数。错误、无效或未验证的模型输出不能提高证据匹配分。重叠高也不表示因果、语义或评分理由成立。

**连续追问**：为什么不做语义相似度指标？本轮只复用可确定的字符来源合同；没有可信的教师语义标注或校准方法，随意引入相似度分数会伪装成教学证据。

**代码与演示**：[evaluation.py](../../src/huipi_cloud/modules/grading/evaluation.py) `_evidence_overlap()`、`_merge_ranges()`、`_intersection_length()`；测试 `test_perfect_scores_and_evidence_match_are_counted_exactly`。

## 6. 如何复现真实 qwen3.5:4b 基线？

**简洁解释**：只调用本机现有 Ollama 模型，默认不自动运行，也不把合成结果说成真实作业准确率。

**约 30 秒回答**：数据 dry-run 默认不发请求。只有 `--run-model` 才建立现有 Ollama Provider；每个请求超时有限，案例数也有限，模型不下载。预测 JSON 保存每例回复、状态和 usage，指标可通过 `--evaluate-predictions` 完全离线重算。当前本机 24 个样本中只有2个通过候选校验，21个协议失败、1个证据门禁失败。

**约 2 分钟深入回答**：真实基线使用已有 `build_text_grading_provider()`、`PROMPT_VERSION`、`SYSTEM_PROMPT`、`build_user_prompt()`、`_call_and_validate()` 与 `_validate_candidate_scores()`。本轮在 in-memory 合成 GradeInput 上运行，不连接 PostgreSQL/MinIO，也不改写人工复核或 proposal 表。Provider 返回的 prompt/completion tokens 和耗时逐例保存；配置摘要不写 endpoint 原文或密钥。错误保存在独立状态，不补造quote、不调整参考标签、不仅报告有效候选。

**连续追问**：测试环境不安装 qwen 时 GitHub CI 如何验证？默认 CI 只运行假 Provider 或纯离线测试，不下载模型；本机真实模型基线是单独的 opt-in 证据。

**代码与演示**：[evaluate_grade_proposals.py](../../scripts/evaluate_grade_proposals.py)、[evaluation.py](../../src/huipi_cloud/modules/grading/evaluation.py)、[Provider](../../src/huipi_cloud/infrastructure/grading/provider.py)。运行命令见[评分开发指南](../development/grade-proposals.md)。

## 7. 当前评测结论的边界是什么？

**简洁解释**：当前有可复算的合成基线，但没有真实教师共识或真实作业精度结论。

**约 30 秒回答**：24个纳入的合成参考中，3个回复通过模型回复协议，2个成为有效候选。两个候选总分都正确，但有效覆盖只有2/24，而且题目有限。因此报告真实记录这个覆盖和失败分布，不把2/2称为准确率。下一步先需要教师独立标签和仲裁，再评估真实作业。

**约 2 分钟深入回答**：这批数据能验证数据协议、SHA 绑定、Decimal 指标、错误分母、Criterion 抵消和 quote 字符区间，也能观测到当前 qwen 的格式问题。它不能衡量不同教师对 Rubric 的一致性、复杂过程题的评分质量、手写/OCR错误传播、Prompt Injection下的语义鲁棒性或跨学校泛化。当前项目没有最终成绩发布闭环，模型 proposal 仍需教师复核。扩展评测前要确认作业授权和去标识化边界，做教师双评、仲裁、分层抽样与失败案例审查。

**连续追问**：进入 P3-C2 前的必要条件？先建立明确的教师参考分数与标签流程，保留双评分歧及仲裁记录，并评估如何由有权限的教师复核模型候选；不要仅靠扩大合成集宣布模型可自动评分。

**代码与演示**：[评测定义与真实合成基线](../evaluation/rubric-candidate-grading.md)；当前预测和逐例报告位于 `data/evaluation/grading/`。

## 8. 如何审计参考标签并解释协议失败？

**简洁解释**：先核对题干、AnswerKey、Rubric、Criterion 分数和学生片段。失败诊断只分类，不修改模型回复，也不改变候选门禁。

**约 30 秒回答**：我逐项复核了26个合成案例，发现三个纳入标签需要明确规则：等值小数是否符合“最简分数”要求，以及两个过程题是否给复写题干中的方程计分。v1 数据、原模型输出和报告保持不变。v2 单独拆分了两种分数表达口径，并把过程分改为真实变形步骤。v1 的21个协议失败再按案例数和引用发生次数分类；例如19个案例有33处 Quote 长度与跨度不一致。诊断只输出字段位置和原文精确匹配计数，不调整引用。模型尝试给未满足 Criterion 的答案分数时仍保留为人工审查风险，不能因为格式错误就忽略。

**约 2 分钟深入回答**：参考标签不是从模型预测反推。每项先检查题目明确要求和 Rubric 字面条件，再核对 Criterion 分数、总分、引用片段与理由。v1 中“最简分数”与“接受等值小数”条件不一致；新 v2 分成严格最简分数形式和明确允许等值表达两例。两个过程题曾让复写题干已知方程获得过程分，v2 改为实际变形标准。因为问题/Rubric改变，v2 不继承历史模型结果，运行清单将其标为 `not_requested`。对于 qwen 的失败，报告把 JSON 可解析、严格 Pydantic 协议、Criterion 与总分约束、原文 Quote 来源、候选可用性、分数对比和教学语义人工判断分开。19个长度错误案例和11个原文位置错误案例有重叠，33与21是引用发生次数，不是失败案例数。精确字符串在别处出现只说明可能是偏移问题，不会使候选通过校验。12个原始数值分项与参考标签不同的案例仍被列出，但它们未通过 P3-B 校验，不能计入候选准确率；语义结论需人工结合 Rubric判断。

**连续追问**：为什么保留 v1 的 `2/2`？它是历史输入和标签下两个有效候选的条件性对比，必须连同2/24覆盖率及等值分数标签歧义解释。不能将其称为总体准确率；v2尚未请求模型，因此也没有新版本准确率。

**代码与演示**：[评测实现](../../src/huipi_cloud/modules/grading/evaluation.py) `evaluate_predictions()`、`_diagnose_protocol_reply()`、`_source_quote_failure_diagnostics()`；[v1/v2案例和审计说明](../evaluation/rubric-reference-audit.md)。运行 v1 离线重算时输出到 `report-qwen3.5-4b-v1-audited.json`，可检查原始预测文件哈希不变。运行 `uv run python scripts/evaluate_grade_proposals.py --dataset data/evaluation/grading/rubric-cases-v2.json --dry-run` 可校验 v2 SHA 和 `provider_requests_sent=0`。
