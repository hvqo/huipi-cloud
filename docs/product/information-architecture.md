# 单校工作空间与前端信息架构

**设计建议，非已实现路由或 Vue 视觉稿。** 不改变 PR #31 或本地视觉分支。依据：[需求第 14 节](requirements/school-exam-baseline-v1.md)、[能力审计](business-capability-audit.md)。API 组编号见 [API 演进](../architecture/api-evolution-plan.md)。

## 1. 统一入口与导航原则

统一 OIDC 登录 → `/me/contexts` 取得允许工作空间及作用域摘要 → `/home` 我的工作台。一个人可多岗位，不再按 teacher/admin 二选一硬跳转。前端能力决定可见菜单，服务端仍逐请求授权。切换工作空间不改变身份、不增加权限。

固定外壳：工作空间切换、当前上下文（学期/场次/班级）、页面路径、待办通知、帮助/账号。业务区可有内容页、扫描多页工作区、匿名阅卷全高工作区，不统一套满屏卡片。未知统计显示未就绪；已加载页筛选明确为“本页筛选”。演示全局显著标识，真实模式不回退演示。

路径中的 Session/Paper/Task 来自服务端实体引用；班级筛选只改变当前集合，不复制考试/试卷。进入无权限深链返回不可访问，不自动展示同名其他资源。授权撤销时清空敏感视图/Blob 并保留未知提交查询入口（仍受权），不把之前加载的数据当永久许可。

## 2. 完整页面树

```text
/login · /auth/callback · /403 · /not-found
/home                              我的工作台
  /home/tasks                      全部待办（可筛任务类型/期限）
  /account                         个人资料与会话、可用岗位只读
/teaching                          教学空间
  /teaching/classes                我的教学班
    /:classId                      班级概览
    /:classId/homework              布置/收交/批改
    /:classId/exams                 历史考试（按当时班级快照）
    /:classId/analysis              学情与典型错误
  /teaching/homework                我的作业/布置情况
    /contents/:assignmentId        共享内容与题目
      /questions/:questionId       题目、答案与 Rubric（草稿）
    /offerings/:offeringId          本班布置与应交名单
      /responses/:responseId       多次提交、当前有效版本
      /responses/:responseId/review 作答复核及候选审核入口
      /analysis                    作业讲评/异常
  /teaching/materials              教学资料（未来受权 HTTP）
  /teaching/assistant              有界 AI 助教（未来受权 HTTP）
/exams                             考务空间
  /exams/events                    考试计划
    /:eventId                      多学科概览
  /exams/sessions/:sessionId        场次总览、按班筛选、进度/异常
    /papers                        关联共享试卷/评分策略
    /roster                        名单预检、锁版及更正
    /rooms                         考场/座位
    /exceptions                    错场、缺页、身份、缺评清单
    /results                       内部预览与来源完整性（E）
    /approvals                     审批/受控发布/更正（E）
  /exams/papers                    试卷库
    /:paperVersionId               原始题本、锁版记录
    /:paperVersionId/mapping       题序/A-B 卷映射与 Rubric 版本
    /:paperVersionId/layouts       完整卷/空白答题卡模板（C）
/scanning                          扫描空间
  /scanning/batches                当前批次/处理状态
    /:batchId/upload               文件接收与完整性
    /:batchId/pages                质检、旋转、漏页/重复
    /:batchId/split                人工分卷工作台
    /:batchId/identities           名单对照与身份冲突
    /:batchId/history              分卷/更正/操作记录
  /scanning/unclaimed              待归属批次、未认领答卷
  /scanning/scripts/:scriptId       完整页序版本与补扫/更正
/marking                           阅卷空间（D）
  /marking/tasks                   我的任务与领取
    /:taskPublicId                 连续阅卷、当前题组/轮次
    /:taskPublicId/history         自己可见的评分历史
  /marking/lead/sessions/:sessionId 组长进度工作台
    /plans                         按班/整卷/题组/混合计划
    /assignments                   派发、回收与异常任务
    /quality                       试评、抽检与复核
    /arbitration                   双评冲突/终评
/management                        管理空间
  /management/organization         学年学期/年级/行政班/教学班/学科
  /management/people               教师、学生必要身份与可信导入
  /management/accounts             OIDC 用户映射与启停
  /management/roles                模板、自定义岗位及版本
  /management/grants               资源范围、授权审批和撤销
  /management/devices              设备凭据、配额、指定接入范围
  /management/audit                管理和敏感访问审计
  /management/system               健康/容量/保留策略（不展示密钥）
```

