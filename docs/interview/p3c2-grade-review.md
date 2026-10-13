# P3-C2：候选评分人工复核与修订记录

## 1. 为什么复核记录只追加，不覆盖 GradeProposal？

**简洁解释：** GradeProposal 是模型生成的原始候选。GradeReview 是后续人工判断。两种事实有不同来源，所以分别保存。

**30 秒回答：** 我把模型候选和人工决定分开保存。人工可以接受、修订、拒绝或要求重新评分，但系统不会改写原 Proposal。每次决定会产生一个新的 revision，并指向前一个 revision。这样可以重建当时模型给了什么、人工改了什么，也能分析之后的评分分歧。

**2 分钟回答：** Proposal 是一个不可变模型产物，包含评分输入绑定、逐项分数、理由、引用和模型版本。若人工更新这条记录，原始模型行为就会丢失，也无法分辨模型输出与人工更正。P3-C2 用 `grade_review_revisions` 保存单独的审计事件，并将 Proposal 与 GradeInput 的 ID 和 SHA 写入每次 revision。数据库唯一键约束 `(proposal_id, review_revision)`、`(proposal_id, request_id)` 和 `supersedes_review_id`，触发器拒绝 UPDATE 和 DELETE。事务锁定 Proposal 行后再分配 revision，因此并发请求不会各自取得同一个版本号。

**代码与验证：** `grade_review_models.py`、`grade_review_repository.append_review()`、迁移 `de9f2b647a13`；`tests/integration/test_grade_review.py::test_pg_minio_accept_revise_history_keeps_sources_immutable_and_cli_redacted` 检查数据库拒绝 UPDATE/DELETE，且 Proposal 与 GradeInput 对象字节不变。

**连续追问：** 如果 COMMIT 成功但客户端断开怎么办？用相同 request ID 重试。服务先查已有记录；相同摘要返回原 revision，不同摘要返回幂等冲突。

## 2. 四种决定分别表示什么？

**简洁解释：** `accepted` 和 `revised` 有复核分数。`rejected` 和 `regrading_required` 没有复核分数。

**30 秒回答：** 接受表示逐项保留模型候选；修订表示人工调整 Rubric 分数；拒绝表示不能采纳当前候选；需要重新评分表示还缺证据或需要之后重新运行流程。后两种状态不生成 reviewed score。四种状态都只是复核记录，不会发布正式成绩。

**2 分钟回答：** 对 `accepted`，请求里不能提交分数，服务从 Proposal 复制原分与证据，并确认当前来源仍有效。对 `revised`，Criterion 集合必须与当前 Rubric 完全一致且不重复；分数用 Decimal，服务检查上下限并计算总分。变化的分数需要理由与来源引用。`rejected` 和 `regrading_required` 需要原因码，但不保存 reviewed criterion score。对于模型弃权，服务禁止 accepted/revised，只允许拒绝或要求之后重评。`regrading_required` 本身不会触发模型调用或创建新 Proposal。

**代码与验证：** `GradeReviewDecisionCreate.validate_status_shape()`、`grade_review_service._build_review_criteria()`；集成测试验证 accepted、revised、弃权和无需模型调用的 regrading 行为。

**连续追问：** accepted 是否等同教师正式确认成绩？不是。当前没有正式成绩发布表或发布流程，也没有认证身份。

## 3. 如何保证分数、Rubric 和证据一致？

**简洁解释：** 服务根据当前 GradeInput 快照校验 Criterion、Decimal 分数和原文引用。

**30 秒回答：** 复核写入前，服务重新读取 Proposal 和 GradeInput，并确认它们与数据库索引一致。每个 Rubric Criterion 必须恰好有一条复核分数，分数不能超过该项上限。总分由服务计算。变化项必须有理由和当前 GradeInput 的引用；Quote 的内容、segment 和 Unicode 码点半开区间都要匹配原文。这个检查只证明引用可追溯，不证明教学判断正确。

