# 单校业务重构：分阶段实施与验收路线

**设计建议，未执行 B–E。** 前置：[阶段 A 评审索引](../product/stage-a-review.md)、[ADR](../architecture/decisions/0001-school-exam-compatible-extension.md)、[验收矩阵](../evaluation/business-acceptance-matrix.md)。本轮只文档，不改正式评分协议、不升级开发数据库、不合并 #31。

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

### B-1.1 权限隔离与组织最小基础

- 前置：A 决策批准；学校确认可信名单责任人（可先合成数据）。
- 后端/迁移：固定 action 目录、最小 RoleRevision/Grant/Scope、考试资源分类 resolver、必要 Audit；Term/Subject/AdministrativeClass/Student 可信导入。复用 User/OIDC，不改旧 Principal 历史。
- 前端/API：I/O 中 `/me/contexts`、名单预检及只读组织；CLI 最先可用，暂不大改菜单。
- 权限：新 exam-protected 不继承 is_admin/AssignmentMembership；未知路由/资源拒绝。任意组合 scope 受服务端验证。
- 测试/验收：真实 PG 下旧作业授权不退化；同名学生不合并；scope 拼接/自行提权/撤权/DB 失败全拒绝；旧敏感路径对构造的保护对象无泄漏；迁移保全旧 User/Assignment。
- 回滚：禁新入口，撤新 grant，不回填旧班级；旧 admin 兼容仅对 legacy 生效。权限不完整不能开放后续桥接资源。

### B-1.2 考试场次、共享试卷与名单冻结

- 前置：B-1.1；组织导入与最小命题/考务授权。
- 后端/迁移：Event/Session/SessionPaper、PaperVersion/QuestionMapping/SourceFile、RosterRevision/Snapshot/Participation；PaperVersion 防原位变更；复合 FK/UNIQUE，保持所有旧表/UUID。
- 前端/API：P/X 中建考试、建场次、导入不含学生作答的原始题本/空白答题卡并人工核对、选已发布内容、锁卷、名单导入预检/确认、班级筛选；先有可执行 CLI/合同测试。
- 权限：paper.read/edit/lock 与 roster.read/lock 独立；命题人不自动获得考生资料。
- 测试/验收：2 班共用 1 Assignment/1 PaperVersion、另一 Session 复用同卷；同名/同考号冲突；同卷 A/B 映射不能错接；锁卷绕过旧 PUT 更新受拒；旧提交不自动成为考试参试记录。
- 回滚：关闭新 Session 创建，保留新表/已锁快照；历史 Assignment 无猜测回填。单切片迁移往返在独立库验证。

### B-1.3 独立大文件接收与人工页面处理

- 前置：B-1.2（也允许有权限的待归属区），S3 配额/临时磁盘方案确认。
- 后端/迁移：Batch/File/Page/UploadSession/part metadata、Operation、独立页处理 job；私有流式接收、SHA/签名/加密 PDF/资源限额，旋转保留变换；不接 OMR/自动身份。
- 前端/API：S 中 batch/uploads/parts/complete/pages/quality，批次上传和页预览/异常队列；无浏览器时受控 CLI 可验收接入。
- 权限：OIDC 扫描员仅指定 Session/Batch；设备功能仍关。
- 测试/验收：断传重试/同 part 冲突/超大/坏 PDF/页数/解码像素/未知 COMMIT、S3 故障；多班不要求 class_id；原件 SHA 不变；1600 页合成输入有界处理且不会撑满进程内存（实测后记录资源，未达则阻塞，不宣布 SLA）。
- 回滚：暂停 job/关闭 upload，保留原件和恢复会话；对账不删除未知 COMMIT 对象。独立 volume/test prefix，不清开发桶。

### B-1.4 人工分卷、身份确认与旧 Submission 原子桥接

- 前置：B-1.1 至 B-1.3、私有存储和解析基础；所有旧路径对新资源隔离已验证。
- 后端/迁移：SplitPlanRevision、AnswerScript/Version、PageRef/CurrentAllocation、IdentityDecision/CurrentParticipationScript、ExamSubmissionLink、物化 job；完整 manifest 和稳定幂等。
- 前端/API：S 中 split-plan/confirm/identity/remediation/operations，人工缩略图分组、姓名/考号候选对照、未认领与错误展示。第一版没有自动建议也可完成。
- 权限：split.edit/confirm 和 identity.verify 分开；对 Batch 所属场次/卷别二次核验，不能把候选自动视为身份。
- 测试/验收：混班/同名/漏页/重复页/错误场次/跨批次加页；并发确认只能占页一次；每单卷事务生成一个 Link+Submission+File+Task；未知 COMMIT 原请求恢复；旧列表/原件/Canonical/Review/Admin/Copilot 无匿名旁路。
- 回滚：冻结批次/当前指针，新事实不物理删除；原始 PageRef/文件/历史 Submission 不改。发生无法安全确认的身份时保留 unclaimed，不补占考生。

### B-1.5 首条闭环浏览器与旧链路验收

- 前置：B-1.4、已审核的完整前端基线。
- 后端/迁移：只补必要合同缺口，原则不增表；使用旧解析流程生成 Canonical/Alignment，人工复核可回溯单卷到批次页。
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

B-1.1～B-1.5 是先后可合并的小切片，合起来交付**一科场次、多班、一个共享卷版、无二维码批次 PDF、纯人工分卷与身份核实、独立 Submission 处理链**。推荐首先开 B-1.1 的后端 PR，不再争论核心对象；其验收不是“扫描业务已完成”。

输入：学校授权的合成名单（2 班、同名学生）、一个 published Assignment/锁卷快照、一个混排 PDF；操作员有指定场次扫描和核验权限。输出：已核实答卷版本、原批次页 manifest、每卷唯一旧提交/File/Task/Link、可查询原请求进度、不能越权的私有源读取。解析可继续既有 Worker；真实教学手写质量不是该合同验收结论。

明确不含：自动分卷/身份、设备 SDK、OMR、跨班阅卷派单、双评、有效成绩、发布、TeacherRequirement 完整工作流。只先预留这些必须的外键/版本身份，不提前建空业务框架。

## 4. 各阶段共同证据

- 真实独立 PostgreSQL/S3 的正/负向、唯一键、并发 Event/Barrier、故障注入和 COMMIT 恢复；禁止用 SQLite 或只有 Mock 报端到端成功。
- 迁移头唯一、upgrade/check/downgrade/re-upgrade、带旧 User/Assignment/Review 记录的演练；仅测试库，生产数据恢复策略单独审批。
- 前端锁依赖/类型/Lint/Vitest/生产构建/演示隔离、真实签名身份浏览器；匿名检查实际返回 JSON/图像/下载字节，不只截图。
- 初始 400 份/1,600 页合成压测记录峰值内存/磁盘/排队/失败恢复；不先承诺吞吐。真实设备质量、真实教师双评另行授权评测。
- 每 PR 检查 .env、冻结评分数据、模型缓存、未合并视觉分支/stash；不产生收费模型调用作为默认 CI。
