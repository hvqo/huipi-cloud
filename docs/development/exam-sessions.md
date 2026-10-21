# 考试计划、科目场次与共享用卷

本切片是受控 Python Service / JWT CLI，不提供考试 HTTP 或 Vue 页面。不包含参试名单、班级快照、考号、考场、扫描、阅卷或成绩。不得把准备完成解释为名单齐全、学生缺考、扫描结束或成绩已发布。

## 领域与数据合同

- `ExamEvent`：真实 AcademicTerm FK、名称、分类、可信创建人/组织者、lifecycle、revision、UTC 创建/更新时间。创建人及组织者均来自当前 Principal，不接受客户端角色或身份。学期和组织者创建后不可换写。
- `ExamSession`：真实 Event 与 Subject FK、名称、带时区起止时间、single/ab 用卷策略、lifecycle、revision 与审计字段。没有冗余可伪造的学期 UUID，学期唯一经 Event 继承；同计划/学科/开始时刻唯一。一个场次没有单班级 FK，不按班克隆场次；以后名单中的多个班可指向同场次。**本切片尚无班级受众配置或名单关联。**
- `SessionPaper`：真实 Session / PaperVersion FK，显式 A/B 代码，active/revoked、绑定时内容和映射摘要、创建人/时间及撤销时间。有效代码与同场次有效卷版各有部分唯一索引。
- `ExamOperation`：actor/request_id 唯一、正文摘要、操作类型、脱敏结果及时间；数据库禁止更新或删除。无学生正文、标准答案、Token或私有对象 Key。

一个 PaperVersion 可被多个场次、多个 ExamEvent 引用，不复制内容载体。PaperVersion 的载体 subject 仍是旧自由文本，不能当作可信 Subject 外键或通过同名字符串推断学科一致；具体卷版是否适合本场学科，当前由受权操作员在绑定前核对，后续需独立的可信试卷学科合同。一场次 single 只允许 A；ab 必须有两个**不同的明确卷版**，A为默认卷别，准备前 A/B 必须齐全。题序/显示标签来自各卷映射，不从题号、相似题干自动猜测两卷等价，也不自动分配学生卷别。

## 生命周期与修改

- Event：draft → active → closed；draft/active 可 cancelled。active 要求至少一个场次，且全部场次 ready；关闭或取消计划前，逐个显式关闭/取消其场次，不隐式改写子记录。已有取消场次的计划不能直接 active，本阶段采用严格的整计划准备规则。
- Session：draft → ready → closed；draft/ready 可 cancelled。closed 需所属 Event active。终态不可恢复，没有 ready → draft。
- Event 草稿可改标题/分类；Session 草稿可改标题、时间和用卷策略，不能换父计划或学科。变更学科应取消旧场次并建立新场次。
- 创建场次要求 Event 为 draft，检查 `expected_event_revision`，成功会推进 Event revision。修改/绑定/撤销/状态推进检查场次 `expected_revision` 并递增；场次内部修改不自动推进父计划 revision。
- ready 目前只表示场次配置及锁定用卷校验通过。没有 roster、参试与到考证据，查询明确返回 `roster_status=not_configured`、`attendance_status=not_available`。

时间输入必须带偏移；PostgreSQL timestamptz 保存绝对时刻，单校业务日期固定按 `Asia/Shanghai` 校验，开始和结束必须在所属学期的闭区间日期内，结束晚于开始。当前学期日期没有应用修改接口；直接特权 SQL 改组织日历后仍需运维一致性审计，不能宣称提供完整组织历史快照。

## 绑定、退役与历史

绑定时持场次 UPDATE 锁及 PaperVersion SHARE 锁；仅 locked 可新增。读取权不等于使用权，不能以旧 admin 或 Membership 绕过。数据库触发器再次核对实际状态、内容/映射摘要、卷别和父资源。

草稿换卷：先 `unbind` 将原关系写为 revoked，再 `bind` 新增关系。取消绑定不删除原行、不改变原卷版和摘要；新请求必须使用上次响应的 revision。相同有效代码或同场次重复有效卷版冲突返回409，不自动替换。历史最多200条关联，场次每计划最多200个，超限要求独立管理方案，不静默截断。

ready/closed/cancelled 场次均不允许改绑。即使还没有扫描/参试实体，也采用这一保守冻结规则；以后新增这些真实依赖时，必须通过引用原 SessionPaper 的真实 FK 和版本化更正继续保留事实，不能恢复通用 UPDATE/DELETE 接口。

PaperVersion 退役不会删除或改写任何 SessionPaper。查询保留绑定时摘要，并返回当前 `paper_status=retired`。新的绑定、场次 ready、计划 active 都拒绝退役卷。已准备/进行中的场次若卷版后来退役，不能将旧关系视为新可用授权，也不自动宣布考试取消；受权人员应检查原因并显式取消场次。原考试和关闭后的历史仍保留。当前没有扫描/成绩功能，不把上述状态当作下游准入证明。

## 权限与授权顺序

固定动作：

- `exam.event.create`：仅 installation。
- `exam.event.read/manage`：installation 或真实 exam_event Scope。
- `exam.session.read/manage/paper.bind`：installation、真实 exam_event 或 exam_session Scope。
- `paper.use`：installation 或真实 paper_version Scope；不包含标准答案读取权。

Scope 每种只允许一种真实 FK，不接受自由 JSON、UUID 字符串模拟授权。Event Scope 只覆盖数据库查询证明属于它的 Session，不覆盖其他考试或 PaperVersion。

绑定/撤销要求**同一个 Grant**同时覆盖目标场次 manage 与 bind；绑定还独立验证目标卷版 paper.use。两者是两个真实资源的必要权限，不能拿别的场次或别的卷版凑数。只有 paper.read 不能绑定；只有场次管理也不能读取题目、标准答案或 Rubric。场次查询只返回关系元数据，不返回这些内容。

