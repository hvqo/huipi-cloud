# 考试 AI 候选评分与教师复核

本切片在已成功的 `ExamParsingJob` 上提供浏览器工作区。教师确认作答、模型候选评分和人工审核是三个独立事实；任何一个都不是正式成绩发布。

## 复用与新增边界

- 自动对齐直接使用 `align_canonical_document`，只读取所选 PaperVersion 的 PaperQuestionMapping 和不可变题目载体。
- 作答选择复用 AnswerReview 1.0、Canonical 节点解析、父子来源冲突检查和 Unicode 码点半开区间。自动 aligned 不是学生确实作答的证明。
- GradeInput 1.0、文本模态门禁、教师答案及完整 Rubric 快照不变。将原 `_assess` 的纯准备部分提取为 `assess_verified_sources`；旧作业入口仍先执行原有查询和权限校验。
- 复用 GradeProposal 1.0、原 Provider、版本化 Prompt、Decimal、逐项上限、总分与精确 Quote 校验；未启用实验 Quote Selection。
- 教师审核复用 GradeReview 1.0 和原完整评分比较函数，不改变 accepted/revised/rejected/regrading_required 的含义。
- 新增考试任务绑定的 Service、HTTP、固定授权动作和最小评分队列。没有通用特权 Session、客户端来源覆盖或 legacy 降级。

## 数据与事务

迁移 `b14c52b3f014` 新增：

1. `ExamGradingWorkspace`：绑定真实解析 Job、PaperQuestionMapping、快照 Question 和现有 AnswerAlignmentArtifact；同一 Job/Mapping 唯一，数据库触发器核对完整关系并禁止改写。
2. `ExamScoringJob`：绑定工作区、精确 AnswerReviewDecision、GradeInputArtifact 和可信申请人。每份作答复核最多一个任务，租约只用于此任务；成功结果指向真实 GradeProposalArtifact。
3. `ExamGradingOperation`：追加审计和同操作者 request_id 幂等收据，不写完整作答或 Token。

AnswerAlignment、AnswerReview、GradeInput、GradeProposal、GradeReview 仍写入原来的产物/审核表。新表和原表在旧 ORM 视图中仍被隐藏。只有已认证教师进入具体 Job/工作区后，固定查询才可读取对应上下文。

写入沿既有考试服务锁序，先校验当前账号、完整单 Grant、Session 和来源，再进行复核版本检查。模型调用与 S3 I/O 不占用长数据库事务；最终提交重新取得来源锁。审核、候选索引、任务成功和对应审计分别在各自同一事务内完成。

S3 对象使用独立不可变身份并回读大小/SHA。数据库 COMMIT 结果不确定时保留对象，按原 request_id/当前任务状态恢复；不根据一次空查询删除可能已登记的对象。可能留下的未索引对象需后续对账，本切片不实现垃圾回收。

降级在存在新工作区、任务、审计或相关授权时拒绝；不是带业务事实的生产回滚方案。只在独立空测试库验证迁移往返。

## 权限和部署

固定动作：`exam.mark.read`、`exam.answer.review`、`exam.score.request`、`exam.score.review`。范围仅真实 installation、exam_event、exam_session。

具体操作要求同一个 Grant 覆盖 mark.read、本次动作、script.read、identity.verify；另核验同场次 roster.read。只拥有扫描/解析权限不会自动增加评分能力。JWT role、旧 is_admin 和 AssignmentMembership 不参与考试授权。读取还检查原解析申请人的当前账号和解析意图；其撤权会使后续评分失效。

迁移提供固定 `exam_mark_operator`/`exam_mark_deployer` 角色（代码中的固定 UUID）。操作员角色包含依赖的考试解析权限，授权必须绑定适当的真实 Scope；安装级部署根应限受控运维使用。

受控部署入口（数据库运维凭据和现有安全委派权，不是公共自助接口）：

```bash
uv run python -m huipi_cloud.modules.exam_grading enable-operator \
  --actor-user-id "$DEPLOY_ACTOR" --target-user-id "$TARGET_USER" \
  --request-id "$REQUEST_ID" --approval-ref "$APPROVAL_REF" \
  --confirm-deployment-operation
```

普通教师授权沿既有 resource_security 的受控 Grant 流程，不能用任意 actor UUID 模拟认证。Worker 只消费数据库中已有任务，HTTP 没有 Worker 控制接口。

Worker 使用现有 GRADING 配置，不修改 `.env`，不自动回退模型：

```bash
uv run python -m huipi_cloud.workers.exam_scoring_worker
```

