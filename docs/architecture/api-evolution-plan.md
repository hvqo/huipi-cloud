# API 演进、事务与前后端对接合同草案

**拟议合同，待审查；本轮没有注册以下新路由。** 现有 34 操作见[真实清单](../product/current-contract-inventory.md)。领域 FK/版本见[领域设计](school-exam-domain.md)，授权见[岗位方案](../product/role-permission-matrix.md)。统一前缀 `/api/v1`。

## 1. 兼容与通用合同

- `/docs`、`/redoc`、`/openapi.json` 是公开接口描述，不含业务数据；34 个操作的计数不包含框架文档路由。
- 现有 URI/DTO/UUID、Unicode SourceRegion、GradeInput 1.0、Proposal/Review JSON 保持；新增字段仅在明确新 DTO 中。旧 history 继续读原版，不静默重算。
- 修改现有接口授权分类属于 B 安全前置：考试内容载体、桥接 Submission、全部衍生对象不能借 legacy Membership/admin 接口读取；未知分类默认拒绝。普通历史作业行为不变。
- 新写请求必填 `request_id: UUID`；版本化资源写必填 `expected_revision: int >= 0`；服务端 `extra=forbid`，拒绝 actor/role/teacher_id 作为权威。所有计分 Decimal 传定点字符串，不使用 JSON float。
- 列表采用 limit(1..100，敏感资产默认 25)/opaque cursor；排序固定 created_at,id，返回 `records,next_cursor,has_next,scope,as_of,truncated`，无授权对象先过滤再分页。所有 top-N/统计分母明示；不暴露绝对全校数量。
- 只读资产下载先认证再授权，`Cache-Control: private,no-store`，安全 Content-Disposition，无原始 S3 Key/签名永久 URL/token query；原卷流有界，取消后的 boto3 同步线程边界继承现有安全原则，不能假称 asyncio 能杀线程。
- 新 ID 的资源归属由 DB FK 决定；传 session_id+paper_id+participation_id 必须同域，不接受“两个资源分别有权就能拼接”。
- 普通错误 `{"detail":"stable_code","request_id":"…"}`，必要时有 `operation_id`；不回堆栈、SQL、Key、姓名。404 对未授权/不存在不可区分；不依赖前端隐藏。

| 状态 | 用途 |
|---|---|
| 200/201 | 查询/事务已确定成功；201 指新业务实体，幂等重放可返回同一实体并 `replayed=true` |
| 202 | 明确已登记的后台处理，返回 operation/job 状态；不是分卷/学生提交已经完成 |
| 400 | 无法解码的请求/分页 cursor/不完整上传格式 |
| 401/403 | 缺少无效身份 / 当前主体无动作权限（设备不冒充人） |
| 404 | 资源不存在或不在允许 scope；不透露身份 |
| 409 | expected_revision、请求摘要/归属/占页、冻结/非法状态或来源版本冲突 |
| 413/415/422 | 大小配额/格式不支持/字段或页/身份候选参数不合法 |
| 503 | 身份/DB/S3 不可用、COMMIT 未知；保留原请求，不自动新建 |

## 2. 路由族与最小能力映射

I=身份授权，O=组织，H=作业，P=试卷，X=考试，S=扫描，M=阅卷，R=结果，W=工作台，K=资料助教，L=现有 legacy。后续每条路由在合入时加入固定权限注册表，默认未注册拒绝。