这些路径是设计；旧 `/assignments` 等保持原合同，在新路由落地后有稳定深链适配，不立即删除。

## 3. 岗位菜单可见性

| 岗位 | 默认可见 | 有额外授权才显示 |
|---|---|---|
| 任课教师 | 我的工作台、教学班/作业、已允许的考试/班级分析 | 考务、扫描、跨班阅卷、发布 |
| 命题教师 | 工作台、指定试卷库/题序/评分标准 | 名单、原卷、成绩 |
| 年级/学科负责人 | 工作台、相应范围教学/考务进度 | 其他范围、身份详情、终评 |
| 考务管理员 | 考试计划/场次/名单/异常、授权扫描范围 | 全部题分编辑、正式发布 |
| 扫描员 | 批次上传/质检/分卷/未认领 | 身份核验名单、历史原卷、阅卷 |
| 阅卷教师 | 我的任务、连续阅卷、自己记录 | 组长派单/仲裁、身份揭示/整卷 |
| 阅卷组长 | 题组计划/派单/抽检/冲突 | 非授权题组、终评、成绩发布 |
| 成绩审核/发布 | 指定场次成绩检查/审批或发布页面 | 修改评分、另一个审批职能 |
| 运维管理员 | 管理账号/设备/系统及授权的审计 | 教学原卷/成绩、考务业务 |

角色并集只决定候选入口，真正操作还须对应资源 scope；不把一个岗位的动作与另一岗位的范围交叉放大。匿名模式在所有工作空间执行一致的隐私约束。

## 4. 页面目标、数据、操作和 API

表中 API 编号对应后端方案的路由族；不代表当前已经存在。目标和核心数据用于后续前端任务拆分，而非只加菜单。

