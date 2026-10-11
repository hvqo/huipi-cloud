# P3-A 面试复盘：可信评分输入与 Rubric 快照

本阶段只构建可追溯的评分输入，不调用评分模型，也不产生分数。所有示例来自当前代码和合成 PostgreSQL/MinIO 集成测试；合成测试不代表真实学生作业准确率。

## 1. 为什么评分前需要可信输入门禁？

**简明解释（ASD-STE100）**：评分系统必须知道它正在读取什么。服务检查作业、题目、标准答案、Rubric、文档来源和人工复核。检查失败时，服务不创建评分输入。

**底层原理**：后续评分若拿到错题、旧答案或印刷题干，就算模型本身正确，结果也没有可靠含义。门禁先固定身份、版本和证据边界。

**30 秒回答**：

我把评分输入准备做成独立门禁。它先验证作业已发布、Question 属于该作业、AnswerKey 和 Rubric 有效，再验证 Canonical、Alignment 和最新人工复核仍指向同一来源版本。只要缺一项就不会生成包。它不判断人工标注一定正确，也不调用评分模型。

**2 分钟回答**：

我把前置条件拆成业务完整性和证据完整性。业务完整性包括已发布作业、教师录入的标准答案，以及分值总和精确等于题目满分的 Rubric。证据完整性包括当前 Canonical 和 Alignment 的对象哈希、Question 集合摘要、最新复核 revision 和 `response_present` 决定。服务先读取对象并形成候选，再在 PostgreSQL 事务里锁定相关行并重新比较版本。这样可以避免“检查后来源刚好改变”的竞态。门禁只证明输入符合已实现规则，不证明 OCR、复核结论或最终评分正确。

**连续追问**：门禁失败会给零分吗？不会。它返回阻塞状态，不创建评分包，也不推断学生未作答。

**代码与演示**：[service.py 的 `_assess()`](../../src/huipi_cloud/modules/grading/service.py)；[repository.py 的 `_lock_and_validate_sources()`](../../src/huipi_cloud/modules/grading/repository.py)；集成测试 `test_synthetic_submission_review_to_grade_input_round_trip` 和 `test_non_present_human_decision_never_creates_a_grade_input`。

## 2. 为什么 `aligned` 不能直接用于评分？

**简明解释**：`aligned` 表示来源区域与某个 Question 的映射有足够证据。它不表示学生已作答。

**底层原理**：Canonical 中的文本可能是印刷题干，也可能包含其他题目、OCR 错字或不完整公式。题号关联和作答存在性是两个不同判断。

**30 秒回答**：

P2-D1 的 `aligned` 只回答“这段来源更可能对应哪道题”。它不回答“学生是否写了答案”。P3-A 需要最新人工作出 `response_present`，并明确选中 Canonical 区域。即使这样，也只是输入门禁通过，不是分数已产生。

**2 分钟回答**：

自动对齐器使用题号、顺序和 Canonical 结构做映射。学生答案存在性需要观察学生笔迹或其他作答证据。纯文本 OCR 可能把题干文本保留下来，却没有任何学生回应。若把 `aligned` 当作“可评分”，会把题干误当答案；若把 `not_observed` 当作“未作答”，又可能把没有识别到的内容误作零分。P3-A 因此保留 alignment 状态与人工 decision 的分离，只让最新且来源版本匹配的 `response_present` 进入纯文本提取。

**连续追问**：如果教师把题干误选为作答呢？本阶段不会验证教师判断的语义正确性；要靠复核流程、审计和后续真实样本质量控制。

**代码与演示**：[AnswerAlignment 协议描述](../../src/huipi_cloud/modules/answer_alignment/protocol.py)；[人工复核来源绑定](../../src/huipi_cloud/modules/answer_review/service.py)；集成测试 `test_unreviewed_input_is_not_prepared`。

## 3. 为什么 `response_present` 仍要重新校验来源？

**简明解释**：人工决定引用某个旧文档版本。生成输入前，服务必须确认决定仍对应当前 Canonical、Alignment 和 Question 集合。

**底层原理**：人工复核是追加修订记录。旧决定保留作审计，但新 revision 或新 Canonical 会使旧决定不再是当前证据。

**30 秒回答**：

`response_present` 是复核结论，不是永久授权。每条决定都绑定 Canonical ID/SHA、Alignment ID/SHA 和 Question digest。P3-A 读取每题最高 revision，调用已有 `is_current_for_bundle()`，并在写索引前再次用 PostgreSQL 行锁校验最新 revision。复核改变后，旧输入不能作为当前包读取。

