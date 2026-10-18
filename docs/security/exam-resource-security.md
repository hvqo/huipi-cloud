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

## 最小新授权基础

八张增量表：`security_installations`、`permission_definitions`、`security_scopes`、`security_role_versions`、`security_role_permissions`、`security_grants`、`security_audit_events`。

仅启用三个固定动作：

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

## 数据库与冻结准备

迁移 `e21a9b4c110a` 只新增分类和上述安全表/约束/触发器，不改历史迁移或业务 UUID。独立测试库从旧 head 注入真实合成提交、来源和审核图，核对逐表行数/摘要；upgrade/check/downgrade/再 upgrade 后保持一致。有保护资源、审计、Grant 或新 Scope/角色时拒绝破坏性降级。空新切片可降级是开发验证，不是已有新业务数据的生产回滚方案。

B-1.2 必须同时完成：PaperVersion 真外键及不可变版本、可信创建事务中关联保护 Assignment、Question/AnswerKey/Rubric/Assignment 的服务写门禁与 DB 触发器、CLI/内部写入覆盖、冻结/解锁审批与审计。不能把本切片的分类不可变误称为试卷内容已冻结。

## 验证证据

`tests/integration/test_resource_security.py` 使用真实独立 PostgreSQL、私有 S3、签名 JWT：旧合法文件可读；受保护整图仍在库但 HTTP/CLI/工具不可读；S3 下载调用不得发生；分页/聚合、伪造角色、未知分类、单 Grant 委派、停用撤权、锁等待、审计失败、未知 COMMIT、幂等和迁移往返都有断言。`tests/unit/test_resource_security_catalog.py` 防止漏登记新模型和新 HTTP 方法/路径。所有材料为合成数据；没有模型请求。

运维步骤见[安全部署指南](../development/resource-security.md)。
