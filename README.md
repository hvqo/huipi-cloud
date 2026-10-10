# 慧批云端

慧批云端是独立重建的高中 AI 教学辅助平台，面向多模态作业解析、自动批改 Agent、教师 AI Copilot 和教师复核。

## 当前实现状态

- 已合并 P1-A：教师作业、题目、人工标准答案、评分细则和发布校验。
- 已合并 P1-B：学生模拟身份提交、PDF/JPG/PNG 原始文件存储、提交查询、文件读取和初始 `pending` 任务记录。
- 已合并 P2-A：PostgreSQL 解析任务状态机、租约领取和续租、过期恢复、有限重试、独立 Worker 框架及安全状态查询。
- P2-B：MinerU 4.x Basic/ONNX 本地解析子进程、PDF/图片输入、私有 S3 解析产物、PostgreSQL 产物索引、解析结果查询 API。PR #5 记录最终验收和真实 MinerU 样本证据。
- 已合并 P2-C：Canonical Document v1、MiddleJson 转换、完整合法素材清单、来源元数据保留、S3 JSON 产物与 PostgreSQL 轻量索引、规范化 CLI 和按页查询 API。
- 已合并 P2-D1：规则式题号候选识别、来源区域切分、与真实 Assignment Question 对齐、S3 不可变结果和 PostgreSQL 幂等索引，以及只读查询 API。`aligned` 只表示来源到 Question 的映射，不表示学生已经作答、区域内没有其他题目内容、OCR/公式/图像识别完整或可以批改；评测和测试不代表真实教学数据准确率。
- P2-D2A（本功能分支）：新增独立的人工作答存在性决定协议 `response_present` / `response_absent` / `uncertain`。没有复核记录是 `unreviewed`，不等同于 `response_absent`。决定只通过本地 CLI 追加到 PostgreSQL，并绑定当前 Canonical、Question 集合和 AnswerAlignment 哈希版本；不修改 P2-D1 结果，不提供匿名写入 API，也不表示 `ready_for_grading`。
- P2-B 不包含题目切分、批改、MongoDB、Celery、RabbitMQ、Redis、Copilot 或身份认证。真实解析要求单独安装 MinerU 和模型；未配置执行器或模型时 Worker 拒绝启动。

`student_ref` 和当前提交、任务、解析结果 API 没有认证或权限控制，只能用于本地或其他受控环境，不能直接暴露到公网。

## 技术栈

- Python 3.12、uv、FastAPI、Pydantic Settings
- PostgreSQL 16、SQLAlchemy 2.x Async、asyncpg、Alembic
- S3 兼容的私有对象存储（本地使用 PGSTY SILO）、boto3
- MinerU 4.x Basic/ONNX 独立本地运行时；PDF 结构预检使用 pypdf
- pytest、HTTPX、Ruff

MongoDB、Celery、RabbitMQ、Redis、LangChain、LangGraph、vLLM 和自动批改 Agent 尚未接入。

## 本地初始化和启动

~~~bash
cp .env.example .env
uv sync --locked
docker compose up -d postgres minio
uv run python scripts/init_minio_bucket.py
uv run alembic upgrade head
uv run uvicorn huipi_cloud.main:app --reload
~~~

API 文档：http://127.0.0.1:8000/docs。存活检查：`GET /api/v1/health`。应用不会通过 `create_all` 隐式建表，数据库版本由 Alembic 管理。

P2-A 提供 `GET /api/v1/submissions/{submission_id}/parsing-task`。P2-B 另提供解析产物摘要和 Markdown 流式读取接口。

`python -m huipi_cloud.workers.normalize_document --submission-id UUID` 只处理已有 `succeeded` 的 P2-B 解析结果，不会重置或重新执行 MinerU。状态摘要为 `GET /api/v1/submissions/{submission_id}/canonical-document`；页面读取为 `GET /api/v1/submissions/{submission_id}/canonical-document/pages/{page_number}`。Canonical JSON 存在私有 S3；页面接口先检查索引大小，再分块读取、校验 SHA-256 和完整结构后只返回指定页。页面 API 默认上限 32 MiB、硬上限 64 MiB；Pydantic 解码会增加峰值内存，并随并发请求增加。

