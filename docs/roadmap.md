# 项目路线图

本页按项目阶段概述已具备能力和后续计划。评测基线与逐案例结果见[评测文档](evaluation/README.md)，不在本页维护单次运行数据。

## P0：工程骨架与基础设施

- 已合并：Python 3.12、uv、src 布局、FastAPI、Pydantic Settings、日志、健康检查、pytest 和 Ruff。
- 已合并：PostgreSQL Async、Docker Compose、Alembic、真实 PostgreSQL 集成测试和 GitHub Actions。
- 已合并：本地 S3 兼容对象存储、私有测试 bucket 和文件适配层。
- 未来：生产配置校验、监控、日志关联和部署流程。

## P1：作业提交、原始文件存储、任务管理

### P1-A：核心数据模型与教师作业管理

- 已合并：Assignment、Question、AnswerKey、RubricCriterion、草稿编辑、发布完整性检查和作业管理 API。

### P1-B：学生提交与原始文件存储

- 已合并：模拟学生标识、单文件 PDF/JPG/PNG 上传、文件大小和签名检查、SHA-256、私有对象存储、提交查询与流式下载。
- 已合并：同一模拟学生对同一作业只能提交一次；Submission、SubmissionFile 和 `pending` ParsingTask 在同一个 PostgreSQL 事务中创建。
- 已合并：数据库失败后的对象补偿删除和 COMMIT 结果不确定时保留对象；进程崩溃仍可能留下孤立对象。
- 未实现：真实学生身份、重新提交版本、对象对账清理。

## P2：OCR/MinerU 多模态解析与题目结构化

### P2-A：可靠解析任务执行框架

- 已合并：`pending`、`running`、`retry_wait`、`succeeded`、`failed` 状态机；数据库约束和唯一提交任务约束。
- 已合并：PostgreSQL `FOR UPDATE SKIP LOCKED` 领取、短事务、lease token、heartbeat、到期恢复、有限次数和有上限的指数退避。
- 已合并：FastAPI 之外的独立 Worker 进程入口、安全状态查询 API、ParserExecutor 协议和 SIGTERM/SIGINT 关闭处理。
- 已合并：真实 PostgreSQL 并发和租约集成测试。测试使用专用的成功/失败执行器验证任务框架。

### P2-B：真实 MinerU 文档解析与解析产物持久化

- 已实现：独立安装 MinerU 4.x Basic/ONNX 本地运行时，受监督子进程解析 PDF 和图片；MinerU 模型不装入应用 `uv` 环境。
- 已实现：分块读取原始提交、校验 SHA-256、PDF 页数预检、归档资源上限、路径防护和按 MinerU 4.x 输出合同校验 MiddleJson。
- 已实现：私有 S3 兼容存储保存原始 ZIP、Markdown、MiddleJson、StructuredContent、图片素材及 manifest；PostgreSQL 只索引元数据、hash 和对象 Key。
- 已实现：成功状态和 `ParsedArtifact` 索引由一个 lease-fenced PostgreSQL 事务写入；提供安全产物摘要和 Markdown 流式读取 API。
- 有 opt-in 真实 MinerU + PostgreSQL + S3 端到端测试，覆盖合成文本 PDF、扫描 PDF 和 PNG；默认 CI 不下载模型，因此真实解析用例需在配置模型的环境中执行。合成样本通过不能证明真实教学数据的识别准确率。
- 配置的 S3 bucket 必须与原始 SubmissionFile 记录一致；单桶配置切换不迁移文件，任务会以安全的永久配置错误停止。
- `PARSING_WORKER` 每个进程一次只领取一个任务；多进程并发、PDF 页数与产物大小上限不是硬内存/CPU 配额。部署时应设置主机/cgroup 限额并先测量峰值 RSS。
- S3 上传取消不能终止已运行的 boto3 同步线程；数据库不提交旧 lease 的产物索引，但对象可能孤立。当前未实现对象对账清理。
- 未实现：对象写入的同一任务去重、孤立解析对象的对账清理、题目切分/题号识别、人工纠错和解析质量运营流程。

