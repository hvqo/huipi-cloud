# 题号与答案区域对齐评测

题号候选与 AnswerAlignment 的合成评测数据位于 `tests/fixtures/answer_alignment/` 及评测脚本使用的版本化样本目录。运行入口：

```bash
uv run python scripts/evaluate_answer_alignment.py
```

当前指标定义按案例、Question 和完整 Canonical `content_pointer` 区分来源身份。字符范围是节点文本内 Unicode 码点半开区间 `[start, end)`；同一节点内按定义归并重叠或相邻区间，不同 ContentNode 的相同本地偏移不会匹配。嵌套节点样本单独报告，不与旧的顶层 Block 样本混合。

题号识别使用保守规则：明确且无冲突的“第 n 题”可以参与自动对齐；`n.`、`n、`、`n)` 和括号题号作为候选，要求进一步检查。模块不读取 AnswerKey、不判断学生是否作答，也不会把 `not_observed` 解释为作答缺失。

样本均为合成数据，不代表真实作业的题号或答案边界准确率。父子节点与共享图形的归属限制见[架构文档](../architecture.md)和[人工复核指南](../development/answer-review.md)。
