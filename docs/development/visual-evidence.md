# 视觉作答证据建议

本工具从原始提交和当前 AnswerAlignment 读取目标页面，并调用已配置的视觉模型 Provider。它生成私有、版本化的候选建议，不写 `AnswerReview` 决定。

## 主要服务：DeepSeek Flash

现有 OpenAI-compatible Vision Provider 支持官方 DeepSeek Chat Completions，不需要第二套 HTTP 客户端。官方 `deepseek-flash` 支持用户消息中的 base64 图片；原始提交先经现有 PDF/图片渲染、SHA校验、页面和像素限制，向模型发送渲染后的页面，不传私有对象URL。

```dotenv
VISUAL_EVIDENCE_ENABLED=false
VISUAL_EVIDENCE_PROVIDER=openai_compatible
VISUAL_EVIDENCE_BASE_URL=https://api.deepseek.com
VISUAL_EVIDENCE_MODEL=deepseek-flash
VISUAL_EVIDENCE_ALLOW_REMOTE=false
VISUAL_EVIDENCE_EXTERNAL_DATA_AUTHORIZED=false
VISUAL_EVIDENCE_DEEPSEEK_THINKING=disabled
VISUAL_EVIDENCE_DEEPSEEK_REASONING_EFFORT=max
VISUAL_EVIDENCE_MAX_OUTPUT_TOKENS=2048
VISUAL_EVIDENCE_TIMEOUT_SECONDS=90
VISUAL_EVIDENCE_MAX_RETRIES=0
```

Key 仅填写在本地 `VISUAL_EVIDENCE_API_KEY`。操作者明确批准合成图像传输后才开启 enabled 和两个授权开关；真实学生资料需要另行确认适用授权。首次 Smoke 显式关闭思考且没有重试，最多一张合成页面和一次视觉请求。只有官方域名接收 thinking/effort 参数；JSON object 输出要求保留。

当前配置档位max，Thinking关闭。effort参数本身可能开启思考，因此非思考请求不发送effort字段，不能声称max推理已生效。若将来明确开启Thinking，需另行核对输出、超时和费用预算。思考和最终JSON共享输出预算，应用上限16384 tokens。配置/实际参数、Token预算、超时和重试设置进入 Provider fingerprint，进而进入输入摘要，同一 request ID 不能复用不同模式的结果。

length 和其他未正常完成的输出不会成为 proposal，非法JSON和越界页面也不会登记成功索引。成功对象记录服务实际返回模型ID。模型建议的BBox仍通过原有保守来源映射；空间候选不是确定归属，unresolved 不会为了完成验收而改为成功。

首次验收方法及当前阻塞状态见[DeepSeek验收文档](../evaluation/deepseek-provider-validation.md)。API Key 缺失时只运行 MockTransport 配合真实 PostgreSQL/S3 的集成测试，不称为真实模型端到端验收。

## 可选开发服务：本地 Ollama

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