### P2-C：Canonical Document 标准化与持久化

- 已合并：独立的 Canonical Document v1 DTO 和转换器，读取已经成功并通过 P2-B 索引的 MinerU 4.x MiddleJson 2.0 与 assets manifest。
- 已合并：保留原始页码、顶层 Block index、文档级确定性 reading order、BBox 坐标语义、文本/公式/表格/图片/布局类型及嵌套来源结构；同时保留 MinerU 根级元数据、完整合法素材清单和可校验的内嵌图片来源引用。
- 已合并：稳定 UUIDv5、键排序 JSON、字节/节点/深度上限、SHA-256 校验；Canonical JSON 放私有 S3，PostgreSQL 只保存轻量索引和安全失败码；并发注册使用幂等唯一约束。
- 已合并：受控 CLI、状态摘要和按页 API。页面 API 默认文档上限 32 MiB、硬上限 64 MiB，读取后校验 SHA-256 和完整结构。真实 MinerU E2E 使用合成文档和独立 Basic/ONNX 环境，不代表真实教学数据准确率。
- 已知边界：DB 登记失败或并发输家可能留下不可变孤立 Canonical 对象；当前无对账清理器。外部图片不联网读取，尚无图片素材读取 API；当前接口没有认证和租户权限，不可直接公开部署。页面 API 的字节限制不是解码后内存上限。

### P2-D1：题号候选识别、学生答案区域切分与作业题目对齐（已合并）

- 基于 Canonical 文本与结构线索识别题号候选；显式且无冲突的 `第 n 题` 只有在边界可信时才可能自动对齐，通用数字、括号子题、未知编号和顺序冲突要求复核。
- 按阅读顺序跨 Block 和页面切分答案来源，保留 Unicode 半开偏移、Canonical pointer、Block ID、页码、BBox、公式/表格类型和图片逻辑引用。
- 结果以版本化 JSON 保存在私有对象存储，PostgreSQL 保存幂等索引；提供受控 CLI 和只读查询 API。不读 AnswerKey，也不在 HTTP 请求内执行对齐。
- 已具备：合成规则单测、13 个离线案例、真实 PostgreSQL + S3 索引/并发/故障测试，以及 Canonical→对齐→API 集成路径。离线评测指标按案例、Question 和完整 Canonical `content_pointer` 区分来源；同节点内按半开范围归并，跨节点相同偏移不匹配。原有顶层案例和嵌套节点专项样本分开报告。数据包含仅有题干的负样本，明确展示 `aligned` 不代表学生作答；样本规模很小，不代表真实学生数据。真实 MinerU 端到端链路依赖独立安装的 MinerU CLI 和模型。
- 业务边界：`aligned` 表示来源区域与 Question 的映射证据足够，不表示学生回答存在、区域内没有其他题目内容、OCR 完整或结果可以批改。当前没有作答存在性检测、`answer_presence` 或 `ready_for_grading` 字段；`not_observed` 也不能解释为学生未作答。
- 已知边界：没有真实手写作业评测、人工纠错、概率校准、身份认证或租户权限；保守复核会降低自动覆盖率。跨 S3/PG 不是原子提交，提交结果不确定时保留对象，仍可能需要孤立对象对账清理。

### P2-D2A：学生作答证据、人工复核与标注基线（已合并）

