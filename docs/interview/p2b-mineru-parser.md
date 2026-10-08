# P2-B：真实 MinerU 解析与产物持久化面试复盘

本文记录 `feat/p2b-mineru-parser` 当前实现和本机验收。它不表示题目切分、批改、MongoDB、Celery 或教师 Copilot 已实现。测试输入均为合成 PDF/PNG。提交和解析结果 API 没有认证，只适合本地或受控环境。

## 实际验证环境

| 项目 | 已验证内容 |
|---|---|
| 应用 | Python 3.12.13、uv 0.11.29、pypdf 6.19.0 |
| 解析器 | MinerU 4.0.10，Basic 档位，ONNX 小模型后端，CPU 执行 |
| 解析环境依赖 | ONNX Runtime 1.30.0、ModelScope 1.40.1；在独立 `/home/amber/.cache/huipi-cloud/mineru-4.0.10` venv，不进入应用 `uv.lock` |
| 模型来源 | ModelScope 下载后保存在独立 `MINERU_HOME`；解析子进程强制 `MINERU_MODEL_SOURCE=local`，不请求远端解析服务 |
| 样本 | 一页文字型 PDF、一页扫描型 PDF、PNG；内容为合成二次方程样例 |
| 完整链路 | 三种样本经 API、PostgreSQL、S3 兼容测试 bucket、独立 Worker 和实际 MinerU CLI；`uv run pytest -q -m mineru_e2e` 为 3 passed，耗时 31.52 秒（包含三个用例的服务和数据库测试开销） |
| CLI 解析观测 | 单独 CLI smoke：文字 PDF 10.03 秒 / 峰值 RSS 1,621,564 KiB；扫描 PDF 8.49 秒 / 1,657,612 KiB；PNG 8.86 秒 / 1,664,252 KiB。属于这三份合成样本的单次观察，不是性能基准或准确率结论 |
| GPU | 执行器设置 `CUDA_VISIBLE_DEVICES=""` 和 CPU ONNX 后端，MinerU 进程不使用 GPU。没有采集可归因到 MinerU 的 VRAM 峰值；桌面全局显存值不能作为解析进程数据 |

独立模型缓存和临时 PDF 不在 Git 仓库内。真实解析没有接入第三方云 API，也没有把 MinerU 深度学习依赖安装到应用虚拟环境。

## 1. MinerU 是什么？它与普通 OCR 有什么区别？

**ASD-STE100 简明解释**：OCR 把图片文字变成字符。MinerU 还分析文档布局，并输出 Markdown 和结构化 JSON。

**底层原理**：扫描页需要识别图像中的文字。文字型 PDF 可以直接包含字符层。MinerU 再用版面、表格、公式和图片处理，把解析内容按页面和区块组织起来。它可能在流水线内部使用 OCR；本项目接收 MinerU 输出，不自行实现另一套 OCR。

**30 秒口语回答**：普通 OCR 的主要结果是识别字符。MinerU 是面向文档的多模态解析流水线，会结合 PDF 文本层、图像 OCR 和版面分析，生成 Markdown、MiddleJson、结构化内容和图片资产。本轮用 MinerU 4.0.10 的 Basic/ONNX 档位真实解析了合成文字 PDF、扫描 PDF 和 PNG；我没有把普通 PDF 文本提取说成 MinerU 解析，也没有做题目切分或批改。

**2 分钟深入回答**：OCR 解决字符识别，但教学作业还需要知道文字来自哪一页、题目与公式如何排布、表格和图片在哪里。文档解析先根据 PDF 文本层或页面图像取得内容，再执行版面与内容类型分析，最后按页和 block 输出。P2-B 将 MinerU 4.x 作为独立进程运行，并校验版本、Basic 档位和 `docvortex.middle` 2.0 合同。应用保留 MinerU 的 Markdown、MiddleJson、StructuredContent、图片素材和原始 ZIP；它不把结果进一步转成题目实体，也不推断答案。OCR 识别质量必须在更广的真实、合规样本集上评测，本轮三份合成文件只能证明技术链路能工作。

**连续追问**：

