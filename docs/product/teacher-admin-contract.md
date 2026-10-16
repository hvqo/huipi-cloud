# 教师与管理员：角色、页面能力及 API 合同

本文供后续前端实现使用。页面只决定展示方式，后端始终重新验证身份与权限。现有 Vue 只作为功能基础版本；本轮没有进行视觉设计验收，也没有新增管理员页面。

## 可信身份与两种角色

- `teacher`：已在数据库登记且启用的普通教师。
- `admin`：数据库 `User.is_admin=true` 且启用的管理员。
- JWT 仅证明外部 `(issuer, subject)` 身份。`role`、`is_admin`、`teacher_id` 和前端路由均不能授予权限。
- 未登录返回 401；已签名但未登记或已停用返回 403。未配置 IdP 返回 503，不启用匿名模式。
- 管理员当前同时拥有账号管理权和全局教学资料访问权。这是既有兼容语义，**不是已经分离的系统管理员与教务管理员体系**。

## 页面能力矩阵

### 两种角色均可进入

- 当前用户资料：`GET /api/v1/me`。仅返回本人的内部 ID、角色、能力与启用状态。
- 新建草稿作业：创建者自动获得可写成员权限。
- 作业列表：教师只看到当前被授权的作业；管理员看到全部作业，包括历史未绑定作业。
- 作业概要、题目、Rubric、提交、原文件、解析与 Canonical：教师须有相应 Assignment 读权限；管理员全局可读。
- 作答复核与候选评分复核：读历史需读权限，追加决定需写权限；管理员仍需满足来源版本、幂等与并发约束。
- 教师只读成员不能写入；页面依据 workbench 的 `can_write` 控制编辑入口，后端依旧检查。
- 不能发布正式成绩。候选评分、人工接受或修订均不等于已发布成绩。

### 仅管理员可进入

- 教师账号列表、登记外部身份、启用/停用教师。
- 教师作业成员关系列表、分配只读/可写权限、撤销权限。
- 只读管理操作审计。
- 普通教师即使是作业创建者也不能使用这些接口。

### 两种角色均不提供的页面能力

- HTTP 管理员晋升、降权、停用管理员、密码管理或公开注册。
- 学生认证、多校租户隔离、任意 SQL/URL 工具、匿名 Agent 对话。
- 正式成绩发布、改变评分质量门禁或启用实验 Quote Selection。

## CurrentUser

`GET /api/v1/me` 返回：

```json
{
  "user_id": "内部UUID",
  "display_name": null,
  "role": "teacher",
  "capabilities": [
    "assignment:create", "assignment:read_scoped",
    "assignment:write_scoped", "review:write_scoped"
  ],
  "is_active": true
}
```

目前没有经过信任治理的显示名来源，因此 `display_name` 为 null，不使用 JWT 任意 name 字段。停用用户不能读取 `/me`，不会得到一个仍可使用的 inactive 会话。

能力是操作类别，不是资源授权凭据。`assignment:write_scoped` 不意味着对全部作业可写。管理员另外得到 `assignment:access_all`、`teacher:manage`、`membership:manage`、`audit:read`。前端应以 role 决定教师端/管理员端入口，以具体资源响应决定操作；所有响应禁止缓存，不将能力保存在长期令牌中。

## 管理接口

完整 OpenAPI 以运行中的 `/openapi.json` 为准；TypeScript 合同从其生成在 `frontend/src/api/generated.ts`，不手工复制后端枚举。

