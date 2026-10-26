# 多人单评阅卷与进度管理

## 可操作范围

本切片在当前已成功解析的考试答卷上分配阅卷任务，不另建评分协议。负责人在 `/marking/manage` 选择考试与科目场次，准备题目工作区，按整份答卷或勾选的一组题目分配给教师。同一场次不同班级可以混合分配。`/marking/my` 提供个人任务、领取、原页预览、已有 AI 批改与 GradeReview、明确提交及进入下一题。`/marking/progress` 显示各教师的分配、待批、批改中、已完成、取消及来源失效记录。

任务单位是一份真实 `ExamGradingWorkspace`，即一个当前解析答卷中的一个 PaperQuestionMapping。整卷和题组只是有界批量分配，不复制题目、答卷、试卷或评分标准。没有双评、仲裁、自动均衡派单或正式成绩发布。

## 数据及有效性

增量迁移 `b16a82e6a017` 新建 `MarkingTask` 与 `MarkingOperation`：

- 任务真实关联场次、工作区、被分配 User、负责人 User 与最终 GradeReviewRevision。
- 同一工作区只能有一个 current 单评任务，部分唯一索引、版本链触发器和延迟约束共同保证。分配世代、操作者、分配时间和前驱不可改写，历史不能删除。
- 状态为 `assigned → in_progress → completed`，未完成任务可取消。重新分配把旧任务原子变为 `superseded`，建立下一代 current；已开始/完成的重派须明确确认和不少于五字的理由。
- 任务提交必须引用该教师在本次分配之后保存的最新 accepted/revised 完整复核，不能把旧审核、模型候选、rejected、弃权或缺评当成任务完成。
- 草稿评分、审核记录仍使用原 GradeInput、GradeProposal、GradeReview、Decimal 与 Quote 合同。保存 GradeReview 与显式提交任务是两步；中途关闭浏览器不会伪造已提交状态。
- 成绩预览读取当前任务的精确 review_id。未提交、取消、重派、教师撤权/停用或来源更正后不再计入有效成绩，整卷保持未完成，不补零。无派单的原有考试批改保持原行为。
- 答卷/名单/卷版变化不重写旧任务或审核事实，当前读取及提交重新验证原解析意图、当前来源、卷版与审核申请人。来源不再有效时展示失效，不能继续提交。

## 最小岗位及授权

固定动作 `exam.task.manage`、`exam.task.work` 使用真实 installation / exam_event / exam_session Scope，当前数据库 Grant 决定权限，JWT role、旧管理员及 AssignmentMembership 不参与。

- `marking_manager`：原受控 `exam_mark_operator` 的配套权限及 task.manage，可准备当前答卷、查看并分配任务；不含 security.grants.manage，不含 task.work，不自动获得成绩班级权限。
- `marking_teacher`：仅 task.work。除真实分配且已领取的当前工作区外，不能通过它浏览其他题目、场次、名单、全校学生、混班原始 PDF 或成绩。
- `marking_deployer`：部署审批岗位，组合上述动作和既有安全委派权，用于批准并分配固定岗位。不是普通负责人或教师岗位，不自动回填任何现有账号。

首次受控部署使用已有安全管理员和明确审批编号：

```bash
uv run python -m huipi_cloud.modules.marking_tasks \
  --actor-user-id "$DEPLOY_ACTOR" --target-user-id "$APPROVED_DEPLOYER" \
  --request-id "$REQUEST_ID" --approval-ref "$APPROVAL_REF"
```

此命令需要数据库运维访问及当前 security.grants.manage；它不是教师自助接口。随后通过既有受控 security Grant 流程将 manager/teacher 固定岗位授予具体 Event/Session Scope。负责人只能从服务端查出的本场次有 task.work 的活跃账号分配，不能授予岗位。当前 User 没有可靠姓名字段，选择器明确显示教师账号短标识，部署时需由操作员核对账号；不虚构教师姓名。

领取后的例外入口绑定具体 workspace、current task 和 assignee，不提供 include_protected、任意 SQL、通用特权 Session 或任务自选场次。它在现有考试业务服务内验证原解析申请人的真实任务意图，而不是将考试资源交给旧 Worker。原有扫描、解析、成绩班级授权及 legacy 可见性保持独立。

教师原页预览只允许本答卷实际页列表，通过任务端点读取有界 JPEG 并校验 SHA/长度，外部 I/O 后再次核验。不能下载含其他学生的整份混班 PDF。

## HTTP 与容量

固定权限目录与 OpenAPI 同步，所有接口要求真实 OIDC/JWT：

- `GET /api/v1/marking-sessions`：负责人有权的考试/场次，20条分页。
- `GET /api/v1/exam-sessions/{session_id}/marking-pool`：5份当前答卷/页，真实题目及分配状态。
- `GET .../marking-teachers`：当前可分配账号，上限100。
- `POST .../marking-jobs/{job_id}/prepare`：复用原对齐和工作区准备。
- `POST .../marking-assignments`：至多100个题目工作区，同事务分配。
- `GET .../marking-progress`：全场次按教师计数及25条任务明细分页。
- `GET /api/v1/my-marking-tasks`：先过滤当前主体及实际 Scope，再分页25条；个人总进度单独聚合。
- `GET /api/v1/marking-tasks/{task_id}`：自己的当前任务和来源状态。
- `POST .../{task_id}/claim|complete|cancel`：严格 request_id / expected_revision。取消需负责人权限。
- `GET .../{task_id}/pages/{page_id}/preview`：实际分配答卷中的有权页面，单张2MiB。

POST 请求上限64KiB；来源、工作区及审核内容继续沿原考试服务限制。负责人列表和统计不会替代成绩证据验证。聚合计数检查数据库来源版本、卷版状态和当前授权，不逐个对全场次 S3 原件做字节验证；教师明细/提交和整卷成绩仍执行原来源校验。

## 事务、撤权和恢复

沿既有顺序校验当前 User/Grant，锁场次，再按工作区 UUID 排序并锁 current task。场次锁使同场次的领取、分配、更正与提交串行化；并发请求不会生成两个有效单评。跨场次可独立进行，首版没有吞吐量保证。

业务更新与只含 ID/动作/请求摘要的不可变操作审计同事务提交；审计失败全部回滚。每主体 request_id 由事务 advisory lock 和唯一约束防重放，正文冲突409；重派后旧请求重新鉴权并拒绝。COMMIT确认未知返回503，浏览器保留原正文人工重试，不能自动换 request_id 或补造成功。任务提交与 GradeReview 为两次明确操作：提交失败时已保存复核保留为未提交，不形成有效题分。

撤权先提交则后续操作拒绝；已获得既有授权共享锁的事务可先提交，撤权等待，不声称全系统在途操作立即取消。原模型 Worker 仍保留调用前后来源、申请人、租约和审核检查；新功能没有增加模型调用或失败回退。

## 当前限制

分配前需当前答卷成功解析；未确认/未解析答卷不产生可领取任务。没有跨场次任务合并、匿名阅卷、双评、仲裁、自动重试/重派、账号姓名资料管理或结果发布。连续下一题优先当前个人分页，末尾明确提示翻页。旧任务与审核留在数据库；首版进度页面以当前任务为主，没有独立完整派单历史浏览器。

已有真实 MinerU/DeepSeek 证据见原考试解析、候选评分及确定性引用验收。本切片新增回归使用明确的 Synthetic 执行器/Provider 验证协作和成绩逻辑，不冒充新的模型效果验收。
