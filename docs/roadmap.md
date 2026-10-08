# 项目路线图

本页区分已经合并的功能、当前 P2-A 分支内容和未来计划。未合并的代码不代表 `main` 已提供该功能。

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

- 当前分支实现，等待 PR 审查：`pending`、`running`、`retry_wait`、`succeeded`、`failed` 状态机；数据库约束和唯一提交任务约束。
- 当前分支实现，等待 PR 审查：PostgreSQL `FOR UPDATE SKIP LOCKED` 领取、短事务、lease token、heartbeat、到期恢复、有限次数和有上限的指数退避。
- 当前分支实现，等待 PR 审查：FastAPI 之外的独立 Worker 进程入口、安全状态查询 API、ParserExecutor 协议和 SIGTERM/SIGINT 关闭处理。
- 当前分支实现，等待 PR 审查：真实 PostgreSQL 并发和租约集成测试。测试使用专用的成功/失败执行器，不模拟真实解析。
- 未实现：OCR/MinerU 适配器、解析结果存储、外部副作用的幂等产物写入和队列指标。

### P2-B：OCR/MinerU 解析与题目结构化（后续阶段）

- 计划接入 OCR/MinerU 读取 PDF 和图片。
- 计划将页面和题目整理为带来源定位信息的结构化结果。
- 计划建立解析质量检查、失败样本管理和人工纠错流程。
- 计划在真实解析产物落库前定义幂等键、覆盖策略和重复执行处理。

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