`GRADING_ENABLED`、Provider、模型、Token、超时与远程传输授权按[评分指南](grade-proposals.md)明确配置。真实验收使用已缓存本地 Ollama；默认 CI 没有真实模型请求。

## HTTP 合同

所有入口要求现有真实 OIDC/JWT 认证和数据库当前 Grant：

- `POST /api/v1/exam-parsing-jobs/{job_id}/grading-workspace`：`request_id`，准备受保护对齐和各题工作区。
- `GET /api/v1/exam-parsing-jobs/{job_id}/grading-questions`：只列该卷版真实映射题目。
- `GET /api/v1/exam-grading-workspaces/{workspace_id}`：题目、教师答案、Rubric、Canonical 文字、原页安全引用、作答确认、评分状态、候选和审核历史。
- `POST .../{workspace_id}/answer-review`：`request_id`、`expected_revision`、既有 AnswerReview payload，及默认 false 的 `manual_attribution`。
- `POST .../{workspace_id}/scoring`：`request_id`、`expected_answer_revision`，返回 202；不接受学生、对象 Key、Provider 或分数覆盖。
- `POST .../{workspace_id}/score-review`：`request_id`、`expected_revision`、既有 GradeReview payload。

公开 DTO 无私有对象 Key。当前明细最多 1000 个文字节点、1 MiB 文字，总投影最多 2 MiB；请求沿扫描生命周期中间件最多 64 KiB、并发 2、100 秒。工作区最多 100 题；作答与审核修订上限沿输入协议为 100。审核只投影最近 20 条，`history_has_more` 提示截断，旧记录保留数据库。大卷应另行设计分页，不静默省略来源。

## 教师浏览器流程

入口：`/scans/sessions/{session_id}/scripts`，选择当前解析成功的答卷，进入「AI 批改与复核」。

1. 准备题目作答核对，选择题目，查看原扫描页、识别文字、标准答案和完整评分项。
2. 拖选 Unicode 文字或明确选择整段，添加一个或多个区域。
3. 自动对齐未确定归属时，必须勾选「我已核对原页，人工指定所选内容属于本题」。这是教师的显式决定，仍拒绝被其他题已确认占用或算法明确归属其他题的来源。不是自动题号识别放宽。
4. 确认实际作答；不能确定则标记人工核验。空白、不确定和模态不支持均不生成评分输入或零分。
5. 请求 AI 评分并读取真实任务状态。Worker 未启动则保持排队，不按固定时间显示成功。
6. 查看模型逐项分值、理由和原文引用；接受确认原始模型候选，修订提交完整 Rubric 快照。changed 始终相对原 Proposal。
7. 修改分值时可明确选择已确认的作答片段作为该项依据，或沿用模型引用；正分仍须真实 Quote，不能用 no_evidence 自动补分。超过 Quote 长度上限的片段须先在作答确认中缩小范围。
8. 拒绝或要求重新评分保留历史，不自动重复模型请求。重新确认作答形成新复核版本，才能生成新的任务。

请求结果未知时页面锁定编辑，允许用原 body/request_id 重试。当前仅内存保留，刷新后应先读取服务端状态，不声称可以恢复尚未持久化的浏览器草稿。

## 队列、来源时效和故障

任务状态 pending/running/succeeded/failed/invalidated。单任务一次 Provider 请求，零无效输出自动重试；5 秒心跳，180 秒租约和总执行预算。过期任务标记 `execution_outcome_unknown`，不自动重发可能收费的调用。旧 Token、迟到执行器不能提交成功。

申请、调用前、调用后和最终事务重查当前答卷、名单、卷版、申请人 Grant 和精确作答复核。另一位教师提交审核时，最终事务还重新校验原评分申请人，不能依赖详情读取时的旧授权。重算解析来源合同中的 Manifest，验证原件与预览；S3 字节异常安全拒绝。答卷/名单更正、卷版退役、场次取消、账号停用或撤权均阻止新有效候选。历史对象不改写，状态读取不把旧候选继续投影为当前结果。

模型弃权是独立状态，协议/证据失败无成功候选。合法引用只证明来自原文，不证明教学评分合理；测试 Provider 只验证工程闭环。

## 后续最小工作

- 对可用模型完成严格协议及教学质量验收；本轮两次真实本地输出均未通过，不能宣称真实 AI 评分闭环已验收成功。
- 正式重评分按钮可进一步引导教师创建新的作答复核版本，首版不自动重复请求。
- 对账孤立 S3 对象、细化运行指标、分页完整历史；不增加匿名特权接口。
- 此切片没有复杂公式/图表/图片评分、阅卷任务分派、双评、有效题分合成或成绩发布。