| 组/阶段 | 方法与资源 | action/数据合同 |
|---|---|---|
| I / B 安全前置 | GET `/me/contexts` | 已解析 User，返回 workspace/capability/scope 摘要及 auth revision，无私有 OIDC 标识 |
| I | GET/POST `/roles`；POST `/roles/{id}/revisions` | role.define，有限目录，版本化不自动扩大旧 grant |
| I | POST `/role-grants`；POST `/role-grants/{id}/revoke` | role.grant/revoke，防自提权/审批/同事务审计 |
| I | GET `/permissions/catalog`；GET `/access-audit` | 对应 admin/read，只显示可委派项或授权审计，不返回答案 |
| O / B | GET/POST `/organization/terms`、`/classes`、`/subjects` | organization.read/manage，班级类型/学期强 FK |
| O / B | POST `/student-imports`；GET `/student-imports/{id}`；POST `/{id}/confirm` | 预检/冲突/确认，必要学生字段；同名不去重 |
| O / 后续 | `/teaching-classes`、`/teaching-assignments`、`/class-enrollments` | 学期有效期关系，组织动作，不自动授阅卷权 |
| H / 后续 | POST/GET `/homework-offerings`；GET `/{id}/participants` | homework.assign/read，多个班引用同一内容 |
| H / 后续 | POST `/homework-participations/{id}/attempts`；POST `/{id}/effective-attempt` | 重交/补交/有效版本，旧单份接口独立保留 |
| P / B | POST/GET `/paper-versions`；GET `/{id}`；POST `/{id}/lock` | paper.edit/read/lock，绑定一个 Assignment，锁摘要 |
| P / B | POST/GET `/paper-versions/{id}/source-files`；GET `/paper-source-files/{id}/file` | paper.edit/read，不含学生作答的题本/空白答题卡；私有接收、签名/SHA/大小检查，锁后不可改 |
| P / B | PUT `/paper-versions/{id}/question-mappings` | 草稿整体替换、显式 A/B logical mapping，无题号猜测 |
| P / C | POST `/paper-versions/{id}/layouts`；POST `/paper-layouts/{id}/lock` | 模板/区域/坐标，需审核，正文与答题卡分离 |
| X / B | POST/GET `/exam-events`；GET `/{id}` | exam.create/configure，学期年级范围 |
| X / B | POST `/exam-events/{id}/sessions`；GET `/exam-sessions/{id}` | subject/session，而非创建同名 Assignment |
| X / B | PUT `/exam-sessions/{id}/papers` | 整体允许卷别集合，新 revision；锁名单/扫描后须受控变更 |
| X / B | POST `/exam-sessions/{id}/roster-imports`；POST `/roster-imports/{id}/confirm` | roster.import/lock，预检/锁版、每条学生真实 FK |
| X / B | GET `/exam-sessions/{id}/participants` | 最少身份名册，scanner 须 identity.verify；按考试时班级筛选 |
| X / 后续 | POST `/exam-sessions/{id}/seating-plans`、`/participation-changes` | 座位/缺考/补考与来源状态分开，变更审计 |
| S / B | POST/GET `/scan-batches`；GET `/{id}` | batch.create/read；可未知场次但 scope 来自受权隔离区 |
| S / B | POST `/scan-batches/{id}/uploads` | upload scope/限额；创建接收会话，不产生 Submission |
| S / B | PUT `/scan-uploads/{id}/parts/{part_no}`；POST `/{id}/complete` | 有界 part+checksum；完成 SHA、PDF解析安全、入 File/质检 job |
| S / B | GET `/scan-batches/{id}/pages`；GET `/scan-pages/{id}/image` | 授权批次页投影，无答案/原始 Key |
| S / B | POST `/scan-pages/{id}/quality-decisions` | page.qc，旋转/缺页/重复/模糊事实与候选分开 |
| S / B | POST `/scan-batches/{id}/split-plans`；GET `/split-plans/{id}` | split.edit，完整页组快照/未归页，版本和 digest |
| S / B | POST `/split-plans/{id}/confirm` | split.confirm + identity.verify，逐卷门禁，生成已确认事实/物化 job |
| S / B | GET `/answer-scripts/{id}`；GET `/{id}/versions` | 扫描/身份核验 scope，不能给匿名阅卷者整卷 |
| S / B | POST `/answer-scripts/{id}/identity-decisions` | 允许保持 unclaimed/disputed，核实需可信名单证据 |
| S / B 修复 | POST `/answer-scripts/{id}/remediations` | script.remediate，新完整页快照、失效影响确认 |
| S / B | GET `/operations/{id}`；GET `/operation-requests/{request_id}` | 同 actor+动作+资源查询，原请求恢复；空结果不是证明失败 |
| M / D | POST/GET `/marking-plans`；POST `/{id}/revisions` | plan.manage，独立计划、题组/轮次/匿名/回避 |
| M / D | GET `/marking-tasks`；POST `/marking-tasks/claim` | task.read/claim，scope 服务端固定，返回匿名任务 |
| M / D | GET `/marking-tasks/{public_id}/evidence` | 授权题区，精简源标识，安全裁剪校验；不回 Submission ID |
| M / D | POST `/marking-tasks/{public_id}/heartbeat`、`/records`、`/return` | lease token/fencing、重查授权和来源；完整逐项记录 |
| M / D | POST `/marking-plans/{id}/reassignments`、`/samples`、`/arbitrations` | 组长/仲裁 scope，不删除已提交评分 |
| R / E | GET `/exam-sessions/{id}/result-checks`；POST `/result-snapshots` | 完整性/来源/规则；缺分不补零 |
| R / E | POST `/result-snapshots/{id}/approvals`、`/publications`、`/corrections` | 独立权限和审批，B 不提供执行路由 |
| W / 后续 | GET `/workbench/tasks`、`/workbench/overview` | 当前可信责任范围内聚合，响应含统计口径与截断 |
| K / 后续 | 受控资料及对话 HTTP 另立合同 | 复用现有服务但不公开 CLI；Agent 工具用同一授权 resolver |

