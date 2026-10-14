# P3-D2A：独立评分质量留出评测

## 目的与边界

本评测用一组独立的合成案例检查 DeepSeek Flash 与 Quote Selection 协议的评分覆盖、逐项分数和高估风险。它不改变 P3-B 评分入口，不写入 GradeProposal 或 GradeReview，也不证明真实教学作业准确率。

参考标签来自 `synthetic_curated_reference`。项目作者先根据题目、答案、Rubric 与学生原文编写逐项分数、理由和引用，再锁定数据集 SHA；标签不是教师双评，也不是根据模型预测调整的结果。每个正分参考项必须能精确回到合成学生原文。结构一致性由 Pydantic 数据集校验和本目录的验证脚本检查；评分理由仍需人工审读。

## 留出集与不可变性

- 数据集：[`rubric-cases-v3.json`](../../data/evaluation/grading/rubric-cases-v3.json)，版本 `3.0`。
- SHA-256：`de59c65d31669043ffd49c062318861a542bd039cfd04c7df4d4645dc4df620d`。
- 案例：60 条，其中 55 条有明确参考分，5 条因关键材料缺失、来源不清或 Rubric 冲突而标为 `excluded_uncertain`。
- 55 条评分案例涵盖过程缺失、过程与结果冲突、部分分、跨段、数学等价表达、中文改写、化学配平、物理单位、题干抄录、答案关键词、重复 Quote、提示词注入和高估陷阱。案例不是把 v2 数字替换后复制而来。
- 5 条不确定材料会作为独立弃权探针各请求一次，但不参加分数误差或候选覆盖分母；参考标签保持空，不强制标为零分。
- v1、v2 数据集、预测和历史报告均保持不变。

先运行 `uv run python scripts/validate_grading_v3_holdout.py`，再用 `scripts/evaluate_quote_selection.py` 的 standalone 模式执行。验证脚本检查数据集、清单和准入条件 SHA，确认每个正分逐项引用与来源文本相符、逐项 Decimal 总和匹配，并检查预先定义的风险类别覆盖。

离线检查和费用预检不发送模型请求：

```bash
uv run python scripts/validate_grading_v3_holdout.py
uv run python scripts/evaluate_quote_selection.py \
  --dataset data/evaluation/grading/rubric-cases-v3.json \
  --dry-run --probe-excluded-uncertain \
  --input-usd-per-million 0.30 --output-usd-per-million 1.20 \
  --max-estimated-cost-usd 0.50
```

真实模型运行必须使用一个从未存在过的输出目录。仅在本轮已授权的合成数据范围内临时打开远程开关；`.env` 中的安全默认值保持关闭：

```bash
GRADING_ALLOW_REMOTE=true GRADING_EXTERNAL_DATA_AUTHORIZED=true \
uv run python scripts/evaluate_quote_selection.py \
  --run-model --standalone --probe-excluded-uncertain \
  --dataset data/evaluation/grading/rubric-cases-v3.json \
  --preregistered-manifest data/evaluation/grading/rubric-cases-v3-manifest.json \
  --output-dir data/evaluation/grading/deepseek-flash-grading-v3-2026-10-14-run-01 \
  --max-requests 55 --model deepseek-flash --timeout-seconds 120 \
  --max-output-tokens 4096 \
  --input-usd-per-million 0.30 --output-usd-per-million 1.20 \
  --max-estimated-cost-usd 0.50
```

费用参数必须按实际运行时段重新核对官方价格；示例仅表示本次运行预检所用的价格档。运行结束后，用 `scripts/summarize_grading_v3.py --run-dir <实际输出目录>` 从原始回复离线复算指标并生成 `human-semantic-review.json`。该清单保留合成题目、标签、模型引用和理由，全部初始状态为 `unreviewed`。

## 预先登记的准入条件

这些阈值保存在 [`p3d2a-acceptance-criteria-v1.json`](../../data/evaluation/grading/p3d2a-acceptance-criteria-v1.json)，清单记录其 SHA，并且都在第一次模型请求前锁定。运行结束后不按结果调整阈值。

| 类别 | 准入条件 |
| --- | --- |
| Provider 响应 | 60 次请求至少 59 次成功；无自动重试 |
| Quote Selection JSON 协议 | 至少 57/60 次有效 |
| 有效评分候选覆盖 | 55 条有标签案例中至少 44 条通过全部确定性校验 |
| Quote 来源定位 | 每个被接受候选中的 Quote 都唯一、精确地定位到指定 segment，要求 100% |
| 弃权探针 | 5 条不确定材料全部弃权；候选评分视为高风险失败 |
| 总分误差 | 有效候选总分 MAE 不高于 0.75 分（满分10分） |
| Criterion 误差 | 有效候选逐项 MAE 不高于 0.50 分 |
| 完全一致率 | 有效候选总分至少80%完全一致，逐 Criterion 至少80%完全一致 |
| 高估保护 | 9 条参考总分为零的高风险案例不得出现正的有效候选总分；关键零分 Criterion 不得被误给分 |
| 教学语义 | 任何生产集成前，合格人工复核必须确认所有正分证据确实支持对应 Criterion；当前机器评测本身不满足此项 |

数字门槛是继续实验的必要条件，不是成绩发布授权。语义充分性不能靠 Quote 字符串匹配判断；在人工审核完成前，即使其余指标全过，也不建议接入生产评分流程。

## 请求与费用约束

实验固定使用 `openai_compatible` Provider、`deepseek-flash`、Thinking disabled、最多4096输出 Token、120秒超时、0次重试。评分输入和标签分开，提示仅包含题干、AnswerKey、Rubric 和学生合成片段。每个有标签案例一次请求，每个不确定探针一次请求；不对错误答案重复请求直到得到期望分数。

费用先使用 UTF-8 输入字节上界及每次最大输出 Token 预估，并把单轮上限设为 USD 0.50。此估算不是账户硬限额或最终账单；最终报告优先记录 Provider 返回的 Token 用量，并标注缺失用量。具体价格必须在运行当天重新核对官方价格页面。

## 人工复核清单

真实响应保存后生成合成数据专用复核清单，包含案例、题目与 Rubric、参考与模型分数、引用/理由、差异和高风险标签。初始 `review_status` 一律是 `unreviewed`。清单不写入正式 GradeReviewRevision，不含真实学生信息。人工审核需区分“引用字面存在”与“引用语义上支持得分”。

## 结果解释

报告分别列出 Provider 成功、JSON 协议、Rubric/Quote 确定性校验、候选覆盖、分数误差、高估风险及人工复核状态。候选准确率以已通过严格验证的候选为条件，必须同时报告覆盖率，不能把条件准确率当作全体案例表现。自动 Quote 定位成功只证明原文位置合法，不证明引用充分或评分理由在教学上正确。

合成留出集通过也不能代表真实课堂作业的准确率。进入后续生产集成前至少需要教师独立标注、双评或仲裁、真实作业覆盖、隐私授权及稳定的高估风险控制。