| 页面/页面组 | 业务目标 | 主要操作 | 核心数据/领域对象 | API 组 |
|---|---|---|---|---|
| 我的工作台 | 今天先做什么 | 按优先级进入待办/异常/班级 | 受权 TaskSummary、活动场次/布置、班级问题、统计口径 | W、I |
| 全部待办 | 不中断后续事项 | 筛选/领取/跳转 | due_at、type、资源深链、阻断原因 | W、M |
| 账号 | 看可信岗位与会话 | 刷新/退出，申请访问 | CurrentUser、Context，不显示 OIDC sub | I |
| 我的班级 | 教学责任范围 | 选学期/教学班 | TeachingAssignment/TeachingClass | O |
| 班级概览 | 班级目前有什么问题 | 看未交/异常/重点题 | 名单、布置、成绩可见性、数据新鲜度 | H、R、W |
| 班级作业 | 收交/批改 | 布置、提醒线下处理、进入作答 | Offering、HomeworkParticipation、有效 Attempt | H |
| 班级考试 | 历史考试回溯 | 选考试时班级快照 | ParticipationSnapshot、已允许结果 | X、R |
| 班级学情 | 找讲评重点 | 题目/周期筛选、授权导出 | 有效分/样本数/缺失项，不含候选冒充正式分 | R |
| 我的作业 | 内容与布置分开管理 | 新内容、对多班布置、筛选 | Assignment + Offering，旧路径 legacy 标识 | H、L |
| 作业内容/题目 | 一份题目多班复用 | 草稿编辑、发布内容版本 | Question、AnswerKey、Rubric | L、H |
| 布置详情 | 看本次谁应交/已交 | 设置期限、名单、更正有效尝试 | Offering/Participation/Attempt | H |
| 学生作答/复核 | 看来源再判断 | 原件预览、证据核对、候选审核 | 当前 Attempt、Review/Proposal 版本 | H、L |
| 作业分析 | 讲评不完整数据不过度推断 | 缺失/典型错误筛选 | 有效审核数据、覆盖率、样本量 | H、R |
| 教学资料/助教 | 可信资料辅助教学 | 导入/检索/引用，限域对话 | 知识来源版本、受控 Agent scope | K（后续） |
| 考试计划 | 管理多学科考试 | 建计划、关联场次 | Event、学期/年级 | X |
| 场次总览 | 统一多班进度 | 班级视图、异常跳转 | Session、SessionPaper、RosterRevision | X、S、M、R |
| 关联试卷 | 确定使用什么卷/规则 | 选择已锁版本、允许 A/B | SessionPaper、PaperVersion、Policy | P、X |
| 参试名单 | 考生身份唯一可信 | 导入预检、冲突解决、锁版/更正 | Student、RosterSnapshot、Attempt | O、X |
| 考场座位 | 管理物理分布 | 分配/导出受限座位名单 | Room、SeatingAssignment | X |
| 场次异常 | 明确阻塞 | 转指定人员、批量确认已解决 | source_incomplete、identity_disputed、missing_mark | X、S、M |
| 成绩预览 | 先检查再审批 | 看缺评/失效、选快照 | ResolvedScore、Snapshot、缺考状态 | R（E） |
| 审批/发布/更正 | 分离责任、保留版本 | 审批、发布、申请更正 | Approval、Publication、Correction | R（E） |
| 试卷库/版本 | 多班多场次共享 | 导入空白题本、建草稿、锁版 | PaperVersion、canonical_assignment_id | P |
| 题序映射 | A/B 不靠题号猜 | 核对题目、等价组、Rubric | Mapping question_key、legacy_question_id | P |
| 页面模板 | 标准答题卡区域可靠 | 模板版本/定位/题区核验 | Layout、coordinate space、Region | P（C） |
| 批次列表/上传 | 明确接收任务 | 选择场次、上传、恢复 | Batch、File、Operation、配额 | S |
| 页面质检 | 页面清楚完整 | 旋转、标异常、查看原页 | ScanPage、派生图/变换/质量 | S |
| 人工分卷 | 少点击、多页可控 | 多选页、拆分/合并/重排、确认 | SplitPlanRevision、Allocation、ScriptVersion | S |
| 身份核对 | 不串人 | 查看该场次候选、核实/保持未认领 | roster、IdentityDecision、冲突原因 | S、X |
| 未认领区 | 不丢未知材料 | 指定场次申请、人工匹配 | 未归属 Batch、unclaimed Script | S |
| 答卷补扫/更正 | 留原件、修来源 | 加页/换页、查看受影响题 | PageRefs、source manifest、依赖 | S、M |
| 批次历史 | 可追溯操作 | 版本比较、查原请求结果 | Plan/Identity/Operation 审计 | S、I |
| 我的任务/领取 | 清楚本题职责 | 受限领取、继续、退回原因 | TaskPublicId、round、deadline | M |
| 连续阅卷 | 长时间高效、匿名 | 键盘给分、保存下一份、暂停 | 安全题区、Rubric、自己的记录 | M |
| 评分历史 | 自己的评分可追踪 | 查看完整快照/修订关系 | MarkingRecord、来源有效性 | M |
| 组长总览 | 发现积压 | 按题组/轮次看进度、异常 | Plan/Slot/Claim、统计口径 | M |
| 计划与派发 | 按班/整卷/按题混合协作 | 设范围/轮次/回避、派单/回收 | Rule、TaskSlot、authority | M |
| 质量抽检 | 校准/复核 | 样卷、抽检、退回 | SampleTask、独立记录 | M |
| 双评仲裁 | 差异解决 | 看允许的独立评/终评 | threshold、ResolutionPolicy、冲突 | M |
| 人员与组织 | 可信名册，不猜班级 | 导入、冲突核验、有效期 | Term/Class/Student/TeachingAssignment | O |
| 账号 | 外部身份内部登记 | 登记/启停，不改角色声明 | User 最小投影 | I、L |
| 岗位与授权 | 最小权限可解释 | 建版本、审批、授/撤范围 | RoleRevision/Grant/Scope | I |
| 设备 | 采集与人隔离 | 登记/轮换/撤销、看配额 | DevicePrincipal、UploadScope | I、S |
| 审计 | 管理可追责 | 有界筛选、看原因/版本 | 不可变 AuditEvent | I |
| 系统状态 | 运维而非全校答卷 | 看健康/容量/限额 | 技术状态，不展示密钥 | I |

## 5. 核心页面文字布局与流转

### 我的工作台

顶部：日期/自然问候、当前学期、一个首要动作。主体左侧：按期限/阻断排序的 3–5 项待办（未交、身份异常、待阅、待审批分组，可展开全部）；主体右侧：我负责的教学班/当前考试摘要。下方：班级/题目问题与小型学情概览，明确有效样本和缺失数据。异常不是绿色“已完成”；缺数据不伪造趋势图。教学统计不再是唯一分析入口。

### 场次总览

