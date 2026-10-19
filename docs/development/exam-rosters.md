# 参试名单、历史快照与冻结

本切片提供 `huipi_cloud.modules.exam_rosters` Service 与签名 JWT CLI。没有新增 HTTP、Vue、扫描、到考登记、阅卷或成绩接口。只用合成数据验收；真实名单须先确认学校授权、责任人与保留政策。

## 身份与版本

- `ExamParticipation`：同一 Session / Student 唯一的稳定身份，真实外键；不可改写或删除。移出某版名单不删除 Participation。**有 Participation 不代表当前仍参试**，当前参试集合只能通过 `RosterRevision.is_current` 的快照查询。
- `RosterRevision`：同场次递增 `version_no`；草稿编辑的乐观锁 `revision` 与名单版次分开。真实同场次前版复合外键、唯一后继、每场最多一个草稿和一个当前冻结版。最多100版。
- `RosterEntrySnapshot`：真实名单版本、Participation、Student、ClassEnrollment、行政班、SessionPaper 关系；名单/参试/卷别复合外键禁止跨场次拼接。保存学生姓名、学号与班级名称/代码的捕获值，而不在读取时回查最新标签。
- `RosterOperation`：actor/request_id 唯一、正文摘要、固定操作和回执；业务与追加审计同事务。审计回执不包含完整名单；不写日志正文、JWT、私有Key。操作原因不要填入不必要的个人信息。

多个行政班的学生可在同一名单中引用同一个 SessionPaper / PaperVersion，不复制场次、试卷、题目或Rubric。同名不合并，学生身份由真实 Student ID 决定。

## 日期、考号和卷别

- `business_date` 必须明确提供，并等于场次开始时刻的 Asia/Shanghai 日期。班级归属按 `[valid_from,valid_to)` 查询；不存在日期内归属或学生未启用则拒绝。名称与班号由数据库提供，客户端不能声明。
- 首次创建名单后，该场次的起止时间及single/ab策略禁止原位修改（数据库触发器）；需要改期/改策略时取消旧场次并建新场次。名单历史不随场次日历漂移。
- 考号为1–32位 ASCII 字母、数字、下划线或连字符；去首尾空白并统一大写。唯一性范围为**同场次的同名单版本**，服务Schema及数据库共同校验。不同场次或不同历史版本可出现相同考号，不据此判断同一学生。草稿允许缺号，冻结不允许缺号。
- 卷别选择传真实 `session_paper_id`，必须属于本场次、active、卷版locked。不能传自由“A/B”字符串；不推断学生卷别。NULL明确表示待核验。
- 冻结允许未知卷别，查询返回 `unknown_variant_count`；**冻结并不证明卷别已确认**。未来身份确认/答卷关联须另外要求实际可用的卷别关系，不能用默认A补齐。

## 草稿、冻结与更正

1. `create` 建首个草稿，可以暂为空；每版最多2000名学生。名单读取按稳定顺序分页，每页1–100条，返回total/offset/limit/truncated。
2. `replace` 是完整替换草稿条目，任何失败整体回滚；不是增量补丁。每次成功递增revision。草稿数据尚不能作为正式参试事实。
3. `freeze` 要求Session ready、父Event draft/active、至少一条记录、考号完整唯一、来源学生和日期归属仍有效、配置的卷版仍locked。整版原子冻结；Session ready本身不能替代名单冻结。
4. `correct` 只针对当前冻结版，要求完整新条目和原因；创建后继草稿。新增、移除、改考号、改卷别都在新版本表达。新草稿按同一业务日期重新获取可信标签/归属，不改旧快照；未变学生复用稳定Participation。
5. 更正草稿冻结前，原冻结版仍current；新版本冻结时，原子切换current。旧版仅允许current由true变false，其内容、revision和冻结事实不变。数据库延迟约束在提交时要求current恰好指向最新冻结版。
6. Session closed/cancelled或Event closed/cancelled后拒绝新的名单写入，历史仍可由当前有权主体读取。取消不抹除名单，也不自动标记缺考。

卷版退役不改历史名单、SessionPaper或快照；退役后新选择/冻结/更正中仍使用该卷的操作会拒绝。Session ready后本来就禁止改绑；受权人员应显式取消场次，不能偷偷更换已冻结用卷。既有冻结名单只是历史事实，未来扫描处理必须重新校验场次、当前名单和卷版可用性。

