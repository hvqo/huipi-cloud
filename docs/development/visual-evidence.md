# 视觉作答证据建议

本工具从原始提交和当前 AnswerAlignment 读取目标页面，并调用已配置的视觉模型 Provider。它生成私有、版本化的候选建议，不写 `AnswerReview` 决定。

## 本地 Provider 配置

默认配置使用仅 loopback 的 Ollama 原生 Provider。需要开发者自行安装并启动 Ollama、获取一个支持图像输入的模型，然后在 `.env` 设置：

```dotenv
VISUAL_EVIDENCE_PROVIDER=ollama_native
VISUAL_EVIDENCE_BASE_URL=http://127.0.0.1:11434/v1
VISUAL_EVIDENCE_MODEL=qwen3.5:4b
VISUAL_EVIDENCE_ENABLED=false
```

配置 OpenAI-compatible Vision API 时，使用 `VISUAL_EVIDENCE_PROVIDER=openai_compatible`，并将密钥放在 `VISUAL_EVIDENCE_API_KEY`。远程 HTTPS endpoint 还要求显式启用 `VISUAL_EVIDENCE_ALLOW_REMOTE=true` 和 `VISUAL_EVIDENCE_EXTERNAL_DATA_AUTHORIZED=true`，且必须先取得数据授权。不要把学生作业发送到未经批准的服务。

## 运行命令

提交必须已有可用 Canonical 和 AnswerAlignment，目标题目必须有可验证来源页。先进行不会调用模型或写 proposal 的 dry-run：

```bash
uv run python -m huipi_cloud.workers.analyze_visual_evidence \
  --submission-id SUBMISSION_UUID \
  --question-id QUESTION_UUID \
  --dry-run
```

显式允许本次本地推理：

```bash
VISUAL_EVIDENCE_ENABLED=true uv run python -m huipi_cloud.workers.analyze_visual_evidence \
  --submission-id SUBMISSION_UUID \
  --question-id QUESTION_UUID \
  --request-id REQUEST_UUID
```

成功结果保存在私有 proposal 对象和 PostgreSQL 索引中。CLI 的 `request_id` 应由调用者保存，以便在结果不确定时按幂等规则重试。

## 解释结果时的边界

- `candidate_response_present` 是模型建议，不是人工确认学生已作答；`candidate_prompt_only` 也不等于 `response_absent`。
- 几何重叠是空间线索，不证明某个框属于目标题。共享素材、跨题区域、旋转或裁剪坐标不确定时，系统会保留 unresolved，而不伪造 Canonical 来源。
- 图片/PDF 渲染、页数、像素和请求大小受到限制，但没有进程级全局并发或硬内存隔离。
- 没有认证或 RBAC；CLI 只适用于受控环境。模型建议与人工复核数据始终分开。

数据集、指标、逐案例预测和实验结果见[视觉证据评测文档](../evaluation/visual-evidence.md)。
