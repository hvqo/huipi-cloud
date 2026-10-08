# P1-B 面试复盘：学生提交与原始文件存储

本文件只说明当前代码。`student_ref` 是本地模拟标识；没有用户认证、权限检查、消息队列、Worker、OCR 或 MinerU 实现。本地对象存储容器采用 PGSTY SILO，一个维护中的 MinIO 兼容分支。

## 1. 为什么原始文件使用 MinIO，而不是 PostgreSQL？

**简明技术说明（短句）**：PostgreSQL 保存结构化记录和事务关系。MinIO 保存大文件。数据库用 Key 定位文件。

**面试口语回答**：作业图片和 PDF 属于二进制对象。PostgreSQL 适合保存提交者、作业关系、文件大小和摘要等结构化数据，也能在一个事务里写入提交元数据和待处理任务。MinIO 提供 S3 兼容的对象接口，适合保存原始文件。数据库只存 bucket 和 object key，所以查询提交记录不需要把文件内容读进数据库。本轮没有做性能对比，因此我不会声称它一定更快。

**代码位置**：`modules/submissions/models.py::SubmissionFile`；`infrastructure/storage/s3.py::S3ObjectStorage`。

**现场演示**：上传后查询 PostgreSQL `submission_files` 元数据，再调用 `GET /api/v1/submissions/{id}/file` 读回对象。对应测试：`test_submission_query_and_file_download_use_real_s3_compatible_storage`。

## 2. PostgreSQL、MinIO 和 MongoDB 分别存什么？

**简明技术说明（短句）**：当前 PostgreSQL 保存业务关系。当前 MinIO 保存原始文件。当前没有 MongoDB。

**面试口语回答**：目前 PostgreSQL 保存 Assignment、Submission、文件定位元数据和 ParsingTask。MinIO 保存 PDF 或图片的原始字节。MongoDB 还没有接入；如果后续确实需要保存结构变化较大的解析结果，可以先根据查询和版本需求评估是否使用 MongoDB。当前项目没有把 MongoDB 规划写成已实现功能，也没有双写逻辑。

**代码位置**：`modules/assignments/models.py`、`modules/submissions/models.py`、`infrastructure/storage/s3.py`。

**现场演示**：查看 Alembic 表定义和 `/api/v1/submissions/{id}` 响应，再从响应中确认 API 不返回文件二进制或公开 URL。

## 3. 为什么数据库只保存 object_key？

**简明技术说明（短句）**：Key 是对象定位符。文件字节保存在对象存储。用户文件名不参与 Key 生成。

**面试口语回答**：数据库记录 bucket 和 object key，应用根据这些信息从私有 MinIO 读取文件。这样提交记录与文件内容分开管理，元数据事务不会承载整份 PDF 或图片。Key 由作业、提交和文件 UUID 组成，避免用用户传入的文件名构造存储路径。原文件名只用于展示和下载响应头。

**代码位置**：`modules/submissions/service.py::create_submission`；`modules/submissions/models.py::SubmissionFile`；`modules/submissions/router.py::get_submission_file`。

**现场演示**：运行 `test_submission_object_key_is_server_generated_and_test_prefixed`，检查记录的文件名与 MinIO Key 各自承担的用途。

## 4. 文件上传成功但数据库写入失败怎么办？

**简明技术说明（短句）**：提交前失败时回滚并尝试删除对象。COMMIT 结果未知时保留对象，避免已提交记录丢失文件。

**面试口语回答**：当前流程先上传对象，再开启 PostgreSQL 写事务。提交、文件元数据和解析任务一起提交。若失败发生在发出 COMMIT 之前，服务回滚并尝试删除刚上传的唯一对象。发出 COMMIT 后，数据库可能已经提交但确认包丢失，因此服务保留对象，避免已提交的记录指向丢失文件。这个选择可能留下孤立对象；进程崩溃也会造成相同风险。未来需要对账清理。

**代码位置**：`modules/submissions/service.py::create_submission` 的显式 flush/commit 边界和异常补偿；`tests/integration/test_submissions_api.py::test_database_failure_rolls_back_and_compensates_uploaded_object`、`test_uncertain_commit_keeps_object_for_possible_committed_submission`。

**现场演示**：测试通过 PostgreSQL 触发器让 ParsingTask 插入失败，然后验证 API 没有提交记录且 MinIO 对象已删除。

## 5. 为什么 MinIO 和 PostgreSQL 无法直接共享事务？

**简明技术说明（短句）**：它们是两个独立服务。PostgreSQL 事务不包含 S3 请求。当前使用补偿，不是分布式原子提交。

**面试口语回答**：数据库事务管理器只控制 PostgreSQL 中的 SQL 操作，不能让 MinIO 的对象写入自动随 PostgreSQL 回滚。当前先上传对象，再在数据库事务中写记录。发 COMMIT 前失败会尽力删对象；COMMIT 确认丢失时保留对象，因为数据库可能已经提交。该流程不提供跨系统原子性，进程崩溃和网络故障仍需通过后续对账处理。当前没有引入分布式事务协议。

