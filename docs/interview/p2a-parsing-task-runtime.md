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

## 本轮代码审查补充：Worker 与租约可靠性

以下内容对应本轮实际代码。默认执行时限是 1800 秒，关闭宽限期是 30 秒，取消宽限期是 5 秒；它们都能通过环境变量调整。没有真实 OCR 或解析器。

### 13. Heartbeat 数据库异常时，Worker 如何处理？

**ASD-STE100 简洁解释**：Heartbeat 失败时，Worker 不知道它是否还拥有任务。Worker 请求 Parser 停止。Worker 不把任务标为成功。数据库记录保留到租约过期。另一个 Worker 可以再领取任务。

**30 秒口语回答**：我把“数据库明确返回租约丢失”和“数据库暂时不可用”分开处理。Heartbeat 返回空值表示 token 或租约已经失效，Worker 取消 Parser。`OperationalError`、连接错误或连接池超时表示租约状态未知；Worker也取消 Parser，但不提交成功或失败状态。Parser 正常响应取消后，Worker 留下 `running` 记录，由租约到期恢复。Session 使用事务上下文关闭；其他未预期数据库错误会在清理 Parser 后向外传播。

**2 分钟深入回答**：如果数据库暂时断开，旧 Worker 不能凭本地状态推断自己还拥有任务。即使文件解析刚好完成，也不能安全提交 `succeeded`。`_execute_claim()` 捕获可重试的数据库连接错误，调用 `_cancel_execution()` 并等待取消宽限期。取消完成后，它保留当前任务状态和 lease，让 PostgreSQL 到期条件成为重新领取的依据。Worker 主循环仍可以领取其他任务；领取查询遇到同类连接故障时，会等一个 poll 周期再试。Heartbeat 明确返回 `None` 时，Worker 知道 lease/token 已不匹配，也执行同一取消动作，但日志标记为租约丢失。IntegrityError 等非连接型数据库错误不被当作瞬时故障吞掉；它们会在当前 Parser 清理后传播，由进程级处理记录故障。Repository 每次调用都创建自己的 AsyncSession，`session.begin()` 负责提交或回滚，Session 上下文负责关闭连接。当前策略仍是 At Least Once；若 Parser 在取消前已写外部结果，后续恢复可能重复执行。

**连续追问**：为什么不把失败任务立即改成 `retry_wait`？因为租约所有权未知时，旧 Worker 可能仍有副作用。先让当前 token 过期，避免错误写入状态；后续领取时由数据库生成新 token。

**代码与现场验证**：`src/huipi_cloud/workers/parsing.py` 的 `_execute_claim()`、`_cancel_execution()`、`run()`；`src/huipi_cloud/modules/parsing/repository.py` 的 `heartbeat()`。运行 `test_heartbeat_database_error_cancels_parser_and_leaves_lease_recoverable`，它在真实 PostgreSQL Session 的 UPDATE 上注入 OperationalError，检查 Parser 收到取消、状态不是 succeeded、连接池无占用，并将过期任务用新 token 重新领取。`test_non_transient_database_error_cancels_parser_and_propagates` 验证非瞬时数据库错误不会被吞掉。

### 14. 为什么 `asyncio.Task.cancel()` 不等于强制终止？

**ASD-STE100 简洁解释**：`cancel()` 向协程发送取消请求。协程可以处理这个请求，也可以忽略它。它不会杀死线程或外部程序。

**30 秒口语回答**：`Task.cancel()` 会在协程的一个等待点抛出 `CancelledError`。如果 Parser 正常传播异常，Worker 可以回收 Task 和 Session。如果 Parser 捕获异常后继续运行，Python 没有安全的 API 从另一个协程强制销毁它。因此当前 Worker 等待有限的取消宽限期；仍不退出时，它写日志并通过 `os._exit(70)` 终止 Worker 进程。真实 MinerU 或原生阻塞代码必须放在可监督的子进程中。线程取消不等于线程终止。

**2 分钟深入回答**：`cancel()` 改变 Task 的取消状态，并在协程恢复时注入 `CancelledError`。这只是一种协作协议。协程可以在 `finally` 中释放异步资源后退出，也可以捕获 `CancelledError`、启动后台 Task、等待线程或进入永不返回的同步调用。Worker 不能安全地从同一个事件循环强制关闭这些工作。`_cancel_execution()` 在 `PARSING_CANCEL_GRACE_SECONDS` 内等待子 Task；超时后调用进程故障退出函数。`os._exit()` 会绕过 Python `finally`、atexit 和正常 Session 清理，但操作系统会回收本进程资源，未完成任务仍靠 lease 恢复。它不会撤销外部副作用，也不会自动终止独立子进程。当前 ParserExecutor 合约只允许协作式异步实现，不允许把 `asyncio.to_thread()` 视为隔离机制。未来若解析器调用 MinerU、原生库或不可中断 I/O，P2-B 必须实现可终止并回收的子进程边界。

**连续追问**：为什么不在同一进程里杀死某个 Task？Python 没有安全的强制销毁协程机制；任意终止会让共享状态、锁和资源处于未知状态。

