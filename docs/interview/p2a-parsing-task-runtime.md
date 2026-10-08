# P2-A：可靠解析任务运行框架面试复盘

本文记录当前功能分支中的实际代码。它不表示已经有 OCR/MinerU 或生产解析器。测试执行器只验证任务状态与 lease 行为。

## 1. 为什么上传后不直接同步运行 MinerU？

**简明技术解释（ASD-STE100 风格）**：文件上传要快速返回。文档解析可能耗时或失败。系统先保存提交和待处理任务，再由 Worker 执行。

**面试口语回答**：上传接口现在只负责校验文件、写入对象存储，再在 PostgreSQL 事务中创建提交、文件元数据和 `pending` 任务。它不会在 HTTP 请求里运行解析器。这样客户端不用一直等解析结束，短暂失败也可以按持久化的重试时间恢复。目前仓库还没有 MinerU 执行器，所以不能把上传成功说成解析成功。

**代码位置**：`src/huipi_cloud/modules/submissions/service.py` 的 `create_submission()`；`src/huipi_cloud/workers/parsing.py` 的 `ParsingWorker.run()`。

**验证方式**：运行 `uv run pytest -q tests/integration/test_submissions_api.py tests/integration/test_parsing_runtime.py`；检查上传响应中的任务仍是 `pending`，再观察任务测试由 Worker 框架领取。

## 2. 为什么需要独立 Worker？

**简明技术解释**：Web 请求和长时间解析有不同的运行时间。独立进程可以在 API 进程退出后继续工作，并能单独重启。

**面试口语回答**：FastAPI 负责请求和响应；Worker 负责领取、执行和更新任务。两者通过 PostgreSQL 任务记录协作，不用 `BackgroundTasks` 把长任务绑在 Web 进程上。Worker 收到终止信号后停止领取新任务，并在宽限时间内处理当前任务。当前 Worker 需要显式配置真实解析器；没有解析器时它会拒绝启动。

**代码位置**：`src/huipi_cloud/workers/parsing_worker.py` 的 `run_worker()`；`src/huipi_cloud/workers/parsing.py` 的 `ParsingWorker.run()`。

**验证方式**：运行 `uv run python -m huipi_cloud.workers.parsing_worker`，未配置 `PARSING_EXECUTOR` 时应以配置错误退出，不能改变任务状态。

## 3. PostgreSQL 如何作为任务队列？

**简明技术解释**：任务状态和到期时间保存在 PostgreSQL。Worker 查询可执行任务，并在同一事务内更新租约。

**面试口语回答**：P1-B 已经在提交事务中创建 `pending` 任务。P2-A 让 Worker 从 PostgreSQL 找到 `pending`、到期的 `retry_wait` 或 lease 已过期的 `running` 任务。它领取任务时设置状态、token、过期时间和尝试次数。PostgreSQL 同时提供持久化、唯一约束和行锁，所以当前规模不需要额外部署 RabbitMQ 和 Redis。具体队列替换会留待需求出现后评估。

**代码位置**：`src/huipi_cloud/modules/parsing/repository.py` 的 `claim_next_task()`；`src/huipi_cloud/modules/submissions/models.py` 的 `ParsingTask`。

**验证方式**：运行 `uv run pytest -q tests/integration/test_parsing_runtime.py`，并检查 `parsing_tasks` 表中的状态、尝试次数和 lease 字段。

## 4. `FOR UPDATE SKIP LOCKED` 有什么作用？

**简明技术解释**：`FOR UPDATE` 锁定当前事务要领取的行。`SKIP LOCKED` 让其他 Worker 跳过这行并查找其他任务。

**面试口语回答**：如果两个 Worker 同时选择同一个 `pending` 行，数据库行锁会保护这次领取。第二个 Worker 使用 `SKIP LOCKED` 后不会等待第一个任务执行，而是跳过已锁的行。事务结束后，状态已经是 `running`，有效 lease 内其他 Worker 也不再匹配它。测试会先在真实 PostgreSQL 中锁定一条任务，再验证 claimant 领取另一条任务，之后才能领取被释放的任务。

**代码位置**：`src/huipi_cloud/modules/parsing/repository.py` 的 `claim_next_task()` 和 `_fail_exhausted_expired_leases()`。

