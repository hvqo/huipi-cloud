# 教师 Copilot：受控本地 Agent

本指南用于受控开发环境。CLI 操作员明确传入一个 Assignment 作用域；这不是教师认证、RBAC 或租户隔离。没有匿名聊天 HTTP 接口，没有成绩/Rubric 写工具。

## 环境与启动

沿用[快速开始](quickstart.md)的 Python 3.12、uv、PostgreSQL 和 Alembic。Copilot 不读取文件正文，不需要为对话单独启动 Worker 或对象存储。上传和解析演示仍需既有 S3 服务。

```bash
uv sync --locked
docker compose up -d postgres minio
uv run alembic upgrade head
uv run python -m huipi_cloud.modules.copilot --help
```

在本地忽略的 `.env` 单独配置 `COPILOT_API_KEY`，不要提交或粘贴到终端历史。只有在允许目标业务数据发送到 DeepSeek 时，才打开：

```dotenv
COPILOT_ENABLED=true
COPILOT_ALLOW_REMOTE=true
COPILOT_EXTERNAL_DATA_AUTHORIZED=true
COPILOT_MODEL=deepseek-flash
```

上述授权并不替代数据主体许可。项目首次验收只使用合成数据。已有评分 Key 可在命令中显式选择 `--use-grading-key`；默认不会读取评分 Key 或降级到 Ollama。不会自动修改 `.env`。

先按[基本流程](basic-workflow.md)创建作业，将实际 UUID 填入变量：

```bash
read -r ASSIGNMENT_ID
uv run python -m huipi_cloud.modules.copilot \
  --assignment-id "$ASSIGNMENT_ID" \
  --question '请查询这份作业的概要和题目列表，说明哪些题目已经配置评分细则。'
```

若显式复用本地评分 Key，在命令末尾增加 `--use-grading-key`。查询某题 Rubric 或某次提交状态，需要在问题中给出其实际 UUID；Agent 也可先通过题目列表取得 Question UUID。没有任意搜索所有作业或列出学生身份的工具。

CLI 输出单轮 JSON：答案、状态、结束原因、计数、用量、成本预留、模型实际 ID 和脱敏轨迹。退出码 0 表示完成回答；`answered_with_tool_error` 表示回答说明了查询失败，不能当作业务查询成功。其他结束原因返回非零退出码。最终自然语言是模型建议，应结合工具状态核对。

## 固定工具

- `get_assignment_summary({})`：名称、学科、年级、作业状态、题目数和提交数。
- `list_assignment_questions({limit, offset})`：每页默认 10、最多 25；只列 ID、题号、题型、满分和答案/细则配置状态。不返回标准答案或题干全文。
- `get_question_rubric_summary({question_id})`：校验 Question 属于作用域；返回顺序、分值和每条最多 300 字的描述，截断有明确标记。
- `get_submission_processing_status({submission_id})`：校验 Submission 属于作用域；返回解析、Canonical/Alignment 索引状态和每题最新人工判断的汇总。不返回学生标识、答案、对象 Key 或凭据。

处理状态标记为 `not_verified_not_scoring_readiness`：只读当前索引，不重新下载 S3、验证来源版本或推断可以评分。人工确认数量可能包含历史来源的判断，不能解释成当前可信评分输入数。所有工具都没有正式成绩信息。

模型不能传入 assignment_id 改变作用域。未知工具、额外参数、重复 JSON 键、非法 UUID 和不合法分页值均拒绝。存在于其他作业的资源与不存在的资源返回同一个安全错误。参数使用 SQLAlchemy 绑定值，不接受 SQL、代码或 URL。数据工具使用独立短事务，PostgreSQL 强制 `READ ONLY`，并设置 3 秒 statement timeout；模型调用时不持有数据库事务。

## 状态和预算

`CopilotRequest` 先完成输入和作用域一致性检查，LangGraph 执行 `model → tools → model`，最终到 END。状态包含消息、待执行工具、已调用指纹、结果和计数。没有持久化会话、checkpointer 或外部 LangSmith 上报；显式关闭 tracing。