- MinerU 一定比单独 OCR 准吗？不能这样保证，要按文档类型和质量评测。
- 现在支持哪些档位？只验证了 Basic/ONNX；其他档位未开放。
- 能否说已完成自动批改？不能，本轮只持久化解析输出。

**代码与验证**：`src/huipi_cloud/infrastructure/parsing/mineru.py` 的 `_run_mineru()`、`_validate_archive()`；`tests/integration/test_mineru_e2e.py::test_real_mineru_worker_persists_and_serves_parsed_outputs`。

## 2. 为什么 PDF 解析需要版面分析？

**ASD-STE100 简明解释**：PDF 页面不只是一串文字。版面分析帮助系统保留文字、公式、表格和图片的位置关系。

**底层原理**：PDF 内容可能以字符、绘制指令和图像分散存储。只按字符顺序拼接会丢失列顺序、阅读顺序、表格单元格和公式区域的关系。版面解析输出页面和 block 结构，供后续功能使用。

**30 秒口语回答**：教师作业里可能有分栏、题号、表格、公式和手写图片。纯文本提取只给字符，常会丢掉阅读顺序和区域类型。MinerU 输出的 MiddleJson 保留页和 block，本轮校验它的 schema 版本和 `page_idx`，但还没有把 block 归并为 Question。

**2 分钟深入回答**：PDF 的视觉位置与逻辑阅读顺序不是一回事。解析器要把字符或图像识别结果映射回页面区域，再根据区域边界和内容类型恢复阅读顺序。例如表格是二维关系，公式可能有上下标，分栏试卷也不能只按坐标排序。P2-B 将 MinerU 的原始 MiddleJson 和 Markdown 都保留，避免只保存一个有损的纯文本结果。当前结果检查只负责基础合同和图片完整性，不负责领域语义、题目边界或教师纠错。后续需要把原始页码、block 次序、坐标和识别来源纳入 Canonical Document 设计，再由单独阶段做题目结构化。

**连续追问**：

- 是否已经验证表格准确率？没有，本轮未做准确率评测。
- MiddleJson 的 page_idx 从几开始？保留 MinerU 原始的零基页码。
- 当前是否自动拆题？没有。

**代码与验证**：`src/huipi_cloud/infrastructure/parsing/mineru.py::_validate_archive()`；`tests/unit/test_mineru_contract.py::test_valid_middle_json_keeps_original_zero_based_page_indexes`。

## 3. 文字型 PDF 和扫描型 PDF 有什么区别？

**ASD-STE100 简明解释**：文字型 PDF 通常有可选择的文字。扫描 PDF 的页面主要是图像，需要 OCR。

**底层原理**：文字型 PDF 可从字符层提取文本，同时分析布局。扫描 PDF 必须先处理页面像素，再识别字符和布局。混合 PDF 可能同时有文字层和图像区域。

**30 秒口语回答**：文字型 PDF 往往包含文字对象，扫描 PDF 通常只有页面图像，所以前者可以读取字符层，后者要经过图像识别。两者的错误模式不一样，不能只用一个文本提取测试代表多模态能力。本轮分别用合成文字 PDF、扫描 PDF 和 PNG 走了真实 MinerU Worker 流程，并验证 Markdown 与样例内容匹配。

**2 分钟深入回答**：在文字型 PDF 中，解析器可以直接获得字符，但字符坐标、阅读顺序和表格关系仍要处理。扫描 PDF 没有可靠的字符层，解析器要从页面图像检测文字区域并 OCR；倾斜、低分辨率、笔迹、压缩伪影都会影响结果。图片上传则没有 PDF 页数和 PDF 字体层，需要作为图像页交给 MinerU。P2-B 在上传后验证文件签名，在 worker 下载后再次检查 SHA；PDF 会通过 pypdf 做损坏、加密和页数预检，之后才启动 MinerU。当前测试样本均为干净合成文件，不能代表真实学生作业的复杂度。

**连续追问**：

- 如果 PDF 有文字层但一部分是扫描图呢？由 MinerU 的文档流水线处理，本轮保留全量原始结果。
- 图片最大输入由什么控制？复用 `MAX_UPLOAD_SIZE_BYTES`；MinerU 结果另有独立资源上限。
- 测了手机拍摄的倾斜照片吗？没有。

