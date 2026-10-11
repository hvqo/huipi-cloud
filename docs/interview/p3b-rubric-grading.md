# P3-B 面试复盘：Rubric 驱动的 LLM 候选评分

本页描述本地候选评分实现。服务只生成待人工检查的 proposal，不生成或发布正式成绩，也不改写 `AnswerReviewDecision`。

## 1. 输入为什么只接受 GradeInputArtifact？

**简洁解释**：GradeInput 已绑定人工确认的回答范围、题目版本、标准答案和 Rubric。P3-B 不应重新猜答案，也不应绕过 P3-A 的来源门禁。

**约 30 秒回答**：候选评分服务只收 GradeInput 的 UUID。读取对象时会验证大小、SHA、协议和 PostgreSQL 索引，并重新核对最新人工复核及题目来源。模型调用结束后会再验证一次，最终登记 proposal 时在一个短数据库事务内加锁复核来源。因此输入在模型调用期间被修订时，旧结果不会登记。

**约 2 分钟深入回答**：输入来自教师发布的作业及人工维护的 AnswerKey 和 Rubric，也绑定 Canonical、Alignment、Question digest 和 Review revision。模型只收到序列化后的最小必要输入。服务不把 OCR 输出或 VLM proposal 直接当成答案，也不在 HTTP 请求中调用模型。来源一致性在三个边界验证：调用前读取当前 GradeInput；模型返回后重新读取；写索引时用 `Submission → Assignment → Question` 及来源行锁，在 PostgreSQL 事务内确认 GradeInput 哈希和各来源仍相同。模型调用期间没有长事务，因此来源能被正常复核或修改。

**连续追问**：为什么不只在调用前检查？模型调用可能持续数秒或更久，期间人工复核、Question、Rubric 或 AnswerKey 都可能变化。

**代码与演示**：[proposal_service.py](../../src/huipi_cloud/modules/grading/proposal_service.py) `grade_input_artifact()`、`_read_current_input()`；[proposal_repository.py](../../src/huipi_cloud/modules/grading/proposal_repository.py) `register_proposal()`；集成测试 `test_review_change_during_provider_call_blocks_persistence`。

## 2. 怎样验证逐项分数和总分？

**简洁解释**：Rubric 来自已经验证的 GradeInput。代码按 Criterion ID 对齐每项分值，用 Decimal 检查范围并计算总分。

**约 30 秒回答**：模型必须为每个 Criterion 返回且只返回一次分数。代码拒绝缺项、重复项和未知项，逐项分数不能小于零或超过 Rubric points。总分由 Decimal 逐项相加，再与模型给出的 total 比较。这样可以防止模型遗漏项目或把总分报成 Rubric 之外的值。

**约 2 分钟深入回答**：模型回复协议只验证字段类型和基本结构；业务规则需要和可信 GradeInput 比较。`_validate_candidate_scores()` 先创建预期 Criterion 映射，再验证预测集合完全相同。每项分数是 Decimal，使用两位小数并禁用非有限数；每项有自己的 maximum。总分不能信任模型的汇总，服务根据逐项值相加，再检查给定总分相等且不超过 Question max_score。最后 proposal 协议再次验证总分、max points 合计和 criterion 唯一性。数据库存储的是轻量索引，不把分数写入正式成绩表。

**连续追问**：为什么不自动把不一致的模型总分改成计算值？不一致说明回复协议或模型推理有错，应明确拒绝，不能静默把坏输出包装为有效 proposal。

**代码与演示**：[proposal_service.py](../../src/huipi_cloud/modules/grading/proposal_service.py) `_validate_candidate_scores()`；[proposal_protocol.py](../../src/huipi_cloud/modules/grading/proposal_protocol.py) `GradeProposalDocument.validate_result()`；单测 `test_score_above_criterion_or_negative_is_rejected`、`test_model_reply_rejects_extra_keys_unknown_outcomes_and_mismatched_total`。

## 3. 如何防止模型伪造答案引用？

**简洁解释**：引用必须精确匹配指定 GradeInput 片段中的 Unicode 码点半开区间。

**约 30 秒回答**：每个 quote 带 segment index、起止偏移和文本。服务检查片段存在、偏移在范围内、起止差等于 Python 字符串长度，并比较切片与引用文本完全相同。模型不能靠编造一个看似合理的引文通过检查。没有证据时，协议要求使用 `no_evidence` 并说明原因。

