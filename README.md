# 慧批云端

慧批云端是一个独立重建的高中 AI 教学辅助平台。项目先建立可信的作业数据与来源链路，为教师检查学生提交、解析作业内容和复核作答证据提供浏览器工作台与后端能力。

当前系统支持作业管理、学生作业文件提交、文档解析、题目来源对齐、视觉证据建议、人工作答复核，以及基于当前人工确认文本与 Rubric 生成候选评分。模型输出只供人工检查，不是最终成绩或教学结论。

## 功能特性

- **教师工作台**：通过浏览器查看作业、提交与受保护来源，选择作答证据并保存人工决定及修订历史。
- **教师身份与作业授权**：验证外部 OIDC access token，按服务端成员授权控制作业、题目、提交和文件访问。
- **作业与题目管理**：创建作业、维护题目、人工录入标准答案和评分细则，并校验后发布。
- **作业提交与文件存储**：提交 PDF、JPG 或 PNG，使用私有 S3 兼容存储保存原文件。
- **文档解析**：通过独立配置的 MinerU 4.x Basic/ONNX 运行时生成 Markdown 和结构化解析产物。
- **文档标准化**：将 MinerU 结果转换为带来源信息的 Canonical Document。
- **题目来源对齐**：识别题号候选、切分可追溯内容区域，并与作业题目进行保守匹配。
- **视觉作答证据建议**：调用经授权的云端视觉模型，生成与人工复核分离的视觉建议。
- **人工复核与评测**：通过受认证 HTTP 记录版本化的人工作答判断；提供合成数据评测工具，用于检查协议、作答判断和区域定位表现。
- **评分输入准备**：从当前人工确认的 Canonical 文本范围构建不可变输入包，并快照教师标准答案与 Rubric；不调用评分模型或产生分数。
- **Rubric 候选评分**：受控本地 CLI 可调用配置的文本模型，为每个评分项生成候选分数、理由和可核验的原文引用；不会发布成绩，也不会修改人工复核记录。
- **候选评分人工复核**：受认证 HTTP 或受控本地 CLI 可接受、修订、拒绝候选分数或标记需要重新评分，并保留不可变修订历史；记录不等于正式成绩。

- **教师 Copilot**：受控本地 CLI 可调用云端模型查询指定作业、题目、Rubric 和提交处理状态，检索显式导入的教师资料并校验来源引用；只读、不发布成绩。
- **单校组织与可信学生名册**：建立学年/学期、入学届别、学科、行政班、学生稳定身份与半开区间的班级归属历史，支持两阶段 CSV 名册导入（预览→确认）；当前仅安装级组织权限，不含教学班、考试、扫描或阅卷能力。

- **考试扫描接收**：为已授权场次建立批次，上传小批多页PDF，查看受保护的逐页预览；页面身份仍待人工核验。参见[使用指南](docs/development/exam-scan-intake.md)。

## 系统架构

```mermaid
flowchart LR
    Client[Vue 教师工作台] -->|access token| API[FastAPI]
    Client -->|Code + PKCE| IdP[OIDC 身份服务]
    CopilotCLI[本地教师 Copilot CLI] --> Graph[受控 LangGraph]
    Graph --> CopilotModel[经授权的云端对话模型]
    Graph --> ReadTools[固定只读业务工具]
    ReadTools --> PG
    API --> PG[(PostgreSQL)]
    API --> S3[(私有 S3 兼容存储)]
    API --> Task[ParsingTask]
    Task --> Worker[独立解析 Worker]
    Worker --> MinerU[MinerU]
    MinerU --> Parsed[Markdown 与 MiddleJson]
    Parsed --> Canonical[Canonical Document]
    Canonical --> Alignment[AnswerAlignment]

    Alignment --> ReviewCLI[AnswerReview 人工复核 CLI]
    ReviewCLI --> PG
    Canonical --> GradeCLI[GradeInput 本地 CLI]
    Alignment --> GradeCLI
    ReviewCLI --> GradeCLI
    Assignment[Question / AnswerKey / Rubric] --> GradeCLI
    GradeCLI --> GradeS3[私有 GradeInput JSON]
    GradeCLI --> PG

    GradeS3 --> ScoreCLI[本地候选评分 CLI]
    Assignment --> ScoreCLI
    ScoreCLI --> TextModel[经授权的云端文本模型]
    TextModel --> ScoreProposal[私有候选评分对象与 PostgreSQL 索引]
    ScoreProposal --> GradeReviewCLI[候选评分人工复核 CLI]
    GradeReviewCLI --> PG

    Original[私有原始作业文件] --> Visual[VisualEvidence]
    Alignment --> Visual
    Visual --> Model[经授权的云端视觉模型]
    Visual --> Proposal[私有模型建议对象与 PostgreSQL 索引]
```

`VisualEvidence` 保存独立的模型建议，不会自动写入 `AnswerReview` 人工决定。候选评分人工复核单独追加到 PostgreSQL，不会改写模型 Proposal 或发布成绩。PostgreSQL 保存业务记录和索引；对象存储保存原始文件及大型不可变产物。