**代码与验证**：`src/huipi_cloud/modules/submissions/service.py` 的上传签名校验；`src/huipi_cloud/infrastructure/parsing/child_guard.py::_validate_pdf()`；真实 E2E 三种参数化样例。

## 4. 为什么要识别公式、表格和图片？

**ASD-STE100 简明解释**：这些内容承载题目本身的信息。解析时要保留它们，不能只留下普通文字。

**底层原理**：公式包含符号和二维排版，表格有行列关系，图片可能承载题干或图表。内容会以结构化 block、Markdown 表达或图片资产形式输出。

**30 秒口语回答**：数学题的公式、表格和几何图可能就是题干的一部分。若预处理时静默丢弃，后面的题目理解和批改会得到不完整输入。当前实现保留 MinerU 的 MiddleJson、StructuredContent、Markdown 和图片资产，并检查本地图片引用是否都能在 ZIP 中找到；它不对这些内容做答案推理。

**2 分钟深入回答**：不同内容类型需要不同表示。公式可以是 LaTeX 或文本 block，表格要保留单元格顺序，图片和图表可能需要坐标及原始像素。P2-B 将完整 MinerU ZIP 作为归档，同时拆出 Markdown、两类 JSON 和 SHA 命名的图片对象，并保存一份映射原始路径到对象 Key 的 manifest。结果校验要求 Markdown/JSON 中的 `images/` 引用都在归档里存在，否则不允许报告解析成功。高质量公式与表格评测、图像视觉问答、题目对齐都不属于当前范围。

**连续追问**：

- 为什么保留 ZIP？便于回溯 MinerU 原始输出及定位拆分问题。
- 图片名冲突怎么办？每次 run 有独立前缀，素材 Key 使用内容 hash。
- 公式是否已转成业务实体？没有，只保存 MinerU 产物。

**代码与验证**：`src/huipi_cloud/infrastructure/parsing/mineru.py::_validate_archive()`、`_persist_result()`、`_image_references_exist()`；`tests/unit/test_mineru_contract.py::test_missing_referenced_image_asset_is_rejected` 与 `test_complete_image_asset_is_materialized_without_path_traversal`。

## 5. MinerU 为什么使用独立进程？

**ASD-STE100 简明解释**：解析可能很慢，也可能进入不可中断的原生代码。独立进程可以由 Worker 监控和结束。

**底层原理**：`asyncio` 不能强行中止正在运行的同步线程或 native call。操作系统进程可以用信号结束；新 session 让 MinerU 与后代进程共享可管理的进程组。

**30 秒口语回答**：MinerU 可能做大量 CPU 推理，也可能调用原生库。若在 FastAPI 或 Worker 事件循环里同步运行，会阻塞心跳和其他协程。当前使用 `asyncio.create_subprocess_exec()` 启动隔离工作目录中的 MinerU 进程组，不使用 shell。超时或取消时先发 TERM，再在宽限期后发 KILL，并回收进程。子进程隔离是生命周期控制，不是安全沙箱。

**2 分钟深入回答**：应用 Worker 使用 asyncio 负责数据库 lease 和心跳。如果将 CPU 密集型同步推理直接放在事件循环，事件循环无法按期执行 heartbeat，任务会失去租约；`asyncio.to_thread()` 也不能强制停止已开始的线程。MinerU 因此通过可监督的子进程调用，参数以 argv 传递，工作目录、HOME、TMP 指向每任务临时目录，环境变量不包含数据库或对象存储密钥。子进程设置独立 session，关闭时对整个进程组 TERM、有限等待、KILL 和 wait。Linux guard 使用父进程死亡信号处理 Worker 意外退出。如果进入 Worker 的 `os._exit` fail-stop，Python 不会运行临时目录清理，因此部署端还需清理遗留的 `huipi-mineru-*` 目录。该机制不提供 seccomp、容器或低权限 OS 身份；部署时还要使用专用用户并限制文件权限。

**连续追问**：

