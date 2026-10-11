# 视觉作答证据评测

本文记录合成数据协议、离线指标定义和当前本地模型观测。实验报告只支持其明确列出的数据和运行条件，不代表真实学生作业表现。

## 数据集与复现

数据集文件位于 `datasets/visual-evidence/synthetic-v1/`，包含 26 个案例和 27 张 PNG。人工标签分布为 `response_present=14`、`response_absent=7`、`uncertain=5`。区域标签包括印刷题干、模拟手写文字/公式、表格填答、几何标记和不清晰区域。笔画和图形均由程序绘制，不是人类真实手写。数据生成配方与标签保存在版本化 JSON 中；Loader 校验文件路径、格式、像素、尺寸和 SHA-256。

数据集清单 SHA-256：`7b3ec71e471087e5ae4a039f946caf3bee75f5c6501858982d43687c8b94d026`。

```bash
uv run python scripts/build_visual_evaluation_dataset.py
uv run python scripts/evaluate_visual_evidence.py --dry-run
```

模型调用必须显式使用 `--run-model` 并设置本地或已授权 Provider；离线重新计算使用已保存预测：

```bash
uv run python scripts/evaluate_visual_evidence.py \
  --evaluate-predictions datasets/visual-evidence/runs/qwen3.5-4b-synthetic-v1.json \
  --output /tmp/qwen3.5-4b-synthetic-v1.json
```

离线工具校验预测文件 schema、数据集 ID/版本/SHA、案例集合和每个模型回复。`status=ok` 的建议必须重新通过 `VisualEvidenceModelReply` 严格验证，且所有预测页面必须属于该案例实际提供的页面。协议失败、请求失败和输入/渲染失败作为独立状态保留。评测只读取预测与标签，指标写到单独的输出文件，不改原始预测和标签。

## 指标定义

- **作答存在性**：在 21 个确定人工标签上计算 TP/FP/FN/TN、Precision、Recall、FPR、FNR、弃权率和覆盖率；5 个 uncertain 标签不进入混淆矩阵。`candidate_prompt_only` 是候选负类，不是人工确认未作答。
- **响应区域候选检测**：只看目标 Question 的 response 区域，按页面和 IoU≥0.5 进行确定性一对一匹配；uncertain 案例不计入确定标签的 response 检测分母。
- **全部视觉区域定位**：在每个案例内，将预测与目标题的所有人工区域按 IoU 从高到低进行确定性一对一贪心匹配，并报告 TP/FP/FN、区域类别差异和每个真值区域的最佳 IoU。
- **Question 来源与自动接受**：视觉-only 数据集没有可信 Canonical/AnswerAlignment 来源指针标签，两项必须标为 `not_evaluated`。一个模型框匹配到唯一空间区域也不能证明属于该题。
- **运行质量**：记录请求/协议失败、耗时、可获得 token 用量、输入图像数、字节和像素数。小样本延迟和质量估计不稳定；没有真实计费资料时不计算成本。

## 当前本地基线

模型为本地 Ollama `qwen3.5:4b`。26 个请求均完成，JSON 协议通过 **26/26**；失败请求和协议失败均为 0。

| 指标 | 实际计数 | 结果 |
|---|---:|---:|
| 作答存在 TP / FP / FN / TN | 8 / 0 / 6 / 7 | Precision 8/8；Recall 8/14 |
| FPR / FNR | 0/7 / 6/14 | 0；0.429 |
| 弃权 / 覆盖 | 0/26 / 21/21 | 0%；100%（确定标签子集）|
| Response 检测 TP / FP / FN，IoU≥0.5 | 0 / 8 / 14 | Precision 0/8；Recall 0/14 |
| 全部区域定位 TP / FP / FN | 0 / 26 / 54 | Precision 0/26；Recall 0/54 |
| 所有真值区域的最佳 IoU 均值 | 总和 1.5841 / 54 | 0.0293 |

26 次调用平均/P50/P95 延迟为 **2927.6/2661/3224 ms**；prompt/completion token 合计 **46153/2005**。输入为 27 张图、584,736 字节及 37,800,000 像素。