上表为目标目录，不要求一个 B PR 实现全部 I/O/M。首闭环精确 API 子集和前置拆分见[开发切片](../development/business-reconstruction-roadmap.md)。

## 3. 关键请求/响应示例（设计草案）

### 3.1 锁定试卷与创建场次

`POST /paper-versions`：

```json
{
  "request_id": "10000000-0000-4000-8000-000000000001",
  "canonical_assignment_id": "20000000-0000-4000-8000-000000000001",
  "title": "合成数学卷 A",
  "paper_family_id": "30000000-0000-4000-8000-000000000001"
}
```

锁卷请求 `expected_revision, expected_question_digest, expected_rubric_digest`；服务端重新计算，不信任客户端 SHA 自声明。响应 `paper_version_id, revision, locked_at, content_digest, layout_state`。无模板为 `unavailable` 而不是已配准。

`POST /exam-events/{id}/sessions` 请求 `request_id,subject_id,start_at,end_at,allowed_paper_version_ids`。响应 SessionSummary；多个班通过名单引用同一 Session，不在请求中 `clone_per_class`。

### 3.2 名单导入与版本

创建 import（1..5000 条可配置）每条 `student_id,class_id,teaching_class_ids,candidate_number,session_paper_id`；服务器查可信组织并冻结必要快照。未知 student 先走组织导入，不由姓名自动建考生。响应 `import_id,input_sha256,conflicts,can_confirm`。confirm 使用 `request_id,expected_session_revision,expected_import_sha256`，锁名单写 revision + participants + audit；同一批重复不增考生。

### 3.3 文件接收

创建 upload 请求 `request_id,filename,size_bytes,sha256,content_type`；校验 filename、配额、身份和目标 scope。响应 `upload_id,part_size,max_parts,expires_at,accepted_bytes`。part 具有 checksum/part_no，重复相同字节幂等，不同字节 409；不向设备暴露任意 S3 bucket/key。

complete 响应 202 `operation_id,intake_state=validating`，SHA/页数确认后 File 状态 received。前端显示已上传/待验证，不显示扫描/识别/分卷完成。分片可后续换协议，但不得把 1,600 页塞进现有单份 multipart。

### 3.4 人工分卷与身份确认

`POST /scan-batches/{id}/split-plans`：

```json
{
  "request_id": "10000000-0000-4000-8000-000000000002",
  "expected_revision": 2,
  "groups": [{
    "script_id": "40000000-0000-4000-8000-000000000001",
    "session_paper_id": "50000000-0000-4000-8000-000000000001",
    "page_ids": ["60000000-0000-4000-8000-000000000001"],
    "participation_candidate_id": null,
    "quality_state": "unchecked"
  }],
  "unassigned_page_ids": []
}
```