- `asyncio.cancel()` 会杀死 MinerU 吗？不会，执行器捕获取消并结束 OS 进程组。
- 有用 `shell=True` 吗？没有，传参数组给 create_subprocess_exec。
- 进程隔离是否阻止解析器读 Worker 用户可读文件？不完全阻止，需低权限运行身份。

**代码与验证**：`src/huipi_cloud/infrastructure/parsing/mineru.py::_run_mineru()`、`_terminate_process_group()`；`src/huipi_cloud/infrastructure/parsing/child_guard.py`；`tests/unit/test_mineru_process.py` 的父进程死亡和 TERM/KILL 测试。

## 6. Worker 如何控制解析超时？

**ASD-STE100 简明解释**：单次执行时限与 lease 时长分开配置。MinerU 超时时，执行器停止其进程组。

**底层原理**：Worker 用 monotonic 时钟测量执行时间。Parser 执行总时限、lease 续租周期和取消宽限分别解决不同问题。

**30 秒口语回答**：`PARSING_EXECUTION_TIMEOUT_SECONDS` 限制一次解析的总运行时间，lease 只用于任务所有权和故障恢复，不能用 lease 时长当解析超时。MinerU 执行器自己的超时 watcher 会终止进程组。只有确认子进程停止后，Worker 才把超时记为可重试失败；如果进程组无法回收，Worker 走致命退出路径，保留任务给 lease 恢复。

**2 分钟深入回答**：任务可能长于 lease，所以 Worker 通过 heartbeat 续租；lease 设置的是当前所有权截止时间。一次解析可以更长，也可以在总时限前续租很多次。`PARSING_EXECUTION_TIMEOUT_SECONDS` 使用 asyncio loop monotonic clock，避免系统时间调整影响时长测量。MinerU 还有子进程生命周期超时：超过时限发 TERM，在 `PARSING_CANCEL_GRACE_SECONDS` 后发 KILL，并等待子进程回收。若未回收，会抛出 WorkerFatalParsingError，不提交 succeeded。其他通用 Async Parser 收到取消仍依赖其协作，不能声称超时可强制终止任意线程。

**连续追问**：

- 超时是否一定能清除子进程？正常 Linux 进程组可发 KILL；若系统层无法回收会按故障退出处理。
- 超时后任务会怎样？确认执行停止后记录可重试失败，后续按退避策略重试。
- lease 为什么不能当超时？它是分布式所有权窗口，长任务可以续租。

**代码与验证**：`src/huipi_cloud/core/config.py` 的执行时限配置；`MinerUParserExecutor._run_mineru()`、`_terminate_process_group()`；`tests/unit/test_mineru_process.py::test_sigterm_then_sigkill_stops_uncooperative_parser_children` 和 P2-A `test_execution_timeout_cancels_cooperative_parser_and_records_retryable_failure`。

## 7. 模型推理阻塞时 Heartbeat 如何维持？

**ASD-STE100 简明解释**：MinerU 在单独进程中运行。Worker 的 asyncio 事件循环可以继续发送 Heartbeat。

**底层原理**：`create_subprocess_exec()` 异步启动子进程；Worker 等待进程退出和异步管道读取时会让出事件循环。心跳周期与模型进程的同步执行线程相互独立。

**30 秒口语回答**：如果把同步模型推理放进 Worker 事件循环，心跳就会被阻塞。现在 MinerU 在子进程运行，父 Worker 异步等进程、异步排空日志，并由自己的 lease loop 定时请求 PostgreSQL Heartbeat。这样即使 ONNX 推理是同步的，也不会堵住父 Worker 的 asyncio 循环。若数据库 heartbeat 报错或 token 失效，Worker 会取消执行器并结束子进程，不提交解析成功。

**2 分钟深入回答**：Repository 领取任务后结束短事务，Parser 执行不持有数据库行锁。`ParsingWorker._execute_claim()` 等待执行器任务，等待超时到 heartbeat 间隔时访问数据库更新租约。MinerU 执行器用 asyncio 子进程 API启动子进程，stdout/stderr 被后台 reader 持续排空，目录大小由另一个异步监控器检查。这些 await 让父事件循环保持调度。Heartbeat 只证明 Worker 仍有权执行，不代表解析在前进。数据库故障后，Worker 停止依赖未知 lease 的解析。真实 E2E 样本在当前都短于一个 lease 周期；持续续租行为由 P2-A 的真实 PostgreSQL集成测试覆盖，而不是声称实际样本验证过长时间持续推理。