**代码与现场验证**：`src/huipi_cloud/workers/parsing.py` 的 `_abort_process_for_unresponsive_parser()` 和 `_cancel_execution()`；`src/huipi_cloud/modules/parsing/executor.py` 的 `ParserExecutor` 合约。运行 `test_non_cooperative_parser_triggers_bounded_worker_shutdown`。该测试用替代钩子模拟进程故障退出，不会真的终止 pytest 进程；生产钩子会记录任务 ID、刷新日志并退出码为 70。

### 15. 为什么需要 Graceful Shutdown？

**ASD-STE100 简洁解释**：关闭时，Worker 不再领取新任务。它给当前任务一个有限的完成时间。Worker 停止后，过期租约可以恢复未完成任务。

**30 秒口语回答**：SIGINT 或 SIGTERM 会设置 Worker 的停止事件。Worker 不再领取新任务，并在 `PARSING_SHUTDOWN_GRACE_SECONDS` 内等待当前 Parser。时间到后它请求协程取消，并只等待 `PARSING_CANCEL_GRACE_SECONDS`。协作式 Parser 会运行清理逻辑；不响应的 Parser 会触发进程故障退出。任务不会因为收到信号就被假报成功。数据库仍保留运行租约，另一个 Worker 会在到期后恢复。

**2 分钟深入回答**：如果 Worker 立即退出，解析任务可能已经完成一部分外部工作，但还没有提交最终状态。若收到 SIGTERM 后忽略信号，发布或部署流程也可能长期卡住。因此关闭分两段：先给 Parser 一个业务宽限期，允许它完成并正常写状态；宽限期结束后再发取消请求，并等待有限时间确认协程已退出。只有 Parser 已真正返回且当前 lease 有效时，`complete_task()` 才能写 `succeeded`。如果取消成功，状态保留为 `running`，并由数据库租约过期机制恢复；如果取消失败，则 Worker 进程 fail-stop。取消路径的 `finally` 可清理协作式 Parser 的资源，但故障退出不承诺执行 Python 清理逻辑，也不能撤销已写入的外部副作用。测试覆盖协作式停止和不合作 Parser 的有限等待。执行时限是另一设置；它只在事件循环仍可运行时有效，不能解开阻塞事件循环的同步调用。

**连续追问**：Shutdown Grace 与执行时限是否相同？不是。Shutdown Grace 响应进程停止信号；执行时限限制单次解析的总运行时间。Lease 决定数据库中任务所有权何时可以恢复。

**代码与现场验证**：`src/huipi_cloud/workers/parsing_worker.py` 的 SIGINT/SIGTERM 注册、`src/huipi_cloud/workers/parsing.py` 的 `run()`。运行 `test_worker_shutdown_cancels_active_parser_and_leaves_lease_recoverable` 与 `test_non_cooperative_parser_triggers_bounded_worker_shutdown`。`test_execution_timeout_cancels_cooperative_parser_and_records_retryable_failure` 验证执行时限会产生可重试失败。

### 16. Lease 和 Heartbeat 如何恢复卡住的任务？

**ASD-STE100 简洁解释**：Worker 定期延长租约。Worker 停止工作后，租约不会继续延长。租约到期后，其他 Worker 可以重新领取任务。

**30 秒口语回答**：领取任务会写入随机 token 和数据库截止时间。Worker 周期性续租。Worker 崩溃或失去数据库连接后，不再续租，任务到期后可以重新领取。尝试次数超过上限后，任务变成 failed。P2-A 另有总执行时限来处理仍在运行的协作式 Parser；它不能强制中断阻塞线程或原生代码。若 Parser 一直正常 Heartbeat 且没有响应超时取消，当前 Worker 会故障退出；不可中断的解析必须由子进程管理。

**2 分钟深入回答**：`claim_next_task()` 用 `FOR UPDATE SKIP LOCKED` 在短事务内领取任务，写入新 token、尝试次数和 lease。Parser 在事务外运行，减少数据库锁持有时间。Heartbeat 只在状态是 `running`、token 仍匹配且截止时间晚于 PostgreSQL 当前时间时续租。Worker 被终止、数据库不可用或明确失去 lease 时，它不能提交成功；任务保持运行直到过期。下一次 claim 会用新 token 重新领取，达到最大次数则终止任务。单次执行超时使用事件循环的 monotonic 时钟，它独立于 lease 周期，触发协作式取消和安全重试。数据库 lease 解决故障恢复，不保证杀死仍运行的旧 Parser，也不保证外部结果只写一次。当前没有解析产物表或幂等写入机制；这是 P2-B 的前置设计工作。

**连续追问**：Heartbeat 成功能证明解析器有进展吗？不能。当前只证明 Worker 仍在续租。若要判断解析进度，未来 Parser 必须提供进度或阶段状态；本轮没有这项功能。