## 技术栈

- Python 3.12、uv、FastAPI、Pydantic Settings
- PostgreSQL、SQLAlchemy 2.x Async、Alembic
- S3 兼容对象存储与 boto3；本地 Compose 使用 PGSTY SILO
- MinerU 4.x Basic/ONNX 独立运行时
- pypdf、pypdfium2、Pillow、HTTPX
- LangGraph（受控 Copilot 状态流程）、pytest、Ruff

## 快速开始

需要 Python 3.12、uv 和 Docker Compose。下面命令启动仅绑定本机端口的 PostgreSQL 与对象存储：

```bash
cp .env.example .env
uv sync --locked
docker compose up -d postgres minio
uv run python scripts/init_minio_bucket.py
uv run alembic upgrade head
uv run uvicorn huipi_cloud.main:app --reload
```

API 文档位于 <http://127.0.0.1:8000/docs>，存活检查为 `GET /api/v1/health`。数据库表由 Alembic 管理，应用启动时不会自动创建表。业务 API 还需配置可信 OIDC 发行方并建立本地教师映射，见[教师认证与授权指南](docs/development/teacher-auth.md)；缺省只开放存活检查和 API 文档。

### 教师工作台

需要 Node.js 24 与已配置的 OIDC 公共客户端：

```bash
cd frontend
npm ci
cp -n .env.example .env.local
# 配置公开的 OIDC authority/client ID，不能填写秘密或 API Key
npm run dev
```

访问 `http://127.0.0.1:5173`。未配置身份服务时业务入口保持锁定；真实学校 IdP 仍需独立验收。部署、选区与测试说明见[工作台指南](docs/development/teacher-workbench.md)。

## 基本使用

基本流程是：创建并发布作业 → 上传一份学生作业 → 运行解析 Worker → 标准化文档 → 对齐题目来源 → 可选地生成视觉建议 → 人工作答复核 → 准备文本评分输入 → 可选地生成并人工复核 Rubric 候选评分。

最小可执行 API 示例和完整本地处理步骤见[作业处理教程](docs/development/basic-workflow.md)。MinerU和云端模型API需要单独配置，快速启动不会自动安装模型或启动解析 Worker。

## 主要接口

后端 API 统一使用 `/api/v1` 前缀，完整的请求和响应结构可在本地 `/docs` 查看。

| 能力 | API 入口 |
| --- | --- |
| 创建、查询和发布作业 | `/assignments`、`/assignments/{id}/publish` |
| 上传和查询学生提交 | `/assignments/{id}/submissions`、`/submissions/{id}` |
| 读取解析任务和 Markdown | `/submissions/{id}/parsing-task`、`/submissions/{id}/parsed-document/markdown` |
| 读取 Canonical 页面 | `/submissions/{id}/canonical-document/pages/{page_number}` |
| 读取题目来源对齐 | `/submissions/{id}/answer-alignment/questions/{question_id}` |

教师可通过受保护 HTTP 复核[作答证据](docs/development/answer-review-http.md)与[候选评分](docs/development/grade-review-http.md)，保留修订历史；审核结果不自动发布成绩。视觉建议仍由受控 CLI 运行，步骤见[开发指南](docs/development/README.md)。

## 当前边界

- HTTP 业务接口已要求可信教师身份与作业授权；浏览器已接入标准 OIDC 登录流程，真实外部 IdP 仍需验收；仍缺少学生认证、完整组织 RBAC 和租户隔离。`student_ref` 是模拟标识，上传为教师代录；本地操作员 CLI 和部署凭据仍需受控管理。
- `aligned` 只表示来源区域与作业题目的映射证据足够，不表示学生已作答、OCR 完整或可以评分。
- `ready_text` 只表示文本评分输入通过来源门禁，不表示已产生分数、图像/表格/公式均可评分或人工标签绝对正确。
- 视觉和文本模型都只生成候选建议；候选分数不是最终成绩，尚无经充分验证的真实教学作业评分质量，没有自动发布成绩；教师 Copilot 只提供本地只读 CLI，可显式传入可信教师身份；没有公开聊天服务。
- 当前视觉评测集由合成样本构成；合成结果不能代表真实学生作业表现。

## 项目文档

- [架构与模块边界](docs/architecture.md)
- [教师认证、作业授权与权限矩阵](docs/development/teacher-auth.md)
- [本地开发指南](docs/development/README.md)
- [单校组织、学生身份与两阶段名册导入](docs/development/organization.md)
- [评分输入准备指南](docs/development/grade-input.md)
- [Rubric 候选评分 CLI 指南](docs/development/grade-proposals.md)
- [候选评分人工复核 CLI 指南](docs/development/grade-review.md)
- [教师 Copilot 本地 CLI](docs/development/teacher-copilot.md)
- [教师知识材料与来源检索](docs/development/scoped-retrieval.md)
- [路线图](docs/roadmap.md)
- [视觉证据评测方法与基线](docs/evaluation/visual-evidence.md)
- [题目来源对齐评测说明](docs/evaluation/answer-alignment.md)
- [文档维护规则](docs/contributing.md)
