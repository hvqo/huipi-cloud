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

所有示例默认用于本地受控环境。HTTP 业务接口已有教师身份和作业成员授权；已有 PKCE 登录客户端；仍没有学生认证、完整组织 RBAC 或租户隔离。

- [教师知识材料导入、版本与作用域检索](scoped-retrieval.md)

- [本地Embedding与混合检索](hybrid-retrieval.md)

- [本地Rerank与两阶段检索](rerank-retrieval.md)

- [教师身份、作业授权与迁移](teacher-auth.md)
- [全部 HTTP 权限矩阵](../security/http-permissions.md)

- [教师作答证据复核 HTTP 工作流](answer-review-http.md)

- [教师候选评分审核 HTTP](grade-review-http.md)：可信审核人、完整评分快照、来源核对与分页历史。

- [教师浏览器工作台](teacher-workbench.md)：Vue 启动、OIDC/PKCE、API 类型、Unicode 选区和浏览器验收。

- [教师/管理员角色与管理接口](admin-roles.md)
- [前端角色、页面能力与 API 合同](../product/teacher-admin-contract.md)

- [单校组织、学生身份与两阶段名册导入（B-1.1b）](organization.md)：学年学期、入学届别、行政班、学号唯一的学生身份、班级归属历史、CSV 预检/确认与安装级组织权限。

- [快速 CI 与本地验收](validation.md)
- [共享试卷版本与锁卷](paper-versions.md)

- [考试计划、科目场次与共享用卷](exam-sessions.md)：受控JWT CLI、真实资源授权、用卷历史与状态规则。

- [参试名单、历史快照与冻结](exam-rosters.md)：Service/JWT CLI、考号、卷别、权限与迁移。