页 ID 必须来自许可文件、全部页显式归组或未分配；页序即数组顺序，不能重复占用。上述 candidate=null 可存草稿但不能正式学生物化。

confirm 请求：`request_id,expected_revision,expected_pages_digest,identity_confirmations[],acknowledged_issues[]`。每个确认含 `script_id,participation_id,roster_revision,expected_source_digest,basis_code`，basis 是可审计人工依据而非“模型分数高”。响应 202 `operation_id,confirmed_scripts,blocked_scripts[{script_id,reason_codes}],materialization_state`。B 最小实现以**单份页组**为确认/物化原子单元；批量包装只返回逐份结果，不宣称全部原子。页面占用交换、身份唯一与 job 登记在每份事务内完成；未归页明确保留。

最终查询得到 `succeeded` 才展示已建立 Submission。S3/解析失败仅重试已冻结物化/任务，不要求操作员再次核实同一份身份。校验变更则 source_changed 阻断，不能继续旧 intent。

### 3.5 阅卷匿名合同

TaskEvidence 仅含：`task_public_id,task_revision,question_key,round_label,rubric_snapshot,authorized_evidence[{evidence_id,media_type,display_geometry,text_fragment?,source_digest}],readiness,reason_codes`。前端按 evidence_id 取受保护字节；不见 S3 Key、student_ref、姓名、班级、Submission/Participation UUID。必要身份访问走另外的授权路径，不在普通 task 请求中加 `show_identity=true`。

提交完整 Criterion 数组 `criterion_id,points,reason,evidence_refs` + `request_id,expected_revision,lease_token,source_digest`，分数和理由/来源校验仍保留；不把模型候选校验机制当教师语义正确性证明。响应 MarkingRecordSummary，不是正式发布分。

## 4. 来源过期、幂等与重放

前端每份对象保存服务器 revision/source digest。409 分为 `revision_conflict,source_changed,page_already_allocated,identity_disputed,request_conflict`，要求读取最新再重新人工确认；不能用新 request_id 盲试。503 `commit_outcome_unknown` 冻结当前正文和目标，通过同 actor/原 request 查询；一次暂时 404/pending 不释放文件，不确认失败后新建。

修改页/Rubric 时，事务先增加 source_epoch 并把当前依赖标待核验；后台再计算细粒度受影响集，不能等待后台完成期间仍提供“可发布”。旧不可变 Record 保持；新写入在 COMMIT 内比较 epoch/摘要。无法证明未影响时保守阻断，不凭模型相似度复用。

## 5. 前端合同演进和旧接口保护

1. 现有 backend OpenAPI 从 `app.openapi()` 导出，有 34 个操作；该合同不包含 Mock DTO 宣称的知识库/聊天/扫描。
2. 新域独立 Pydantic Schema/OpenAPI 标签，前端从生成类型引用，不手写假定 backend 字段。
3. 在 PR #31 的 HuipiServices 增加 exams/papers/scanning/marking 等独立接口，不把 exam 偷塞进 assignments 原 DTO；main 在 #31 未合前不复制第二套 Service 框架。
4. Mock 使用同样明确状态机、数据版本和错误；真实不支持则未开放，不伪造签名/身份/发布成功。
5. 旧 `/me.role=teacher/admin` 兼容，菜单新用 `/me/contexts`；旧客户端可继续操作 legacy 作业，但不能对新考试资源降级访问。
6. 新桥接资源在 Assignment/Submission 列表、workbench、受保护文件、Canonical/Markdown/复核路径及 Copilot 中全部 fail-closed。别名 URL、分页、question_id 过滤、asset endpoint 同样纳入；范围先过滤再统计，连资源 ID 都不泄漏。
7. 使用一个通用授权 resolver 生成资源父链和数据投影策略，路由、服务、CLI 不各写不同规则；Worker 的处理权限与用户读权限分离，不能把 Worker 凭据返回浏览器。

## 6. 不在本轮承诺的接口

不开放成绩发布、教师任意 SQL、匿名 Agent HTTP、设备桶级下载、真实 AI 手写自动评分。OMR、双评和统计 API 只有通过其阶段门禁后才实现，不把设计路径写入当前客户端调用。
