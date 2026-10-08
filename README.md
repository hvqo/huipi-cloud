# 慧批云端

慧批云端是独立重建的高中 AI 教学辅助平台，面向多模态作业解析、自动批改 Agent、教师 AI Copilot 和教师复核。

## 当前实现状态

P1-A 已提供教师作业、题目、人工标准答案、评分细则和发布校验。P1-B 增加学生模拟身份提交、PDF/JPG/PNG 原始文件存储、提交查询、文件读取和待解析任务登记。当前没有登录、RBAC、OCR、MinerU、Worker、消息队列或自动批改。

`student_ref` 仅用于本地开发演示，不是经过认证的身份。提交、查询和下载接口没有访问控制，只能在本地或其他受控环境使用，不能直接暴露到公网。

## 技术栈

- Python 3.12、uv、FastAPI、Pydantic Settings
- PostgreSQL 16、SQLAlchemy 2.x Async、asyncpg、Alembic
- MinIO 兼容的 S3 存储（本地使用 PGSTY SILO 社区分支）、boto3
- pytest、HTTPX、Ruff

MongoDB、Celery、RabbitMQ、Redis、LangChain、LangGraph、vLLM、OCR 和 MinerU 尚未接入。

## 本地初始化与启动

首次设置配置文件：

~~~bash
cp .env.example .env
uv sync --locked
~~~

如果已有 `.env`，保留现有数据库设置，并从 `.env.example` 补入 `MINIO_*` 与 `MAX_UPLOAD_SIZE_BYTES` 配置。示例密钥只供本机开发使用。

启动本轮使用的 PostgreSQL 和 MinIO。已有 PostgreSQL 数据卷会继续使用：

~~~bash
docker compose up -d postgres minio
uv run python scripts/init_minio_bucket.py
uv run alembic upgrade head
uv run uvicorn huipi_cloud.main:app --reload
~~~

- API 文档：http://127.0.0.1:8000/docs
- 存活检查：http://127.0.0.1:8000/api/v1/health
- MinIO 控制台：http://127.0.0.1:59001

存储桶保持私有。应用不使用 `create_all` 建表；表结构只由 Alembic 迁移管理。

## API 概览

- `POST /api/v1/assignments`、`GET /api/v1/assignments`、`GET /api/v1/assignments/{id}`：作业管理。
- `POST /api/v1/assignments/{id}/questions`：添加题目。
- `PUT /api/v1/questions/{id}/answer-key`、`PUT /api/v1/questions/{id}/rubric`：设置标准答案和整体替换评分细则。
- `POST /api/v1/assignments/{id}/publish`：检查后发布作业。
- `POST /api/v1/assignments/{id}/submissions`：提交一份 PDF、JPG/JPEG 或 PNG 文件（multipart 字段 `student_ref`、`file`）。默认上限为 20 MiB；请求体在解析和文件读取阶段都有大小限制。
- `GET /api/v1/submissions/{id}`：查询提交元数据和待解析任务。
- `GET /api/v1/assignments/{id}/submissions`：分页查询作业提交。
- `GET /api/v1/submissions/{id}/file`：经后端流式读取原始文件。

作业必须已发布。每个模拟学生对同一作业只允许成功提交一次。对象 Key 由服务端生成，不含原始文件名；API 不返回公开 URL。

可以这样手工提交（先将 `ASSIGNMENT_ID` 换成已发布作业的 UUID）：

~~~bash
curl -X POST "http://127.0.0.1:8000/api/v1/assignments/$ASSIGNMENT_ID/submissions" \
  -F 'student_ref=student-001' \
  -F 'file=@./answer.pdf;type=application/pdf'
~~~

## 测试与检查

集成测试使用独立的 PostgreSQL `_test` 数据库和 `MINIO_TEST_BUCKET`，不清空开发数据库或开发存储桶。对象只使用测试 Key 前缀；测试后只删除自身上传的对象。

~~~bash
docker compose up -d postgres minio
uv run python scripts/init_minio_bucket.py
uv run alembic upgrade head
uv run ruff check .
uv run pytest
uv run alembic check
~~~

CI 在 Ubuntu、Python 3.12、PostgreSQL 16 和真实 S3 兼容对象存储容器上运行同一组静态检查、迁移验证和测试。容器使用 PGSTY SILO（MinIO 兼容分支）；没有配置本地 MinIO 兼容服务时，对象存储集成测试会跳过并说明原因。

## 数据流与限制

上传时，应用分块校验并计算 SHA-256，先写私有 MinIO，再在一个 PostgreSQL 事务里写入 `Submission`、`SubmissionFile` 和 `ParsingTask(pending)`。确认事务尚未进入 COMMIT 时失败，会尝试删除刚写入的对象；COMMIT 请求的结果不确定时保留对象，避免已提交的数据库记录指向被删除的文件。进程崩溃和不确定的 COMMIT 仍可能留下孤立对象；未来应增加定期对账清理。

MinIO 社区版上游已停止发布官方容器镜像。本地 Compose 固定使用 PGSTY SILO 的 MinIO 兼容分支版本，它保留 S3 API 和 `MINIO_*` 配置，但不是 MinIO 官方产品。部署前应检查该分支的更新、兼容性和 AGPL 许可证要求；不能把本地演示配置作为生产存储方案。

## 项目结构

~~~text
src/huipi_cloud/
  api/v1/                         HTTP API 路由
  core/                           应用配置、日志
  infrastructure/database/       Async Engine、Session、ORM Base
  infrastructure/storage/        S3 兼容对象存储适配器
  modules/assignments/            作业领域模型与服务
  modules/submissions/            提交、文件元数据、解析任务登记
  modules/{parsing,grading,...}/  后续模块边界
  workers/                         后续后台任务入口
migrations/                        Alembic 迁移
tests/unit/                        单元测试
tests/integration/                 PostgreSQL 与 MinIO 集成测试
docs/                              架构、路线和面试复盘
~~~
