# 架构与模块边界

慧批云端采用 src 布局的模块化单体。P1-A、P1-B、P2-A 已合并到 `main`；当前 P2-B 功能分支增加真实 MinerU 文档解析和产物索引。自动批改、题目结构化、用户权限仍未实现。

## 模块职责

- `api/v1`：HTTP 路由、请求校验、响应结构和错误码。
- `core`：环境配置和日志。
- `modules/assignments`：作业、题目、人工答案、评分细则和发布校验。
- `modules/submissions`：模拟学生标识校验、文件检查、提交编排、提交查询和原始文件元数据。
- `modules/parsing`：解析任务状态、状态迁移规则、PostgreSQL repository、解析产物索引、错误分类、重试时间策略、状态查询 Schema 与 ParserExecutor 协议。
- `workers`：独立于 FastAPI 的解析任务轮询进程，管理领取、解析调用、heartbeat、结果提交和受控退出。
- `infrastructure/database`：共享 Engine 工厂与按请求/操作创建的 AsyncSession；Alembic 管理结构，应用启动不会调用 `create_all`。
- `infrastructure/storage`：boto3 S3 兼容适配器。本地对象存储使用 PGSTY SILO。
- `infrastructure/parsing`：隔离的 MinerU 4.x 子进程启动、PDF 预检、输出归档校验和不可变解析产物上传。
- `migrations`：按版本演进的 PostgreSQL Schema。

路由负责 HTTP 输入输出；领域服务组织用例；Repository 负责具体数据库查询。当前没有通用 Repository 框架、微服务、Celery、RabbitMQ 或 Redis。

## 数据关系

```mermaid
erDiagram
    ASSIGNMENT ||--o{ QUESTION : contains
    QUESTION ||--o| ANSWER_KEY : has
    QUESTION ||--o{ RUBRIC_CRITERION : scored_by
    ASSIGNMENT ||--o{ SUBMISSION : receives
    SUBMISSION ||--|| SUBMISSION_FILE : stores
    SUBMISSION ||--|| PARSING_TASK : registers
    PARSING_TASK ||--o| PARSED_ARTIFACT : indexes
```

同一 `Submission` 有一份 `SubmissionFile` 元数据与唯一的 `ParsingTask`。提交、对象定位信息和初始 `pending` 任务在一个 PostgreSQL 事务内写入。原文件放在私有 S3 兼容存储，数据库只保存 bucket、服务端生成的 `object_key`、MIME、大小和 SHA-256。

解析成功后，`ParsedArtifact` 在 PostgreSQL 记录解析器版本、模型档位、MiddleJson schema 版本、页数、摘要和产物对象 Key。每个 ParsingTask 和 Submission 最多有一条索引记录。Markdown、MiddleJson、StructuredContent、原始 ZIP、素材文件和素材 manifest 放在同一私有 S3 兼容存储；数据库不保存大块解析正文。成功状态与产物索引由一个带当前 lease token 的数据库事务提交。

## 任务状态与执行边界

允许的主要状态迁移：

```text
pending ──claim──> running ──success──> succeeded
                       ├──temporary failure──> retry_wait ──due claim──> running
                       └──permanent/exhausted──> failed
```

`running` 任务的 `lease_token` 与 `lease_expires_at` 必须同时存在；其他状态不得保留 lease。`retry_wait` 必须有 `next_run_at`。成功和最终失败必须有 `finished_at`。数据库约束防止不合法字段组合；Service/Repository 根据错误类型、尝试次数和当前 lease 选择合法迁移。

`claim_next_task()` 在短事务中先用 `FOR UPDATE SKIP LOCKED` 选出最早的到期任务，生成唯一 token 并增加尝试次数。数据库在写入领取状态时用 `clock_timestamp()` 计算新 lease 截止时间，因此扫描和锁定工作不会提前消耗大部分租约。事务提交后行锁释放；Parser 执行在事务之外。长任务按 heartbeat 间隔续租。完成和续租使用带 token、状态和数据库当前时间条件的 `UPDATE`。失败也在一个条件 `UPDATE` 中检查租约并按尝试次数选择重试状态。PostgreSQL 在 `READ COMMITTED` 下等待行锁后会重新检查 `UPDATE` 的条件；条件中的 `clock_timestamp()` 在更新时判断，不会使用锁等待前读到的旧时间。过期任务可由后续 Worker 领取；过期且达到上限的任务以固定安全错误摘要转为 `failed`。

