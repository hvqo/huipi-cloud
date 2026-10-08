# 慧批云端

慧批云端是一个独立重建的高中 AI 教学辅助平台，面向多模态作业解析、自动批改 Agent、教师 AI Copilot 和教师复核。

## 当前实现状态

本分支完成 P1-A：教师侧作业草稿、题目、人工标准答案、评分细则、作业查询和发布检查。使用 PostgreSQL、SQLAlchemy 2.x Async 与 Alembic。当前没有学生提交、文件上传、OCR、AI 批改、登录鉴权或教师权限控制；开发 API 不应直接暴露到生产环境。

## 技术栈

- Python 3.12、uv
- FastAPI、Pydantic Settings
- PostgreSQL 16、SQLAlchemy 2.x Async、asyncpg、Alembic
- pytest、HTTPX、Ruff
- Docker Compose 仅提供本地 PostgreSQL

MongoDB、MinIO、Celery、RabbitMQ、Redis、LangChain、LangGraph、vLLM、OCR 和 MinerU 均未引入。

## 本地启动

首次启动时，在仓库根目录执行：

~~~bash
cp .env.example .env
uv sync
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn huipi_cloud.main:app --reload
~~~

PostgreSQL 仅绑定到本机 127.0.0.1:55432。Compose 首次初始化数据卷时会创建应用数据库和独立测试数据库。应用启动不会自动创建表；结构变更必须通过 Alembic 迁移。

- API 文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/v1/health

停止本轮数据库服务并保留数据卷：

~~~bash
docker compose stop postgres
~~~

## API 概览

- POST /api/v1/assignments：创建草稿作业。
- GET /api/v1/assignments：分页查询作业摘要。
- GET /api/v1/assignments/{assignment_id}：查询作业及题目、答案、评分细则。
- POST /api/v1/assignments/{assignment_id}/questions：添加题目。
- PUT /api/v1/questions/{question_id}/answer-key：设置或替换人工标准答案。
- PUT /api/v1/questions/{question_id}/rubric：整体替换题目评分细则。
- POST /api/v1/assignments/{assignment_id}/publish：发布满足完整性要求的草稿。

没有鉴权和教师权限边界；任何能访问本地 API 的调用方都可以修改作业。

## 测试与检查

~~~bash
uv run ruff check .
uv run pytest
uv run alembic check
~~~

集成测试使用 TEST_DATABASE_URL 指向名称以 _test 结尾的 PostgreSQL 数据库，并在测试前运行 Alembic。未设置测试数据库 URL 时，PostgreSQL 集成测试会明确跳过；不要把该配置指向开发数据库。

## 项目结构

~~~text
src/huipi_cloud/
  api/v1/                         HTTP 路由
  core/                           配置、日志
  infrastructure/database/       Async Engine、Session、ORM Base
  modules/assignments/            作业管理模型、Schema、Repository、Service、Router
  modules/{parsing,grading,...}/  后续业务模块位置
  workers/                         后台任务位置
migrations/                        Alembic 环境和版本迁移
tests/
  unit/
  integration/
docs/
  interview/
~~~