P2-D1 已合并到 `main`。`python -m huipi_cloud.workers.align_answers --submission-id UUID` 只处理可用 Canonical 文档和所属 Assignment 的真实 Question。查询接口为 `GET /api/v1/submissions/{submission_id}/answer-alignment` 与 `GET /api/v1/submissions/{submission_id}/answer-alignment/questions/{question_id}`。对齐 JSON 存在私有 S3，PostgreSQL 保存版本、题目集合摘要和安全查询索引。`ANSWER_ALIGNMENT_MAX_DOCUMENT_BYTES` 默认 32 MiB、硬上限 64 MiB；`ANSWER_ALIGNMENT_MAX_QUESTION_RESPONSE_BYTES` 默认 2 MiB、硬上限 8 MiB。读取会加载并解析整份对齐 JSON，结构解码和并发请求会增加内存。通用数字标签及括号子题号默认待复核；未标号内容保持未观察/未分配，不推断学生未作答。`matching_status=aligned` 只表示来源区域已关联到 Question，不表示学生确实作答、文本不是印刷题干、OCR 完整或结果可以直接批改；当前没有单独的作答存在性或 `ready_for_grading` 状态。当前 API 无认证和 RBAC，只能用于受控环境。

本分支提供本地受控的人工复核 CLI（没有 HTTP 写入 API）：

~~~bash
# 将 SUBMISSION_UUID、QUESTION_UUID、REQUEST_UUID 替换为实际 UUID。
uv run python -m huipi_cloud.workers.review_answers inspect --submission-id SUBMISSION_UUID
uv run python -m huipi_cloud.workers.review_answers record --submission-id SUBMISSION_UUID --question-id QUESTION_UUID --request-id REQUEST_UUID --reviewer-ref local-reviewer --decision response_present --response-region '/pages/0/blocks/0/content:6:13' --reason-code student_work_visible
uv run python -m huipi_cloud.workers.review_answers history --submission-id SUBMISSION_UUID --question-id QUESTION_UUID
uv run python -m huipi_cloud.workers.review_answers export --submission-id SUBMISSION_UUID --output /tmp/answer-review-redacted.json
uv run python scripts/evaluate_answer_presence.py
~~~

CLI 输出 `reviewer_ref` 只是自声明标记，不是登录身份。不要将未认证的 API、原始作业或 CLI 检查输出暴露到公网。JSON Pointer 必须能定位现有 Canonical ContentNode；文本选择格式使用 Python Unicode 码点半开偏移 `CONTENT_POINTER:START:END`。嵌套节点按 Canonical 树判断包含关系：整节点父容器覆盖其子节点；父节点包含别题或未分配来源时，不能确认为本题作答。Block 级图片引用不会让空白的非图片子节点成为可选证据；共享图片的 Question 归属无法证明时只能记为 `uncertain`。标注导出会去掉自声明 reviewer、学生/提交原始 ID、对象 Key 和外部 URI，并仅提供当前导出内的随机化引用。这是去标识化，不是真正匿名化；区域位置、内容标签或外部关联仍可能暴露身份，因此导出和原始复核数据都应按敏感数据保护。当前没有自动作答检测器，所以作答 Precision/Recall/FPR 是 `not_evaluated`；13 个合成范围中 12 个有人工作答标签、3 个为 uncertain，覆盖率和不确定比例只说明该合成协议数据，不能代表真实作业。

## 配置和启动真实解析

