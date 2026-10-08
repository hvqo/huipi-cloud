# 慧批云端

慧批云端是独立重建的高中 AI 教学辅助平台，面向多模态作业解析、自动批改 Agent、教师 AI Copilot 和教师复核。

## 当前实现状态

- 已合并 P1-A：教师作业、题目、人工标准答案、评分细则和发布校验。
- 已合并 P1-B：学生模拟身份提交、PDF/JPG/PNG 原始文件存储、提交查询、文件读取和初始 `pending` 任务记录。
- P2-A 功能在当前功能分支实现并等待 PR 审查：PostgreSQL 解析任务状态机、租约领取和续租、过期恢复、有限重试、独立 Worker 框架及安全状态查询。
- 当前没有真实 OCR、MinerU、解析结果存储、Celery、RabbitMQ、Redis、自动批改、Copilot 或身份认证。Worker 默认拒绝启动；必须配置真实解析器工厂。测试专用假执行器只用于验证任务框架，不能代表真实解析。

`student_ref` 和当前提交/任务查询 API 没有认证或权限控制，只能在本地或其他受控环境使用，不能直接暴露到公网。

## 技术栈

- Python 3.12、uv、FastAPI、Pydantic Settings
- PostgreSQL 16、SQLAlchemy 2.x Async、asyncpg、Alembic
- S3 兼容的私有对象存储（本地使用 PGSTY SILO）、boto3
- pytest、HTTPX、Ruff

MongoDB、Celery、RabbitMQ、Redis、LangChain、LangGraph、vLLM、OCR 和 MinerU 尚未接入。

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

P2-A 新增 `GET /api/v1/submissions/{submission_id}/parsing-task`。该接口返回安全的任务状态与错误摘要，不返回租约 token。

独立 Worker 命令为：

~~~bash
uv run python -m huipi_cloud.workers.parsing_worker
~~~

当前没有真实解析器，因此未设置 `PARSING_EXECUTOR` 时 Worker 会以配置错误退出，不会领取或伪造成功任务。后续解析器应提供 `module.path:factory` 工厂，并实现 `ParserExecutor.execute()`。

## 验证

完整集成测试要求本地 PostgreSQL `_test` 数据库和本地 S3 兼容服务。它们缺失时测试会失败，不会静默跳过关键集成覆盖。

~~~bash
docker compose up -d postgres minio
uv run python scripts/init_minio_bucket.py
uv sync --locked
uv run ruff check .
uv run pytest -q
uv run alembic check
~~~

P2-A 任务采用 PostgreSQL 持久队列和 At Least Once 执行。过期 lease 可恢复，旧 lease token 不能提交新状态。租约不能阻止已失联 Worker 继续产生外部副作用；P2-B 必须为解析产物设计幂等写入。当前没有真实解析执行与性能数据。

## 项目结构

~~~text
src/huipi_cloud/
  api/v1/                         HTTP 路由
  core/                           配置和日志
  infrastructure/database/       Async Engine、Session、ORM Base
  infrastructure/storage/        S3 兼容对象存储
  modules/assignments/            作业领域
  modules/submissions/            提交、原始文件和任务登记
  modules/parsing/                解析任务状态、执行器协议和查询
  workers/                        独立解析 Worker 进程
migrations/                        Alembic 迁移
tests/unit/                        单元测试
tests/integration/                 PostgreSQL 和 S3 集成测试
docs/                              架构、路线和面试复盘
~~~
