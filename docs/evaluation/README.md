# 评测资料

- [视觉作答证据：数据集、协议、指标和模型基线](visual-evidence.md)
- [题号与答案区域对齐：合成评测定义](answer-alignment.md)
- [Rubric 候选评分：协议、版本化合成集和离线质量评测](rubric-candidate-grading.md)
- [Rubric 候选评分参考标签审计与协议失败归因](rubric-reference-audit.md)
- [DeepSeek Provider：授权、合成验收与 v2 评测计划](deepseek-provider-validation.md)
- [P3-D1 DeepSeek 严格 Quote 与确定性定位对照](p3d1-deepseek-quote-selection-2026-10-14.md)
- [P3-D2A 独立评分质量留出集与准入门槛](p3d2a-grading-quality-v3.md)
- [P3-D2A v3参考标签审计、Quote口径与评分失败归因](p3d2a-v3-audit.md)

- [Rubric语义一致性Prompt实验与独立留出验证](p3d2a-rubric-semantics.md)

评测报告必须区分合成数据与真实作业、模型协议通过与识别正确、视觉候选与 Canonical 来源映射。没有可信来源标注时，应使用 `not_evaluated`，不能推算准确率。

- [教师 Copilot 云端与业务闭环验收](teacher-copilot-validation.md)

- [作用域知识检索基线与真实合成验收](scoped-retrieval.md)

- [本地Embedding与混合检索基线](hybrid-retrieval.md)

- [Rerank独立留出评测](rerank-retrieval.md)