## 权限与事务

固定 `exam.roster.read/manage/freeze/correct` 支持 installation、真实ExamEvent和ExamSession Scope。Event范围必须由数据库证明父子关系。创建/编辑需manage；冻结需freeze；更正及更正草稿编辑需**同一Grant**覆盖manage+correct。读取独立需要read。旧管理员、Membership、JWT role及客户端身份字段均不授权。

新角色版本：`11111111-b12c-4000-8000-000000000001` roster_operator（安全委派+四个名单动作，可信部署根）；`...0002` roster_manager（四动作）；`...0003` roster_reader；`...0004` roster_freezer（read+freeze）。旧封存角色不修改，不自动扩大旧授权。

部署人员经审批、持数据库凭据执行：

```text
uv run python -m huipi_cloud.modules.exam_rosters enable-operator \
  --actor-user-id UUID --target-user-id UUID --request-id UUID \
  --approval-ref 审批编号 --confirm-deployment-operation
```

这不是OIDC代登录或公共自助提权；仍校验actor当前security.grants.manage，授权和审计同事务。普通命令只接受stdin签名JWT。继而使用现有安全CLI向指定Event/Session委派对应角色。

写锁顺序：当前User SHARE → actor/request advisory → 当前Grant SHARE → Event SHARE → Session UPDATE → Roster UPDATE → 学生/归属和卷版锁。相同场次的编辑、冻结和更正串行；expected_revision拒绝过期请求。撤权等待已授权在途事务，提交后新请求和原请求重放均拒绝；不声称能撤回已返回数据。

同request_id/正文返回首次回执，异正文409；503 `roster_commit_outcome_unknown`保留原请求再按当前权限重放，不声称已经回滚。其他数据库故障默认拒绝。唯一键、外键、来源及冻结触发器保护直接SQL写入；数据库所有者禁用触发器/TRUNCATE不在应用威胁边界内。

## CLI流程

严格正文见[schemas.py](../../src/huipi_cloud/modules/exam_rosters/schemas.py)。不要将JWT放在argv、文件示例或日志。

```text
uv run python -m huipi_cloud.modules.exam_rosters create --token-stdin --payload roster.json
uv run python -m huipi_cloud.modules.exam_rosters inspect --token-stdin --id UUID --offset 0 --limit 100
uv run python -m huipi_cloud.modules.exam_rosters replace --token-stdin --payload replace.json
uv run python -m huipi_cloud.modules.exam_rosters freeze --token-stdin --payload freeze.json
uv run python -m huipi_cloud.modules.exam_rosters correct --token-stdin --payload correct.json
uv run python -m huipi_cloud.modules.exam_rosters history --token-stdin --id SESSION_UUID
```

create正文为request_id、exam_session_id、business_date、reason、entries。每条entry只有student_id、exam_number（可空）、session_paper_id（可空）。replace为request_id、roster_revision_id、expected_revision、完整entries。freeze不含entries。correct为旧冻结版的ID/revision、request_id、reason和完整新entries。正文上限1MiB。

inspect包含获授权名单的学生和班级信息，是敏感输出，仅可存入受控位置；history只有版次元数据。未知/无权资源统一404；非法输入422；业务冲突409；数据库不可用或提交未知503。底层数据库不返回SQL或内部堆栈。

## 迁移与验收

增量迁移 `b12c9d20e411`，在SessionPaper增加(id,exam_session_id)唯一键以支撑真实复合外键；旧UUID、审核记录和历史迁移不改。无新名单、审计、Participation或名单角色Grant时可往返；存在新事实即拒绝破坏性downgrade。生产恢复采用保留数据的向前修复。

测试见[集成测试](../../tests/integration/test_exam_rosters.py)与[协议测试](../../tests/unit/test_exam_roster_protocol.py)。使用独立真实PostgreSQL和现有私有S3测试夹具，默认不调用收费API。本文不把测试定义当作执行成功，最终执行证据以交付记录为准。

未实现：名单CSV导入/班级一键展开、自动考号生成、考场座位、补考关联、到考/缺考、扫描及身份确认、真实学校试用。后续必须引用Participation与**明确冻结名单版内的Entry/SessionPaper**，并重新检查current、场次及卷版可用性；移除名单不能删除旧身份和历史答卷。扫描新依赖的失效传播及更正审批需在相应切片独立实现。