**2 分钟回答**：

首先由 `load_verified_alignment_bundle()` 读取并校验对象大小、SHA 与协议。之后服务选择 Submission/Question 最新 review revision，检查它引用的 Canonical、Alignment 和 Question 集合是否与当前 bundle 完全一致。准备对象期间，另一个复核请求可能新增 revision；因此 repository 使用与复核模块相同的 Submission 锁，再核对最新 review ID、revision、request digest 和选中区域摘要。如果锁定后发现版本变化，就回滚并清理确定未登记的对象。数据库 commit 后，历史包仍保存，但读取函数会重新计算当前输入摘要并拒绝过期包。

**连续追问**：两个复核请求会覆盖彼此吗？不会；现有 AnswerReview 追加 revision，并在 Submission 行锁下递增。

**代码与演示**：[review_service.is_current_for_bundle](../../src/huipi_cloud/modules/answer_review/service.py)；[repository._lock_and_validate_sources](../../src/huipi_cloud/modules/grading/repository.py)；集成测试 `test_latest_review_revision_invalidates_old_package_for_current_consumption`。

## 4. 如何从 Canonical 恢复学生答案？

**简明解释**：服务用完整 `content_pointer` 找到 Canonical 节点，再用人工确认的 Unicode 半开区间切片该节点文本。

**底层原理**：偏移属于节点内字符串，不属于整个页面或整篇文档。完整 pointer 可以区分同一个 Block 下不同子节点的相同本地偏移。

**30 秒回答**：

我不使用 Alignment 的全文 `text_projection`。服务把 Canonical 树建立成 pointer 索引，再验证页码、Block ID、reading order、类型和素材引用都与复核记录一致。文本使用 Python `str[start:end]`，偏移是 Unicode 码点，终点不包含。输出保留每段 trace，跨页按 Canonical 顺序排序，不去重相同文字。

**2 分钟回答**：

每个人工确认区域保存完整 JSON Pointer、节点元数据和可选的 start/end。准备输入时，服务重新读取经过 SHA 校验的 Canonical JSON，从 `/pages/{page}/blocks/{block}/content` 开始沿 `/children/{index}` 定位具体节点。随后比较复核记录中的页码、Block UUID、reading order、source type、normalized type、BBox 和安全素材引用。如果有文本范围，服务要求节点是标量字符串、end 不越界、区间非空并且不是全空白。`start:end` 直接使用 Python Unicode 码点语义。多个相同字符串保留为不同 segment，因为来源指针不同，不能按文本去重。

**连续追问**：为什么不直接取 `text_projection`？它可能含题干、其他来源区域或未被人工确认的文本。

**代码与演示**：[service._extract_confirmed_regions](../../src/huipi_cloud/modules/grading/service.py)；测试 `test_nested_nodes_with_same_local_offsets_remain_distinct_and_keep_duplicate_text`、`test_text_offsets_use_unicode_codepoints_and_regions_keep_source_order_across_pages`。

## 5. 如何绑定 Question、AnswerKey、Rubric 和来源版本？

**简明解释**：输入 JSON 保存所有评分相关快照和来源 ID/SHA；PostgreSQL 保存它们的轻量索引。

**底层原理**：不可变对象记录历史内容，哈希将对象与索引及来源版本关联。下游重读时可以检测内容替换和来源过期。

**30 秒回答**：

包内记录 Submission、Assignment、Question 快照、原始文件 SHA、Canonical ID/SHA/schema、Alignment ID/SHA/aligner/Question digest、Review ID/revision、AnswerKey 快照和 Rubric Criterion 快照。AnswerKey 与 Rubric 即使现在通过 API 只允许发布前修改，也要复制到输入包，避免将来数据库变化影响历史评分输入。

**2 分钟回答**：

`GradeInputCore` 是语义输入。`input_sha256` 对规范化后的 Core JSON 计算，覆盖题目、答案、Rubric、人工来源片段和版本绑定，但不覆盖 artifact ID、创建时间或自身字段。最终 S3 字节再计算独立的 `object_sha256`。PostgreSQL 的 `GradeInputArtifact` 存 request ID、来源外键和摘要、对象 bucket/key、字节数等轻索引。读取时先确认该包仍匹配当前来源，再校验对象长度、对象 hash、Pydantic 合同和 DB 索引。

**连续追问**：为什么同时保存输入 hash 和对象 hash？前者用于语义幂等和版本比较；后者检测实际 JSON 字节损坏。