每次领取、续租、完成或失败更新都创建独立 AsyncSession。请求期间不共享 Session 实例。Engine/Session 工厂是进程级资源；SQLAlchemy Session 不跨请求或 Worker 操作共享。Repository 使用上下文管理器提交或回滚事务，并关闭 Session。租约判断默认使用 PostgreSQL `clock_timestamp()`，所有 Worker 共享数据库时钟；测试可显式注入固定 UTC 时间。

重试错误使用数据库保存的 `next_run_at` 和有上限的指数退避，不在 Worker 中长时间等待某一任务。执行模型是 At Least Once。lease 只能围住 PostgreSQL 状态提交，不能撤销过期 Worker 已经产生的外部副作用；未来解析产物必须按任务或提交版本幂等写入。

Worker CLI 要求 `PARSING_EXECUTOR=module.path:factory`，否则拒绝启动。当前生产工厂为 `huipi_cloud.infrastructure.parsing.mineru:build_mineru_executor`。ParserExecutor 接收不可变的任务与原文件元数据，返回经过校验的 `ParsedArtifactResult`；不能用 `None` 表示成功。测试 Fake executor 只用于验证状态框架，另有 opt-in 测试运行真实 MinerU。

真实执行器只支持 MinerU 4.x Basic/ONNX 本地模型。模型与 CLI 不进入应用 `uv.lock`；部署者需单独安装，配置 `MINERU_EXECUTABLE` 和 `MINERU_HOME`。启动时先验证 CLI 版本和模型。Worker 下载原文件时分块写入权限为 `0700` 的临时目录，复核数据库记录的大小和 SHA-256，再由 `pypdf` 检查 PDF 可读性、加密状态和页数。

MinerU 运行在新 session 的 Linux 子进程组中。输入命令使用参数数组，不经 shell；子进程只收到模型路径和必要运行参数，不继承数据库 URL 或 MinIO 密钥，`HOME` 和临时目录指向每次任务的私有临时目录。输出被持续排空但日志内容不写入应用日志；Worker 监测超时和临时目录占用，超出限制时按 TERM、短宽限、KILL 的顺序结束进程组，并在有界时间内等待进程组消失。若直接子进程无法回收或后代进程仍存活，执行器报致命错误，由 Worker 故障退出，交由租约恢复任务。这个 `os._exit` fail-stop 不执行 Python 的临时目录清理；部署环境需清理由崩溃留下的 `huipi-mineru-*` 工作目录。父进程死亡时，隔离 guard 用 `PR_SET_PDEATHSIG` 清理同组子进程。此机制依赖 Linux，不能终止主机掉电前已发出的外部副作用，也不能保证内核故障时立刻回收。它是进程生命周期管理，不是 OS 安全沙箱；子进程仍以 Worker 的 OS 用户运行，部署时需要用低权限、专用的 Worker 用户和文件权限限制解析风险。

输出 ZIP 会在解压前检查成员数、重复路径、绝对路径、`..`、反斜杠和解压总大小。执行器要求 `markdown.md`、`middle_json.json`、`structured_content.json`，校验 `docvortex.middle` 2.0、MinerU 4.x 生产者版本、档位、页数组和图片引用，再将素材安全地逐块提取并计算 SHA-256。配置限制包括最大输入字节数、PDF 页数、归档/展开字节数、单个文本输出字节数和 ZIP 成员数。

结果对象 Key 使用服务端生成的运行 UUID，形如 `parsed/{submission_id}/tasks/{task_id}/runs/{artifact_id}/...`；如果配置了测试前缀，该前缀会置于 `parsed/` 之前。每个尝试使用新前缀，避免旧 Worker 覆盖新运行的输出。它提供不可变运行隔离，但不构成幂等去重。数据库成功事务失败或进程在对象写入后退出时可能留有孤立对象，当前没有自动清理或对账任务。

查询 `GET /api/v1/submissions/{submission_id}/parsed-document` 返回状态、解析器来源和校验和，不暴露对象 Key。`GET /api/v1/submissions/{submission_id}/parsed-document/markdown` 从私有存储流式传回 Markdown；当前未实现解析资产读取 API。

