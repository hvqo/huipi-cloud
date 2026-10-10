# 项目路线图

本页区分已经合并的功能、PR 跟踪的改动和未来计划。P1-A 至 P2-D1 已合并到 `main`；P2-D1 由 PR #9 交付。P2-D2A 的当前实现位于独立功能分支，尚未合并。

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

### P2-D1：题号候选识别、学生答案区域切分与作业题目对齐（已合并，PR #9）

- PR #9 实现：基于 Canonical 行首文本与结构线索识别题号候选，涵盖 `第 n 题`、`n.`、`n、`n)` 和常见中英文括号题号；公式、表格、图片、列表和页码结构不作为题号来源。
- PR #9 实现：将候选题号与 Submission 所属 Assignment 的真实 Question ID 匹配；显式且无冲突的 `第 n 题` 只有在前后答案边界可信时才可能自动对齐。未知编号、可能列表项、括号子题、重复题号和顺序冲突要求复核或进入未分配区域。
- PR #9 实现：按 Canonical 阅读顺序跨 Block/跨页切分答案，保留半开文本偏移、Canonical JSON pointer、Block ID、页码、BBox、公式/表格类型和图片逻辑引用。题号候选与无法精确定位的图片来源处于同一 Block 时保留引用并要求复核；区域不重叠，复杂嵌套边界保留并要求复核。
- PR #9 实现：`huipi.answer.alignment` 1.0 结果合同和 `ALIGNER_VERSION=1.1.0`、S3 不可变运行对象、PostgreSQL 唯一约束和幂等索引、受控对齐 CLI、摘要与单题 GET API。不读 AnswerKey，不在 HTTP 请求内执行对齐，不提供自动批改。旧 1.0.0 产物不会被覆盖；查询按当前算法版本选择结果。
- PR #9 测试：合成规则单测、人工标注的 13 个离线案例、真实 PostgreSQL + S3 索引/并发/故障测试，以及 Canonical→对齐→API 集成路径。PR #10 将离线评测指标升至版本 2.1：候选检测、Question 关联和答案边界均保留完整 Canonical `content_pointer`，按案例、Question 和 ContentNode 隔离；同节点内归并半开范围，跨节点偏移不匹配。原 13 个顶层案例独立报告，嵌套节点专项样本另行报告。数据包含仅有题干的负样本，明确展示 `aligned` 不代表学生作答；样本规模很小，不代表真实学生数据。真实 MinerU 端到端链路依赖独立安装的 MinerU CLI 和模型。
- 业务边界：`aligned` 表示来源区域与 Question 的映射证据足够，不表示学生回答存在、区域内没有其他题目内容、OCR 完整或结果可以批改。当前没有作答存在性检测、`answer_presence` 或 `ready_for_grading` 字段；`not_observed` 也不能解释为学生未作答。
- 已知边界：没有真实手写作业评测、人工纠错、概率校准、身份认证或租户权限；保守复核会降低自动覆盖率。跨 S3/PG 不是原子提交，提交结果不确定时保留对象，仍可能需要孤立对象对账清理。

### P2-D2A：学生作答证据、人工复核与标注基线（本功能分支）

- 已实现于当前分支：`answer_review_decisions` PostgreSQL 追加记录，绑定 `Submission`、真实 `Question`、当前 `AnswerAlignmentArtifact`、Canonical SHA 和 Question 集合摘要；每题以 `revision` 与 `supersedes_decision_id` 留下人工修订历史。
- 已实现于当前分支：本地 CLI `inspect`、`record`、`history`、`export`。没有新增 HTTP 写入 API；`reviewer_ref` 是自声明值，不是已验证身份。文本范围使用 Canonical 节点内 Unicode 码点半开区间，所有 pointer 必须实际存在。
- 来源归属复核：服务按真实 Canonical 父子节点关系识别父容器对后代的覆盖；父节点若含有另一题或未分配来源，不可被确认成当前题作答。Block 级图片引用不会赋予任意非图片子节点整节点选择资格；共享素材归属不明时保留 `uncertain`。同一资产跨 Block/ContentNode 重复引用会按资产身份识别。
- 隐私边界：导出去标识化会移除 reviewer 与原始 ID、对象 Key、外部 URI，但不会保证匿名；Canonical 区域位置和标签仍可能与外部资料关联，导出仍按敏感数据保护。
- 已实现于当前分支：13 个纯合成标注案例和独立作答存在性统计，覆盖印刷题干、跨页、未关联题号、公式、图像/几何、表格、OCR 遗漏、共享图形、多区域、歧义和 unreviewed。12/13 范围已有人工决定，3/12 为 uncertain；自动作答检测 Precision/Recall/FPR 均为 `not_evaluated`。
- 边界：`alignment_status` 与人工 `decision` 分开保存；无记录是 unreviewed。`response_present` 不等于可评分，系统没有 `ready_for_grading`。纯文本合成数据不验证笔迹识别，也不代表真实作业准确率。
- 下一步仍需完成独立代码审查和 Draft PR；以后收集经许可、脱敏的真实作业，建立双人标注/仲裁与复核者认证权限。P2-D2B、P3 不在本轮范围。

## P3：标准答案管理、Rubric 评分、AI 自动批改 Agent

- 计划支持标准答案与 Rubric 版本。
- 计划实现可追踪、可解释的自动批改 Agent 和失败回退。
- 当前标准答案由教师 API 手工设置；模型生成答案和自动批改尚未实现。

## P4：教师 Copilot、检索与工具调用

- 计划围绕作业和批改结果提供教师 Copilot。
- 计划加入权限约束的检索、上下文组织和工具调用记录。
- 计划根据实际队列需求评估 Celery 与 RabbitMQ；当前不维护第二套任务队列。

## P5：教师复核、异步任务优化、模型推理与性能评测

- 计划将 P2-D2A 的本地记录基线扩展为受认证和 RBAC 保护的教师复核、修改和反馈工作流。
- 计划优化异步执行的可观测性、吞吐和资源使用。
- 计划评估模型推理部署，并建立质量、延迟和成本评测；当前没有相关性能数据。