**验证方式**：运行 `uv run pytest -q tests/integration/test_parsing_runtime.py::test_postgres_skip_locked_allows_different_workers_to_claim_distinct_tasks`。

## 5. 为什么领取后立即提交事务？

**简明技术解释**：事务只保护选取和租约更新。解析不在事务内运行，所以数据库锁不会被长时间持有。

**面试口语回答**：领取函数在 `async with session.begin()` 内锁住一行、写入 `running` 和租约，然后返回。离开上下文时事务已经提交。Worker 随后才调用执行器。若把 MinerU 调用放在事务中，网络或 CPU 工作会延长锁和连接占用时间，也会让其他 Worker 等待。当前没有真实 Parser，因此这条边界由框架代码和 PostgreSQL 锁测试验证。

**代码位置**：`src/huipi_cloud/modules/parsing/repository.py` 的 `claim_next_task()`；`src/huipi_cloud/workers/parsing.py` 的 `_execute_claim()`。

**验证方式**：查看 claim 返回后数据库里的状态为 `running`，并用独立事务读取该行；运行 `test_postgres_skip_locked_allows_different_workers_to_claim_distinct_tasks`。

## 6. Worker 崩溃后怎么办？

**简明技术解释**：任务保留 `running` 和到期时间。到期后，另一个 Worker 可以重新领取。超过尝试次数的任务变成 `failed`。

**面试口语回答**：每次领取都会写 lease 期限。期限使用 PostgreSQL 时钟判断，避免 Worker 主机时钟偏差。Worker 崩溃后不会有 heartbeat，期限最终会到。达到尝试上限前，下一次 claim 可以更换 token 并重新执行；达到上限后，claim 事务会将任务设为 `failed`。这是租约恢复，不是立即探测进程是否死亡，所以恢复可能要等 lease 超时。数据库状态更新失败时，Worker 不会假报成功或失败。

**代码位置**：`src/huipi_cloud/modules/parsing/repository.py` 的 `claim_next_task()`、`_fail_exhausted_expired_leases()`；`src/huipi_cloud/core/config.py` 的 lease 配置。

**验证方式**：运行 `test_expired_lease_is_reclaimed_with_new_token_and_old_worker_is_fenced` 与 `test_expired_lease_after_max_attempts_is_marked_failed`。

## 7. 如何避免两个 Worker 同时拥有同一个有效任务？

**简明技术解释**：PostgreSQL 行锁负责原子领取。任务状态、租约 token 和到期时间一起构成当前所有权。

**面试口语回答**：领取查询锁住任务行并跳过其他 Worker 正在锁定的行。第一个事务提交后，任务已经是 `running`，且 lease 未过期，不再匹配领取条件。即使旧进程之后恢复，它的 token 也不会匹配新 Worker 的 token。这里没有 Redis 锁；数据库状态就是当前协调依据。

**代码位置**：`src/huipi_cloud/modules/parsing/repository.py` 的 `claim_next_task()`、`heartbeat()`、`complete_task()` 和 `record_failure()`。

**验证方式**：运行 `test_postgres_skip_locked_allows_different_workers_to_claim_distinct_tasks` 与 `test_concurrent_claims_never_return_the_same_live_task`。

## 8. At Least Once 和 Exactly Once 有什么区别？

**简明技术解释**：At Least Once 表示任务可能重复执行。Exactly Once 表示业务效果只发生一次。租约本身不能提供 Exactly Once。

**面试口语回答**：Worker 执行期间如果 lease 过期，另一个 Worker 可以重新领取。旧 Worker 可能仍在运行，因此解析逻辑可能重复执行。token 只能阻止旧 Worker 更新新执行的任务状态，不能撤销已经写出的外部文件或远程调用。本轮按 At Least Once 设计，不承诺 Exactly Once。P2-B 必须给解析产物设计稳定幂等键和重复写入策略。

**代码位置**：`src/huipi_cloud/modules/parsing/repository.py` 的条件状态更新；`docs/architecture.md` 的任务执行边界。

**验证方式**：运行 `test_expired_lease_is_reclaimed_with_new_token_and_old_worker_is_fenced`，观察旧 token 的完成更新返回 `False`。

