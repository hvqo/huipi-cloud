# B-1.1a：考试资源安全隔离

范围：安全分类、可信授权和旧入口隔离。这里的合成受保护 Assignment 是测试载体，**不是已上线的考试、试卷或扫描功能**。组织导入在 B-1.1b，冻结试卷和场次在 B-1.2。没有新增公开 HTTP 接口或前端页面。

## 已实施的数据边界

- `assignments.security_classification` 有非空默认值 `legacy_homework`；增量迁移将历史作业标为该类，不改历史 UUID、内容或审核记录。
- `exam_protected` 只经受控安全服务在首次 INSERT 时建立；创建、分类与审计同事务，不先创建普通作业再补分类，也不自动建立旧 Membership。
- `unclassified` 表示待确定，所有旧教学读取/写入都拒绝。其他值及 NULL 由数据库约束拒绝；应用只把精确的 `legacy_homework` 当作旧资源。
- 分类不可 UPDATE；受保护或待确定 Assignment 不可 DELETE。禁止降级后走旧管理员捷径。数据库所有者直接 INSERT 是可信运维能力，不是客户端可用接口。
- Submission、Question 及派生产物不复制易漂移的分类字段；过滤器沿真实外键回到 Assignment。带多个父级字段的索引必须逐个通过，不能只信冗余 assignment_id。

## 统一旧入口

`identity.service.require_assignment()` 先从数据库核对分类，再执行旧管理员/成员规则。`require_legacy_assignment()` 同时用于旧身份 CLI 的 grant/revoke；客户端 role、admin、scope、分类字段均不授权。

`infrastructure.database.base` 安装全局 `Session.do_orm_execute` 策略，适用于项目内独立创建的 AsyncSession，而非只作用于 HTTP 注入会话。`resource_security.visibility` 为已映射资源添加 legacy 父级 EXISTS 条件：

- Assignment、Question、AnswerKey、RubricCriterion。
- Submission、SubmissionFile、ParsingTask、ParsedArtifact。
- CanonicalArtifact、AnswerAlignmentArtifact、AnswerReviewDecision。
- GradeInputArtifact、GradeProposalArtifact、GradeReviewRevision、VisualEvidenceArtifact。
- KnowledgeDocument、KnowledgeChunk、AssignmentMembership、绑定 Assignment 的 AdminOperation。

过滤在 SQL 分页和聚合前执行，涵盖 ORM 实体、列投影、别名、子查询、CTE、UNION、批量 UPDATE/DELETE 和关系读取。没有公开的 execution_option 绕过开关。`Session.get()` 读取未缓存对象同样过滤；受控创建服务可能持有自己的新对象，不得将该实例转交旧业务入口。

**这不是 PostgreSQL RLS**。原生 SQL、Core Table 查询、已加载对象以及 ORM flush 不是通用安全沙箱；拥有数据库凭据的任意代码属于可信部署边界，不能向教师或模型开放。此次已逐条审计现有 Core/原生 SQL：安全服务只读分类/校验真实父键；作答复核 `decision_region_page()` 的原生 JSONB 坐标投影增加 ORM 可见性前置检查；其余业务原生 SQL只设置事务、锁或数据库时间。新增此类查询必须提供父级授权和回归测试。

## HTTP 调用清单

[机器可验证方法/路径清单](resource-security-paths.json)完整枚举当前注册 HTTP，单测与 FastAPI 路由比对；原始[身份权限矩阵](http-permissions.md)继续描述 legacy 行为。

