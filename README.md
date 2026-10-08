# 慧批云端

慧批云端是一个独立重建的高中 AI 教学辅助平台，面向教师侧多模态作业解析、自动批改 Agent 和教师 AI Copilot。

## 当前实现状态

当前仓库仅包含可运行的 FastAPI 工程骨架：基础应用配置、日志初始化、/api/v1/health 健康检查和对应单元测试。作业、OCR、批改、Agent、Copilot、数据库及其他外部服务均未实现或连接。

## 技术栈

- Python 3.12
- uv
- FastAPI
- Pydantic Settings
- Uvicorn
- pytest、HTTPX、Ruff

## 初始化与运行

在仓库根目录执行：

~~~bash
uv sync
uv run uvicorn huipi_cloud.main:app --reload
~~~

服务启动后可访问：

- Swagger 文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/v1/health

本地配置可从 .env.example 复制为 .env。示例文件不含密钥或内部服务地址。

## 测试与代码检查

~~~bash
uv run pytest
uv run ruff check .
~~~

## 项目结构

~~~text
src/huipi_cloud/
  api/v1/             HTTP 路由与健康检查
  core/               应用配置与日志
  modules/            作业、解析、批改、Copilot、复核领域包
  infrastructure/     数据库、存储、LLM、消息、检索适配器位置
  workers/             后台任务入口位置
tests/
  unit/
  integration/
docs/
  architecture.md
  roadmap.md
~~~