## 9. 为什么需要 heartbeat 和 lease token？

**简明技术解释**：Heartbeat 延长仍在工作的租约。Token 标识某次领取。旧 token 不能续租或提交新状态。

**面试口语回答**：如果任务比初始 lease 更长，Worker 必须定期续租，否则另一个 Worker 可能重新领取。Heartbeat 只在任务仍为 `running`、token 匹配且 lease 未过期时成功。任务完成和失败也使用相同条件。旧 Worker 在新 Worker 接管后不能覆盖数据库结果。外部副作用仍需由 Parser 自己做幂等处理。

**代码位置**：`src/huipi_cloud/workers/parsing.py` 的 `_execute_claim()`；`src/huipi_cloud/modules/parsing/repository.py` 的 `heartbeat()`、`complete_task()`、`record_failure()`。

**验证方式**：运行 `test_heartbeat_extends_only_the_current_unexpired_lease` 和旧 token fence 测试。

## 10. 为什么要区分可重试和不可重试错误？

**简明技术解释**：网络或资源错误可能稍后恢复。文件格式错误通常不会因重试而改变。

**面试口语回答**：执行器用 `RetryableParsingError` 表示暂时故障，用 `PermanentParsingError` 表示确定的输入或格式问题。暂时故障在剩余尝试次数内进入 `retry_wait`，并保存 `next_run_at`；退避指数增长，但不会超过配置上限。永久错误直接进入 `failed`。未知异常只保存固定的通用摘要，并在有限次数内重试；原始异常文本不会返回给 API 或存入任务记录。

**代码位置**：`src/huipi_cloud/modules/parsing/errors.py` 的错误分类；`src/huipi_cloud/modules/parsing/service.py` 的 `retry_delay_seconds()`；`src/huipi_cloud/modules/parsing/repository.py` 的 `record_failure()`。

**验证方式**：运行 `test_non_retryable_failure_finishes_without_retry`、`test_retryable_failure_waits_until_persisted_due_time` 和 `test_error_classifier_never_exposes_raw_exception_text`。

## 11. 为什么当前不使用 Celery 和 RabbitMQ？

**简明技术解释**：当前只有一个任务类型和一个 PostgreSQL。增加 Broker 会增加部署和一致性成本。

**面试口语回答**：P1-B 已经把待解析任务写入 PostgreSQL。本阶段重点是验证状态、租约、恢复和幂等边界。PostgreSQL 队列足以实现当前最小执行闭环，而且任务数据和业务数据在同一数据库中。Celery、RabbitMQ 和 Redis 尚未加入。若以后确认吞吐、调度或路由需求超过数据库方案，再通过指标和运行需求评估迁移，而不是现在同时维护两套队列。

**代码位置**：`src/huipi_cloud/modules/parsing/repository.py`；`pyproject.toml` 当前依赖列表。

**验证方式**：运行 `uv run uv tree`，确认没有 Celery、RabbitMQ 客户端或 Redis 客户端依赖；检查任务领取直接使用 SQLAlchemy/PostgreSQL。

## 12. 未来如何把执行层替换成 Celery？

**简明技术解释**：保留任务和 ParserExecutor 的业务边界。更换调度和投递层，不改文件校验与解析错误分类。

**面试口语回答**：当前 `ParserExecutor` 定义执行输入和成功/失败语义，`ParsingWorker` 负责任务领取与 lease 管理。未来若采用 Celery，可以将任务投递适配到 Celery，并保持解析器接口、文件元数据和安全错误分类稳定。但不能简单同时向 PostgreSQL 与 Broker 写任务；需要事务性 outbox 或可恢复的投递机制，还要设计 Broker 重复投递、ack、重试和数据库状态对账。这个迁移目前只是规划，没有实现。

**代码位置**：`src/huipi_cloud/modules/parsing/executor.py` 的 `ParserExecutor`；`src/huipi_cloud/workers/parsing.py`；`docs/roadmap.md` 的 P4 规划。

**验证方式**：查看当前 worker 和 executor 的边界；仓库目前没有 Celery 任务、Broker 配置或 outbox 表，因此该替换方案不能宣称已验证。
