# 开发与运行指南

面向本地开发者的操作文档。首次启动从[快速开始](quickstart.md)开始；要验证从 API 上传到题目对齐的完整流程，使用[基本作业流程](basic-workflow.md)。

- [快速开始：环境、PostgreSQL、对象存储与 API](quickstart.md)
- [基本作业流程：创建、提交、解析和对齐](basic-workflow.md)
- [安装 MinerU 并运行解析 Worker](mineru.md)
- [运行视觉作答证据建议](visual-evidence.md)
- [人工复核 CLI 与脱敏导出](answer-review.md)
- [可信评分输入检查与生成 CLI](grade-input.md)
- [Rubric 候选评分 CLI](grade-proposals.md)
- [Rubric 候选评分人工复核 CLI](grade-review.md)

- [教师 Copilot 本地 Agent CLI](teacher-copilot.md)

所有示例默认用于本地受控环境。HTTP 业务接口已有教师身份和作业成员授权；仍没有登录界面、学生认证、完整组织 RBAC 或租户隔离。

- [教师知识材料导入、版本与作用域检索](scoped-retrieval.md)

- [本地Embedding与混合检索](hybrid-retrieval.md)

- [本地Rerank与两阶段检索](rerank-retrieval.md)

- [教师身份、作业授权与迁移](teacher-auth.md)
- [全部 HTTP 权限矩阵](../security/http-permissions.md)
