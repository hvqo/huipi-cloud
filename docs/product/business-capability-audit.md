# 阶段 A：现有业务能力与差距审计

审计日期：2026-10-18。状态：**待独立审查，未实施阶段 B–E**。

## 1. 依据与证据等级

- **已由文档明确**：学校提供的[需求基线 1.0](requirements/school-exam-baseline-v1.md)，共 732 行，原文按字节保存；其自身为讨论基线，不代表学校已批准全部政策。
- **已由仓库证实**：本次读取的模型、路由、Service、迁移、测试定义及 GitHub 状态。测试定义证明已有用例，不能等同本轮重跑通过。
- **设计建议**：下列方案以及配套领域、权限、API、页面、路线文档；尚非运行合同。
- **需要学校确认**：名单来源、设备、匿名回避、双评公式、发布审批、保留期限、外传授权和容量目标。

完整交付入口：[阶段 A 评审索引](stage-a-review.md)。源文档 SHA 见索引。

## 2. Git 现场与推荐处理顺序

只读核查所得：

- 本地 `main`、`origin/main` 与 GitHub `main` 均为 `680421cb7502c8eed208c9b0a79a96ec6a087a8f`。
- 唯一 Open PR 为 [#31](https://github.com/hvqo/huipi-cloud/pull/31)，Draft、可合并，HEAD `4aed261fd3c119bcbef077afbab2561767283c8d`。backend、teacher-workbench 均 SUCCESS；运行 [39023224671](https://github.com/hvqo/huipi-cloud/actions/runs/39023224671)。这不代表新考试业务已验收。
- 接手分支 `feat/design-pilot-refresh` HEAD `1744eb3d2271a2c93861d0a0e2a53dfee181b3e9`，工作区干净；与 PR #31 的 merge-base 恰为该 PR HEAD，领先 7 个本地提交。远端未查询到同名视觉分支。
- 视觉提交依次为 `461f2d5`、`0047c57`、`d945f7b`、`8203bc4`、`72ab835`、`0bcf01b`、`1744eb3`；不能只 cherry-pick 第一条就视为保留最新设计。
- Zcode 的 `47156dd`、`c9af44b`、`39c4bd6`、`3c5579e` 都是 PR/视觉分支祖先；没有丢失。
- 保留 `backup/p3d1-deepseek-local-927dac2`、其他未合并分支及 `stash@{0}`（P3-D1 Quote 实验）；未修改 `.env`、模型或冻结评测。
- 本轮文档分支 `codex/stage-a-business-architecture` 从上述 main 创建，不包含 PR #31 或视觉分支业务改动。

推荐顺序：本轮文档独立评审；PR #31 另行完成业务/安全审查，用户决定是否合并；再将视觉分支按原祖先关系重新基于已合并前端集成，保留全部 7 个提交并重跑合同/浏览器检查。当前不存在需要强制整理历史的证据，但视觉分支与未来页面重构都触及导航、Service 和页面，必须安排合入顺序。**本轮不合并 #31，不重排视觉历史。**

main 只有基础 Vue 工作台；完整教师/管理员页面在未合并 PR 中。下文严格区分两者。[PR 前端合同](https://github.com/hvqo/huipi-cloud/blob/4aed261fd3c119bcbef077afbab2561767283c8d/docs/frontend/backend-integration-handoff.md)是审计证据，不是 main 上已上线能力。

## 3. 可复用能力

| 能力与分类 | 实际证据 | 可复用范围与限制 |
|---|---|---|
| 已实现可复用：模块化单体、PostgreSQL、S3 | [pyproject](../../pyproject.toml)、[会话](../../src/huipi_cloud/infrastructure/database/session.py)、[S3](../../src/huipi_cloud/infrastructure/storage/s3.py) | 请求会话、短事务、私有对象；不具备跨系统原子事务 |
| 已实现可复用：作业发布 | [模型](../../src/huipi_cloud/modules/assignments/models.py)、[Service](../../src/huipi_cloud/modules/assignments/service.py) | Assignment 仅 draft/published；每题答案、Rubric 总分门禁；不是考试状态机 |
| 已实现需扩展：单份文件接入 | [上传校验](../../src/huipi_cloud/modules/submissions/uploads.py)、[提交服务](../../src/huipi_cloud/modules/submissions/service.py) | PDF/JPEG/PNG、默认 20 MiB、分块暂存、SHA、补偿；不等于批次上传 |
| 已实现可复用：解析任务恢复 | [任务仓储](../../src/huipi_cloud/modules/parsing/repository.py)、[Worker](../../src/huipi_cloud/workers/parsing.py) | SKIP LOCKED、lease token、数据库时间、心跳、失败重试；后续新任务类型不能冒充 ParsingTask |
| 已实现可复用：结构解析与规范化 | [MinerU](../../src/huipi_cloud/infrastructure/parsing/mineru.py)、[Canonical](../../src/huipi_cloud/modules/canonical_documents/service.py) | 产物/资产检查、版本、SHA、页坐标；不能保证手写正确，没有 OMR |
| 已实现可复用：题目来源对齐 | [Alignment](../../src/huipi_cloud/modules/answer_alignment/service.py)、[协议](../../src/huipi_cloud/modules/answer_alignment/protocol.py) | 来源 pointer + Unicode 半开范围；aligned 不是已作答/可评分 |
| 已实现可复用：人工证据复核 | [Service](../../src/huipi_cloud/modules/answer_review/service.py)、[仓储](../../src/huipi_cloud/modules/answer_review/repository.py) | 父子节点归属、角色范围冲突、revision、幂等、来源门禁；不是身份确认或阅卷派单 |
| 已实现可复用：可信文本评分输入 | [GradeInput](../../src/huipi_cloud/modules/grading/service.py)、[注册](../../src/huipi_cloud/modules/grading/repository.py) | 人工 response_present、来源/答案/Rubric 快照、非文本拒绝；不能证明题目必需材料完整 |
| 已实现可复用：候选评分与人工修订 | [Proposal](../../src/huipi_cloud/modules/grading/proposal_service.py)、[人工复核](../../src/huipi_cloud/modules/grading/grade_review_repository.py) | Decimal、正分 Quote、完整修订快照、不可变链；不是有效考试题分或成绩发布 |
| 已实现可复用：可信身份与撤权 | [JWT](../../src/huipi_cloud/infrastructure/auth/jwt.py)、[授权](../../src/huipi_cloud/modules/identity/service.py) | RS256、issuer/audience、JWKS 有界缓存/轮换、数据库 User；不信任 JWT role |
| 已实现需扩展：管理员与审计 | [管理服务](../../src/huipi_cloud/modules/identity/admin_service.py)、[模型](../../src/huipi_cloud/modules/identity/models.py) | 账号登记/启停、作业授权、追加审计；仅 teacher/admin 两类 |
| 已实现需扩展：Copilot/RAG | [可信工具](../../src/huipi_cloud/modules/copilot/authorized.py)、[检索](../../src/huipi_cloud/modules/knowledge/retriever.py) | 受控 CLI/服务、固定 Assignment 工具、私有知识版本；无聊天/知识库 HTTP；默认词法，混合/精排可选 |
| 已实现需扩展：教师工作台投影 | [投影路由](../../src/huipi_cloud/modules/assignments/workbench_router.py) | 有界题目/Rubric 摘要，无 AnswerKey；不是全校待办/阅卷/学情聚合 |

## 4. 冲突、缺失与不可绕开的限制

| 问题 | 仓库证据 | 后续处理 |
|---|---|---|
| 与新业务冲突：同作业学生只能一份提交 | [Submission 模型](../../src/huipi_cloud/modules/submissions/models.py)：`UNIQUE(assignment_id, student_ref)`，文件/任务一对一 | 不删除唯一键；新答卷版本映射独立 Submission，旧学生字符串不升级成真实身份 |
| 与新业务冲突：全作业授权、admin 全局访问 | `identity.service.require_assignment` 跳过 admin 成员检查 | 新考试资源不继承该捷径；旧接口也需识别桥接资源后拒绝；见权限设计 |
| 与新业务冲突：自由文本班级/学生 | Assignment.grade_level/subject，Submission.student_ref | 不回填猜测组织；建立可信组织和参试快照 |
| 完全缺失：考试、场次、试卷版本、参试名单 | 20 个已加载 ORM 表中无对应实体 | 增量模块和 FK，不按班克隆题目 |
| 完全缺失：扫描批次、页面归组与身份核验 | 现有上传只产生 Submission/File/ParsingTask | 独立接入、分卷版本、未认领区、受控物化 |
| 完全缺失：答题卡 OMR | 无模板定位/涂卡状态模型 | 阶段 C 独立候选证据，不伪造 Canonical 文本 |
| 完全缺失：任务派单、双评/仲裁 | GradeProposal 只有模型候选索引；GradeReview 是针对 proposal 的修订 | 阶段 D 新 MarkingPlan/Task/Record，不用旧复核 revision 冒充一评二评 |
| 完全缺失：有效题分、成绩审批发布 | 34 个 OpenAPI 操作无此链路 | 阶段 E；缺页/缺评不补零，先不开放发布 |
| 完全缺失：设备身份、自定义岗位、匿名题级授权 | Principal 只有 user/issuer/subject/expiry/is_admin | 增加独立权限上下文和设备主体；不让模型选择身份 |
| 能力边界：没有教师要求材料声明 | GradeInput 检查当前已选证据，不能从题意确定缺图 | 后续 TeacherRequirement + SourceReadiness；来源不足与空答分开 |

## 5. 数据库与迁移证据

本轮加载 ORM 元数据得到 **20 表**；[持久化和 HTTP 清单](current-contract-inventory.md)逐表列明 FK、唯一键及全部 34 个操作。16 份迁移，单一代码 head `c72d9e14a510`，由 `uv run --no-sync alembic heads` 验证。**未连接开发库，部署库实际 alembic_version 未核查**；不能宣称开发库已经到 head。

重要历史约束：

- Question：`UNIQUE(assignment_id, question_number)`；AnswerKey：`UNIQUE(question_id)`；Rubric：`UNIQUE(question_id, sort_order)`；分数 `Numeric(8,2)`，题号/分值正数。
- ParsedArtifact：每 Submission/ParsingTask 唯一成功索引；补扫不能直接重用该 Submission 覆写已解析结果。
- Canonical：`(parsed_artifact_id, normalizer_version)`；Alignment：`(canonical_artifact_id, aligner_version, assignment_questions_digest)`。
- AnswerReview：`(submission_id, question_id, request_id/revision)` 和唯一 supersedes；GradeInput 同题 request_id；GradeProposal 全局 request_id；GradeReview 每 proposal request/revision 和唯一 supersedes。
- 迁移 [9d2a17c4e6b0](../../migrations/versions/9d2a17c4e6b0_authenticated_answer_review.py)、[de9f2b647a13](../../migrations/versions/de9f2b647a13_grade_review_revisions.py)、[c72d9e14a510](../../migrations/versions/c72d9e14a510_admin_operation_audit.py)包含防更新/删除触发器。新有效性索引应在旁表维护，不能修改追加历史。

## 6. HTTP 与身份审计结论

[总路由](../../src/huipi_cloud/api/v1/router.py)把业务路由放入统一 `authorize_request`。除存活健康检查，注册的业务操作均需认证与精确路径/方法白名单；新路径无默认授权。Question/Submission 向上找 Assignment，多 ID 路径要求同作业；GradeReview 额外核实 proposal 来源绑定。

`GET /assignments/{id}` 包含标准答案；原文件、Canonical、解析 Markdown、复核历史都不是匿名阅卷安全投影。**这些端点在旧粗粒度成员合同下受保护，但不足以向“只有一道题权限”的阅卷员开放。** 不能仅让前端隐藏姓名。

User 的 `issuer+subject` 唯一；身份不匹配、停用、撤权或 DB 故障拒绝访问。已有管理员降权/ORM 缓存刷新修复不可回退。当前 `is_admin` 仍具有全局教学读写语义；迁移前不把系统运维角色发给扫描员/阅卷员。

## 7. 前端能力和演示证据分层

- main：`frontend/src/App.vue` 内基础路由、作业/提交/作答复核；没有完整工作空间体系。
- PR #31：Vue Router、HuipiServices、Real/Mock Adapter、教师与管理员页面。真实授权/单份上传/来源与人工复核可调现有 HTTP。
- PR 的知识库、助教会话、教学统计、全局待办等存在 `UnsupportedCapabilityError`；Mock 展示不能算后端已实现。真实工作台和管理员计数只是有界资源组合，不是全量统计。
- 视觉分支：新增样板/品牌/浮动助教与布局调整，没有 Python/迁移变更。保持为外观工作，不据此声称扫描或考试存在。
- 新页面在后端合同实现前必须显示未接入，不在真实失败时退回 Mock；生成类型只来自对应版本 OpenAPI。

## 8. 已有回归证据与本轮验证边界

读取了这些真实测试定义，未运行它们/未操作数据库：

- [teacher_authorization](../../tests/integration/test_teacher_authorization.py)：所有旧敏感路由无 Token 拒绝、跨题/提交拼接、撤权、签名/role 伪造、DB 故障。
- [admin_role_http](../../tests/integration/test_admin_role_http.py)：全局 admin 兼容、降权竞态、ORM 缓存、审计原子性。
- [answer_review_http](../../tests/integration/test_answer_review_http.py)：Unicode、父子归属、原请求重放、并发 revision、写前撤权、不可变触发器。
- [grade_review_http](../../tests/integration/test_grade_review_http.py)：完整快照、来源过期、COMMIT 丢确认、权限锁与历史。
- [parsing_runtime](../../tests/integration/test_parsing_runtime.py)：行锁时间、旧 token、持续心跳、非协作取消、有界关闭。
- [grade_input](../../tests/integration/test_grade_input.py)：混合模态阻断、不完整来源、S3/PG 失败与未知 COMMIT。

本轮验证仅覆盖 Git、迁移图、静态 ORM/OpenAPI 导出一致性、文档覆盖和链接；不是新业务实现或真实学校试运行验收。未来测试计划见[验收矩阵](../evaluation/business-acceptance-matrix.md)。