- 已合并：`answer_review_decisions` PostgreSQL 追加记录，绑定 `Submission`、真实 `Question`、当前 `AnswerAlignmentArtifact`、Canonical SHA 和 Question 集合摘要；每题以 `revision` 与 `supersedes_decision_id` 留下人工修订历史。
- 已合并：本地 CLI `inspect`、`record`、`history`、`export`。没有新增 HTTP 写入 API；`reviewer_ref` 是自声明值，不是已验证身份。文本范围使用 Canonical 节点内 Unicode 码点半开区间，所有 pointer 必须实际存在。
- 来源归属复核：服务按真实 Canonical 父子节点关系识别父容器对后代的覆盖；父节点若含有另一题或未分配来源，不可被确认成当前题作答。Block 级图片引用不会赋予任意非图片子节点整节点选择资格；共享素材归属不明时保留 `uncertain`。同一资产跨 Block/ContentNode 重复引用会按资产身份识别。
- 隐私边界：导出去标识化会移除 reviewer 与原始 ID、对象 Key、外部 URI，但不会保证匿名；Canonical 区域位置和标签仍可能与外部资料关联，导出仍按敏感数据保护。
- 已合并：13 个纯合成标注案例和独立作答存在性统计，覆盖印刷题干、跨页、未关联题号、公式、图像/几何、表格、OCR 遗漏、共享图形、多区域、歧义和 unreviewed。12/13 范围已有人工决定，3/12 为 uncertain；自动作答检测 Precision/Recall/FPR 均为 `not_evaluated`。
- 边界：`alignment_status` 与人工 `decision` 分开保存；无记录是 unreviewed。`response_present` 不等于可评分，系统没有 `ready_for_grading`。纯文本合成数据不验证笔迹识别，也不代表真实作业准确率。
- 后续仍需收集经许可、脱敏的真实作业，建立双人标注/仲裁与复核者认证权限。

### P2-D2B：基于 VLM 的视觉作答证据辅助识别（已合并）

- 实现范围：本地受控 CLI 读取私有 S3 原始提交文件，只渲染 P2-D1 已关联到目标 Question 的页面。PDF 使用 PDFium；JPG/PNG 用 Pillow 检查尺寸和 EXIF。输入 SHA、MIME、文件签名、页数、像素、DPI、页面数和渲染图大小均有校验或上限。
- VLM：实现 OpenAI-compatible Vision Provider，并提供仅 loopback 的 Ollama 原生适配器。默认禁用且 endpoint 默认只允许 loopback；访问外部 HTTPS 同时要求显式允许 endpoint 和学生数据授权。有限重试只用于网络、429、5xx 和超时；非法 JSON、越界 bbox、未知字段不重试。Ollama 原生端显式关闭思考，避免思考 token 占满本地上下文而没有 JSON 正文。
- 协议：输出 `candidate_response_present`、`candidate_prompt_only` 或 `uncertain`。`candidate_prompt_only` 不是 `response_absent`；模型无权返回 Question ID 或 Canonical pointer。无未校准置信度、评分、身份推断或自由文本说明。
- 来源归属：Proposal 协议 1.1 保存 PDF MediaBox/CropBox、EXIF、渲染页信息及原始 `source_bbox_iou`。PDF MediaBox/CropBox不一致、页面框原点非零、旋转或非默认 EXIF 时拒绝 Canonical 映射。IoU 0.08 仍是未经校准的空间候选阈值，不是概率；单一候选不证明来源真的属于该 Question。`printed_prompt` 永不获得答案来源 pointer；模型框仍保留为 unresolved。
- 持久化：完整 proposal 写入私有 S3 不可变 UUID Key；PostgreSQL 记录轻索引和 source/model/version/input 摘要。相同 request ID + 输入摘要重放旧记录，不同摘要冲突；并发请求最多登记一份 proposal。VLM 调用在事务外，写入时锁定并复核版本。确定回滚可清理对象；COMMIT 开始后的未知结果一律保留对象，即使查询空或暂时不可用。S3 与 PostgreSQL 非原子，取消 boto3 的 `asyncio.to_thread` 不会杀线程，可能留下孤立对象。
- 测试边界：GitHub CI 测协议、资源限制、来源归属和 Fake Provider 下的真实 PostgreSQL + MinIO 持久化；CI 不下载模型。五个按需生成样本用于本地真实 VLM 请求合同检查，不代表真实学生手写准确率。实际本地模型结果应与 CI 测试分开报告；还需用许可的真实标注样本评估 Precision/Recall、校准和成本。
- 本地观测：2026-10-10 五个生成页面均通过严格响应协议，但几何标记被误判为仅印刷题干。2026-10-11 的真实 Ollama + PostgreSQL + MinIO 业务闭环使用合成 Canonical/Alignment fixture，模型仍把几何标记判断为 printed_prompt；该区域与合成答案框 IoU 0.952。程序现保留 BBox/IoU 并拒绝来源 pointer。此结果不是 MinerU E2E 或真实作业准确率。
- 尚未实现：HTTP 查询/写接口、教师界面、认证、RBAC、人工标注回流、全局推理并发限额、系统级内存硬隔离、孤立 S3 proposal 对账和真实作业精度评估。模型建议不会修改 P2-D2A 记录，也不产生 `ready_for_grading`。

