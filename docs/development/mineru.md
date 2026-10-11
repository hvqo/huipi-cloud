# MinerU 本地解析

应用 `uv` 环境不安装 MinerU 或解析模型。Worker 使用独立 Python 3.12 环境和 MinerU 4.x Basic/ONNX 本地模型运行真实解析。

## 安装与模型

在仓库根目录之外为 MinerU 建立独立环境。下面的示例固定使用已验证的 MinerU 4.0.10：

```bash
uv venv /绝对路径/mineru-venv --python 3.12
uv pip install --python /绝对路径/mineru-venv/bin/python 'mineru==4.0.10'

MINERU_HOME=/绝对路径/mineru-home \
MINERU_MODEL_SOURCE=modelscope \
/绝对路径/mineru-venv/bin/mineru-kit models download \
  --tier basic --small-backend onnx --source modelscope

MINERU_HOME=/绝对路径/mineru-home \
MINERU_MODEL_SOURCE=local \
/绝对路径/mineru-venv/bin/mineru-kit models verify \
  --tier basic --small-backend onnx
```

模型下载源由开发者按环境选择。下载或安装过程不会由项目启动命令自动触发。

## 配置与启动 Worker

在本地 `.env` 中设置实际绝对路径：

```dotenv
PARSING_EXECUTOR=huipi_cloud.infrastructure.parsing.mineru:build_mineru_executor
MINERU_EXECUTABLE=/绝对路径/mineru-venv/bin/mineru-kit
MINERU_HOME=/绝对路径/mineru-home
MINERU_TIER=basic
```

确保 PostgreSQL 和私有对象存储已经启动，并且至少有一条待解析提交，再运行：

```bash
uv run python -m huipi_cloud.workers.parsing_worker
```

Worker 运行在 FastAPI 之外。它领取 `pending` 任务、续租、调用受监督的 MinerU 子进程，并把不可变解析产物写入对象存储。Worker 不会隐式建立数据库表；先执行 `uv run alembic upgrade head`。

## 后处理

解析完成后，在仓库根目录运行：

```bash
uv run python -m huipi_cloud.workers.normalize_document --submission-id SUBMISSION_UUID
uv run python -m huipi_cloud.workers.align_answers --submission-id SUBMISSION_UUID
```

任务状态、解析摘要和 Markdown 分别可通过 `GET /api/v1/submissions/{id}/parsing-task`、`GET /api/v1/submissions/{id}/parsed-document` 和 `GET /api/v1/submissions/{id}/parsed-document/markdown` 查询。Canonical 和对齐结果也有摘要及受限页面/题目查询 API；完整 Canonical 对象保存在私有对象存储中。

解析输入、页数和归档展开内容均有上限，但这些限制不是对子进程设置的硬内存或 CPU 配额。并行 Worker 会增加主机负载；运行真实作业前应按部署环境设置进程/容器资源约束，并检查孤立对象的清理策略。