考试/科目/时间/卷别与状态 → 多班筛选（提示只是视图）→ 名单/接收/核验/阅卷进度（分别分母）→ 阻断异常 → Tab：参试/试卷/扫描/阅卷/内部结果。点击异常带资源 ID 跳扫描或阅卷空间，回跳保持筛选。

### 分卷工作台

左侧：虚拟化全部页缩略图，页序/质检状态、未分配计数。中间：当前页原图/变换、相邻页、框选多页、键盘移动。右侧：当前页组、场次卷别、名单候选与冲突、页完整性、确认按钮。底部：原请求进度/保存草稿/确认；批量确认显示可确认与被阻断数量，不把一页错人隐藏在成功提示中。

### 连续阅卷

顶部：匿名任务号、题组/轮次、进度、暂停。左侧：安全题区（分页/缩放，无整卷下载）；中部：题目与评分标准（可折叠）；右侧：Decimal 逐项给分、理由、需复核、保存下一份。双评不显示另一评。未知保存结果冻结当前输入，原 request_id 查询；409 刷新比对，不能自动移到下一卷。

```mermaid
flowchart LR
  Home[我的待办] --> Session[场次异常或进度]
  Session --> Split[扫描分卷]
  Split --> Identity[身份核对]
  Identity --> Pipeline[后台生成提交与解析]
  Pipeline --> Review[来源核验]
  Review --> Task[受权阅卷任务]
  Task --> Preview[内部成绩检查]
  Preview --> Gate[后期独立审批发布]
```

跨空间跳转在目标页重新授权。阅卷任务只携匿名 ID，不把真实学生上下文装进 query；不能从匿名任务“返回原提交”。

## 6. 旧页面逐项决策

| 当前 main / PR #31 页面 | 决策 | 兼容路径 |
|---|---|---|
| main App.vue 内联 Router / PR app/router | 扩展多空间导航，不整体重写登录 | `/login`、callback、safeReturn 保留；新 contexts 单独合同 |
| WorkbenchHome | 改造为责任驱动工作台 | 旧 `/workbench` 在新工作台就绪后受权跳转 `/home` |
| Assignments | 保留旧作业目录；逐步分离内容/布置 | 旧 `/assignments` 不解释成考试列表 |
| AssignmentDetail / QuestionEdit | 保留 legacy；新增锁卷编辑边界 | 旧 UUID/已发布只读不变，考试由 PaperVersion 门禁包裹 |
| SubmissionDetail | 保留日常作业；考试另建 Script 视图 | exam-protected 不通过旧详情预览 |
| AnswerReview | 保留证据选区组件与协议 | 新考试页面用安全 QuestionResponse Adapter，不直接拿整份 Canonical |
| GradeReview | 保留作业候选人工审核 | 不冒充 MarkingTask/正式成绩；新阅卷另建工作区 |
| Assistant / FloatingAssistant | 保留受控能力边界 | HTTP 未接入就不宣称可用；不能读取匿名任务身份 |
| Knowledge | 保留资料域，后续受权 HTTP | 不把学生答卷当知识材料自动索引 |
| Insights | 拆成班级/作业/场次分析 + 汇总入口 | 候选统计与有效/发布结果分开展示 |
| Settings | 合并账号入口及岗位只读信息 | 不把前端角色开关视为授权 |
| AdminHome | 改为管理事项入口 | 与教务/考务首页分开 |
| AdminTeachers | 保留账号映射，组织信息另页 | 用户 UUID/启停 API 继续兼容 |
| AdminAuthorizations | 旧作业授权保留兼容专区 | 新 grant/editor 明确资源类型与动作，无全局开关冒充题级授权 |
| AdminAudit | 扩展事件类型和范围 | 原管理事件仍可回查，不回写历史 |
| AdminSystem | 保留技术健康，分离业务信息 | health 仍是存活，不虚构服务就绪 |
| Forbidden / NotFound | 保留 | 不泄漏资源存在性/身份 |

## 7. 前后端实现顺序与验收

先批准领域/权限/API → B 后端迁移与合同测试 → 再生成 TypeScript → 新 Service Adapter/真实页面 → Mock 同合同合成资料 → 真 API 浏览器验收。不可先在 Mock 预造识别成功或发布成绩。

PR #31 与视觉分支先各自审查；本方案不要求回滚它们。保留 Unicode 选区、Blob 授权、request_id、expected_revision、来源摘要、Decimal 和 OIDC。服务未就绪的菜单由 capabilities 隐藏或明确标注，不靠虚构 HTTP 承诺填界面。