### P2-D2C：视觉作答证据质量评测与真实解析闭环

- 已具备：带 SHA-256 校验的 26 案例/27 页合成评测集、按需调用现有 VLM Provider 的 CLI，以及经过协议校验的离线预测重算。指标分开统计作答存在性、候选区域检测、视觉区域定位和运行质量；没有 Canonical/Alignment 指针真值的指标标记为 `not_evaluated`。
- 已验证：真实 MinerU 4.0.10 Basic/ONNX → Canonical → AnswerAlignment → 本地 Ollama → 私有 S3/PostgreSQL 专项链路可运行。专项数据由程序生成，视觉来源映射仍为 `unresolved`；具体模型输出与指标见[评测结果](evaluation/visual-evidence.md)。这不代表真实教学作业准确率。
- 尚未实现：真实学生作业授权标注集、人工标注分歧仲裁、真实数据准确率及校准评测、模型微调、自动评分。不得把 synthetic 结果用于真实作业质量承诺。

## P3：标准答案管理、Rubric 评分、AI 自动批改 Agent

### P3-A：可信评分输入构建与 Rubric 快照

- 已实现：只有作业已发布、教师 AnswerKey 存在、Rubric 分值总和与题目满分一致、Canonical/Alignment 当前且最新人工决定为 `response_present` 时，才准备评分输入。
- 已实现：按人工选择的 Canonical `content_pointer` 和 Unicode 码点半开区间还原纯文本片段；保存页码、Block、阅读顺序和范围 trace。印刷题干、公式文本、图片/公式图像、表格和其他模态有分离的提取或拒绝规则。
- 已实现：AnswerKey 与 Rubric 形成不可变快照，以 `Decimal` 校验分值；Canonical、Alignment、Question digest、复核 revision 和原始文件 SHA-256 共同绑定输入版本。
- 已实现：受控本地 CLI、私有 S3 不可变 JSON、PostgreSQL 轻索引、SHA-256、同 request ID 幂等、并发唯一约束和提交结果未知时保留对象。尚无 HTTP 写接口、认证或 RBAC。
- 边界：只构建可供后续文本评分消费的输入，不调用评分模型，不产生分数。`ready_text` 不证明人工标签绝对正确、OCR 完整或评分结果经过教学审核；不支持的必要模态会阻止创建文本包。

### P3-B：Rubric 驱动的 LLM 候选评分

- 已实现：本地 CLI 只消费已经持久化的 GradeInput；在模型调用前、调用后和登记事务中重新校验人工复核、AnswerKey、Rubric、Canonical、Alignment 与题目版本。
- 已实现：支持 loopback Ollama 原生接口和需显式授权的 HTTPS OpenAI-compatible 文本接口；严格校验逐项 Decimal 分数、总分、原文证据引用和显式弃权结果。
- 已实现：不可变候选结果保存在私有 S3，PostgreSQL 登记版本化索引；同 request ID 幂等，数据库唯一约束保护并发登记，COMMIT 结果未知时保留对象。
- 边界：模型调用是可选本地 CLI，不提供 HTTP 评分写 API；proposal 是待人工检查的候选分数，不是最终成绩，不修改人工复核记录，也不将缺失证据自动转换为零分。
- 尚未实现：受认证的教师评分复核与发布、真实作业质量评测、按教师反馈校准，以及受认证的教师 Copilot 工作流。