**连续追问**：

- Heartbeat 返回有效就代表 MinerU 正常吗？不代表，只代表数据库仍认可 lease。
- 如果父事件循环被别的代码阻塞呢？heartbeat 仍可能逾期；不要在事件循环运行同步阻塞工作。
- 本轮真实推理跨越多个 heartbeat 周期了吗？合成样本没有；任务框架的跨周期行为由独立集成测试验证。

**代码与验证**：`src/huipi_cloud/workers/parsing.py::_execute_claim()`；`MinerUParserExecutor._run_mineru()`；`tests/integration/test_parsing_runtime.py::test_worker_renews_lease_across_multiple_heartbeats_and_blocks_second_claim`；真实解析验证见 P2-B E2E。

## 8. 为什么解析结果放 S3，索引放 PostgreSQL？

**ASD-STE100 简明解释**：对象存储保存大文件。PostgreSQL 保存任务、来源、状态和对象索引。

**底层原理**：对象存储适合以 Key 读写大对象。关系数据库适合外键、唯一约束和事务。两个服务没有共同事务，所以对象写入和数据库索引要分开处理。

**30 秒口语回答**：原始 PDF 和 MinerU 输出 ZIP、Markdown、JSON、图片都是文件，放私有 S3 兼容存储；PostgreSQL 保存来源 Submission、ParsingTask、parser/schema 版本、页数、Key、字节数和 SHA-256。成功索引与 task succeeded 在 PostgreSQL 的同一个 lease-fenced 事务里提交。S3 写入和 PostgreSQL 提交无法原子化，所以崩溃可能产生没有 DB 索引的对象。

**2 分钟深入回答**：如果把文件二进制存在关系表，会把业务表和大对象生命周期耦合，文件读取也会经过数据库连接池。对象存储用 bucket+Key 承载二进制；PostgreSQL 用 ParsedArtifact 与 Task/Submission 的外键和唯一约束提供可查询的权威索引。Worker 在任务独立目录解析并上传输出对象，上传后以 HEAD 校验大小；随后 Repository 在一个数据库事务里校验状态、lease token、未过期时间，写 artifact 行并更新 succeeded。对象上传已经完成但 DB 事务失败时，产物是孤儿；当前不删除结果，因为提交结果可能不确定且对象 Key 也可能已被有效事务引用。后续需要依据索引清理并设置保留窗口。

**连续追问**：

- API 会返回公开 S3 URL 吗？不会，bucket 私有；Markdown 由后端流式读取。
- 解析产物是不是都存 PostgreSQL？不是，数据库保存索引和摘要。
- 孤儿清理是否已实现？没有。

**代码与验证**：`src/huipi_cloud/modules/parsing/models.py::ParsedArtifact`；`src/huipi_cloud/modules/parsing/repository.py::complete_task_with_artifact()`；`src/huipi_cloud/infrastructure/parsing/mineru.py::_persist_result()`；E2E 对 S3 内容和 DB hash 做逐对象校验。

## 9. 为什么解析产物需要幂等写入？

**ASD-STE100 简明解释**：任务可能运行多次。数据库 token 可以拒绝旧状态更新，但不会撤销旧对象。

**底层原理**：任务执行是 At Least Once。Worker 可能在对象上传后、数据库成功提交前崩溃。下一轮会再次处理同一输入，因此需要定义重复运行产物的身份与保留策略。

**30 秒口语回答**：当前系统不能保证 exactly once。每次 MinerU 执行都生成 artifact UUID 和新的不可变 run 前缀，两个不同 lease 不会写同一个可变结果路径；数据库只接收当前有效 token 的完成。这个设计防止旧运行覆盖新对象，但不等于去重：旧 run 可能变成孤儿。稳定输入版本键、最终结果挑选和孤儿清理还没有实现。