**2 分钟回答：** `read_grade_proposal_artifact()` 和 `read_grade_input_artifact()` 分别校验 S3 对象 SHA、PostgreSQL 索引及严格协议。服务比较 Proposal 中的 Criterion ID 和 max points 与 GradeInput 的 Rubric 快照。接受路径不能改分；修订路径要求 Criterion 集合完全相同，使用 Decimal 计算各项与总分，并调用 P3-B 的 Quote 校验器重新核对文本和偏移。服务还会在事务中锁定并再次验证 Assignment、Question、AnswerKey、Rubric、Canonical、Alignment 和最新 AnswerReview，防止人工查看后来源发生变化。即使 Quote 正确，分数和理由仍需人工判断。

**代码与验证：** `grade_review_service._validate_proposal_binding()`、`_build_review_criteria()`、`_validate_score_evidence()` 和 `grade_review_repository.append_review()`；集成测试覆盖超分、伪造 Quote、越界范围和来源在预检后变化。

**连续追问：** 为什么要求正分引用？它是可确定性检查，要求每个正分至少能指向真实学生文本。它不能证明该文本足以支持分数。

## 4. 如何处理来源过期？

**简洁解释：** 复核绑定不可变 Proposal 和 GradeInput；任一当前来源变化都会阻止创建新 revision。

**30 秒回答：** 记录复核时，我校验 Proposal、GradeInput 以及当前 Canonical、Alignment、Question、AnswerKey、Rubric 和最新 AnswerReview。如果这些内容与评分时的快照不同，服务返回 `source_stale`，不把旧分数迁移到新来源。历史 revision 仍可查看，但它对应旧版本。

**2 分钟回答：** Proposal 保存 GradeInput ID/SHA，GradeInput 又绑定源文件、Canonical、Alignment、Question digest、AnswerReview revision、AnswerKey 和 Rubric。服务先验证 S3 对象与索引，再在 PostgreSQL 写事务中锁定来源并调用已有的 `lock_and_validate_sources()`。所以如果审核期间题目、Rubric、AnswerKey 或人工作答决定发生变化，事务内检查仍会阻止旧结果成为当前记录。回放一个已经成功提交的相同 request ID 是例外：服务先按请求摘要返回原记录，以支持 COMMIT 确认丢失后的幂等恢复。

**代码与验证：** `grade_review_service.record_grade_review()`、`grade_review_repository.append_review()`；集成测试 `test_changed_sources_reject_new_review_without_creating_history` 和 `test_source_change_after_cli_read_but_before_append_is_rejected`。

**连续追问：** 为什么保留旧记录？它是历史审计事实，不再代表当前来源的有效结论。CLI 会标出历史来源状态。

## 5. 并发 revision 与 request 幂等如何工作？

**简洁解释：** 锁住 Proposal 行以串行分配 revision，并用唯一约束做最后一道保护。

**30 秒回答：** `append_review()` 在同一个 PostgreSQL 事务里对 Proposal 行执行 `FOR UPDATE`，再检查 request、验证来源、读取最新 revision 并插入下一版。第二个并发请求等第一个提交后重新读取状态；如果它提供的 expected revision 已过期，就得到业务冲突。唯一约束可以捕捉任何未预期的重复写入。

**2 分钟回答：** revision 不是按时间戳排序，而是事务锁和数据库整数序号。相同 Proposal 上的事务争用同一索引行锁。事务拿到锁后再次查 request_id，接着核对当前来源和 expected revision，再写 `latest + 1`，并通过 supersedes 指向前一条记录。若同 request ID 已存在且摘要相同，服务返回已存在记录；同 ID 不同摘要返回冲突。显式 COMMIT 前的错误回滚事务；COMMIT 调用后若确认不明，返回未知结果，客户端需用原 request ID 重试。

**代码与验证：** `grade_review_repository.append_review()`；`tests/integration/test_grade_review.py` 覆盖两路并发、expected revision 冲突、相同请求重放、不同载荷冲突和模拟丢失 COMMIT 确认后的恢复。

**连续追问：** 为什么不需要分布式锁？参与写入的状态都在同一个 PostgreSQL 数据库，单行事务锁和约束已能保证顺序。