### P3-C1：Rubric 候选评分质量评测

- 已建立版本化、SHA-256 固定的合成参考数据集和离线重算 CLI；参考答案与模型预测分离，模型请求默认关闭。
- 复用 P3-B Provider、Prompt、模型回复协议、分值和证据校验；报告总分与 Criterion 误差、MAE、覆盖率、弃权、协议/证据/Rubric/Provider 失败和引用区域重叠。
- 合成标签不是教师独立标注。真实作业准确率仍未评估；还需要经许可的数据、教师独立双评和仲裁、语义证据审查及更广泛的分层评测。

### P3-C2：候选评分人工复核与修订记录

- 已实现：受控本地 CLI 对不可变 GradeProposal 追加 `accepted`、`revised`、`rejected` 或 `regrading_required` 决定，并保留版本、修订关系和请求幂等信息。
- 已实现：修订分数使用 Decimal；Criterion 集合、分值边界、原文引用和当前 GradeInput 来源由服务校验。PostgreSQL 行锁、事务和唯一约束保护并发修订，触发器阻止历史行被更新或删除。
- 边界：这不是成绩发布流程；旧 CLI 的 `reviewer_ref` 仍是自声明文本；教师 HTTP 可绑定可信身份，仍无正式成绩记录。人工复核记录不是经独立仲裁的参考标签，也不自动触发模型重评分。
- 后续前置：接入教师工作流前先设计真实身份、授权、审计读取与数据留存；把复核反馈用于模型评测前，需有许可数据、独立双评和仲裁规则。

## P4：教师 Copilot、检索与工具调用

### P4-A：受控教师 Copilot 核心

- 已实现：LangGraph 单轮状态图、DeepSeek Flash Tool Calling、独立对话 Provider、本地 CLI 和显式作业作用域。
- 已实现：四个只读工具，查询作业概要、分页题目、Rubric 摘要和提交处理索引状态；参数白名单、PostgreSQL 只读事务、输出/上下文/步数/次数/超时/预估费用约束和脱敏内存轨迹。
- 限制：没有教师认证、RBAC、租户隔离、聊天 HTTP API、成绩查询或发布。模型最终解释需要人工核查；处理索引汇总不等于当前来源可评分。

### P4-B1：Assignment作用域教师知识检索

- 已实现：受控Markdown/TXT导入、私有S3原文、PostgreSQL来源版本与Unicode分块；输入SHA、幂等、版本替换和撤销。
- 已实现：中文词法覆盖检索基线、固定检索工具、源SHA/范围校验、本轮Citation校验、无命中拒绝编造和有限上下文。
- 边界：没有自动索引学生/OCR/标准答案/评分记录；没有认证或租户授权。Citation可追溯不等于答案语义正确，也没有真实教师场景准确率结论。
- P4-B2A已实现可选本地Embedding、有界精确向量召回、Sparse+Dense融合与合成Recall@K/MRR基线；P4-B2B已增加实验性本地Rerank；独立引用语义审核仍待完成。生成、规划、视觉、评分和Tool Calling仍使用云端DeepSeek。既有MinerU本地解析作为历史待迁移路径，不是新检索功能的必选条件。

### 保留的 P3 质量阻塞

- Quote Selection v2 仅用于实验，P3-D2B 不得默认开启或进入生产集成。
- 仍需解决：错误算术获得正确计算分、遗漏单位换算获得满分、关键词替代语义关系、同义表达被错误扣分。
- 仍需教师声明题目所需材料的可信元数据，及合格教师独立复核/双评参考标签。停止纯 Prompt 迭代，不新增 v5 合成评测。
- 后续改进应结合可信教师标签、独立校验与来源准备机制。保留现有冻结预测和失败记录。
- 计划根据实际队列需求评估 Celery 与 RabbitMQ；当前不维护第二套任务队列。

## P5：教师复核、异步任务优化、模型推理与性能评测