## 上传和对象一致性

上传时先校验作业、文件名、文件大小、扩展名、声明类型和文件签名，分块读取文件并计算 SHA-256，再将对象写入私有存储。随后在 PostgreSQL 事务内锁定并复核作业状态、写入 `Submission`、`SubmissionFile` 和 `ParsingTask(pending)`。若上传失败，不写提交记录；若数据库事务失败，会尝试删除已上传对象。数据库与对象存储没有共享事务，因此进程崩溃或 COMMIT 结果不确定仍可能留下孤立对象。未来应定期对账清理。

## 安全与可用性边界

`student_ref` 不是认证身份。作业提交、文件下载、任务状态和解析结果查询没有登录、RBAC、归属校验或租户隔离，只能在本地或受控环境使用。任务状态 API 不返回 `lease_token`、对象 Key、文件内容、堆栈或原始异常信息；解析摘要也不返回 bucket 和私有对象 Key。执行错误只保存固定的错误码和短摘要。`GET /api/v1/health` 是存活探针，不检查 PostgreSQL 或对象存储就绪状态。数据库/对象存储连接故障由 API 返回不含内部凭据的安全响应。

## 并发与故障语义

PostgreSQL 是当前任务队列。行锁与 `SKIP LOCKED` 避免 Worker 等待同一行；lease token 和条件更新让旧 Worker 无法覆盖新执行的状态。租约到期不证明旧进程已停止，因此系统不承诺 Exactly Once。Worker 在 SIGINT/SIGTERM 后停止领取任务，并在 `PARSING_SHUTDOWN_GRACE_SECONDS` 内等待当前执行。之后它请求协程取消，并最多等待 `PARSING_CANCEL_GRACE_SECONDS`。若 Parser 不响应取消，Worker 记录任务 ID 后以退出码 70 直接结束进程，让租约过期后恢复任务。该故障退出不会运行 Python 清理钩子，也不能回滚已产生的外部副作用；它只适用于无法通过协程协议安全停止执行的情况。

`PARSING_EXECUTION_TIMEOUT_SECONDS` 限制一次解析的总运行时长，和短期租约时长是不同设置。通用 Async Parser 收到协程取消，但 Python 取消本身不强制终止线程。MinerU 使用受监督的独立进程组，超时时结束整个子进程组。心跳数据库错误或明确失去租约时，Worker 取消执行器；MinerU 子进程清理完成后，不提交 `succeeded`，让租约到期后重新领取。若 Parser 不响应取消，Worker 使用有界等待并按配置 fail-stop；MinerU 的进程组终止不依赖 Async Parser 合作。

Heartbeat 已确认本轮 Task 的状态和租约。它不保证解析产物只写一次。任务可能在对象上传完成后、状态提交前失去租约并被重跑。当前每次执行使用新的 run UUID 目录，旧 token 不能提交 `succeeded` 或索引；但已上传的旧运行对象可能变成孤儿，也没有同一输入的去重策略。后续需要决定稳定解析版本键、重复运行保留策略和孤立对象对账清理，不能把当前不可变 Key 说成产物幂等。

## 配置、迁移和验证

数据库、对象存储、任务尝试次数、lease、heartbeat、执行时限、关闭宽限、退避和 MinerU 资源上限都从环境变量读取。`PARSING_MAX_ATTEMPTS` 的值在创建任务时保存到记录，以便后续配置变化不改写已有任务的重试策略。P2-B Alembic 迁移仅新增 `parsed_artifacts` 表和对应约束/索引，不修改已应用迁移；旧的 `pending` 行继续等待处理。

集成测试要求真实 PostgreSQL；文件和产物集成测试还需要本地 S3 兼容服务。CI 使用 PostgreSQL 16 与 PGSTY SILO；默认 CI 不下载 MinerU 模型。`tests/integration/test_mineru_e2e.py` 使用受控合成文本 PDF、扫描 PDF 和 PNG，经 API、真实 Worker、独立 MinerU 子进程、PostgreSQL 与 S3 验证数据流；运行它需显式启用 `MINERU_E2E=1` 并配置本地模型。测试仅操作 `_test` 数据库和测试对象前缀，不清空开发数据卷。