**2 分钟深入回答**：At Least Once 的恢复逻辑重跑过期任务是必要的，但外部对象存储不参与任务表事务。若 worker 在上传对象后丢失租约或数据库 COMMIT 回应不确定，系统无法仅通过 task 状态推断该对象是否被有效引用。因此不能用共享固定 Key 覆盖旧结果，也不能在提交结果不确定时贸然删除。当前使用每次运行唯一的 artifact UUID，索引唯一约束限制一个 task/submission 只有一个当前有效记录，lease token 让旧 worker无法再插入/完成。它能隔离写入，不能回收冗余版本。后续需选择由输入 SHA、MinerU 版本、档位和 schema 组成的稳定解析版本标识，决定重试重用、版本升级和原子发布策略，并做带保留窗口的对象对账。

**连续追问**：

- 唯一有效索引等于只有一个对象吗？不是，S3 可能有多个未引用 run。
- 为什么不直接复用 submission ID 当 Key？旧 Worker 会覆盖当前运行对象。
- COMMIT 超时就删除对象吗？不能，事务可能已经提交成功。

**代码与验证**：`src/huipi_cloud/infrastructure/parsing/mineru.py::_persist_result()` 的 run UUID；`src/huipi_cloud/modules/parsing/repository.py::complete_task_with_artifact()` 的 token 条件和唯一索引；`tests/integration/test_parsing_runtime.py::test_expired_lease_is_reclaimed_with_new_token_and_old_worker_is_fenced`、`test_parsed_artifact_insert_failure_rolls_back_success_transition`。

## 10. 解析失败如何重试，如何区分永久性错误？

**ASD-STE100 简明解释**：临时服务错误可以重试。输入损坏或结果合同无效不会因为重复运行自动修好。

**底层原理**：执行器把错误映射为固定错误码和 retryable 标志。Worker 按 attempt count、max attempts 和指数退避更新任务状态；原始异常详情不返回给客户端。

**30 秒口语回答**：MinIO 连接问题、MinerU CLI 短暂失败或单次执行超时会作为可重试失败，进入 `retry_wait`，按数据库保存的 due time 再领取。输入损坏、文件哈希不符、页数超限、缺少必要输出或 MiddleJson schema 不匹配会标成永久失败。错误表述是固定码，不把存储路径、凭据或原始 traceback 放进 API。达到 max attempts 后任务进入 failed。

**2 分钟深入回答**：失败类型要根据能否通过重试恢复区分。原始 S3 服务暂时不可用、进程启动问题和受控超时可能由临时资源或系统恢复，因此执行器抛 RetryableParsingError。PDF 签名已被上传层检查，但 worker仍复核大小和 SHA；哈希不符、pypdf 无法读取/加密、超过页数，以及 MinerU 返回不完整 ZIP/错误 schema 都是确定输入或合同问题，抛 PermanentParsingError。错误分类器把类型转换为短固定的 FailureSummary。Repository 的条件 UPDATE 检查 token 与数据库 lease，并根据 retryable 和剩余次数写 retry_wait/failed。当前没有 DLQ、人工重放 UI 或错误原因分析平台。

**连续追问**：

- `mineru-kit` 返回一般非零退出码会怎样？目前归为短暂 parser_unavailable；确定的 PDF 预检码映射为永久错误。
- HTTP 错误是否会包含 stderr？不会。
- 重试次数在哪里保存？ParsingTask.max_attempts，创建时即固定。

**代码与验证**：`src/huipi_cloud/modules/parsing/errors.py`；`MinerUParserExecutor._run_mineru()` 和 `_validate_archive()`；`src/huipi_cloud/modules/parsing/repository.py::record_failure()`；`tests/integration/test_parsing_runtime.py::test_permanent_parser_error_is_recorded_as_failed`、`test_retryable_failure_waits_until_persisted_due_time`；`tests/unit/test_mineru_contract.py`。

## 11. 如何保证解析结果能追溯到原 PDF？

**ASD-STE100 简明解释**：索引保存 Submission、ParsingTask、原始文件 SHA 和解析器版本。这样可以确认输出来自哪次输入和解析运行。

**底层原理**：数据库外键关联任务和提交。原始 SHA 用于检测下载内容变化。解析版本、档位、schema 版本和唯一 artifact UUID 描述运行来源。