**代码与演示**：[GradeInputCore / GradeInputDocument](../../src/huipi_cloud/modules/grading/protocol.py)；[GradeInputArtifact](../../src/huipi_cloud/modules/grading/models.py)；集成测试下载对象并比较 `object_sha256`。

## 6. 为什么 Rubric 使用 `Decimal`？

**简明解释**：分值是十进制业务数值。用 Decimal 可以避免二进制浮点误差。

**底层原理**：许多十进制小数不能被 IEEE-754 二进制浮点精确表示。相加后可能出现很小误差，使总分比较错误。

**30 秒回答**：

题目满分和评分项都是两位小数，所以数据库使用 `NUMERIC(8,2)`，服务使用 Python `Decimal`。Rubric 总分从 `Decimal("0.00")` 开始累加，再与题目满分做精确相等比较。输入快照也保留 Decimal 值，JSON 中以十进制字符串序列化。

**2 分钟回答**：

自动评分还没实现，但评分输入必须先保证 Rubric 总分正确。若用 float 表示 0.1 或 0.2，二进制近似值相加可能不等于预期十进制结果。P1-A 的 SQL `Numeric(8,2)` 已把持久化精度固定为两位；P3-A 不转成 float，而是将数据库 Decimal 与 Question.max_score Decimal 比较。快照中还包括 criterion ID、描述、分值和顺序，确保以后能复现评分尺度。

**连续追问**：那最终 HTTP JSON 为什么显示字符串？JSON 没有 Decimal 原生类型；字符串可以保留精确十进制文本，下游应解析为 Decimal。

**代码与演示**：[RubricCriterion 模型](../../src/huipi_cloud/modules/assignments/models.py)；[service._assess](../../src/huipi_cloud/modules/grading/service.py)；测试 `test_synthetic_submission_review_to_grade_input_round_trip` 和 `test_invalid_rubric_total_blocks_input_even_if_human_review_is_present`。

## 7. 图片、公式和表格如何处理？

**简明解释**：只在 Canonical 提供可验证文字表示时保留文本。其他必要模态会阻止文本评分包生成。

**底层原理**：图像占位符不等于图像内容，表格 HTML 不等于验证过的单元格结构。把它们变成普通文本会制造虚假的可评分输入。

**30 秒回答**：

普通 text 节点可以按复核范围提取。公式只有在内容是纯文本或 LaTeX 且没有图片素材引用时，才作为独立 `formula_text` 片段保存。图片/几何、图片形式公式、表格和图表进入素材审计清单并返回 `unsupported_modality`。这样系统宁可停止，也不猜一个文本结果。

**2 分钟回答**：

Canonical 节点有 `normalized_type`、scalar/children 结构和安全的 asset refs。P3-A 只接受验证过的纯文本范围。公式文本可以保留类型，不和普通语句混为一段。若公式含图片引用或被 Canonical 标记为 inline image，就作为不支持模态。表格当前没有经验证的 cell model，所以即使有字符串，也不推断行列。纯图片节点保留 asset id/hash/content type 等安全引用，不把图片塞入普通字符串。任何 unsupported 区域都会阻止创建 `ready_text` 包。

**连续追问**：为什么能处理 LaTeX却不处理表格 Markdown？当前公式节点能够以原始文本区间和 source pointer追溯；表格结构尚未定义经过验证的 cell 坐标与合并语义。

**代码与演示**：[service._extract_confirmed_regions](../../src/huipi_cloud/modules/grading/service.py)；测试 `test_plain_formula_text_is_typed_but_image_formula_and_table_are_unsupported`、`test_pure_image_selection_is_audited_but_never_becomes_text`。

## 8. 幂等和未知 COMMIT 结果如何处理？

**简明解释**：同一请求使用唯一约束。COMMIT 结果不确定时保留对象，并用相同 request ID 重试。

**底层原理**：S3 和 PostgreSQL 没有共同事务。客户端可能在数据库已提交后失去确认，所以一次查询为空不能证明对象安全删除。

**30 秒回答**：

每次 S3 上传使用随机不可变 Key。服务上传后，在 PostgreSQL 事务中重验版本并按 `(submission_id, question_id, request_id)` 唯一键登记。明确的提交前回滚可以清理对象；若开始 COMMIT 后连接断开，Repository 返回 unknown outcome，Service 不删除对象。相同 request ID 重试会查询已提交索引，避免重复有效记录。孤立对象仍可能出现，当前没有对账清理器。

**2 分钟回答**：