- `GET /api/v1/admin/teachers?limit=25&offset=0`：`TeacherPage`。仅列普通教师，包含已停用账号，不列管理员；每项 `AdminUserSummary` 为 user_id、role、is_active、created_at，不返回 OIDC subject/issuer。
- `POST /api/v1/admin/teachers`：`TeacherRegistration` 为 request_id、issuer、subject。Issuer 必须与服务配置完全一致；Subject 由受控 IdP 管理流程取得。只建立普通教师，不接受 role/is_admin/actor 字段。成功 201，返回 `AdminOperationResult`；身份已存在返回 409，不自动重新启用或修改既有账号。
- `PATCH /api/v1/admin/teachers/{user_id}`：`TeacherActivation` 为 request_id、is_active（JSON 布尔值）。成功 200；管理员账号不属于此资源，返回 404。停用不删除历史记录和成员关系；重新启用后未撤销的成员权限恢复。
- `GET /api/v1/admin/teachers/{user_id}/memberships`：`MembershipPage`。每项 `AssignmentMembershipSummary` 为 assignment_id、user_id、can_write、is_active、created_at、revoked_at。is_active 同时考虑账号启用与成员未撤销；can_write 仅说明已存储的授权类型，账号停用时不代表可执行。
- `PUT /api/v1/assignments/{assignment_id}/members/{user_id}`：正文 `{ "request_id": "UUID", "can_write": false }`，成功 200。返回兼容的 assignment_id、user_id、can_write，以及 operation 审计结果。true 为作业内写权限；已停用教师返回 409。管理员账号不走普通教师授权入口，其全局权限来自部署策略。
- `DELETE /api/v1/assignments/{assignment_id}/members/{user_id}?request_id=UUID`：成功 204，响应头 X-Operation-ID、X-Request-ID。旧调用仍可省略 request_id；新前端必须发送，以支持未知提交结果的幂等恢复。
- `GET /api/v1/admin/audit-events`：`AdminAuditPage`，可按 target_user_id、assignment_id、request_id 筛选。只读，不提供修改/删除接口。

所有管理列表为 records、limit、offset、has_next、next_offset，limit 1–100，offset 0–100000。顺序固定；并发变更时 offset 分页不是整个列表的冻结快照。

`AdminOperationResult` 包含 operation_id、request_id、action、target_user_id、assignment_id（可空）、state、created_at。审计摘要另有 actor_user_id。state 只包含布尔权限状态，不包含身份 subject、学生作答、答案或对象 Key。

操作类型为 teacher_registered、teacher_activation_changed、assignment_membership_granted、assignment_membership_revoked。权限撤销的 state 表示成员 inactive/can_write=false；历史关系行保留原始 can_write 与撤销时间，不能把它视作当前授权。

## 错误与客户端处理

- 401：authentication_required / invalid_token。清除会话并重新认证。
- 403：teacher_not_provisioned / administrator_required / route_not_authorized。停止对应操作，不靠前端改 role 重试。
- 404：teacher_not_found / resource_not_found_or_forbidden。统一不存在/无资源访问边界，不泄露别的作业资料。
- 409：admin_request_conflict / identity_already_registered / teacher_inactive。保留输入，提示人工处理。
- 422：identity_issuer_not_configured 或 FastAPI 字段校验错误。固定业务错误 detail 为字符串；字段错误沿用 FastAPI detail 数组。
- 503：identity_operation_unavailable / authorization_unavailable / identity_service_unavailable / authentication_not_configured。数据库提交可能已成功；保留同一个 request_id 和原始正文重放，不能产生新 ID 后盲目重试。

同一操作者、同一 request_id、同一内容返回原操作结果，不重复写审计；不同内容返回 409。不同操作者不能通过 request_id 取到对方的重放结果。重放也会检查当前管理员状态。客户端应同时保留目标和原始请求，不能把旧响应当作当前权限快照；修改后重新查询列表或 `/me`。

## 全局管理员兼容性与迁移计划

现有 Assignment、Question、Submission、原文件、AnswerReview、GradeReview 都通过 `require_assignment` 或等价资源检查，管理员绕过成员要求；多资源 URL 仍必须同属一个作业。Copilot 的 Principal 入口也沿用该语义，每次工具调用前后复查；模型不能指定身份。操作员 CLI 持有数据库凭据，是部署边界，不是教师授权。

本轮不改变这些读写能力，也不为历史作业自动添加成员。迁移仅新增管理审计表。未来若要分离“仅账号管理”和“教学资料读取”，需先提供独立系统管理/资料访问能力、显式迁移既有管理员、补全各资源及文件/Agent 回归，再收紧全局绕过。不能只隐藏管理员页面来声称已完成隔离。

当前全局管理员泄露或误配可访问全部教学资料，是明确残余风险。生产部署应限制管理员数量、保护 IdP 与数据库凭据，并实施独立部署操作审计。