默认约束：4 次模型请求、6 次工具调用、12 个图节点执行步骤；模型单次 30 秒、工具单次 5 秒、全轮 90 秒；输出最多 1024 Token，工具结果最多 8192 UTF-8 字节，上下文预算 16384 Token。重复相同工具及规范化参数会停止；预算到达后不再发新请求。失败工具不会被自动重试，下一次模型只能解释错误，不能再调用工具。

上下文采用序列化 UTF-8 字节数加消息封装余量作保守 Token 估计，并为输出预留空间；不是精确 tokenizer。结果超限会被替换为安全错误，而不是截掉正文后伪称完整结果。分页与字段投影先限制查询结果。

每次发请求前，按 Flash 高峰无缓存价格（输入 $0.30/M、输出 $1.20/M）预留该请求上界；默认全轮预估预算 $0.05，不会因失败而退还预留。返回用量单独累计，若超过本次预留规模则停止。价格可能变化，这不是服务商硬扣费上限；超时或截断的实际计费用量可能未知，保留预留，不编造用量。默认无自动重试。

P4-A 固定 `thinking.type=disabled`，不发送 reasoning_effort。支持原生 Chat Completions 的 assistant.tool_calls 与 role=tool 回填，保留服务返回的 reasoning_content 在内存中以满足合同，但不输出到轨迹。对话不套用评分 JSON Schema。`length`、401、429、模型不存在及无效外层协议均安全终止；不退回本地模型。

## 安全边界

用户问题和工具正文都不可信。Prompt Injection 防护的硬边界是固定工具、重新校验、显式作用域和只读事务，不是宣称模型永远服从提示词。模型可能产生错误解释，最终自然语言未获得事实证明，不能代替工具结果或人工核查。`completed_candidate`、人工接受/修订、拒绝/重新评分和正式成绩是不同概念；本轮没有任何评分结果查询工具或正式成绩接口。

轨迹只保存 run_id、版本、模型 ID、次数、状态、参数及结果 hash/字节数、耗时和 Token，不记录正文或教师身份。运行结束后不持久化对话；CLI 答案仍可能包含授权查询得到的作业/Rubric内容，应按业务数据保护。未知程序缺陷和调用者取消会继续传播；协作式异步 HTTP/数据库取消能释放资源，但 asyncio 不保证强行中止不合作线程。当前工具和 Provider 不运行任意同步插件。

## 测试与真实验收

```bash
uv run pytest -q tests/unit/test_copilot.py tests/integration/test_copilot_business.py
```

普通测试使用 Scripted/MockTransport Provider 和真实专用 PostgreSQL、S3 合成来源；不发送收费请求。收费测试默认跳过，测试库必须与开发库分离：

```bash
RUN_COPILOT_E2E=1 COPILOT_SYNTHETIC_TRANSFER_AUTHORIZED=1 \
  uv run pytest -q -m copilot_e2e tests/integration/test_copilot_business.py
```

该路径只构造合成 Assignment、Question、Submission 和已验证格式的测试解析产物；Canonical/Alignment 与人工确认调用实际服务。此处没有重新执行真实 MinerU。它先检查一般说明，再检查四工具与实际测试库查询、答案、脱敏轨迹及业务表不变。真实结果见[验收记录](../evaluation/teacher-copilot-validation.md)。

## 后续检索接口

P4-B 若加入检索，应提供新工具的严格输入与结构化输出：绑定作用域、来源版本、引用 ID、字节/数量上限和数据授权；检索内容仍是不可信数据。认证和租户隔离需先单独实现，不能使用 reviewer_ref。不得将整份学生答案、S3 Key、标准答案或未经许可的文档随意塞入模型上下文；Embedding/Rerank 可以本地运行，生成仍使用经授权云端 API。本轮没有向量数据库或检索工具。
