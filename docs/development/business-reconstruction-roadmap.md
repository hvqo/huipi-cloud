# 单校业务重构：分阶段实施与验收路线

## 当前纵向 MVP 优先级

阶段推进调整为「扫描接收与逐页预览 → 人工分卷及身份确认 → 受保护桥接现有解析/评分 → 教师复核」。先交付可操作的小批闭环，暂不以1600页、断点续传或完整扫描管理能力作为首版准入；不因此降低资源授权、名单历史或成绩发布门禁。

首片[扫描接收MVP](exam-scan-intake.md)提供真实Vue/HTTP、私有原件和页面索引，32MiB/50页，页面未确认，不创建Submission/ParsingTask。名单批量优化为独立工作，不依赖未经审查的Draft。后续分卷必须版本化引用稳定ScanPage和冻结名单，重用既有MinerU、GradeInput、Proposal与人工复核。尚未实现的扫描/OCR/阅卷阶段仍为规划，不以目录或演示代替业务验收。


**阶段 A 设计基线；实施状态按下文各切片标注。** 前置：[阶段 A 评审索引](../product/stage-a-review.md)、[ADR](../architecture/decisions/0001-school-exam-compatible-extension.md)、[验收矩阵](../evaluation/business-acceptance-matrix.md)。阶段 A 只交付文档；后续按独立切片实施，不改正式评分协议、不升级开发数据库、不合并 #31。

## 1. 集成顺序和代码基线

- A 文档从 main `680421c` 独立分支提交。PR #31 仍 Draft，视觉分支在其上另有 7 个提交。本方案不夹带前端历史。
- 新后端工作可以在 main 上增量推进；真正新前端从审核后的 #31/视觉集成版本继续，先重跑生成类型、OIDC/权限/Unicode/幂等浏览器用例。若 #31 尚未合并，暂用受控 CLI 验证后端，不复制第二套 Vue 实现。
- 每切片独立 PR，迁移只追加，不编辑已应用历史；迁移在独立测试库做 upgrade/check/downgrade/re-upgrade、旧种子数据计数/摘要对比。生产已有数据时优先回滚应用/关闭入口，**不通过 downgrade 删除业务数据**。
- 所有设计状态与 Mock UI 不能算实现证据。每 PR 写清已实现 API 和有意关闭的能力。

## 2. 分切片计划

每项按“前置；后端/迁移；前端/API；权限；测试/验收；回滚”给出，可独立审查/合并。表名以最终切片 Schema 审查为准，具体语义不得退化为字符串猜测。

### A-1 本轮：审计、合同与决策

- 前置：需求 1.0 原文、当前 Git 现场。
- 改动：仅产品/架构/API/权限/测试规划文档，无迁移/业务代码。
- 前端/API：页面树、DTO 草案；不生成未实现 API 类型。
- 权限：确定新考试拒绝旧整卷/admin 捷径、设备隔离、匿名和双评边界。
- 验收：原文完整+SHA、34 HTTP/20 表清单、T01–T30 全覆盖、文档链接、独立 Draft PR。
- 回滚：仅文档 revert；现有分支/数据不动。等待独立审查后才进入 B。

### B-1.1a 考试资源安全分类与最小权限基础

- 前置：A 决策有条件通过；不需要预建考试或组织实体。
- 后端/迁移：可信 Assignment 安全分类、固定 PermissionDefinition、最小角色版本/用户授权、具有真实 Assignment FK 的 Scope、授权变更审计；集中拒绝旧 API/服务对 exam_protected 的访问。历史作业保持 legacy 合同。
- 范围门禁：不存在 ExamSession/PaperVersion/MarkingTask 时，其动作仅预留且不可执行，不用自由 UUID/JSON 假装已有资源授权。未知分类/Scope/Permission fail closed。
- 前端/API：不改 Vue；现有 HTTP 合同保持兼容，不开放考试管理后台。
- 测试/验收：真实独立 PG 下旧管理员/成员兼容；受保护资源列表、分页、文件、Canonical、审核、Agent/Knowledge 全拒绝旁路；撤权、并发、幂等、审计失败与 DB 故障；迁移往返保历史。
- 回滚：停止新保护资源创建；保留已有分类/授权/审计，不能通过删除分类让数据退回旧 admin 路径。

### B-1.1b 可信组织、学年学期与学生导入（后端基础已实现）

