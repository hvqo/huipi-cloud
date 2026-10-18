# 快速 CI 与本地完整验证

自动 `Fast CI / fast-ci` 只运行 Ruff、`tests/unit`、单迁移头/历史连通性/模型表与外键的静态检查，以及提交差异检查。前端文件或合同导出脚本变化时，在同一任务中追加锁定安装、OpenAPI 生成差异、TypeScript 和 ESLint。没有 PostgreSQL/MinIO 容器、浏览器下载、真实集成测试或收费模型请求。

全部单元测试已在清空数据库与模型凭据的环境执行；进程取消测试只启动合成 Python 子进程，不启动 OCR/模型或下载权重。静态迁移检查不连接数据库，不能证明真实 DDL、索引或触发器一致。

## 合并前按改动风险执行

- 普通纯函数修改：`uv sync --locked`、`uv run ruff check .`、相关单测、`git diff --check`。
- 数据库/授权/并发修改：必须使用独立真实 PostgreSQL，执行相关集成、安全负向/并发测试、独立空库 upgrade/check/downgrade/再 upgrade、历史数据摘要对比。不得迁移用户开发库；已有新数据时拒绝破坏性降级。
- 对象存储修改：私有独立测试桶及唯一 Key 前缀的真实 S3 集成测试。
- HTTP 合同修改：`uv run python scripts/export_frontend_openapi.py`、`npm run api:generate --prefix frontend`，检查生成差异，并执行前端类型和必要合同测试。
- 前端认证/复核交互修改：既有前端单测、构建及显式 `RUN_WORKBENCH_E2E=1 uv run pytest -q tests/integration/test_teacher_workbench_browser.py`。
- 重要业务切片最终至少一次 `uv run pytest -q` 全后端回归。测试需要隔离的 `TEST_DATABASE_URL`（库名以 `_test` 结尾，不能等于应用库）及独立 S3 测试桶。按现有文档配置，不把凭据写入命令历史。

模型/本地运行时验收维持 opt-in，默认不执行，不将未运行项目称为通过。单元 Fast CI 通过不能代替数据库和业务验收；PR 记录本地执行范围和真实结果，不删测试或断言缩短运行。

## 分支保护

实施时 REST 确认 main `protected=false`，仓库 rulesets 为空，未发现依赖旧 `backend`、`teacher-workbench` 名称的 Required Checks。未来若启用保护，应配置 `fast-ci`；有旧必需检查时先由授权维护者迁移规则，不绕过保护合并。