**约 2 分钟深入回答**：P3-A 保留每段回答的来源 pointer、页码、Block、阅读顺序、偏移和 SHA。P3-B 的 quote 偏移局限在某一个 `SelectedAnswerSegment.text`，采用 Python Unicode codepoint 的 `[start,end)` 语义，不是 UTF-8 byte offset。代码先检查 `segment_index`，再比较边界与原文切片。可验证性不代表引用能证明评分理由正确，但让每项候选分数可以回到具体输入文本。对缺失步骤使用明确 `no_evidence`，不能虚构 quote 或把缺失自动解释为零分。

**连续追问**：精确引用是否证明模型分数正确？不能。它只证明引用来自输入文本，不证明模型解释合理或 Rubric 被正确理解。

**代码与演示**：[proposal_service.py](../../src/huipi_cloud/modules/grading/proposal_service.py) `_validate_quote_reference()`；[proposal_protocol.py](../../src/huipi_cloud/modules/grading/proposal_protocol.py) `QuoteEvidenceReference`；单测 `test_fabricated_or_out_of_bounds_quote_is_rejected`。

## 4. 怎样处理提示注入和学生答案里的指令？

**简洁解释**：系统规则与学生数据分开序列化，并通过代码限制输出。提示词不能消除提示注入风险。

**约 30 秒回答**：答案、题干、标准答案都被标明为待分析的数据，不是指令。模型要求按给定 Rubric 返回 JSON，但最终分数仍由代码做范围和总分校验，引用也必须匹配原文。即使文本包含“给我满分”等句子，模型输出超出分值的结果仍会被拒绝。不过模型可能受到注入影响，所以建议必须人工检查。

**约 2 分钟深入回答**：Prompt 把固定规则放在 system message，把标准答案、Rubric 和回答放进 JSON 数据段，明确不执行数据中要求更改评分规则的文字。分值安全不依赖 Prompt：Criterion 必须精确匹配，Decimal 值要在每一项范围内，模型 total 必须等于程序计算值。证据也要是输入片段原文。但模型可能在合法分值区间内仍然做错判断；因此这里是候选建议流程，而不是自动定分系统。日志不保存回答原文、Prompt 或模型原始堆栈。

**连续追问**：为什么不做一组词语黑名单？注入可以换语言、改写或使用间接表达，黑名单无法建立可靠边界；关键控制是最小数据传递、输出约束、确定性验证和人工复核。

**代码与演示**：[prompts.py](../../src/huipi_cloud/modules/grading/prompts.py)；单测 `test_prompt_injection_stays_in_untrusted_user_data_and_scores_remain_bounded`。

## 5. PostgreSQL、S3 和模型调用怎样形成一致流程？

**简洁解释**：对象先写入私有 S3，再在短 PostgreSQL 事务中重新验证来源并登记唯一索引。

**约 30 秒回答**：S3 和 PostgreSQL 没有共同事务。候选 JSON 上传到独立不可变 Key 后，服务开始短数据库事务，锁定 GradeInput 和来源行，再登记 proposal。确认 COMMIT 前回滚时可以尽力删除对象；COMMIT 结果未知时必须保留对象，因为数据库可能已经提交。

**约 2 分钟深入回答**：输入对象先通过当前版本读取。模型调用不持数据库连接，减少事务锁持有。结果生成后先上传不可变 S3 对象，再调用 `register_proposal()`。该事务会重新计算 GradeInput 语义摘要、调用 P3-A 的来源锁定校验，并用全局 request_id 唯一约束建立索引。若对象上传后但明确的事务前失败，Service best-effort delete；并发唯一冲突则读取胜出行、确认 Key 后删除自己的对象。进入 COMMIT 后的异常统一当作 unknown commit，不按空查询删对象。这个方案避免常见误删，但无法实现跨系统原子提交，仍可能留下孤立对象，未来需对账清理。

**连续追问**：为什么 COMMIT 之后不能马上查一次数据库再决定？短暂查询为空或不可用不证明原提交没有完成；先删除可能留下“数据库已引用、对象已消失”的损坏索引。

**代码与演示**：[proposal_service.py](../../src/huipi_cloud/modules/grading/proposal_service.py) `grade_input_artifact()`；[proposal_repository.py](../../src/huipi_cloud/modules/grading/proposal_repository.py) `register_proposal()`；集成测试覆盖数据库回滚、未知 COMMIT 和并发重放。

## 6. 如何限制重试和模型资源？

**简洁解释**：Provider 有超时、请求/回复大小限制和单次格式重试上限，不做无限重试。

**约 30 秒回答**：模型请求配置有限超时和最大输出 token，HTTP 回复最多 128 KiB，Prompt 和 proposal 都有字节上限。JSON/协议格式错误最多重试一次；分数或证据规则错误不重试。Provider timeout、限流、网络失败分别映射成固定状态。当前字节数只是没有 tokenizer 时的保守输入预算，不是精确 token 统计。