流程先算候选输入并上传，再在短事务里锁 Submission、Assignment、Question 和来源索引。并发同请求只有一行能通过唯一约束。若事务在 COMMIT 前失败并确认回滚，当前对象没有被索引，可以尽力删除。若数据库 COMMIT 已发出但客户端没有收到确认，结果可能已经持久化；服务不能用空查询推断回滚，因此保留该不可变对象。重试时如果已有相同 request ID 和 input hash，读回旧包；若输入不同则冲突。这个方案是补偿策略，不是跨系统原子性或 Exactly Once；进程崩溃仍可能留下孤儿。

**连续追问**：怎样清理孤儿？后续需要保留期、PG 引用查询和 S3 枚举对账；本轮没有实现 GC，不能声称已解决。

**代码与演示**：[repository.register_success](../../src/huipi_cloud/modules/grading/repository.py)；[service.prepare_grade_input](../../src/huipi_cloud/modules/grading/service.py)；集成测试 `test_postgres_failure_before_commit_rolls_back_and_cleans_uploaded_object`、`test_unknown_commit_keeps_indexed_object_and_same_request_can_replay`、`test_concurrent_prepare_registers_one_index_and_keeps_one_s3_object`。

## 9. 为什么 VLM 建议不能触发评分？

**简明解释**：VLM 结果是机器建议，不是教师复核。它不能替代 `response_present` 人工决定。

**底层原理**：视觉模型可能把几何标记看成印刷内容，也可能把共享图形归到错误题目。模型协议正确不等于来源归属正确。

**30 秒回答**：

P2-D2B 的 VisualEvidence 和 P2-D2A 的 AnswerReview 使用不同的数据表和写入流程。P3-A 只读最新人工作答复核，不查询或导入 VLM proposal。即使模型输出协议通过，也不能直接成为评分证据。它必须经过人工确认，并绑定当前 Canonical 和 Alignment 版本。

**2 分钟回答**：

现有视觉模型返回候选结果和 bbox，但来源映射可能是 unresolved。空间重叠不等于题目归属，题目归属不等于学生作答存在。P2-D2B 明确不会修改 review 表。P3-A 又要求存在真实 AnswerReview revision，并只提取其 `response_regions`。如果只有 VisualEvidence 数据而没有人工决定，系统返回 review_required，不生成 GradeInput。

**连续追问**：将来能否用 VLM 自动做预复核？可以作为人工界面中的候选，但必须独立评估、保留不确定状态和审计，不能把建议静默升级成教师决定。

**代码与演示**：[VisualEvidence 模型](../../src/huipi_cloud/modules/visual_evidence/models.py)、[AnswerReview 模型](../../src/huipi_cloud/modules/answer_review/models.py)、[GradeInput `_assess`](../../src/huipi_cloud/modules/grading/service.py)；集成测试确认 review 记录数量不因 GradeInput 准备改变。

## 10. 如何保护学生作答隐私？

**简明解释**：完整评分输入只进私有对象存储。数据库索引、日志和 CLI inspection 不输出学生作答原文。

**底层原理**：作业答案、人工标签、标准答案和 Rubric 都可能识别学生或教学内容。哈希用于完整性，不会使内容匿名。

**30 秒回答**：

GradeInput 的 JSON 包含学生文本和标准答案，所以存入私有 S3。PostgreSQL 只保留 UUID、哈希、来源版本和对象索引。CLI inspection 只显示来源和摘要，不显示回答、完整答案、文件名或对象 Key。项目目前没有认证、RBAC 或租户隔离，所以 CLI 只允许受控本地使用。

**2 分钟回答**：

我把内容和索引分开：S3 对象存完整输入，PostgreSQL 存可验证的来源绑定和 Key。日志只记异常类型，不记作答文字或敏感配置。CLI 输出状态、revision、哈希和区域元数据，不显示 answer content。对象仍然是敏感数据；SHA-256 只是完整性校验，不是脱敏或匿名化。由于当前没有用户身份、授权和租户边界，不能把 API 或 CLI 部署到公网环境。正式使用前还需要存储访问控制、审计、留存和删除政策。

**连续追问**：哈希后的答案能否公开？不应默认公开。短文本可能被字典猜测，且相关 ID、范围信息仍可能识别个人。

**代码与演示**：[GradeInputInspection](../../src/huipi_cloud/modules/grading/protocol.py)、[CLI 输出](../../src/huipi_cloud/workers/prepare_grade_input.py)、[S3/PG 集成测试](../../tests/integration/test_grade_input.py)。
