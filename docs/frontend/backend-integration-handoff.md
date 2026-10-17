# 教师端与管理员端：后端对接合同

## 运行与隔离

前端位于 `frontend/`，沿用 Vue 3、TypeScript、Vite、Element Plus 和原有 OIDC Code + PKCE。

```bash
npm ci --prefix frontend
npm run dev:demo --prefix frontend
```

浏览器打开终端显示的本地地址，选择“教师演示”或“管理员演示”。演示不需要 PostgreSQL、OIDC、S3 或模型；只使用合成资料，不产生收费调用。不要上传真实学生资料。

真实模式使用 `npm run dev --prefix frontend`，按 `frontend/.env.example` 配置公开的 OIDC 客户端参数与代理地址。生产使用 `npm run build --prefix frontend`，由同源反向代理承载 `/api/v1`，并配置 SPA history fallback。`VITE_*` 是公开构建配置，禁止放 API Key 或 Client Secret。缺少真实身份配置时不允许匿名进入。

只有 Vite 的 `serve --mode demo` 会开启演示分支。普通生产构建及 `vite build --mode demo` 均关闭演示；构建后扫描种子与 Mock 标记。真实接口失败不会回退到演示数据。

## 页面与服务入口

- `/workbench`：教师工作台，任务入口、提交与解析摘要、最近操作。`reviews.digest`。
- `/assignments`：作业分页、当前页搜索/筛选、新建草稿。`assignments.list/create`。
- `/assignments/:aid`：题目、评分细则、发布检查、学生提交分页、逐文件上传。`assignments.detail/workbench/publishCheck/publish`、`submissions.list/create`。
- `/assignments/:aid/questions/:qid`：题目、标准答案和完整 Rubric 编辑；`new` 为新增。已发布作业只读。`assignments.addQuestion/updateQuestion/replaceAnswerKey/replaceRubric`。
- `/assignments/:aid/submissions/:sid`：受保护原文件、任务状态、标准化页面、逐题导航。`submissions.*`、`reviews.progress`。
- `/assignments/:aid/submissions/:sid/questions/:qid/review`：三栏作答核对、证据选区、判断、历史、冲突及未知提交重放。`reviews.inspect/save/history`。
- `/assignments/:aid/submissions/:sid/questions/:qid/grading`：候选列表、来源及逐项分数、接受/完整修订/拒绝/重评、只读历史。`grading.list/inspect/save/history`。
- `/assistant`：会话、检索状态、回答、原文引用、停止、重试和删除会话。`assistant.*`。
- `/knowledge`：分类、上传、作业关联、索引状态、详情、检索、删除确认。`knowledge.*`。
- `/insights`：时间范围、提交与核对趋势、作业工作量。`insights.summary`，不包含正式成绩排名。
- `/settings`：当前用户、会话刷新、退出与只读权限说明。`me.current`。
- `/admin`：教师账号与近期管理操作概览。`admin.overview`、`system.modules`。
- `/admin/teachers`：教师分页、当前页筛选、登记、停用/启用、授权详情。`admin.teachers/register/setActive/memberships`。
- `/admin/authorizations`：教师选择、只读/可写授权、权限变更、撤销、历史入口。`admin.assignableAssignments/memberships/grant/revoke`。
- `/admin/audit`：只读分页、教师/作业筛选、当前页日期/操作筛选、事件详情。`admin.audit`。
- `/admin/system`：健康与只读部署说明。`system.modules/health`，不提供配置写入、密钥读取或管理员晋升。
- `/login`、`/auth/callback`、`/403`、未找到页面：身份入口及明确失败状态。

## 数据层与稳定 DTO

页面只通过 `src/services/index.ts` 取得 `HuipiServices`。合同在 `src/services/types.ts`；真实实现位于 `services/real`，演示实现位于 `services/mock`。受保护请求由 `api/client.ts` 统一发送。已存在的 DTO 直接引用 OpenAPI 生成的 `api/generated.ts`，禁止手写复制后端结构。

缺口 DTO：`WorkbenchDigest`、`QuestionReviewProgress`、`GradeProposalSummary`、`KnowledgeDocSummary/Detail`、`RetrievalHit`、`AssistantConversation/Message`、`InsightsSummary`、`AdminOverview`、`SystemModuleStatus`。它们是前端合同建议，不代表已有 HTTP 端点。真实管理员用户无可信姓名时显示内部 ID 的短摘要，不伪造姓名。

候选检查只额外允许可选的 `candidate_rationales` 展示摘要：目前仅演示实现提供。真实后端不返回模型理由正文时明确说明，不能把引文当作模型理由。

## 已存在的 HTTP 能力

- `GET /api/v1/me`：当前数据库角色、能力及用户状态。
- `GET/POST /api/v1/assignments`、`GET /assignments/{id}`、`GET /assignments/{id}/workbench`、`POST /assignments/{id}/publish`。
- `POST /assignments/{id}/questions`、`PUT /questions/{id}/answer-key`、`PUT /questions/{id}/rubric`。
- `POST/GET /assignments/{id}/submissions`、`GET /submissions/{id}`、`GET /submissions/{id}/file`。
- 提交下的 `parsing-task`、`canonical-document`、`canonical-document/pages/{page_number}`、`answer-alignment` 及逐题 alignment 只读端点。
- `GET/POST /submissions/{sid}/answer-reviews/questions/{qid}`、`GET /submissions/{sid}/answer-reviews?question_id={qid}`。
- `GET /submissions/{sid}/questions/{qid}/grade-proposals/{pid}/review` 及 `GET/POST /submissions/{sid}/questions/{qid}/grade-proposals/{pid}/reviews`。
- `GET/POST /admin/teachers`、`PATCH /admin/teachers/{uid}`、`GET /admin/teachers/{uid}/memberships`、`GET /admin/audit-events`。
- `PUT/DELETE /assignments/{aid}/members/{uid}`。
- `GET /api/v1/health` 只表示存活，不证明数据库、对象存储或模型就绪。