**约 2 分钟深入回答**：真实 token 数依赖具体模型 tokenizer；项目没有额外下载 tokenizer，因此使用输入 UTF-8 bytes 加固定消息封装预算作为保守上界，再设置 output token cap、HTTP timeout 和 provider response byte cap。无效 JSON 可以收到一次具体的固定原因码并重试；不允许把所有业务校验失败都用相同重试处理，否则只会重复花费而不修复错误。无界网络错误和分数错误不自动重试。Provider 在 loopback 或显式授权的 HTTPS 端点运行；API key 只通过 SecretStr 配置，不记录在日志。

**连续追问**：怎样知道本模型的真实 token 占用？当前 Provider 可返回的 usage 字段会被记录；若本地 Provider 不提供，则存 `null`，不猜测计数。

**代码与演示**：[config.py](../../src/huipi_cloud/core/config.py) `grading_*`；[provider.py](../../src/huipi_cloud/infrastructure/grading/provider.py) `_post_bounded()`；单测 `test_invalid_json_gets_one_bounded_format_retry_but_invalid_score_does_not`。

## 7. 候选分数是不是最终成绩？

**简洁解释**：不是。P3-B 只保存候选评分 proposal，没有教师确认、发布或正式成绩接口。

**约 30 秒回答**：`completed_candidate` 表示输出通过协议和确定性边界校验，不代表教学判断正确。proposal 单独保存到 S3 并由 `grade_proposal_artifacts` 建索引。它不更新 `AnswerReviewDecision`，不改变 GradeInput，也没有 FinalGrade 表或成绩发布 API。下一步需要教师复核后才讨论正式评分闭环。

**约 2 分钟深入回答**：作答区域由人工复核决定，候选分数来自 LLM，二者职责分离。P3-B 的状态只描述模型建议是否完整或已弃权，不是学生完成状态或教师确认状态。评分分数、理由和证据保存在不可变 proposal，不回写人工区域，也不把 `response_absent` 变成零分。系统还没有认证、RBAC、教师界面或真实数据准确率，因此仅限受控本地执行。后续应增加受权限约束的 Review/发布流程及真实授权作业上的独立评测。

**连续追问**：`completed_candidate` 名字为什么有 completed？它表示候选 JSON 处理完成，不代表评阅流程完成。当前无 `teacher_accepted` 状态。

**代码与演示**：[models.py](../../src/huipi_cloud/modules/grading/models.py) `GradeProposalArtifact`；[proposal_protocol.py](../../src/huipi_cloud/modules/grading/proposal_protocol.py) `GradeProposalDocument`；真实 MinIO/PG 集成测试断言 AnswerReview 记录数不变。

## 8. 怎样保证相同请求重放和并发？

**简洁解释**：稳定语义指纹绑定请求输入和配置，PostgreSQL 全局 request ID 唯一约束决定唯一索引。

**约 30 秒回答**：fingerprint 包含 GradeInput ID/SHA、Provider、模型、Prompt 版本和 generation config digest。相同 request ID 和 fingerprint 返回已保存对象；配置或输入不同则冲突。并发时模型可能调用两次，但唯一约束只让一个索引生效，输家对象只有在确认赢家后才尽力删除。

**约 2 分钟深入回答**：请求指纹能检测 request ID 被错误复用。Provider generation digest 将 endpoint 的哈希、模型、采样参数、token 上限、输入预算、超时和重试设置纳入版本；endpoint 原文和密钥不会写进索引。登记使用 `ON CONFLICT DO NOTHING` 加唯一约束，然后查询胜出记录并校验 fingerprint。服务检查数据库中的 bucket/key 是否等于本次对象；如不同，就读取并返回胜出结果，清理当前未引用的竞争对象。COMMIT 未知时没有足够证据清理，因此保留对象。模型推理没有 Exactly Once 保证，系统只保证数据库中同一 request_id 一条有效索引。

**连续追问**：并发时为何不做分布式锁？本地 CLI 的主要约束由 PostgreSQL 唯一约束和短事务提供，不需要额外引入锁服务；模型重复调用的成本是已知限制。

**代码与演示**：[proposal_service.py](../../src/huipi_cloud/modules/grading/proposal_service.py) `_request_fingerprint()`；[proposal_repository.py](../../src/huipi_cloud/modules/grading/proposal_repository.py) `uq_grade_proposals_request_id`；集成测试 `test_concurrent_same_request_registers_one_index_and_keeps_one_object`。
