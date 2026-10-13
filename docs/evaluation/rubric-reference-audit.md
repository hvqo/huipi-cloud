# Rubric 候选评分参考标签审计

## 审计边界

本次逐项核对 v1 的 26 个合成案例：题干、AnswerKey、Rubric 上限和措辞、学生片段、Criterion 分数、引用和理由。参考标签由案例规则审查，不按模型输出调分。审计结果只说明这批合成标注与书面条件的一致性，不代表教师之间已经达成评分共识。

| v1 案例 | 审计结论 | 核对说明 |
| --- | --- | --- |
| `linear-correct` | 一致 | 正确答案满足唯一 Criterion。 |
| `linear-wrong-answer` | 一致 | 答案不满足方程，且没有可评分的正确过程，记 0。 |
| `linear-missing-step` | 修订到 v2 | v1 给“正确写出原方程”3分，但学生只重复题干中的已知方程。这不是题目要求的变形过程，v2 不再给复写分。 |
| `equivalent-fraction` | 拆分到 v2 | 题干和 Rubric 要求最简分数，但参考理由额外接受等值小数；标签隐含了未写明的宽容规则。 |
| `linear-sign-error` | 一致 | 移项和结果均不符合 Rubric，记 0。 |
| `physics-missing-unit` | 一致 | 数值计算符合6分项；缺少明确要求的单位，单位项记0。 |
| `chemistry-balanced` | 一致 | 配平方程及系数满足两项要求。 |
| `biology-incomplete` | 一致 | 只覆盖遗传信息储存，未覆盖调控功能。 |
| `geometry-cross-page` | 一致 | 定理、运算和结果分别有可追溯片段，三项均有证据。 |
| `photosynthesis-omission` | 一致 | 只写出葡萄糖，氧气项记0。 |
| `off-topic-zero` | 一致 | 学生片段与所问沸点无关，记0。 |
| `prompt-injection-data` | 一致 | 指令样文字不构成变形步骤；最终答案正确，只给答案项分。 |
| `corrected-redundant-work` | 一致 | 正确乘法被明确更正，最终结果符合题意。 |
| `answer-only-no-method` | 一致 | Rubric 明确拆分方法和答案；只给最终答案项分。 |
| `correct-transform-wrong-result` | 修订到 v2 | v1 的5分“保留方程”项可由照抄题干满足，却被放在过程题里；v2 改为要求实际写出两边同除以3。 |
| `ratio-simplification` | 一致 | 4:6 化成最简比2:3。 |
| `pythagorean-proof` | 一致 | 定理、平方和及斜边结果均符合各项要求。 |
| `physics-substitution` | 一致 | 公式、代入计算和单位均有对应证据。 |
| `neutralization-products` | 一致 | 两种生成物正确；没有写完整方程，配平项记0。 |
| `two-segment-evidence` | 一致 | 两个独立片段分别支持运算和答案。 |
| `quadratic-factorization` | 一致 | 两数条件及因式分解均满足 Rubric。 |
| `fraction-partial` | 一致 | 通分和加法正确；未化成最简结果，简化项记0。 |
| `zero-evidence-absence` | 一致 | 该合成片段不含相关答案；不据此推断真实学生是否会答。 |
| `literature-two-criteria` | 规则一致但范围窄 | 标签符合这道措辞明确的合成题；不能据此推断开放式文学题评分可靠。 |
| `ambiguous-handwriting-excluded` | 正确排除 | 文字样本无法确定原始手写数字，没有用模型猜测填标签。 |
| `ambiguous-rubric-excluded` | 正确排除 | “适当提高”缺少可操作定义，没有伪造确定分数。 |

结论：21 个纳入案例的 v1 标签符合各自明确的题干和 Rubric；3 个纳入案例需要明确评分边界；2 个不确定案例仍排除。v1 的原始分数在本次审计中不修改。

## v2 参考口径

v2 独立发布为 [rubric-cases-v2.json](../../data/evaluation/grading/rubric-cases-v2.json)，不覆盖 v1。其 SHA-256 为 `0da5948b79a15839d6abb939f134948e8d58dad101b66862ed7d4abaac40b26d`，由同目录清单校验。

- `equivalent-fraction-strict-format`：题目和 Criterion 明确要求最简分数，学生片段 `0.5` 的参考分为0/5。
- `equivalent-fraction-equivalent-form-allowed`：题目和 Criterion 明确接受数值等价小数，学生片段 `0.5` 的参考分为5/5。
- `linear-missing-step-v2`：不再把复写题干方程算作过程分；正确的最终答案得3分，未展示变形得0分。
- `correct-transform-wrong-result-v2`：过程分要求实际展示两边同除以3；复写题干和错误答案均不满足要求，参考分为0/10。

这些分数来自修订后的明确条件，不来自模型预测。v2 当前25个纳入案例和2个排除案例都没有进行模型调用。`rubric-cases-v2-run-manifest.json` 将纳入案例标为 `not_requested`，并绑定 v2 数据集 SHA。v1 的预测及原始报告保留原样，不能用于冒充 v2 预测。

## 真实模型协议失败诊断

v1 的模型输出保持字节不变。评测器新增只读诊断：使用 JSON 解析、Pydantic 结构错误类型、字段路径和原文精确匹配；不依据单条报错文本做脆弱分类，不改 quote 偏移，不搜索替换引用。逐例报告只写案例 ID、字段位置、字符数、原文精确匹配次数和是否匹配声明区间，不写完整 Quote、答案或理由。

本地 v1 预测包含21个 `protocol_error` 案例。以下“案例数”按不同案例去重；“发生次数”按独立字段或引用位置计数，类别可能重叠：

| 诊断类别 | 案例数 | 发生次数 |
| --- | ---: | ---: |
| Quote 长度与偏移跨度不一致 | 19 | 33 |
| 声明区间与原文位置不符，但相同字符串在该片段其他位置出现 | 11 | 21 |
| Quote 文本在声明片段中不存在 | 2 | 2 |
| 空 Quote | 2 | 2 |
| `no_evidence` 对象含协议外字段 | 1 | 1 |

有20个协议失败案例至少包含长度或偏移诊断，类别有重叠。另有2个合法协议候选的 Quote 在原文声明位置有效；`off-topic-zero` 没有 Quote，却给出正分，只能通过 `positive_score_without_quote` 证据门禁被拒绝。精确字符串匹配仅用于诊断偏移与文本是否可能错位，不能使候选通过协议。

## 数值差异与语义边界

诊断报告另列12个案例，其中原始 JSON 的未验证候选分数与 v1 参考 Criterion 分数存在差异。它们包含错误答案、遗漏单位或步骤、无关回答、缺少第二项要点等情况。这里展示的是未通过完整 P3-B 校验的原始数值意图，不进入候选评分指标，也不能自动定性为教学评分错误；每项仍需结合明确的 Rubric 和人工复核。`linear-missing-step` 的旧分数尤其受本次标签审计影响，不能拿来评价模型。

报告分别展示：请求有响应、JSON 可解析、`GradeModelReply` 协议有效、Criterion 与总分约束有效、Quote 来源有效、最终候选可用、候选总分比较。v1 的 `2/2` 是两个有效候选上的条件性数值一致，`2/24` 是候选覆盖率，`3/24` 是严格回复协议通过率。评分理由是否符合教学语义始终标记为人工判断；总分相等也不能证明逐项评分或理由正确。

原始 v1 预测和报告保持不变；新增审计报告为 `report-qwen3.5-4b-v1-audited.json`，它重算原 v1 预测，并附加失败归因与阶段解释。原报告中已有的数值指标字段经回归测试逐项比较，保持不变。