已知几何标记案例的人工标签是 `response_present / geometry_mark`，模型返回 `candidate_prompt_only / printed_prompt`，属于模型视觉语义误判；它没有发生协议或服务错误。预测框未达到定位 IoU 阈值。逐案例的输出、BBox 和失败类别保存在 `datasets/visual-evidence/runs/qwen3.5-4b-synthetic-v1.json` 与对应 `.metrics.json`。不得据此声称真实手写准确率。

完整逐案例失败统计显示：26 个案例都存在区域未检测和未匹配预测；在确定人工标签中，另有 6 个作答存在性判断不符，案例为 `baseline_geometry_mark`、`simulated_fraction_formula`、`table_with_fill`、`visible_geometry_no_text`、`low_contrast_formula`、`formula_equals_only`。这两类错误分别反映区域定位失败和案例级作答判断差异，不能合并为请求或 JSON 协议失败。

## 真实 MinerU 专项闭环

专项测试使用程序绘制的合成 PDF，而非真实学生数据。MinerU 4.0.10 Basic/ONNX 实际解析出 `docvortex.middle 2.0`；Canonical 与 AnswerAlignment 由该 ParsedArtifact 生成，目标题状态为 `aligned`。随后真实本地 Ollama `qwen3.5:4b` 返回 `candidate_response_present` 和 `geometry_mark` 框，proposal 写入测试 MinIO 与 PostgreSQL，并通过下载后大小和 SHA-256 检查。人工作答复核记录数未变化。

该视觉框到 Canonical 来源的映射仍为 `unresolved`，原因是 `source_mapping_unavailable`、IoU=0.0。此次模型调用耗时 7,793 ms，返回 prompt/completion tokens 为 1,748/119；端到端耗时 11,551 ms。proposal 为 1,989 字节，下载后的大小和 SHA-256 均匹配。解析、对齐和持久化链路真实运行成功，不代表模型区域归属正确。完整运行记录位于 `datasets/visual-evidence/runs/real-mineru-ollama-qwen3.5-4b.json`。默认 CI 不下载 MinerU 模型或调用 Ollama。

### Ollama 调用合同与 fixture 持久化检查

此前的五个单图合成调用都通过 JSON 协议（5/5），但几何标记被判为 `printed_prompt`。另一次真实 Ollama + PostgreSQL + MinIO 闭环使用合成 PNG 和合成 MiddleJson fixture；Canonical/Alignment 的持久化链路真实运行，但输入不是 MinerU 输出。`qwen3.5:4b` 对几何标记返回 `candidate_prompt_only` / `printed_prompt`，模型框 `[0.21, 0.365, 0.798, 0.674]` 与合成来源框 IoU 为 0.952。该次调用耗时 9,359 ms，prompt/completion tokens 为 1,807/120；proposal 下载后为 1,967 字节，SHA-256 正确，人工作答复核记录数未变。服务保留视觉框，但拒绝建立来源 pointer。

这是模型视觉语义误判，不是协议错误、坐标变换失败或 Canonical 映射成功。IoU 只描述几何重叠，不能证明内容类型或 Question 归属。这些合成观察不是准确率或性能基准。

如需重跑 MinerU 与 Ollama 专项闭环，先配置独立 MinerU 4.0.10 Basic/ONNX 环境、本地 Ollama、PostgreSQL 和测试 MinIO，再执行：

```bash
MINERU_E2E=1 \
VISUAL_EVIDENCE_E2E=1 \
MINERU_EXECUTABLE=/绝对路径/mineru-venv/bin/mineru-kit \
MINERU_HOME=/绝对路径/mineru-home \
uv run pytest -q -s tests/integration/test_mineru_e2e.py::test_real_mineru_to_canonical_alignment_ollama_proposal_e2e
```

命令只适用于隔离的测试环境；默认 CI 不安装模型、不下载权重，也不访问 Ollama。

## 数据缺口

后续评估真实作业前，需要合法授权、隐私处理、覆盖实际作业分布的样本，以及明确的人工标注和分歧仲裁流程。还需要独立 Canonical/Alignment 来源真值，才能评估 Question 链接。合成数据用于回归和流程检查，不能替代这些材料。