**30 秒口语回答**：ParsedArtifact 通过外键关联 ParsingTask 和 Submission，并保存原始文件 SHA-256、MinerU 名称和版本、档位、MiddleJson schema 版本、产物 Key、hash、大小和页数。worker 下载原始对象时重新算 hash，确认和 SubmissionFile 元数据一致。每次运行有自己的 artifact UUID，API 只返回安全摘要，不公开私有 Key。

**2 分钟深入回答**：追溯需要既知道“来源对象是什么”，也知道“哪个解析器怎样处理”。SubmissionFile 保存原始对象位置、类型、大小和 hash；ParsingTask 把异步处理过程持久化；ParsedArtifact 的外键把成功解析绑定回 task 和 submission，同时记录原始 SHA、MinerU 版本、Basic 档位和 schema version。每个解析产物有单独的对象 SHA 和字节数，E2E 测试从 S3 实际下载 ZIP、Markdown、JSON、manifest 及素材再比对摘要。当前表没有保存操作人、代码镜像 digest、完整运行配置或解析置信度；未来如需要审计级可复现，需要增加经过评审的版本字段与保留策略。

**连续追问**：

- SHA 能证明文件由谁上传吗？不能，它只支持内容一致性校验。
- 如何区分 MinerU 版本升级？索引保存 parser_version，且每次运行有独立 Key。
- 原始文件会被结果覆盖吗？不会，解析产物使用独立的 `parsed/` 前缀。

**代码与验证**：`src/huipi_cloud/modules/submissions/models.py::SubmissionFile`；`src/huipi_cloud/modules/parsing/models.py::ParsedArtifact`；`MinerUParserExecutor._download_original()`；`tests/integration/test_mineru_e2e.py` 的原始 hash 和产物验证。

## 12. 后续 `Document → Page → Block` 如何设计？

**ASD-STE100 简明解释**：Document 表示一次解析文档。Page 表示文档页。Block 表示页内的文字、公式、表格或图片区域。

**底层原理**：解析输出可能很大且嵌套。分层实体可按页读取，并保存原始零基页码、顺序、坐标、类型、内容和源 parser block ID。它应由未来阶段定义，不能把当前 ZIP 索引误称为规范化文档模型。

**30 秒口语回答**：我会先保留 MinerU 原始 MiddleJson，然后在后续阶段定义 Canonical Document、Page、Block。Document 关联来源 Submission 和 parser 版本；Page 保存原始零基页码和页尺寸；Block 保存页内顺序、类型、坐标、文本或结构化 payload，以及来源 block ID。当前 P2-B 没有建这些实体，也没有做题目对齐，所以不能把 ParsedArtifact 说成 Document 模型。

**2 分钟深入回答**：原始 MinerU 结构是供应商输出合同，直接作为业务表会让查询和模型升级都耦合 parser schema。未来可以以 ParsedArtifact 作为一轮原始输出的版本锚点，再生成 Canonical Document。Document 保存 source file hash、parser/schema 版本、状态；Page 用 document_id 与原始 `page_idx` 唯一关联，并保存宽高/方向；Block 用 page_id、原始顺序和来源 block id 维持顺序与追溯，type 区分 text、formula、table、image、unknown。坐标系要注明页尺寸和归一化规则。大型 block payload 可单独放对象存储或 JSON；先做真实样本评估，再决定是否引入 MongoDB。需保留 unknown block，避免静默丢内容。P2-B 当前只验证并存储原始产物，没有实施这些实体和字段。

**连续追问**：

- 为什么保留零基页码？它来自 MinerU 原始索引，UI 层可显示加一后的页号。
- 是否每个 block 都进关系表？要按查询负载和数据量评估，当前没有决策。
- 题目关联由谁负责？后续题目结构化阶段，不属于 P2-B。

**代码与验证**：当前来源见 `src/huipi_cloud/modules/parsing/models.py::ParsedArtifact` 和 `MinerUParserExecutor._validate_archive()`；`tests/unit/test_mineru_contract.py::test_valid_middle_json_keeps_original_zero_based_page_indexes`。没有 Document/Page/Block 代码可演示。