## 6. 为什么 reviewer_ref 不能作为身份认证？

**简洁解释：** `reviewer_ref` 是请求中的普通字符串，没有登录、签名或授权验证。

**30 秒回答：** 当前 CLI 让操作者填写 `reviewer_ref`，用于记录一个自声明标签。系统没有身份认证、RBAC 或租户隔离，因此不能证明填写者是谁，也不能把这条记录当作经过授权的教师决定。教师产品上线前需要真正的身份和权限系统。

**2 分钟回答：** 数据库保存 `reviewer_ref` 是为了让受控开发流程中的记录有一个简单的操作者标签。协议仅校验长度和字符范围，不会验证账户、机构或权限。CLI 也不是 HTTP 接口，但本机文件、数据库和对象存储仍必须受控。后续接入教师工作流时，要把审计主体绑定到已验证账户、做题目级授权并定义敏感材料的读取审计和留存周期。旧自声明值不能追溯提升为认证身份。

**代码与验证：** `GradeReviewDecisionCreate.reviewer_ref`、`review_grade_proposal.py`；集成测试确认 inspect 输出 `reviewer_identity_authenticated=false`。README 和开发指南明确说明只适用于受控环境。

**连续追问：** 是否应该新增一个 HTTP 写 API？本阶段不应。没有认证和授权时，该接口会扩大匿名写入和伪造审计身份风险。

## 7. 人工复核如何用于未来评分评测？

**简洁解释：** 它保存候选与人工修订的差异，但单次人工记录不是高可信真值。

**30 秒回答：** 每条记录保留模型逐项分数、复核分数、差值、原因、来源版本和时间，所以可以在未来分析常见错误。不过当前 `reviewer_ref` 没有认证，复核也没有独立双评和仲裁，因此不能直接把一条修订当成标准答案。建立真实评测集前要先有许可、去标识化、双人评分和分歧仲裁。

**2 分钟回答：** P3-C1 合成参考标签与模型预测分开存储。P3-C2 人工复核可以产生一个新的反馈来源，但它与合成标签不同，也没有自动写回评测基线。未来可以按 Rubric、题型、模型和 Prompt 版本比较 proposed 与 reviewed score 的逐项差异，并分析拒绝原因、重评比例和人工一致性。只有经过授权且身份受控的独立教师评分，再经过仲裁，才能形成更可信的教学参考标签。小样本或单次准确吻合都不能说明真实作业质量。

**代码与验证：** `GradeReviewRevisionDocument`、`GradeReviewRevision` 与 `grade_review_history()`；集成测试检查候选分数、修订分数和历史数据并存，且 P3-C1 文件未被修改。

**连续追问：** 当前会不会自动把复核结果写入训练或评测集？不会。没有这样的数据管道，也不会改写 P3-C1 标签。

## 8. 为什么复核记录不等于成绩发布？

**简洁解释：** 当前记录是候选的人工处理结果，没有正式成绩模型、发布接口或受认证教师流程。

**30 秒回答：** `accepted` 表示操作者认为候选可以进入后续待发布流程，`revised` 表示操作者调整了分数。系统当前没有正式成绩表和发布动作，CLI 也会明确显示 `grade_published=false`。所以这不是学生可见的最终成绩。

**2 分钟回答：** 评分候选、人工复核和成绩发布是不同业务状态。P3-C2 只持久化人工对模型 Proposal 的评价，并记录来源快照和修订链。它不改原始模型输出，不更新作答复核，也不向学生或教师客户端发布分数。未来成绩发布还需要认证身份、权限、班级与学生归属、成绩更正策略、审计和通知边界。本阶段不能把 `accepted` 当成这些流程的替代品。

**代码与验证：** `GradeReviewInspection`、`GradeReviewRecordSummary` 明确输出 `grade_published=false`；迁移只新增 `grade_review_revisions`。集成测试验证 Proposal、GradeInput 和 AnswerReview 决定保持不变。

**连续追问：** 将来是否能复用这张表作为最终成绩？不应直接复用。未来成绩发布需要单独的正式成绩实体和发布审计。
