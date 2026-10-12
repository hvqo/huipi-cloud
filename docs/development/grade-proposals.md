# Rubric 候选评分 CLI

本工具对 P3-A 已保存的 GradeInput 调用文本模型，生成逐 Rubric 项候选分数。输出是待人工检查的建议，不是最终成绩；工具不提供 HTTP 写接口、不修改人工复核决定，也不发布分数。

## 前置条件

目标作业必须已发布，并完成解析、Canonical、AnswerAlignment、当前版本的 `response_present` 人工复核和 P3-A GradeInput 准备。纯文本输入只包含复核选定且通过多模态安全检查的文本。检查与准备步骤见[可信评分输入指南](grade-input.md)。

评分只接受已准备的 `GradeInputArtifact` UUID，不从 API 直接读取学生提交。服务会在模型调用前、调用后，以及 PostgreSQL 登记事务内重新验证输入来源。调用期间不持有数据库事务；源版本发生变化时，不登记候选结果。

## 默认使用本地 Ollama

在本机安装并启动 Ollama，先确认本机模型已存在。编辑忽略跟踪的 `.env`，设置本地模型名称：

```dotenv
GRADING_ENABLED=true
GRADING_PROVIDER=ollama_native
GRADING_BASE_URL=http://127.0.0.1:11434
GRADING_MODEL=本机已安装的模型名称
GRADING_TIMEOUT_SECONDS=120
GRADING_MAX_OUTPUT_TOKENS=1200
GRADING_INVALID_RESPONSE_RETRIES=1
```

模型名来自当前 Ollama 安装，不由项目自动下载。候选评分默认关闭。Ollama endpoint 只允许 loopback HTTP，不会把学生文本发送到公网。

## 检查并生成建议

从 GradeInput 准备命令结果中取 `artifact_id`：

```bash
export GRADE_INPUT_ID='00000000-0000-0000-0000-000000000001'
export REQUEST_ID='00000000-0000-0000-0000-000000000002'

uv run python -m huipi_cloud.workers.grade_submission inspect \
  --grade-input-id "$GRADE_INPUT_ID"

uv run python -m huipi_cloud.workers.grade_submission grade \
  --grade-input-id "$GRADE_INPUT_ID" \
  --request-id "$REQUEST_ID"
```

`inspect` 检查输入仍然有效、Provider 配置和资源限制，不返回学生回答、标准答案、Rubric 描述或对象 Key。`grade` 返回 proposal UUID、输入与 proposal SHA-256、候选分数、总分、弃权原因码和实际可用的 token/耗时统计；不返回回答引用、理由文本或对象 Key。完整建议保存在私有 S3，不通过 HTTP 提供。

相同 `request_id`、相同输入、模型和生成配置会复用已有结果。该 ID 绑定不同输入或配置时返回冲突。并发重试可能分别调用模型，但 PostgreSQL 唯一约束只允许一个有效 proposal 索引。每次尝试使用独立不可变对象 Key。

## Provider 配置与数据边界

- `GRADING_PROVIDER=ollama_native`：仅允许 loopback Ollama 原生 `/api/chat`。
- `GRADING_PROVIDER=openai_compatible`：loopback 可直接使用；远程 endpoint 必须是 HTTPS，并同时显式设置 `GRADING_ALLOW_REMOTE=true` 与 `GRADING_EXTERNAL_DATA_AUTHORIZED=true`。密钥使用 `GRADING_API_KEY` 环境变量，不要放进命令行或提交到 Git。
- `GRADING_TIMEOUT_SECONDS`、`GRADING_MAX_INPUT_BYTES`、`GRADING_MAX_INPUT_TOKENS`、`GRADING_MAX_OUTPUT_TOKENS`、`GRADING_MAX_PROPOSAL_BYTES` 限定单次调用和保存结果的大小。
- 当前没有独立 tokenizer；UTF-8 字节数连同固定协议开销作为 token 预算的保守上界，因此它不是精确 token 计数。
- 仅无效 JSON/协议最多重试一次。Provider 故障、分数错误、证据引用错误不会无限重试。

不要对真实学生数据启用远程 Provider，除非已取得适用的数据传输授权。项目没有认证、RBAC 或教师归属检查；仅在受控本地环境运行。

## 结果含义与限制

候选协议 `huipi.grading.proposal` 1.0 保存每项分数、理由、精确引用或 `no_evidence`。引用偏移是对应已选片段内的 Unicode 码点半开区间 `[start, end)`；服务确认区间与原文一致。每个正分 Criterion 至少需要一条经原文和范围校验的 quote；仅有 `no_evidence` 的正分会以 `positive_score_without_quote` 拒绝。零分可只引用 `no_evidence`，部分分可同时引用有效 quote 和 `no_evidence`。服务还检查每项分数不超过可信 Rubric 分值、Criterion 清单完整且唯一、逐项分数之和与模型总分相等。

本规则改变新模型输出的接收门禁，不改变 `GradeProposalDocument` 1.0 字段，因此无需数据库迁移或协议版本升级。新提示词版本为 `rubric-candidate-v3`，它参与请求指纹；新旧提示词不会共用同一请求指纹。输入来源仍有效时，已保存的不可变历史 proposal 继续按它自身保存的 prompt 版本、数据库索引、SHA 和 1.0 协议读取；本次新门禁不会追溯改写历史对象，也不会把旧对象重新判定为新输出。

模型输出仍可能错误、偏见或受提示注入影响。结构门禁只能证明正分项附带了可追溯的作答文字，不能证明该文字在教学语义上足以支持分数，也不能发现所有评分幻觉。系统分隔系统规则与学生数据并限制分值、证据和输出，但这不能消除模型风险。严格协议通过不代表评分正确。`grading_abstained` 是模型明确弃权，不是零分；无 `no_evidence` 也不能解释为学生未作答。没有教师复核与发布流程，不得将 proposal 当作正式成绩。

PostgreSQL 与 S3 不共享事务。确定的数据库回滚会触发尽力删除；已确认的并发输家也会清理。COMMIT 结果未知或上传协程被取消时保留对象，避免删掉数据库可能已引用的数据；进程崩溃仍可能留下孤立对象。当前没有自动对账清理器。

## 验证

默认集成测试使用真实 PostgreSQL 和本地 MinIO，但使用测试 Provider；默认测试不会下载模型。可在已启动本地 PostgreSQL、MinIO 和 Ollama，且测试数据库为专用 `_test` 库时运行真实模型链路：

```bash
GRADING_ENABLED=true \
GRADING_PROVIDER=ollama_native \
GRADING_BASE_URL=http://127.0.0.1:11434 \
GRADING_MODEL='本机已安装的模型名称' \
RUN_GRADE_LLM_E2E=1 \
uv run pytest -q -s -m grade_llm_e2e
```

专项验收使用合成作业、合成答案和合成 Rubric。它验证 Provider 调用、严格结果校验及 PostgreSQL/S3 持久化，不表示真实教学作业的准确率。

实现位置：[候选评分服务](../../src/huipi_cloud/modules/grading/proposal_service.py)、[协议](../../src/huipi_cloud/modules/grading/proposal_protocol.py)、[PostgreSQL 登记](../../src/huipi_cloud/modules/grading/proposal_repository.py)、[Provider](../../src/huipi_cloud/infrastructure/grading/provider.py)、[本地 CLI](../../src/huipi_cloud/workers/grade_submission.py)。