- Assignment 列表、新建、详情、workbench：身份依赖与分类过滤；只有新建固定为 legacy。
- Question 创建、AnswerKey/Rubric 替换、发布：路径资源解析到父 Assignment；旧 service/repository 也只加载 legacy。
- Submission 上传/列表/详情/原件：授权在存储调用前执行。受保护资源与不存在资源统一安全 404，不包含标题、文件字节或 Key。
- ParsingTask、ParsedArtifact/Markdown、Canonical 状态/页：Submission 父级先过滤再读取索引和 S3。
- Alignment 汇总/题目、AnswerReview inspect/history/write：Submission 与 Question 双向归属，受保护对象不进入来源加载；原生区域投影也显式检查。
- GradeReview inspect/history/write：`authorize_proposal()` 核对 Proposal/GradeInput/Submission/Question；不可凭旧 admin 读受保护审核历史。
- 管理员成员管理、成员分页、审计分页：仍需现有数据库管理员，但受保护资源不在 legacy 列表中，不能给它授予有效旧成员访问。
- `/me`、教师账号管理：不读取考试教学资料；兼容能力 `assignment:access_all` **只表示 legacy**，不得据此开放考试。
- `/health`、OpenAPI/docs：仅公开存活及接口描述，无教学数据。未登记方法/路径仍默认拒绝。

目前没有 Canonical 素材单独下载、GradeInput 创建、GradeProposal 生成、知识检索或 Copilot 聊天 HTTP。不能把未注册的路径宣传成已保护的新 API。

## CLI、Worker 与内部调用

- 身份 CLI：grant/revoke 增加显式分类检查，不能插入受保护资源的旧成员。
- Assignment service 的锁草稿、Question/AnswerKey/Rubric 写路径：旧 ORM 加载不可见受保护对象；未来冻结状态需要进一步的 DB 写保护，不能只靠该过滤器。
- Parsing Worker：领取、续租、完成、过期恢复的 ORM Task/Submission 查询均受真实父级过滤；不把受保护合成任务交给旧 Worker。
- Canonical/Alignment/AnswerReview/GradeInput/GradeProposal/GradeReview/VisualEvidence：来源和索引加载受统一 ORM 过滤，现有事务、不可变触发器与 SHA 检查保留。
- `BusinessTools` 的四项业务工具，以及 `AuthorizedBusinessTools` 的可信 Principal 作用域：前者查询不到受保护父对象，后者另有每次当前身份/授权复核。
- Knowledge 导入/历史/撤销先核对真实 Assignment；Lexical/Hybrid/Rerank 检索及 citation 验证由源文档/Chunk 过滤，不按 S3 全桶枚举。默认检索仍 Lexical。
- JSON 导出只处理已取得的对象，不是任意数据库访问接口；私有对象存储凭据及直接对象读取仍是受控运维能力。

## 后续切片：受控考试处理通道（B-1.4/B-1.5 前置）

全局 legacy 可见性默认只允许 `legacy_homework`。因此未来考试答卷即使桥接为 `Submission`，旧 Parsing Worker、旧业务服务与旧 CLI 也同样无法领取、续租或完成 `exam_protected` 来源——**这是预期的安全边界，不是待修复的兼容缺陷**。不得用下面任一方式“解决”它：

- 把旧 Worker 或旧管理员提升为全局可见/全局绕过；
- 把考试资源重新标记回 `legacy_homework`；
- 增加任意业务代码可自行启用的通用安全绕过（execution option、环境变量、`include_protected` 参数、由调用方自报分类）。

B-1.4/B-1.5 必须新增**受控的考试解析处理通道**，要求：

1. 内部处理绑定**具体任务、真实资源和来源版本**（解析任务、答卷版本、页 manifest 与摘要）；领取、续租、完成各阶段分别复核，任务、来源版本或租约失效即拒绝。
2. 处理经过专用服务/专用 Worker 执行器与任务类型，不把考试任务塞回旧 `ParsingTask` 的旧查询路径；处理的产物索引仍由分类过滤保护，旧列表、旧文件下载、Canonical/Alignment/复核/工具入口继续不可读。
3. 通道本身是受控部署能力，不是教师或模型可调用的 HTTP 能力；其授权来自服务端任务意图，而不是 JWT 自声明角色或旧 `AssignmentMembership`。
4. 验收加入**考试来源的真实解析端到端测试**：受保护桥接 Submission 经受控通道完成解析，产物可被受控处理链读取，同时旧业务路径仍拒绝；该通道不在 B-1.1a/B-1.1b 实现。

