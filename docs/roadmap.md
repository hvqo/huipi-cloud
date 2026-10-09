# 项目路线图

本页区分已经合并的功能、当前功能分支范围和未来计划。PR #5 记录 P2-B 的最终验收证据及剩余风险；P2-C 当前仍在功能分支验收，尚未合并。

## P0：工程骨架与基础设施

- 已合并：Python 3.12、uv、src 布局、FastAPI、Pydantic Settings、日志、健康检查、pytest 和 Ruff。
- 已合并：PostgreSQL Async、Docker Compose、Alembic、真实 PostgreSQL 集成测试和 GitHub Actions。
- 已合并：本地 S3 兼容对象存储、私有测试 bucket 和文件适配层。
- 未来：生产配置校验、监控、日志关联和部署流程。

## P1：作业提交、原始文件存储、任务管理

### P1-A：核心数据模型与教师作业管理

- 已合并：Assignment、Question、AnswerKey、RubricCriterion、草稿编辑、发布完整性检查和作业管理 API。

### P1-B：学生提交与原始文件存储

- 已合并：模拟学生标识、单文件 PDF/JPG/PNG 上传、文件大小和签名检查、SHA-256、私有对象存储、提交查询与流式下载。
- 已合并：同一模拟学生对同一作业只能提交一次；Submission、SubmissionFile 和 `pending` ParsingTask 在同一个 PostgreSQL 事务中创建。
- 已合并：数据库失败后的对象补偿删除和 COMMIT 结果不确定时保留对象；进程崩溃仍可能留下孤立对象。
- 未实现：真实学生身份、重新提交版本、对象对账清理。

## P2：OCR/MinerU 多模态解析与题目结构化

### P2-A：可靠解析任务执行框架

- 已合并：`pending`、`running`、`retry_wait`、`succeeded`、`failed` 状态机；数据库约束和唯一提交任务约束。
- 已合并：PostgreSQL `FOR UPDATE SKIP LOCKED` 领取、短事务、lease token、heartbeat、到期恢复、有限次数和有上限的指数退避。
- 已合并：FastAPI 之外的独立 Worker 进程入口、安全状态查询 API、ParserExecutor 协议和 SIGTERM/SIGINT 关闭处理。
- 已合并：真实 PostgreSQL 并发和租约集成测试。测试使用专用的成功/失败执行器验证任务框架。

### P2-B：真实 MinerU 文档解析与解析产物持久化

- 已实现：独立安装 MinerU 4.x Basic/ONNX 本地运行时，受监督子进程解析 PDF 和图片；MinerU 模型不装入应用 `uv` 环境。
- 已实现：分块读取原始提交、校验 SHA-256、PDF 页数预检、归档资源上限、路径防护和按 MinerU 4.x 输出合同校验 MiddleJson。
- 已实现：私有 S3 兼容存储保存原始 ZIP、Markdown、MiddleJson、StructuredContent、图片素材及 manifest；PostgreSQL 只索引元数据、hash 和对象 Key。
- 已实现：成功状态和 `ParsedArtifact` 索引由一个 lease-fenced PostgreSQL 事务写入；提供安全产物摘要和 Markdown 流式读取 API。
- 有 opt-in 真实 MinerU + PostgreSQL + S3 端到端测试，覆盖合成文本 PDF、扫描 PDF 和 PNG；默认 CI 不下载模型，因此真实解析用例需在配置模型的环境中执行。合成样本通过不能证明真实教学数据的识别准确率。
- 配置的 S3 bucket 必须与原始 SubmissionFile 记录一致；单桶配置切换不迁移文件，任务会以安全的永久配置错误停止。
- `PARSING_WORKER` 每个进程一次只领取一个任务；多进程并发、PDF 页数与产物大小上限不是硬内存/CPU 配额。部署时应设置主机/cgroup 限额并先测量峰值 RSS。
- S3 上传取消不能终止已运行的 boto3 同步线程；数据库不提交旧 lease 的产物索引，但对象可能孤立。当前未实现对象对账清理。
- 未实现：对象写入的同一任务去重、孤立解析对象的对账清理、题目切分/题号识别、人工纠错和解析质量运营流程。

### P2-C：Canonical Document 标准化与持久化（当前分支，待验收）

- 本分支实现：独立的 Canonical Document v1 DTO 和纯转换器，读取已经成功并通过 P2-B 索引的 MinerU 4.x MiddleJson 2.0 与 assets manifest。此项尚未合并到 main。
- 本分支实现：保留原始页码、顶层 Block index、文档级确定性 reading order、BBox 坐标语义、文本/公式/表格/图片/布局类型及嵌套 Block/Span 来源结构；未知源类型和损坏引用明确失败。
- 本分支实现：稳定 UUIDv5、键排序 JSON、字节/节点/深度上限、SHA-256 校验；Canonical JSON 放私有 S3，PostgreSQL 只保存轻量索引与确定性失败码。
- 本分支实现：受控 CLI 单独规范化；状态摘要和按页 API 不暴露 S3 Key。规范化失败不会回写 P2-B ParsingTask 状态。
- 本分支测试：合成合同转换测试与真实 PostgreSQL + S3 索引/并发/故障补偿集成测试。MinerU E2E 测试扩展为解析后规范化和页面 API 验收，但运行仍要求独立安装 CLI 与 Basic/ONNX 模型，默认 CI 不下载模型。
- 已知边界：DB 登记失败或并发输家可能留下不可变孤立 Canonical 对象；当前无对账清理器。外部图片不联网读取，尚无图片素材读取 API；当前接口没有认证和租户权限，不可直接公开部署。

## P3：标准答案管理、Rubric 评分、AI 自动批改 Agent

- 计划支持标准答案与 Rubric 版本。
- 计划实现可追踪、可解释的自动批改 Agent 和失败回退。
- 当前标准答案由教师 API 手工设置；模型生成答案和自动批改尚未实现。

## P4：教师 Copilot、检索与工具调用

- 计划围绕作业和批改结果提供教师 Copilot。
- 计划加入权限约束的检索、上下文组织和工具调用记录。
- 计划根据实际队列需求评估 Celery 与 RabbitMQ；当前不维护第二套任务队列。

## P5：教师复核、异步任务优化、模型推理与性能评测

- 计划支持教师复核、修改和反馈记录。
- 计划优化异步执行的可观测性、吞吐和资源使用。
- 计划评估模型推理部署，并建立质量、延迟和成本评测；当前没有相关性能数据。
