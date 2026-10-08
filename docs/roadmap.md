# 项目路线图

本文区分当前仓库已实现的内容与未来计划。本分支只覆盖 P1-B，不开始 P2。

## P0：工程骨架与基础设施

- 已实现：Python 3.12 与 uv、src 包、FastAPI、Pydantic Settings、日志、健康检查、pytest 与 Ruff。
- 已实现：PostgreSQL Async、Docker Compose 本地 PostgreSQL、Alembic、PostgreSQL 集成测试和 GitHub Actions 基础检查。
- 已实现：本地 MinIO 兼容存储 Compose 服务、私有开发存储桶初始化和 S3 兼容适配层。
- 后续计划：生产环境配置校验、监控、日志关联和部署流程。

## P1：作业提交、原始文件存储、任务管理

### P1-A：核心数据模型与教师作业管理

- 已实现并合并：Assignment、Question、AnswerKey、RubricCriterion、草稿编辑、完整性校验和作业发布 API。

### P1-B：学生作业提交与原始文件存储

- 本分支实现：模拟学生标识、单文件 PDF/JPG/PNG 上传、大小和签名检查、SHA-256、MinIO 私有存储、提交元数据查询和安全流式读取。
- 本分支实现：同一学生对同一作业只能成功提交一次；Submission、SubmissionFile 和 `pending` ParsingTask 在同一 PostgreSQL 事务中创建。
- 本分支实现：对象存储与数据库失败补偿，并记录进程崩溃可能造成孤立对象的风险。
- 尚未实现：真实学生身份、重复提交版本、文件删除 API、后台对账清理、任务队列、Worker 和解析状态流转。

## P2：OCR/MinerU 多模态解析与题目结构化

- 计划接入 OCR/MinerU 处理 PDF 和图片。
- 计划把页面、题目、答案区域整理成可追踪的结构化结果。
- 计划建立解析质量检查和人工纠错流程。
- 当前 `ParsingTask` 仅为数据库待处理记录，不会触发解析。

## P3：标准答案管理、Rubric 评分、AI 自动批改 Agent

- 计划支持标准答案与 Rubric 版本。
- 计划实现可追踪、可解释的自动批改 Agent 和失败回退。
- 当前标准答案由教师作业 API 手工设置；AI 生成答案和自动批改尚未实现。

## P4：教师 Copilot、检索与工具调用

- 计划围绕作业和批改结果提供教师 Copilot。
- 计划加入受权限约束的检索、上下文组织和工具调用记录。

## P5：教师复核、异步任务优化、模型推理与性能评测

- 计划支持教师复核、修改和反馈记录。
- 计划优化异步任务吞吐、可观测性和资源使用。
- 计划评估模型推理部署，并建立质量、延迟和成本评测。