## 最小新授权基础

七张增量表：`security_installations`、`permission_definitions`、`security_scopes`、`security_role_versions`、`security_role_permissions`、`security_grants`、`security_audit_events`。

仅启用三个固定动作（B-1.1b 追加四个组织动作，见下节）：

- `security.grants.manage`：安装级受控委派/撤销。
- `security.protected.create`：原子创建受保护载体。
- `security.classification.inspect`：检查分类元数据，**不能读取题目、学生作答或评分**。

`exam.session.read` 仅为禁用目录预留；即便数据库将其 enabled 改为 true，代码固定目录仍拒绝。没有 ExamSession/PaperVersion/MarkingTask 的真实表和归属校验，不能产生其 Scope 或执行能力。

Scope 只有两种：FK 指向唯一 `SecurityInstallation(id=1)` 的安装范围，或 FK 指向真实 Assignment 的范围；互斥 CHECK 与唯一约束拒绝未知类型、空父级及虚构 UUID。安装根是单部署技术安全根，不是多学校租户。Scope 不可原位改父级；本切片没有公开的范围编辑入口。

角色版本封存后不可改名、改版本、改权限；扩展须新版本。内置版本为安全操作员、受保护载体创建者和分类审计员。授权固定绑定真实 User、RoleVersion、Scope，身份不可修改，只允许首次撤销；重授须新 Grant。没有从 JWT role/is_admin 或旧管理员自动映射新 Grant。

委派时必须由**同一个有效 Grant**同时覆盖 manage、全部拟委派动作及完整目标 Scope，不能拼接多条 Grant 的动作/范围。禁止自授。角色、权限目录及 User 均从当前数据库读取；没有跨请求授权缓存。

## 事务、撤权与失败

- 写操作短事务重新读取启用 User，并持 SHARE 锁；当前 Grant 也持 SHARE，撤权 UPDATE 排队。已开始且持锁的合法操作可完成；撤权提交后新操作立即拒绝，不承诺在途全系统强制取消。
- actor/request_id 的事务 advisory lock 与唯一审计约束防重复执行；相同正文重放返回同一结果，不同正文 409。重放仍验证当前身份和权限。
- 变更与审计同事务，审计失败不留下 Assignment/Grant 半成品。审计 append-only，不保存 Token、学生正文、标准答案或私有 Key。
- 数据库异常返回安全 503；COMMIT 确认丢失报告 `security_commit_outcome_unknown`，同 request_id 可按当前权限重放恢复，不伪称失败已撤销。
- scope/role 输入无效为稳定 422，未知/未实现权限拒绝；业务资源缺失/旧路径越权统一 404。故障不会恢复旧 admin 后门。

### B-1.1a 合入时的已知限制

- **单 Grant 撤权语义（B-1.1b 已修）**：B-1.1a 的 `security_grants` 没有“同一 User/RoleVersion/Scope 仅一条有效”的部分唯一索引，用不同 `request_id` 重复授予同一组合会留下多条有效 Grant，`revoke --grant-id` 只撤销一条。B-1.1b 迁移 `b11b0a7c4e21` 增加 `uq_security_grants_active` 部分唯一索引（`WHERE revoked_at IS NULL`），`grant_role` 把重复授予稳定映射为 409 `security_grant_already_active`；升级前若已存在重复有效 Grant，迁移会拒绝并要求先人工清理。
- **审计 append-only 的应用边界**：`security_audit_events` 的触发器只覆盖 `UPDATE/DELETE`，不覆盖 `TRUNCATE`；它是应用业务角色边界，不是对持有数据库凭据的受控运维的防篡改审计。
- **ORM 可见性覆盖范围**：全局 legacy 过滤只保证经 `Session.execute`/加载器改写的语句；未来若引入单语句 `joinedload`、`from_statement` 或 `session.connection()` 原生读取，必须单独审计并补回归测试。

## B-1.1b：安装级组织与学生名册权限（最小过渡）

