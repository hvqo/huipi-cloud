# 慧批云端

慧批云端是独立重建的高中 AI 教学辅助平台，面向多模态作业解析、自动批改 Agent、教师 AI Copilot 和教师复核。

## 当前实现状态

- 已合并 P1-A：教师作业、题目、人工标准答案、评分细则和发布校验。
- 已合并 P1-B：学生模拟身份提交、PDF/JPG/PNG 原始文件存储、提交查询、文件读取和初始 `pending` 任务记录。
- 已合并 P2-A：PostgreSQL 解析任务状态机、租约领取和续租、过期恢复、有限重试、独立 Worker 框架及安全状态查询。
- P2-B：MinerU 4.x Basic/ONNX 本地解析子进程、PDF/图片输入、私有 S3 解析产物、PostgreSQL 产物索引、解析结果查询 API。PR #5 记录最终验收和真实 MinerU 样本证据。
- 已合并 P2-C：Canonical Document v1、MiddleJson 转换、完整合法素材清单、来源元数据保留、S3 JSON 产物与 PostgreSQL 轻量索引、规范化 CLI 和按页查询 API。
- 已合并 P2-D1：规则式题号候选识别、来源区域切分、与真实 Assignment Question 对齐、S3 不可变结果和 PostgreSQL 幂等索引，以及只读查询 API。`aligned` 只表示来源到 Question 的映射，不表示学生已经作答、区域内没有其他题目内容、OCR/公式/图像识别完整或可以批改；评测和测试不代表真实教学数据准确率。
- 已合并 P2-D2A：独立的人工作答存在性决定协议 `response_present` / `response_absent` / `uncertain`。没有复核记录是 `unreviewed`，不等同于 `response_absent`。决定只通过本地 CLI 追加到 PostgreSQL，并绑定当前 Canonical、Question 集合和 AnswerAlignment 哈希版本；不修改 P2-D1 结果，也不表示 `ready_for_grading`。
- P2-D2B（当前功能分支，未合并）：基于原始提交页图像的 VLM 建议、受限 PDF/JPG/PNG 渲染、严格 JSON 合同、候选 Canonical 来源映射，以及私有 S3 proposal + PostgreSQL 幂等索引。模型结果与人工复核记录隔离；没有认证或教师界面。
- P2-B 不包含题目切分、批改、MongoDB、Celery、RabbitMQ、Redis、Copilot 或身份认证。真实解析要求单独安装 MinerU 和模型；未配置执行器或模型时 Worker 拒绝启动。

`student_ref` 和当前提交、任务、解析结果 API 没有认证或权限控制，只能用于本地或其他受控环境，不能直接暴露到公网。

## 技术栈

- Python 3.12、uv、FastAPI、Pydantic Settings
- PostgreSQL 16、SQLAlchemy 2.x Async、asyncpg、Alembic
- S3 兼容的私有对象存储（本地使用 PGSTY SILO）、boto3
- MinerU 4.x Basic/ONNX 独立本地运行时；PDF 结构预检使用 pypdf，页面渲染使用 pypdfium2 和 Pillow
- 配置的 OpenAI-compatible Vision API；HTTPX 异步客户端，默认仅允许本机服务
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

P2-D2A 提供本地受控的人工复核 CLI（没有 HTTP 写入 API）：

~~~bash
# 将 SUBMISSION_UUID、QUESTION_UUID、REQUEST_UUID 替换为实际 UUID。
uv run python -m huipi_cloud.workers.review_answers inspect --submission-id SUBMISSION_UUID
uv run python -m huipi_cloud.workers.review_answers record --submission-id SUBMISSION_UUID --question-id QUESTION_UUID --request-id REQUEST_UUID --reviewer-ref local-reviewer --decision response_present --response-region '/pages/0/blocks/0/content:6:13' --reason-code student_work_visible
uv run python -m huipi_cloud.workers.review_answers history --submission-id SUBMISSION_UUID --question-id QUESTION_UUID
uv run python -m huipi_cloud.workers.review_answers export --submission-id SUBMISSION_UUID --output /tmp/answer-review-redacted.json
uv run python scripts/evaluate_answer_presence.py
~~~

CLI 输出 `reviewer_ref` 只是自声明标记，不是登录身份。不要将未认证的 API、原始作业或 CLI 检查输出暴露到公网。JSON Pointer 必须能定位现有 Canonical ContentNode；文本选择格式使用 Python Unicode 码点半开偏移 `CONTENT_POINTER:START:END`。嵌套节点按 Canonical 树判断包含关系：整节点父容器覆盖其子节点；父节点包含别题或未分配来源时，不能确认为本题作答。Block 级图片引用不会让空白的非图片子节点成为可选证据；共享图片的 Question 归属无法证明时只能记为 `uncertain`。标注导出会去掉自声明 reviewer、学生/提交原始 ID、对象 Key 和外部 URI，并仅提供当前导出内的随机化引用。这是去标识化，不是真正匿名化；区域位置、内容标签或外部关联仍可能暴露身份，因此导出和原始复核数据都应按敏感数据保护。当前没有自动作答检测器，所以作答 Precision/Recall/FPR 是 `not_evaluated`；13 个合成范围中 12 个有人工作答标签、3 个为 uncertain，覆盖率和不确定比例只说明该合成协议数据，不能代表真实作业。