准确方法、响应类型和参数以生成的 OpenAPI 为准。发布检查通过现有作业详情在客户端计算；服务器仍对发布进行最终校验。多文件上传是多次独立的单文件事务，逐文件显示结果，不伪称批量原子操作。

## 尚无 HTTP 支持：真实模式明确限制

- 题目正文更新、作业删除：不能伪造成功，真实模式仅提供既有新增、答案和 Rubric 接口。
- 工作台全局聚合、逐题核对进度：真实首页仅组合有限页现有资源，显示范围；不推断全量统计。
- 评分候选列表：真实审核可通过已有的 `?p=<proposal_id>` 打开明确候选；未知 ID 时显示缺口。生成候选仍由受控后台流程完成。
- 知识库 HTTP 管理/检索、助教会话 HTTP、教学统计聚合：真实模式显示“暂未接入”，不会执行现有 CLI 或自造 HTTP 路径。
- 服务依赖健康、模型配置、全局授权数：未提供时显示未知；管理员概览计数明确为当前加载页。
- 全局作业/教师搜索和审计日期/操作筛选：后端没有对应参数，现有筛选只作用于已加载页，保留分页。

## 安全、来源与写入约束

- `/me` 决定入口与导航。管理员路由进入前刷新用户状态；前端守卫不是服务端权限控制。真实后端逐请求执行签名、账号状态和作业成员校验。当前管理员仍具有全局教学资料访问能力，不宣称已拆分教务管理与资料读取权限。
- 作业 ID 来自受控路由；读取 Submission 后检查其 Assignment。Question 同样校验归属。客户端 role/teacher_id 不作为授权依据。
- 原文件通过带 Bearer 的 fetch 读取 Blob，再建立当前页面对象 URL；不把 Token 放 URL 或持久化。关闭/卸载撤销对象 URL，取消请求，使用大小限制。
- 作答证据按完整 ContentNode pointer 和半开 Unicode 码点范围保存。图片整节点选择须依据服务端 evidence，不能根据共享 Block 放开选择。来源与 revision 保存前重新检查。
- `response_absent` 不是自动判零；`uncertain` 不是已确认作答；alignment 也不代表可评分。
- 评分用十进制字符串和整数百分位运算，保存完整 Criterion 快照。`changed` 相对于原始候选。新增正分引文必须通过返回的 GradeInput 映射转换，不能把 Canonical 偏移直接当评分片段偏移；不凭未知节点生成 segment 0。
- 接受、修订、拒绝及重评不发布正式成绩。候选、人工评分审核、作答存在性是分开的事实。
- 401 退出本地认证状态；403/404 不绕过；409 刷新重新核对；422 保留可更正输入；413/415 显示文件限制；503/网络中断保留原 request_id 和原始正文，人工触发原请求重试，不生成新请求。管理写入结果未确认时冻结其他管理写入，保留原操作闭包（当前页面内存；刷新前须核对审计）。评分未知提交同时保存原候选目标，刷新后仍绑定原目标，不能切换候选后误重放。
- 助教消息、知识片段、题干均按文本显示，不用 `v-html` 执行资料中的 HTML/脚本。

## 演示状态与局限

共享状态存于 `huipi.demo.state.v3`，演示身份单独存于 sessionStorage。上传原文件存于独立 IndexedDB `huipi-demo-files-v2`，刷新后可回读；顶部“重置”只清理演示状态与该对象库，不碰用户 `.env`、真实数据或身份服务。不同浏览器/标签页并不具备跨端事务一致性，这不是生产授权实现。

演示解析和 PDF/DOCX 索引只是合成状态迁移，明确不是 OCR、MinerU 或 LLM；TXT/Markdown 按受限原文分段。检索为中文滑动二元词和英文词的包含计数，分数为演示相关性，不是 BM25、校准置信度或教学评分。助教根据命中片段生成本地合成回答，无命中明确说明缺少资料。真实模式不沿用这些回答。

## 建议的后端开发顺序

1. 增加受授权的候选列表与逐题审核进度、分页聚合；复用现有不可变索引，不更改评分合同。
2. 提供知识材料 HTTP 导入/列表/版本/撤销/检索，沿用已有服务的 Assignment 作用域、来源 SHA 与资源限制。
3. 将可信 Principal 传给受控 Agent，提供有界会话与工具轨迹 HTTP；引用必须程序校验，补鉴权、并发、费用与取消边界。
4. 提供带口径定义的工作台/统计聚合与全局搜索；不把候选分数当正式成绩。
5. 讨论题目修改与发布后版本策略，再开放更新接口。作业删除须定义软删除、留存、关联文件与审计，不直接增加危险按钮。
6. 最后完善管理健康与策略展示；密钥继续留在部署环境，管理员晋升仍走受控初始化流程。

## 验证与截图

执行方式、真实测试依赖及当前验证记录见 [前端验收](validation.md)。截图均为合成演示，位于 [截图目录](screenshots/)。本轮不修改后端业务、数据库迁移、评分协议或冻结评测数据。