B-1.1b 在迁移 `b11b0a7c4e21` 中新增组织、学生与名册导入表（见[组织与学生名册](../development/organization.md)），**没有新增 Scope 类型**。本切片只启用安装级组织动作：

- `organization.read`：读取学年/学期、入学届别、学科、行政班、最小学生身份与班级归属历史。
- `organization.manage`：受控创建学年/学期/届别/学科/行政班与显式转班（当前仅受控 CLI）。
- `student.import.preview`：上传 CSV 并生成冻结预检批次；**不修改**正式学生或班级归属。
- `student.import.confirm`：在单一事务中重验当前学生/班级状态后正式入库。

约束：

- 这四个动作的 Scope 都只有安装级（单校部署技术根），**不是按年级/班级/考试的学校 RBAC**。组织实体 Scope 只有在真实 FK、归属校验与执行策略落地后才能逐项启用，不能提交自由 UUID/JSON 授权。
- 旧 `User.is_admin`、旧 `AssignmentMembership`、JWT 自声明 role 均不产生组织权限；组织数据不属于 legacy 资源图，也没有旧业务旁路入口。
- 新增角色版本：`organization_operator`（`11111111-b11b-4000-8000-000000000001`，包含 B-1.1a 三个安全动作与四个组织动作，是部署 bootstrap 可委派组织的根角色）、`organization_manager`（`...0002`）、`student_importer`（`...0003`）。B-1.1b 起 `deployment_bootstrap` 授予 `organization_operator`；已初始化的旧部署必须由受控运维显式补授，不得恢复旧管理员捷径。
- 普通任课教师查看自己的教学班名单**尚未实现**：缺少可信任教关系、班级 Scope 与名单范围授权，不能以全校学生接口替代。
- 所有组织写操作使用 `organization_operations` 追加审计与 `request_id` 幂等（相同正文重放、不同正文 409、COMMIT 未知返回 503 后同请求恢复）；变更与审计同事务。
- 组织 API 的机器清单在 [http-permissions.json](http-permissions.json) 与 [resource-security-paths.json](resource-security-paths.json) 中逐项登记；未登记方法/路径默认拒绝。

已知边界：B-1.1b 只有安装级组织权限，**不得**表述为已完成按年级/班级细分的学校 RBAC；学生登录认证、考试场次/试卷/参试名单/扫描/阅卷/成绩仍未实现。

## 数据库与冻结准备

迁移 `e21a9b4c110a` 只新增分类和上述安全表/约束/触发器，不改历史迁移或业务 UUID。独立测试库从旧 head 注入真实合成提交、来源和审核图，核对逐表行数/摘要；upgrade/check/downgrade/再 upgrade 后保持一致。有保护资源、审计、Grant 或新 Scope/角色时拒绝破坏性降级。空新切片可降级是开发验证，不是已有新业务数据的生产回滚方案。

B-1.2 必须同时完成：PaperVersion 真外键及不可变版本、可信创建事务中关联保护 Assignment、Question/AnswerKey/Rubric/Assignment 的服务写门禁与 DB 触发器、CLI/内部写入覆盖、冻结/解锁审批与审计。不能把本切片的分类不可变误称为试卷内容已冻结。

## 验证证据

`tests/integration/test_resource_security.py` 使用真实独立 PostgreSQL、私有 S3、签名 JWT：旧合法文件可读；受保护整图仍在库但 HTTP/CLI/工具不可读；S3 下载调用不得发生；分页/聚合、伪造角色、未知分类、单 Grant 委派、停用撤权、锁等待、审计失败、未知 COMMIT、幂等和迁移往返都有断言。`tests/unit/test_resource_security_catalog.py` 防止漏登记新模型和新 HTTP 方法/路径。所有材料为合成数据；没有模型请求。

B-1.1b 的组织/学生/导入证据见 `tests/integration/test_organization.py`、`tests/integration/test_student_import.py`、`tests/integration/test_organization_migration.py`：真实独立 PostgreSQL 覆盖学年学期边界、同名学生、学号唯一、归属排他约束与转班历史、并发入班、真实签名 JWT 的授权/撤权/停用、旧 admin 无新权限、重复 Grant 409、预检不污染正式数据、确认幂等/409/预检后数据变化、审计失败回滚、COMMIT 未知恢复与迁移往返；全部为合成数据，没有模型请求。