角色版本：`11111111-b12b-4000-8000-000000000001` exam_operator 是受控部署根，含现有安全/组织/试卷与本切片动作，不是普通教师；`...0002` exam_manager 可绑定具体 Event；`...0003` session_manager 可绑定具体 Session；`...0004` paper_user 仅允许指定卷版使用。已有用户不自动获得这些 Grant。

部署人员持数据库凭据，经审批运行：

```text
uv run python -m huipi_cloud.modules.exams enable-operator \
  --actor-user-id UUID --target-user-id UUID --request-id UUID \
  --approval-ref 审批编号 --confirm-deployment-operation
```

该操作仍检查 actor 当前 security.grants.manage 与账号状态，授权及审计同事务。它属于可信部署代操作，参数 actor UUID **不代表 OIDC 登录证明**，不能作为 HTTP/Agent 自助提权入口。具体 Event/Session/Paper Scope 的委派和撤销继续用现有安全 CLI，由实际部署根执行。

## JWT CLI 与基本流程

普通命令要求 `--token-stdin`，裸 JWT 从受控 stdin 输入，执行现有签名、Issuer、Audience、有效期和当前 User 验证；不把 Token放入 argv 或日志。所有写入正文均含 request_id，JSON上限128 KiB，额外身份/作用域字段被拒绝。

```text
uv run python -m huipi_cloud.modules.exams create-event --token-stdin --payload event.json
uv run python -m huipi_cloud.modules.exams inspect-event --token-stdin --id UUID
uv run python -m huipi_cloud.modules.exams edit-event --token-stdin --payload event-edit.json
uv run python -m huipi_cloud.modules.exams create-session --token-stdin --payload session.json
uv run python -m huipi_cloud.modules.exams inspect-session --token-stdin --id UUID
uv run python -m huipi_cloud.modules.exams edit-session --token-stdin --payload session-edit.json
uv run python -m huipi_cloud.modules.exams bind --token-stdin --payload bind.json
uv run python -m huipi_cloud.modules.exams unbind --token-stdin --payload unbind.json
uv run python -m huipi_cloud.modules.exams session-state --token-stdin --payload session-state.json
uv run python -m huipi_cloud.modules.exams event-state --token-stdin --payload event-state.json
```

1. create-event 正文：`request_id/academic_term_id/title/category`。category 为 midterm/final/monthly/other，默认other。
2. create-session：`request_id/exam_event_id/subject_id/expected_event_revision/title/starts_at/ends_at/paper_policy`；例如 `2026-11-01T09:00:00+08:00`。先保存响应中的场次 ID，再用新的 Event revision 建下一个科目场次。
3. bind：`request_id/exam_session_id/expected_revision/paper_version_id/variant_code`。返回 binding 和新 session；下一次操作使用新 revision。
4. unbind：将 paper_version_id/variant_code 替换为 `session_paper_id`，保留历史。
5. session-state：`request_id/exam_session_id/expected_revision/lifecycle`；准备后不可改绑。event-state 类似，使用 exam_event_id 和 Event revision。
6. edit-session 提交完整 title/starts_at/ends_at/paper_policy；edit-event 提交完整 title/category，不是增量补丁。

完整严格合同见 [schemas.py](../../src/huipi_cloud/modules/exams/schemas.py)。查询计划返回计划和场次数；查询场次返回有界用卷历史，两个查询均不返回学生或试卷正文。当前没有批量查询/前端接口。B-1.2c起计划查询的roster_status为per_session；场次返回not_configured/draft/frozen元数据，不返回名单正文或推断到考。

## 事务、错误及验证

写入顺序：当前 User SHARE → actor/request 事务 advisory lock → 当前 Grant SHARE → Event锁 → Session锁 → 卷版SHARE → 业务/审计 → COMMIT。Paper退役用已有 Paper UPDATE锁。撤权等待已授权在途事务结束，提交后新操作（包括幂等重放）拒绝；不声称能撤回已经返回的数据。

- 404 `resource_not_found_or_forbidden`：缺失或无权限统一正文。
- 409 `exam_revision_conflict`、`exam_request_conflict`、`exam_paper_not_usable`、`exam_variant_not_allowed`、`exam_state_conflict` 等：需重新检查当前授权和业务状态。
- 数据库 FK/唯一键/触发器冲突安全映射为409 `exam_write_conflict`；不返回 SQL或内部路径。
- 503 `exam_operation_unavailable` / `authorization_unavailable`：数据库故障，拒绝操作。
- 503 `exam_commit_outcome_unknown`：保留原 request_id 与正文，按当前权限重放。不能声称已经回滚；没有自动重试或S3补偿删除。重放返回的是原操作回执，不代表当前状态；随后应inspect，后来撤权或停用的用户不能读取该回执。

运行 [真实 PostgreSQL 测试](../../tests/integration/test_exam_sessions.py)及[试卷安全回归](../../tests/integration/test_paper_versions.py)。迁移在独立库执行 upgrade/check/downgrade/再次 upgrade，并检查旧记录和真实锁卷摘要；降级在检查前先锁表阻止新建竞态；存在考试记录/审计/Scope/新角色Grant时拒绝破坏性降级，生产回滚采用保留数据的向前修复。数据库特权所有者禁用触发器/TRUNCATE不在应用威胁边界内。

B-1.2c的参试身份、名单快照、冻结和更正现由[名单服务](exam-rosters.md)提供，读取名单需独立的exam.roster.read权限。首次建名单后场次日期和用卷策略不可原位改写。不得由本切片的 session_count 或 ready 推断参试人数、缺考、考试材料完整或成绩有效。