- 状态：2026-10-18 在 `feat/b11b-school-organization` 实现后端基础，迁移 `b11b0a7c4e21`。本文首段「未执行 B–E」写于阶段 A；B-1.1a 已随 #33 合入 main，本切片同时交付导入服务、真实 PostgreSQL 集成/迁移测试与合同同步；B-1.2 起仍未执行。真实 PostgreSQL/S3 浏览器验收与真实学校数据试用不在本轮范围。
- 已实现（组织模型）：`AcademicYear`/`AcademicTerm`（学期必须落在学年内、同学年时间不重叠）、`GradeCohort`（年级按学年派生，不是学生永久属性）、`Subject`、`AdministrativeClass`（行政班，≠教学班/考场）、`Student`（`school_student_code` 唯一，姓名非键、不自动合并）。
- 已实现（归属历史）：`ClassEnrollment` 半开区间 `[valid_from, valid_to)`；GiST 排他约束保证同一学生同一时刻唯一行政班；转班关闭旧行 + 插入新行，历史触发器禁止删除/改写身份列与已设置 `valid_to`。
- 已实现（两阶段导入）：`StudentImportBatch`/`StudentImportRow` 冻结预检；三种模式（`student_only`/`enrollment_only`/`student_with_enrollment`）、UTF-8（含 BOM）、2 MiB/5000 行限制、行级 `row_number/field/code/message` 错误、预览不写正式名册；确认在单一事务内重验当前学生/班级/学年状态，支持同 request_id 幂等、409 冲突与 COMMIT 未知恢复；原始 CSV 字节不落库。
- 已实现（权限与合同）：固定动作 `organization.read/manage`、`student.import.preview/confirm`，**仅安装级 Scope**（不是按年级/班级的学校 RBAC）；旧 `User.is_admin`/AssignmentMembership/JWT role 不授权；迁移新增 `organization_operator`/`organization_manager`/`student_importer` 角色版本，bootstrap 改授 `organization_operator`，并补 `security_grants` 活动授权部分唯一索引（重复授予 409）；11 个组织 HTTP 操作进入固定策略表与 OpenAPI，组织基础字典写操作只经受控 CLI；生成的 TypeScript 合同只有新增、无既有操作漂移。
- 已实现（迁移安全）：`btree_gist`、学期/归属 GiST 排他约束、学期学年内触发器、预检批次/行与归属历史保护触发器、组织操作追加审计；存在新数据时拒绝破坏性降级。
- 测试验收：真实 PostgreSQL 集成/迁移测试与纯解析/领域单测定义见[组织与学生名册](organization.md#9-验证证据与测试状态)（`tests/integration/test_organization*.py`、`tests/integration/test_student_import.py`、`tests/unit/test_organization_*.py`）；组织/权限/迁移领域单测 23、真实 PG 组织/授权 22、独立迁移往返 1、导入子系统单元 63 与真实 PG 集成 26 均通过；统一门禁后端全量 977 passed/16 skipped、独立空库 upgrade/check/downgrade/再 upgrade 通过；不以 Mock/SQLite 替代真实数据库结论。
- 仍暂未实现：教学班/教学选课/任教关系及按年级/班级的组织 Scope；普通教师查看本班名单；组织/名册前端页面；学生登录。B-1.2c及后续仍需的 `PaperSourceFile`/`RosterRevision`/`RosterEntrySnapshot`/`ExamParticipation`/`ExamRoom`/`SeatingAssignment`，以及 `ScanBatch`/`ScanBatchFile`/`ScanPage`/分卷与身份/旧 Submission 桥接均未实现；阅卷、有效题分与成绩发布仍未实现。
- 学校待确认：可信名册来源/责任人、真实数据导入授权、字段最小集与学号规则、CSV 编码、转班生效与更正政策、组织操作员人选与离任撤权（见[组织与学生名册](organization.md#10-需学校确认事项)）。
- 回滚：停导入和新组织写入，保留原始导入审计与冻结批次；破坏性降级会被迁移拒绝，不猜测回填旧 `grade_level`/`student_ref`。

### B-1.2a 共享试卷版本与不可变锁卷

- 兼容扩展已在独立切片实现：PaperFamily/PaperVersion/QuestionMapping/Operation；授权的旧作业复制到同事务建立的专用 exam_protected Assignment，不复制成员/提交。真实 FK 的 paper_version Scope 与固定 create/read/edit/lock 动作；CLI/Service 受控执行，没有新 HTTP。
- 内容版次、并发 revision、来源/内容/题目/Rubric/映射摘要分开；草稿受控编辑，锁定后 Assignment/Question/AnswerKey/Rubric/Mapping 与卷版字段由数据库触发器保护；变化建立后继版本。
- 详见[试卷版本指南](paper-versions.md)。不以 published 作业当数据库冻结，不把 locked 试卷当成绩发布。
- 试卷题本 PDF、空白答题卡和版式文件拆为后续独立「试卷原件与版式」切片；当前没有 PaperSourceFile 或 PDF 导入，不能据此提前运行扫描。

### B-1.2b 考试计划、科目场次与共享用卷

- 已实施后端Service/JWT CLI：ExamEvent、ExamSession、SessionPaper、ExamOperation；真实Term/Subject FK与Event/Session Scope；显式A/B、跨场次同卷复用、退役禁止新绑定且保留历史。
- 准备完成只代表用卷配置，不代表名单/扫描/阅卷完成。不新增Vue/HTTP，不复制题目，不改变评分协议。
- 详见[考试场次指南](exam-sessions.md)，交付后等待独立审查，不自动进入下一切片。

### B-1.2c 参试名单与冻结（后端基础已实现）

- Service/JWT CLI已实现RosterRevision/RosterEntrySnapshot、ExamParticipation、考试日期学生/班级快照、考号、显式卷别与待核验、冻结及完整版本化更正；真实复合FK与数据库不可变保护，不覆盖历史用卷。详见[名单指南](exam-rosters.md)。无名单HTTP/导入前端、到考或扫描能力；新切片等待独立审查。
- PaperSourceFile/PDF/空白卡与版式仍是独立待开发切片；无上传功能不得宣称已支持。

### B-1.3 独立大文件接收与人工页面处理

- 前置：B-1.2（也允许有权限的待归属区），S3 配额/临时磁盘方案确认。
- 后端/迁移：Batch/File/Page/UploadSession/part metadata、Operation、独立页处理 job；私有流式接收、SHA/签名/加密 PDF/资源限额，旋转保留变换；不接 OMR/自动身份。
- 前端/API：S 中 batch/uploads/parts/complete/pages/quality，批次上传和页预览/异常队列；无浏览器时受控 CLI 可验收接入。
- 权限：OIDC 扫描员仅指定 Session/Batch；设备功能仍关。
- 测试/验收：断传重试/同 part 冲突/超大/坏 PDF/页数/解码像素/未知 COMMIT、S3 故障；多班不要求 class_id；原件 SHA 不变；1600 页合成输入有界处理且不会撑满进程内存（实测后记录资源，未达则阻塞，不宣布 SLA）。
- 回滚：暂停 job/关闭 upload，保留原件和恢复会话；对账不删除未知 COMMIT 对象。独立 volume/test prefix，不清开发桶。

### B-1.4 人工分卷、身份确认与旧 Submission 原子桥接

- 前置：B-1.1a、B-1.1b 至 B-1.3、私有存储和解析基础；所有旧路径对新资源隔离已验证。**同时必须具备受控考试解析处理通道**：全局 legacy 可见性默认拒绝 `exam_protected` 来源，旧 Parsing Worker 按设计不能处理考试答卷；不得通过恢复旧管理员全局访问、把资源改回 legacy 或通用绕过开关解决。
- 后端/迁移：SplitPlanRevision、AnswerScript/Version、PageRef/CurrentAllocation、IdentityDecision/CurrentParticipationScript、ExamSubmissionLink、物化 job；完整 manifest 和稳定幂等；专用考试解析任务/执行器，内部处理绑定具体任务、资源与来源版本（领取/续租/完成分别复核），产物索引继续受分类过滤。
- 前端/API：S 中 split-plan/confirm/identity/remediation/operations，人工缩略图分组、姓名/考号候选对照、未认领与错误展示。第一版没有自动建议也可完成。
- 权限：split.edit/confirm 和 identity.verify 分开；对 Batch 所属场次/卷别二次核验，不能把候选自动视为身份；受控解析通道是部署内部能力，不暴露给教师 HTTP 或模型。
- 测试/验收：混班/同名/漏页/重复页/错误场次/跨批次加页；并发确认只能占页一次；每单卷事务生成一个 Link+Submission+File+Task；未知 COMMIT 原请求恢复；旧列表/原件/Canonical/Review/Admin/Copilot 无匿名旁路；**考试来源真实解析端到端**（受控通道解析成功且旧路径仍拒绝）。
- 回滚：冻结批次/当前指针，新事实不物理删除；原始 PageRef/文件/历史 Submission 不改。发生无法安全确认的身份时保留 unclaimed，不补占考生。

### B-1.5 首条闭环浏览器与旧链路验收

- 前置：B-1.4、已审核的完整前端基线、受控考试解析处理通道已通过单独验收。
- 后端/迁移：只补必要合同缺口，原则不增表；使用受控解析通道生成 Canonical/Alignment（旧 Worker 不得直接处理考试来源），人工复核可回溯单卷到批次页。
- 前端/API：考试/批次/分卷/身份/处理状态，保存未知冻结与原请求查询；只读跳转不能借 Submission 深链泄露整卷。
- 权限：考务/扫描/教师/无权限账号真实签名身份分开验证；跨班按题阅卷未实现，保持关闭。
- 测试/验收：2 班、同卷同场、至少两学生同名、无二维码混排 PDF → 人工分卷 → 确认名单身份 → 每学生独立旧 Submission → 私有 PDF SHA/页序和 DB Link 一致 → 解析任务/来源可追溯；未认领/缺页不误写完整提交。再测第二场复用同卷不串场。
- 回滚：入口 feature flag，暂停物化；旧链路保持已确认对象可读于有权人员，不清库。**此处才是第一条完整新增产品业务验收，不是新成绩系统。**

### B-2 日常作业布置、有效提交和班级视图

- 前置：组织/身份及 B 来源桥；重交规则经学校确认。
- 后端/迁移：Offering/Participation/Attempt/current pointer、迟交/补交状态；旧唯一键不取消。
- 前端/API：H 的多班布置/应交名单/重交与版本/作业讲评，保留 legacy 旧作业入口。
- 权限：可信 TeachingAssignment/Offering scope，任教不能扩为跨班考试。
- 测试/验收：未交与来源缺失分开；2 班共享内容、有效版本原子切换；旧单份 409 行为继续。
- 回滚：停新布置，保留 attempts 与旧提交，不改原 student_ref。

### B-3 受限设备接入与自动建议（分别小 PR）

- 前置：纯人工闭环稳定、学校设备样本及隐私授权。
- 后端/迁移：DeviceCredential/UploadGrant；模板候选/身份候选独立记录。采集客户端/受控 SFTP 接收只映射已授权批次。
- 前端/API：设备登记轮换、候选审核；不自动确认。
- 权限：设备仅上传，不能看原卷/名单；候选不授予权限。
- 测试/验收：撤销/过期/错场设备令牌、离线续传；无码无身份信息保持拒识；真实设备小规模验收独立于合成合同。
- 回滚：撤设备凭据/关建议，保留人工操作链和原件。

### C-1 标准答题卡模板与坐标

- 前置：B、学校确认空白模板。
- 后端/迁移：Layout/Region/TransformRevision；题区与 question_key FK，配准证据版本。
- 前端/API：P 的模板区域编辑/审核、配准可视核验。
- 权限：模板编辑/锁版与原卷查看独立。
- 测试/验收：旋转/裁剪/偏移/A3-A4混排，未知变换 unresolved；不假造 Unicode pointer。
- 回滚：退人工区域核验，不改旧 Canonical 协议。

### C-2 OMR 和统一 QuestionResponse

- 前置：C-1、设备授权样本、OMR 阈值评测方案。
- 后端/迁移：OMR 候选、ResponseDependency、TeacherRequirement/SourceReadiness；证据查询适配器，不替换旧 JSON。
- 前端/API：逐题原图/填涂候选/人工判读/来源门禁。
- 权限：候选查看≠确认；关键缺图/模糊进入处理队列。
- 测试/验收：空涂、多涂、擦痕、缺区域不可直接零；完整卷和答题卡都可查真实来源；必须先过真实标注质量验收才允许确定性规则自动确认。
- 回滚：关闭自动判读，仅人工；历史候选及来源保留。

### C-3 精确失效传播与修复

- 前置：逐页/区域依赖可验证。
- 后端/迁移：SourceEpoch/CurrentValidity、依赖事件；未完成计算时先阻断 affected/unknown，而不是短暂维持有效分。
- 前端/API：补扫影响预览、确认、待核验列表。
- 权限：修复/身份改判原因、受限批准。
- 测试/验收：补一页只失效可证明相关题；不确定则整卷隔离；旧记录不可改；Rubric/身份变更单独传播。
- 回滚：保守全部相关来源待复核，不伪造“未受影响”。

### D-1 单评任务、匿名安全题区与人工记录

- 前置：B/C 来源就绪；匿名裁剪无泄漏验证。
- 后端/迁移：Plan/GroupRule/Task/Slot/Claim/MarkingRecord/CriterionMark；DB token fencing、授权锁、单评有效分准备。
- 前端/API：M 中任务/领取/连续阅卷、组长按班/整卷/题组/混合派单。
- 权限：按 task+round+group，只发经核验题区；旧所有路径绕权负测。AI 辅助仍仅候选，默认严格旧评分合同，Quote v2 不转生产。
- 测试/验收：抢单、撤权/过期提交、重派、未知 COMMIT、整卷/其他题/身份字节隔离；人工分数全快照审计。
- 回滚：停领取，已记录只读保留；不将待评变零分。

### D-2 双评、仲裁、抽检（逐子能力 PR）

- 前置：D-1、学校独立性/阈值/合分/回避政策签字。
- 后端/迁移：IndependenceGuard、Round/ResolutionPolicy、抽检任务和仲裁记录；不复用 GradeReview revision 冒充双评。
- 前端/API：双评隐藏另一评，组长抽检/差异、仲裁界面。
- 权限：同人两轮拒绝，自班回避按学校策略；组长不默认能改别人记录。
- 测试/验收：两任务并发同人领取 guard 锁；差异超阈值不出有效分；规则变更保已完成记录。
- 回滚：暂停合分/仲裁，保留全部独立记录，不能自动降成单评发布。

### E-1 有效题分与内部成绩快照

- 前置：D、选做/缺考/补考/舍入政策明确。
- 后端/迁移：ResolutionInput/ResolvedScore/Snapshot/Item；完整名单、源和必要题覆盖检查。
- 前端/API：R 内部成绩预览、缺评/失效原因及班级历史分析。
- 权限：结果预览范围，不等于发布。
- 测试/验收：缺题、错误版本、超分、未仲裁、缺考、错误选做规则全部阻断；Decimal 精确。
- 回滚：停快照创建，保旧快照，始终显示未发布。

### E-2 审批、受控发布和更正

- 前置：E-1、学校审批人/发布范围/保留/申诉流程确定，安全与部署审查。
- 后端/迁移：Approval/Publication/Correction，双人审批策略与完整审计。
- 前端/API：独立审批/发布，正式更正而非覆盖分数。
- 权限：自批自发拒绝、受控导出、小群体隐私检查；学生/家长端另立需求。
- 测试/验收：旧授权撤销、来源晚变、并发批准、发布后复查；新快照/发布链完整可回放。
- 回滚：撤当前可见性，保历史 Publication；禁止 downgrade 删结果。

## 3. 第一条开发闭环的精确边界

B-1.1a、B-1.1b～B-1.5 是先后可合并的小切片，合起来交付**一科场次、多班、一个共享卷版、无二维码批次 PDF、纯人工分卷与身份核实、独立 Submission 处理链**。推荐首先开 B-1.1a 的后端 PR，不再争论核心对象；其验收不是“扫描业务已完成”。

输入：学校授权的合成名单（2 班、同名学生）、一个 published Assignment/锁卷快照、一个混排 PDF；操作员有指定场次扫描和核验权限。输出：已核实答卷版本、原批次页 manifest、每卷唯一旧提交/File/Task/Link、可查询原请求进度、不能越权的私有源读取。解析可继续既有 Worker；真实教学手写质量不是该合同验收结论。

明确不含：自动分卷/身份、设备 SDK、OMR、跨班阅卷派单、双评、有效成绩、发布、TeacherRequirement 完整工作流。只先预留这些必须的外键/版本身份，不提前建空业务框架。

## 4. 各阶段共同证据

- 真实独立 PostgreSQL/S3 的正/负向、唯一键、并发 Event/Barrier、故障注入和 COMMIT 恢复；禁止用 SQLite 或只有 Mock 报端到端成功。
- 迁移头唯一、upgrade/check/downgrade/re-upgrade、带旧 User/Assignment/Review 记录的演练；仅测试库，生产数据恢复策略单独审批。
- 前端锁依赖/类型/Lint/Vitest/生产构建/演示隔离、真实签名身份浏览器；匿名检查实际返回 JSON/图像/下载字节，不只截图。
- 初始 400 份/1,600 页合成压测记录峰值内存/磁盘/排队/失败恢复；不先承诺吞吐。真实设备质量、真实教师双评另行授权评测。
- 每 PR 检查 .env、冻结评分数据、模型缓存、未合并视觉分支/stash；不产生收费模型调用作为默认 CI。