运维步骤见[安全部署指南](../development/resource-security.md)。


## B-1.2a：真实试卷 Scope

新增 paper.create（仅安装级）、paper.read/edit/lock（安装级或具有 PaperVersion FK 的 paper_version Scope），不扩大旧 Assignment Scope 或旧分类检查动作。新试卷仍被全局 legacy 查询隐藏，新服务仅在当前 Principal/Grant 校验后使用固定 Core 查询。没有通用 ORM 绕过开关或任意 SQL。

版本建立、专用载体、映射、Scope 和审计同事务；数据库/服务的冻结路径、受控部署代操作和权限边界见[试卷指南](../development/paper-versions.md)。普通 paper_editor/locker/reader 应绑定具体卷版，不拥有学生资料或名册权限；paper_operator 是经审批部署安全根，不是普通命题角色。未来 ExamSession/MarkingTask 仍未建立，其保留动作不可执行。原件上传不在本切片，冻结只覆盖实际已纳入的题目内容与映射。

## 考试计划与用卷授权

B-1.2b新增真实ExamEvent/ExamSession FK Scope，事件范围只有在数据库证明父子关系时才能覆盖场次。绑定要求单个Grant覆盖场次manage+bind，并独立检查指定PaperVersion的paper.use；读取卷版不能代替使用授权，场次管理也不能读取标准答案。无新的考试HTTP或Agent工具；旧接口继续默认隐藏exam_protected。具体CLI、事务和历史保护见[考试指南](../development/exam-sessions.md)。


## 参试名单安全合同

B-1.2c的exam.roster.read/manage/freeze/correct沿真实Event/Session Scope执行；旧admin不授权。更正要求同Grant完整覆盖manage+correct；名单读取单独授权。Participation身份、冻结Snapshot与追加审计由数据库保护；新版本原子切换current，不删除旧名单。没有新HTTP或Agent名单工具。详见[名单指南](../development/exam-rosters.md)。


## 扫描接收HTTP隔离

`scan.batch.create/read`、`scan.file.upload/read`只作用于真实安装/Event/Session Scope。扫描表独立于legacy图，不继承旧is_admin或AssignmentMembership。文件、页、下载均上溯同一Batch→Session；场次选择列表按当前Grant过滤，没有授权返回空列表。上传预登记和最终stored提交分别重新校验账号和Grant；撤权后重放也不返回原结果。受控部署扫描委派根不等于公共提权API。固定路由及调用入口已登记在两份机器权限清单中；详见[扫描接收](../development/exam-scan-intake.md)。

## 人工分卷与名单身份

`exam.script.read/edit/confirm`与`exam.identity.verify`复用真实Event/Session Scope；扫描旧角色不自动增加学生名单权限。确认同一Grant必须覆盖edit+confirm+identity.verify，另独立核对同场次roster.read。无名单权限的Script历史投影不返回学生或名单/Participation标识。页面仍需scan.file.read；没有Assignment/管理员回退。路由调用清单和OpenAPI已登记人工分卷八个操作，写入在存储核验后重复当前授权；详见[合同](../development/exam-answer-scripts.md)。


## B-1.4b考试专用任务

`exam.parse.start/read`只作用于真实installation/Event/Session。固定Job后端使用受控Core查询复核精确来源和当前发起者授权，复用Worker生命周期及MinerU算法；旧队列后端默认不变。ExamSubmissionLink的ORM可见性仍沿真实Submission父级过滤，受保护ParsedArtifact/Canonical也继续对旧服务隐藏。公开API不提供通用特权Session、Worker控制或自由对象Key。消费前严格重算Manifest摘要；物化、执行和完成的授权/版本/租约门禁见[专用合同](../development/exam-protected-parsing.md)。