**代码位置**：`modules/submissions/service.py::create_submission`；`infrastructure/storage/s3.py`。

**现场演示**：检查上述数据库故障触发测试的对象清理，再在架构文档查看崩溃孤儿风险及未来对账策略。

## 6. 如何检查文件大小、格式和内容？

**简明技术说明（短句）**：检查扩展名和请求 MIME。再检查文件签名。读取时执行字节数限制。

**面试口语回答**：接口限制 PDF、JPEG 和 PNG。代码同时检查文件扩展名、multipart 声明的 MIME 和文件头签名，不能只信任客户端的 Content-Type。文件按 64 KiB 分块读取，超过配置的字节数就返回 413。文件名剥离路径部分并拒绝控制字符。SHA-256 在同一次分块读取中计算。

**代码位置**：`api/v1/upload_limits.py::UploadRequestLimitMiddleware`；`modules/submissions/uploads.py::stage_upload`、`_safe_filename`、`_signature_matches`；`core/config.py::Settings.max_upload_size_bytes`。

**现场演示**：运行 `test_upload_request_limit_rejects_stream_without_content_length`、`test_unsupported_extension_and_mismatched_signature_are_rejected` 和 `test_upload_enforces_configured_size_limit`。

## 7. 多个学生同时上传时有什么并发风险？

**简明技术说明（短句）**：并发请求可能同时通过预检查。数据库唯一约束决定唯一成功记录。失败请求删除自己的对象。

**面试口语回答**：两个请求可能都在上传前读到“没有提交”。因此不能只依靠应用层先查再写。数据库对 `(assignment_id, student_ref)` 建唯一约束，保证最终只有一个提交事务成功。两个请求有不同的服务端 Key；唯一冲突的事务回滚，并尝试删除自己上传的对象。对作业行的锁和第二次状态检查，也能避免草稿状态检查与发布竞争。

**代码位置**：`modules/submissions/models.py::Submission.__table_args__`；`modules/submissions/service.py::create_submission`；`main.py::database_error_handler`。

**现场演示**：运行 `test_concurrent_duplicate_submissions_keep_one_record_and_object`。测试用 `asyncio.Barrier` 同步请求，不用 sleep 推测时序。

## 8. 如何避免把大文件全部加载到内存？

**简明技术说明（短句）**：应用按块读取。临时文件最多保留 1 MiB 在内存。对象上传也使用文件对象。

**面试口语回答**：FastAPI 的 UploadFile 使用可溢写的临时文件。业务代码每次读取 64 KiB，更新大小和哈希，并写入另一个阈值为 1 MiB 的 SpooledTemporaryFile。超过阈值后临时内容落到磁盘。boto3 上传从文件对象读取，并在线程池中执行，因此不会先把整个文件转成一个 Python bytes 对象，也不会在事件循环上执行阻塞的 boto3 调用。

**代码位置**：`modules/submissions/uploads.py::stage_upload`；`infrastructure/storage/s3.py::S3ObjectStorage.upload_fileobj`。

**现场演示**：检查 `stage_upload` 的分块循环和临时文件初始化；可以将 `MAX_UPLOAD_SIZE_BYTES` 调小，运行大小限制测试。

## 9. SHA-256 在文件存储中有什么作用？

**简明技术说明（短句）**：摘要用于标识上传字节。读取和写入元数据时不用保存文件内容。

**面试口语回答**：服务在接收每个文件块时更新 SHA-256，并将摘要写入 PostgreSQL。它可以用于后续完整性核对、重复文件分析或存储对账。本轮只计算和保存摘要，没有实现自动定期校验，也不把 SHA-256 当作身份验证或访问授权机制。

**代码位置**：`modules/submissions/uploads.py::stage_upload`；`modules/submissions/models.py::SubmissionFile.sha256`。

**现场演示**：运行 `test_published_assignment_accepts_pdf_and_records_pending_task`，将响应摘要与测试数据的 Python `hashlib.sha256` 结果核对。

## 10. 后续 Celery 和 MinerU 如何读取原始文件？

**简明技术说明（短句）**：当前只登记 pending 任务。未来 Worker 可用提交 ID 查询对象 Key，再从私有 MinIO 读取。

**面试口语回答**：当前 API 在 PostgreSQL 中创建 `ParsingTask(pending)`，并没有发布 RabbitMQ 消息，也没有 Celery Worker。后续可以在事务提交后通过 Outbox 或可靠投递流程唤醒 Worker。Worker 使用 submission ID 查询 bucket、object key、内容类型和摘要，然后通过 S3 接口读取原文件，再交给 MinerU。失败状态、重试次数和任务投递目前都还没有实现。

**代码位置**：`modules/submissions/models.py::ParsingTask`；`modules/submissions/service.py::create_submission`；`infrastructure/storage/s3.py::S3ObjectStorage.download`。

**现场演示**：调用提交 API 后查看 `parsing_tasks.status = 'pending'`，并确认仓库没有 Celery、RabbitMQ 或 MinerU 运行代码。