- 计划将 P2-D2A 的本地记录基线扩展为受认证和 RBAC 保护的教师复核、修改和反馈工作流。
- 计划优化异步执行的可观测性、吞吐和资源使用。
- 计划评估模型推理部署，并建立质量、延迟和成本评测；当前没有相关性能数据。

### 本地Embedding与混合检索

新增可选本地Qwen3-Embedding、受控内存精确Dense与RRF融合，保留PG/S3来源校验和显式词法降级；默认lexical。合成评测与真实云端工具闭环单列，不作为真实教师质量结论。P4-B2B已提供实验性本地Rerank与独立合成评测；真实检索质量审核与规模评测仍待完成；不引入向量数据库或认证系统。P3-D2B仍禁止默认开启，既有评分质量风险保留。

### 两阶段精排

可选dense_rerank/hybrid_rerank已实现，默认仍lexical。独立合成门槛中Hybrid→Rerank的量化项通过，Dense→Rerank因候选覆盖不足未通过；不自动推广。仍有来源相关但不能回答具体事实的误命中、相关片段被过滤和未经教师双评的限制。后续需独立引用/问答语义评审、更多许可资料及共享GPU部署评估。不修改评分生产协议，P3-D2B限制继续保留。


## 教师认证与授权基础

已实现资源服务器 JWT 验证、服务端 User 映射、Assignment 只读/可写成员、管理员授权/撤销与全业务 HTTP 保护；历史作业需要显式授权。Copilot 增加 Principal 入口但不开放聊天 HTTP。数据库迁移保留原业务记录。

后续仍需外部 IdP 部署验收、登录交互、学生身份、组织与租户隔离、访问审计、限流和部署秘密管理；当前 CLI 数据库操作员边界不等于教师权限。继续保留所有 P3 质量风险，P3-D2B 不默认开启，RAG 不切换默认检索。


## 教师作答复核 HTTP

已提供受教师身份及作业权限保护的历史、单题检查和追加复核工作流，复用来源验证、不可变 revision 和幂等。提交时再次校验权限及来源；新增可信审核人外键和数据库不可变保护，历史声明身份不回填。

后续可在相同 Principal、作用域、来源版本与审计边界下设计候选评分人工复核 HTTP；尚未开放，不等于成绩发布。继续保留真实 IdP、组织/租户隔离、教师前端和 P3 质量准入的未完成项。


## P5-A3：可信教师候选评分审核

- 已实现：固定权限矩阵内的候选检查、分页历史与追加决定；Proposal/GradeInput/Submission/Question 真实绑定，读写成员权限分离。
- 已实现：HTTP 审核人来自 Principal/User 外键；完整 Rubric 快照、原文 Quote、来源版本、并发 expected_revision、审核人绑定的 request_id 幂等与未知提交恢复。旧 CLI 和不可变历史保留。
- 边界：没有成绩发布、自动重评分、新 Prompt 或实验 Quote Selection 生产集成。教师接受不等于评分质量门禁通过，历史摘要不证明来源当前有效。
- 后续教师界面需遵循 HTTP 检查/分页/受保护来源跳转与完整快照合同；完整审计正文读取、真实 IdP 部署、租户权限、数据留存与正式成绩发布需独立验收。

## P5-B1：教师工作台

- 已实现：Vue 教师工作台、作业列表/新建草稿/题目与 Rubric 概要、学生提交、认证原始文件预览、逐题来源与人工复核、分页只读历史。
- 已实现：标准 PKCE 登录客户端、内存 Token、统一权限 API、Unicode 码点选区、来源过期提示、未知提交原请求重放与并发冲突恢复；独立 PostgreSQL/S3 与 Mock OIDC 浏览器验收。
- 边界：真实外部 IdP、真实教师数据和部署访问仍需专门验收；没有学生身份、聊天 HTTP、多校租户体系或正式成绩发布。P3 评分质量风险及 P3-D2B 禁止默认开启保持。
- P5-B2：基于现有候选评分审核 HTTP 构建完整 Rubric 快照界面、理由/证据核对和只读审计；不以教师确认直接发布成绩。
