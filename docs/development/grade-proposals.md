# Rubric 候选评分 CLI

本工具对 P3-A 已保存的 GradeInput 调用文本模型，生成逐 Rubric 项候选分数。输出是待人工检查的建议，不是最终成绩；工具不提供 HTTP 写接口、不修改人工复核决定，也不发布分数。

## 前置条件

目标作业必须已发布，并完成解析、Canonical、AnswerAlignment、当前版本的 `response_present` 人工复核和 P3-A GradeInput 准备。纯文本输入只包含复核选定且通过多模态安全检查的文本。检查与准备步骤见[可信评分输入指南](grade-input.md)。

评分只接受已准备的 `GradeInputArtifact` UUID，不从 API 直接读取学生提交。服务会在模型调用前、调用后，以及 PostgreSQL 登记事务内重新验证输入来源。调用期间不持有数据库事务；源版本发生变化时，不登记候选结果。

## 主要服务：DeepSeek

使用现有 OpenAI-compatible Chat Completions 适配器，不需要新增 SDK。先在被 Git 忽略的本地 `.env` 中配置以下非敏感值，并自行填写 `GRADING_API_KEY`：

```dotenv
GRADING_ENABLED=false
GRADING_PROVIDER=openai_compatible
GRADING_BASE_URL=https://api.deepseek.com
GRADING_MODEL=deepseek-flash
GRADING_ALLOW_REMOTE=false
GRADING_EXTERNAL_DATA_AUTHORIZED=false
GRADING_DEEPSEEK_THINKING=disabled
GRADING_DEEPSEEK_REASONING_EFFORT=max
GRADING_MAX_OUTPUT_TOKENS=4096
GRADING_TIMEOUT_SECONDS=120
GRADING_INVALID_RESPONSE_RETRIES=0
```

操作者明确批准合成数据传输后，才将 enabled 和两个授权开关设为 true。没有 Key 或授权时，Provider 在发 HTTP 前拒绝运行。首次验收仅使用合成数据；这些开关不能代替真实学生资料所需的数据传输授权。密钥不要放进命令行、日志或 Git。

DeepSeek 默认开启思考，本项目向官方 `https://api.deepseek.com`（可带 `/v1`）显式发送 `thinking.type=disabled`。非思考模式保留 temperature=0；显式开启思考时发送 `thinking.type=enabled` 及 low/high/max effort，并不发送在该模式下无效的 temperature。仅官方域名匹配时注入这些参数，其他兼容服务和 Ollama 不接收 DeepSeek 参数。

当前两模块统一使用 Flash，配置档位max、Thinking关闭。官方接口中effort本身也能启用思考，所以非思考请求不发送effort字段；报告把配置max和实际effort未生效分开记录。不能称为“非思考max推理”。将来显式开启Thinking属于独立实验，需重新核对成本、输出及超时预算。应用输出硬上限16384 tokens，思考与最终输出共享预算；这不是 DeepSeek 平台的最大能力。保持单次格式重试为0。Thinking、配置/实际effort、输出预算和超时参与 generation config digest 与请求指纹；旧 Proposal 协议及不可变对象不改写。

结束原因为 length 时使用 `provider_response_truncated`，即使正文恰好能解析也不接受。其他非正常结束、401、404、429和超时均为安全失败；日志不包含服务端错误正文、密钥或 reasoning_content。离线预测保留实际返回模型ID、finish_reason、用量及可用的思考/缓存Token计数。completion_tokens 已含思考Token，估算费用时不能再相加。

## 可选开发服务：本地 Ollama

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

## Rubric 候选评分离线评测

评测工具默认使用 v2 的27个合成案例，其中25个纳入评分，2个不确定案例不调用模型。默认 dry-run 不会请求模型：

```bash
uv run python scripts/evaluate_grade_proposals.py --dry-run
```

先完成 DeepSeek 合成 Smoke，再进行 v2 基线。远程评测必须显式传 `--run-model`、新输出文件和自行核对的当前单价。未缓存输入单价用于保守估算；程序在发请求前检查整轮输入预算与输出Token上限是否超过费用预算。它不是账户级限额，单价变动和服务器计数差异仍需核对。完整命令和未请求状态见[DeepSeek验收与评测计划](../evaluation/deepseek-provider-validation.md)。

如需复现历史本地 v1 基线，必须显式指定 v1，使用全新的文件名：

```bash
uv run python scripts/evaluate_grade_proposals.py \
  --dataset data/evaluation/grading/rubric-cases-v1.json \
  --run-model --model qwen3.5:4b --max-requests 24 --timeout-seconds 90 \
  --output data/evaluation/grading/predictions-qwen3.5-4b-v1-new-run.json \
  --report-output data/evaluation/grading/report-qwen3.5-4b-v1-new-run.json
```

用已保存预测离线重新计算时，不会调用 Provider；评测器会先核对数据集版本和 SHA，再重新校验每条模型回复：

```bash
uv run python scripts/evaluate_grade_proposals.py \
  --dataset data/evaluation/grading/rubric-cases-v1.json \
  --evaluate-predictions data/evaluation/grading/predictions-qwen3.5-4b-v1.json \
  --report-output /tmp/huipi-v1-recalculated-new.json
```

此路径只构造内存中的合成 `GradeInputDocument`，不访问 PostgreSQL、MinIO 或候选结果索引。每个模型请求只会看到题干、答案标准、Rubric 和合成学生片段，不会看到参考分数、标注理由或标签来源。报告中的分数指标只使用通过 P3-B 现有协议、分值和引用验证的候选；协议失败、弃权和请求故障都保留在覆盖和逐例失败统计中。详见[Rubric 候选评分评测定义与基线](../evaluation/rubric-candidate-grading.md)。