## P2-D2B：视觉作答证据建议（当前功能分支）

P2-D2B 从 PostgreSQL 的 `SubmissionFile` 读取私有 S3 原始文件，分块验证大小和 SHA-256，再只渲染 P2-D1 为指定 Question 关联的 PDF 页面；JPG/PNG 经格式、尺寸、EXIF 和解压炸弹检查。默认上限为原文件 20 MiB、PDF 200 页、单页 12 MP、每题最多 5 页，单页 JPEG 3 MiB、总图像 12 MiB。PDF 加密、损坏、方向不明或资源超限时安全失败。临时文件使用私有临时目录；渲染线程使用有界不可变输入字节，避免协程取消时与目录删除竞争。

视觉模型默认关闭。`.env.example` 选择 loopback only 的 `ollama_native` 适配器；它使用 Ollama `/api/chat`、关闭内部思考并要求 JSON 输出，适合本地模型。标准 `openai_compatible` 适配器仍可通过 `VISUAL_EVIDENCE_PROVIDER=openai_compatible` 使用兼容 Vision API：

~~~bash
VISUAL_EVIDENCE_ENABLED=true uv run python scripts/run_visual_evidence_vlm_e2e.py
~~~

目前配置示例是 `VISUAL_EVIDENCE_PROVIDER=ollama_native`、`VISUAL_EVIDENCE_BASE_URL=http://127.0.0.1:11434/v1`、`VISUAL_EVIDENCE_MODEL=qwen3.5:4b`。原生 Ollama 适配器只允许本机 HTTP loopback，不接受远程 endpoint；标准 OpenAI-compatible 适配器最多重试临时网络、429、5xx 错误，非法 JSON 不重试。远端 HTTPS 还必须同时显式设置 `VISUAL_EVIDENCE_ALLOW_REMOTE=true` 和 `VISUAL_EVIDENCE_EXTERNAL_DATA_AUTHORIZED=true`，并完成学生数据授权与隐私审查。不要在 endpoint URL 放 Key；Key 用 `VISUAL_EVIDENCE_API_KEY`，日志不记录 Key、图像、提示词或作业内容。

对实际 Submission 的本地命令（需先运行 P2-B/P2-C/P2-D1，确保当前 Canonical 和 Alignment 可用）：

~~~bash
uv run python -m huipi_cloud.workers.analyze_visual_evidence --submission-id SUBMISSION_UUID --question-id QUESTION_UUID --dry-run
VISUAL_EVIDENCE_ENABLED=true uv run python -m huipi_cloud.workers.analyze_visual_evidence --submission-id SUBMISSION_UUID --question-id QUESTION_UUID --request-id REQUEST_UUID
~~~

输出是 `candidate_response_present`、`candidate_prompt_only` 或 `uncertain`。`candidate_prompt_only` 只表示看到了印刷题干，不是 `response_absent`。模型不能输出 Question ID、Canonical pointer、任意解释文本或 URL；服务将图像区域与已验证的题目来源作保守空间匹配，仅在坐标方向可信、唯一关联到目标题且没有跨题区域/共享素材冲突时写入程序生成的候选 pointer。空间重叠只是候选，不是精确几何证明。旋转 PDF 页、非默认 EXIF 朝向、缺少对齐来源或多题共享图形不会获得确定归属。

完整建议 JSON 写入私有 S3 不可变 Key；PostgreSQL 只保存 source/model/version/input digest、状态、SHA、大小和对象索引。同一 `request_id` 且输入摘要相同会重放既有记录；摘要不同返回冲突。模型调用位于数据库事务外；写入前事务锁定 Submission 与 Assignment 并重新核验来源版本。数据库 COMMIT 结果不确定且无法查询时保留对象，以后对账清理；对象存储和 PostgreSQL 没有跨系统原子事务。该流程只产生机器建议，不写 `answer_review_decisions`，不表示作答已确认或 `ready_for_grading`。当前 CLI 未认证，只能在受控本地环境运行。

真实本地 VLM E2E 脚本运行五个按需生成的虚构图像样本：印刷题干、印刷加手写、手写公式、几何标记、空白答题区。它记录协议有效数、输出区域类型、调用耗时和服务可提供的 Token 用量；样本只验证端到端请求/JSON合同，不代表真实学生笔迹准确率。GitHub CI 不下载模型，数据库与 MinIO 路径由 Fake Provider 集成测试覆盖；真实模型质量需要后续用许可、去标识并双人标注的教学数据评测。

2026-10-10 本机实测：Ollama 0.31.2、`qwen3.5:4b` 原生接口，5/5 响应通过协议校验，结构失败 0；单次耗时约 4.8–6.2 秒，可用时每次 prompt 用量为 1771 tokens。混合手写与公式样本分别返回手写文字/公式候选；几何标记样本被判为“仅印刷题干”，这是已观察到的语义误判。来源映射因样本没有 Canonical/Alignment 上下文而未测试，弃权比例 0/5。以上只证明真实模型调用和严格响应合同可运行，不代表视觉准确率。

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
