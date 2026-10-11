# 本地快速开始

## 环境要求

- Python 3.12
- uv
- Docker Compose
- curl（作业流程教程还会使用 jq）

应用依赖由 `pyproject.toml` 和 `uv.lock` 管理。MinerU 及模型不属于应用虚拟环境，按需安装，见[MinerU 指南](mineru.md)。

## 启动 API

在仓库根目录执行：

```bash
cp .env.example .env
uv sync --locked
docker compose up -d postgres minio
uv run python scripts/init_minio_bucket.py
uv run alembic upgrade head
uv run uvicorn huipi_cloud.main:app --reload
```

开发容器的端口只绑定 `127.0.0.1`。`.env.example` 中的凭据只用于本地示例；在共享或生产环境中必须换成受控配置，不要提交 `.env`。

- Swagger UI：<http://127.0.0.1:8000/docs>
- OpenAPI JSON：<http://127.0.0.1:8000/openapi.json>
- 存活检查：`GET http://127.0.0.1:8000/api/v1/health`

应用不会在启动时调用 `create_all`。新增或修改业务表需通过 Alembic 迁移。

停止本地服务但保留数据：

```bash
docker compose stop postgres minio
```

此命令不删除 Compose 数据卷。不要用 `down -v` 清理包含开发数据的环境。

## 开发验证

```bash
uv sync --locked
uv run ruff check .
uv run pytest -q
docker compose config --quiet
```

完整 PostgreSQL/S3 集成测试需要本地测试数据库和对象存储。真实 MinerU/Ollama 测试需要额外显式启用，默认 pytest 不会下载模型或访问模型服务。