MinerU 运行时和模型目录应与应用虚拟环境隔离。按照 [MinerU 官方安装说明](https://opendatalab.github.io/MinerU/quick_start/)安装 MinerU 4.x，并下载和验证 Basic/ONNX 本地模型。然后在 `.env` 设置：

~~~bash
uv venv /绝对路径/mineru-venv --python 3.12
uv pip install --python /绝对路径/mineru-venv/bin/python "mineru==4.0.10"
MINERU_HOME=/绝对路径/mineru-home MINERU_MODEL_SOURCE=modelscope /绝对路径/mineru-venv/bin/mineru-kit models download --tier basic --small-backend onnx --source modelscope
MINERU_HOME=/绝对路径/mineru-home MINERU_MODEL_SOURCE=local /绝对路径/mineru-venv/bin/mineru-kit models verify --tier basic --small-backend onnx
~~~

~~~dotenv
PARSING_EXECUTOR=huipi_cloud.infrastructure.parsing.mineru:build_mineru_executor
MINERU_EXECUTABLE=/绝对路径/mineru-venv/bin/mineru-kit
MINERU_HOME=/绝对路径/mineru-home
MINERU_TIER=basic
~~~

执行器强制读取本地模型，不向 MinerU 子进程传递数据库、MinIO 密钥或远端模型源。Worker 启动时检查 MinerU 4.x CLI 和本地模型：

~~~bash
uv run python -m huipi_cloud.workers.parsing_worker
~~~

每次解析运行的产物保存到私有对象存储的不可变 run 前缀。API 仅返回不含私有对象 Key 的产物摘要；Markdown 接口由后端流式读取对象。进程崩溃可能留下没有 PostgreSQL 索引的孤立解析对象，目前没有定期对账清理任务。

## 验证

完整集成测试要求本地 PostgreSQL `_test` 数据库和本地 S3 兼容服务。它们缺失时测试会失败，不会静默跳过 PostgreSQL/S3 覆盖。P2-D1 的数据库、S3 集成测试从合成 MiddleJson 开始，验证 Canonical → 答案对齐 → API；这不是实际 MinerU E2E。单独的 MinerU E2E 测试默认跳过，覆盖真实 MinerU Worker 和 Canonical 页面 API，不包含 D1 对齐。本地当前没有 MinerU CLI 和模型，因此这 4 项无法执行。

~~~bash
docker compose up -d postgres minio
uv run python scripts/init_minio_bucket.py
uv sync --locked
uv run ruff check .
uv run pytest -q
uv run alembic check
uv run python scripts/evaluate_answer_alignment.py
~~~

~~~bash
MINERU_E2E=1 MINERU_EXECUTABLE=/绝对路径/mineru-venv/bin/mineru-kit MINERU_HOME=/绝对路径/mineru-home uv run pytest -q -m mineru_e2e
~~~

P2-A 任务采用 PostgreSQL 持久队列和 At Least Once 执行。P2-B 的解析产物以每次执行独立的不可变 run Key 保存；PostgreSQL 产物索引与 `succeeded` 状态在同一租约校验事务中提交。这不构成 MinIO 和 PostgreSQL 的跨系统原子事务，也没有实现运行去重或孤立对象清理。解析执行设置输入字节、PDF 页数、归档展开字节、文本大小和归档成员数上限；这些设置不是 MinerU 子进程的硬内存上限。

P2-C 复核使用 MinerU 4.0.10 Basic/ONNX CPU 环境，4 个合成样本（文字 PDF、扫描 PDF、PNG、含公式/表格/图片的结构 PDF）通过真实 Worker + PostgreSQL + S3 E2E。结构 PDF 检查真实 MinerU MiddleJson 与素材清单；这验证数据流和样本内容断言，不代表真实学生作业准确率；GitHub CI 不下载模型。Canonical 会保留全部合法素材清单项，包括当前 Block 未引用的素材。内嵌图片不会写入 Canonical JSON；Canonical 保存 MiddleJson 来源指针、编码、媒体类型、解码后大小和 SHA-256，可在取得对应不可变 MiddleJson 并通过来源校验后恢复。

P2-D1 的离线评测集包含 13 个合成案例，包含仅有题干的负样本，不代表真实学生作业分布。指标版本 2.1 按案例、Question 和完整 Canonical `content_pointer` 隔离来源；字符范围是对应 ContentNode 文本中的 Unicode 码点半开区间，只归并同一节点内重叠或相邻的范围。嵌套节点专用样本单独评测，不与原 13 个顶层样本合并。当前规则只允许明确的“第 n 题”标记在无冲突且顺序一致时自动关联；`n.`、`n、`、`n)` 和括号题号作为候选但要求复核。它不读取 AnswerKey，也不推断 `not_observed` 等于未作答。结果由 CLI 生成并写入私有 S3 与 PostgreSQL；API 不返回 S3 Key。模块当前未实现认证、人工纠正、学生作答检测、真实手写样本评测、OCR 或评分。

## 项目结构

~~~text
src/huipi_cloud/
  api/v1/                         HTTP 路由
  core/                           配置和日志
  infrastructure/database/       Async Engine、Session、ORM Base
  infrastructure/storage/        S3 兼容对象存储
  infrastructure/parsing/        MinerU 子进程和解析结果合同校验
  modules/canonical_documents/    Canonical 协议、转换、轻量索引和查询 API
  modules/answer_alignment/       题号候选、答案区域、Question 匹配和结果查询
  modules/answer_review/          作答存在性复核协议、追加历史、来源验证和合成标注评测
  modules/assignments/            作业领域
  modules/submissions/            提交、原始文件和任务登记
  modules/parsing/                解析状态、产物索引、执行器协议和查询
  workers/                        独立解析 Worker、规范化、对齐和本地复核 CLI
migrations/                        Alembic 迁移
tests/unit/                        单元测试
tests/integration/                 PostgreSQL、S3 和可选 MinerU 集成测试
docs/                              架构、路线和面试复盘
~~~