**代码与现场验证**：`src/huipi_cloud/modules/parsing/repository.py` 的 `claim_next_task()`、`heartbeat()` 和过期 lease 查询；`src/huipi_cloud/workers/parsing.py` 的 `PARSING_EXECUTION_TIMEOUT_SECONDS` 检查。运行 `test_worker_renews_lease_across_multiple_heartbeats_and_blocks_second_claim` 和 `test_expired_lease_is_reclaimed_with_new_token_and_old_worker_is_fenced`。

### 17. 为什么 At Least Once 仍需要幂等解析产物？

**ASD-STE100 简洁解释**：同一任务可能运行多次。Lease Token 能阻止旧 Worker 更新任务状态。它不能删除旧 Worker 已写的文件或数据。

**30 秒口语回答**：Worker 可能在写解析结果后、提交 `succeeded` 前崩溃。租约到期后，另一个 Worker 会再跑一次。数据库 token 会阻止旧 Worker 更新新一轮任务状态，但不能撤销它已经写入的对象或 MongoDB 文档。因此 P2-B 必须用稳定的提交版本和解析器版本构造幂等键，并用唯一约束或原子替换保证重复执行不会复制产物。本轮没有实现幂等产物。

**2 分钟深入回答**：数据库和 OCR、对象存储、MongoDB 等系统之间没有共同事务。如果任务执行先产生副作用，再因为进程崩溃而未能更新 PostgreSQL，恢复机制只知道 lease 已过期，不知道外部副作用是否成功。At Least Once 会重新调用 Parser，所以同一份输入可能有多份运行结果。lease token 是数据库写状态的 fencing 条件；它不能跨存储撤销写入。后续解析产物应有确定性身份，例如 submission ID、输入文件 SHA-256、Parser 版本和 schema 版本的组合。写入使用唯一约束、幂等 upsert 或临时对象加原子发布，再由数据库状态引用已确认的版本。还要考虑运行版本升级、任务重试和旧结果清理。本轮只保存原始文件元数据和任务状态，没有声称解决产物幂等。

**连续追问**：为什么不靠 `succeeded` 状态防重复？因为外部结果写入和 PostgreSQL 状态提交不在同一事务里，两者之间仍有崩溃窗口。

**代码与现场验证**：`src/huipi_cloud/modules/parsing/repository.py` 的 lease token 条件更新；`docs/architecture.md` 的 At Least Once 边界。运行 `test_expired_lease_is_reclaimed_with_new_token_and_old_worker_is_fenced` 可看到旧 token 被拒绝；该测试不验证外部产物幂等，因为本轮没有这种产物。

### 18. PostgreSQL 时间和 Worker 时间有什么区别？

**ASD-STE100 简洁解释**：所有 Worker 使用同一个 PostgreSQL 时钟判断租约。Worker 使用 monotonic 时钟计算本地运行时限。两种时间用于不同目的。

**30 秒口语回答**：如果每台 Worker 用自己的系统时间比较租约，时钟偏差可能让一台机器提前续租或提前抢任务。因此 Repository 把 `clock_timestamp()` 放进 PostgreSQL 条件更新。它在取得行锁后重新检查租约，并从同一个数据库时间生成新截止时间。执行超时用 `asyncio` 的 monotonic 时钟，因为它只需要测量经过多久，不会因系统墙上时间调整而跳变。

**2 分钟深入回答**：数据库中的 `lease_expires_at` 是跨进程协调数据，必须来自统一时钟。Worker 主机时间受 NTP 校正、时区和手工设置影响，所以不应作为租约所有权来源。以前的逻辑先查数据库时间，再等待行锁，最终使用旧值作比较。若租约在等待期间过期，旧 Worker 仍可能续租或提交。现在 Heartbeat、完成和失败都在写操作里直接比较 `lease_expires_at > clock_timestamp()`；PostgreSQL `READ COMMITTED` 在更新等待并取得最新行版本后会重检条件。失败状态通过一条条件 UPDATE 计算，避免 SELECT FOR UPDATE 使用等待前的时间。claim 查询用数据库时间判断可领取条件，并在真正写入运行状态时重新用数据库时钟计算新的截止时间。执行超时不是持久化租约，它使用 monotonic elapsed time；测试可以为 Repository 的时间参数注入固定 UTC 时间。

**连续追问**：`clock_timestamp()` 与 `CURRENT_TIMESTAMP` 有什么差别？前者返回实际墙上时间，并在语句执行期间变化；`CURRENT_TIMESTAMP` 固定在事务开始时间。这里需要在锁等待后取得当前时间，因此使用前者。

**代码与现场验证**：`src/huipi_cloud/modules/parsing/repository.py` 的 `_time_expression()`、`_database_time_after()`、`heartbeat()`、`complete_task()`、`record_failure()` 和 `claim_next_task()`；`src/huipi_cloud/workers/parsing.py` 使用 event loop monotonic time 计算执行 deadline。运行 `test_lease_operation_rejects_worker_after_expiry_while_waiting_for_row_lock`（真实 PostgreSQL 确认语句在等锁后跨过 lease 截止时间）以及 `test_claim_lease_duration_starts_at_database_update_not_before_claim_scan`。
