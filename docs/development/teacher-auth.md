# 教师身份与作业授权

HTTP 业务接口统一需要可信教师 Token。存活探针和静态 API 文档公开；配置缺失时不会启用匿名模式。学生上传接口目前是教师代录入口，`student_ref` 不是学生认证。没有开放 Copilot 聊天、评分、候选评分复核或任意 SQL 的 HTTP 写接口。人工作答证据复核提供[受保护 HTTP 工作流](answer-review-http.md)。

## 身份合同

采用外部 OIDC 发行方签发、面向本 API 的 JWT access token。这里实现资源服务器验证，不实现登录页面、密码存储、授权码流程或自建 Token 签发器。固定允许 RS256（RSA 至少 2048 位）；必须有 `iss/sub/aud/exp/iat`，同时验证存在的 `nbf`。时间必须为有限数字，过期或未来签发拒绝。

`Principal` 是不可变的服务内对象：`user_id`、`issuer`、`subject`、`expires_at`、`is_admin`。只有签名验证后的 `(issuer, subject)` 能定位本地启用的 User。`is_admin` 来自数据库，Token 中的 `teacher_id`、`role`、自声明请求头均不参与授权。服务直接使用 Principal 时也重新检查用户、管理员状态与到期时间。

本地 User 必须由可信数据库操作员预先建立，不会根据一个陌生但签名有效的 Token 自动注册。数据库管理员有全局作业访问及授予/撤销成员权限；普通教师有自己的作业成员权限，不具有提权入口。

## 配置与启动

先使用独立环境验证迁移，再由操作员安排开发/部署库迁移。本任务不会自动升级用户开发库。

```bash
uv sync --locked
# 在自己的部署环境设置这些值；不要复制测试发行方到生产。
# AUTH_ISSUER=https://identity.example.org/realms/huipi
# AUTH_AUDIENCE=huipi-api
# AUTH_JWKS_URL=https://identity.example.org/realms/huipi/protocol/openid-connect/certs
uv run alembic upgrade head
uv run python -m huipi_cloud.modules.identity provision --subject '<发行方稳定sub>'
# 只有持有数据库凭据的可信操作员可以首次建立管理员：
uv run python -m huipi_cloud.modules.identity provision --subject '<管理员sub>' --admin
uv run uvicorn huipi_cloud.main:app --reload
```

Issuer、Audience 与 JWKS 必须由服务端固定配置，不能取自 Token 的 `jku/x5u` 或教师输入。仅允许无凭据、无查询串的 HTTPS URL；不跟随重定向，不读取环境代理。Issuer 必须与签发值完全一致。IdP 应为本 API 分配独立 Audience，不要把 ID Token 的客户端 Audience 作为 API Audience。

JWKS：每次 I/O 3 秒、包括锁等待的总预算 5 秒、64 KiB 上限、最多 16 个键，默认缓存 300 秒；未知 kid 最快每 10 秒触发一次刷新（轮换时有最长冷却延迟）。刷新后移除旧键；过期缓存刷新失败返回 503，不继续用陈旧键。密钥撤销在缓存刷新前仍有传播窗口。HTTP 身份缺失/错误为 401，未建立本地用户或管理员权限不足为 403，资源不存在或无授权统一 404，外部身份/授权数据库故障为 503。不会返回底层异常、Token 或 DSN。

## 授权与业务闭环

1. 教师从自己的可信 IdP 获取 access token。应用不生成 Token。
2. `POST /api/v1/assignments` 创建作业，并在同一个事务建立当前教师的可写 Membership。
3. `GET /api/v1/assignments` 在 SQL 分页之前过滤当前授权；管理员可以列出全部。
4. `GET /api/v1/assignments/{id}` 返回已授权作业、题目、标准答案及 Rubric。题目和 Rubric 的写入沿用已有草稿/发布规则。
5. 教师代录提交后，原文件、解析、Canonical 与对齐 API 都向上核对 Submission 的 Assignment；复合 URL 中 Question 和 Submission 必须属于同一作业。

```bash
# 在本地安全载入 HUIPI_ACCESS_TOKEN；不要把实际值写入脚本/提交。
curl -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"title":"合成练习","subject":"数学","grade_level":"高一"}' \
  http://127.0.0.1:8000/api/v1/assignments
curl -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" \
  http://127.0.0.1:8000/api/v1/assignments
```

管理员可以 `PUT /api/v1/assignments/{id}/members/{user_id}`，正文 `{"can_write":false}` 授予只读成员，`true` 授予作业内写权限；`DELETE` 撤销。普通教师即使是作业创建者也不能操作成员管理接口或授予管理员。用户建立/停用仅有本地操作员 CLI：

```bash
uv run python -m huipi_cloud.modules.identity grant --assignment-id UUID --user-id UUID --write
uv run python -m huipi_cloud.modules.identity revoke --assignment-id UUID --user-id UUID
uv run python -m huipi_cloud.modules.identity disable --user-id UUID
```

授权没有跨请求缓存：撤销提交后的后续请求立即查到失效；用户停用和管理员降权也重新查库。已经授权且正在执行的业务事务/文件流不承诺被中途撤回，不持有长授权锁或缓存 Token 权限。HTTP 身份映射与资源授权各阶段最多等待 5 秒，SQL statement timeout 为 3 秒。历史 Assignment 没有默认成员；普通教师不能抢领，需要可信操作员或管理员明确授权。迁移不重写原始作业和提交。

## Copilot

受控本地 CLI 保留数据库操作员入口；它拥有数据库凭据，不代表某个教师，也不是公开服务。新增 `--token-stdin` 从标准输入读取 Token，建立 `AuthorizedBusinessTools`。身份及 Assignment 权限先于本地检索预加载验证；每次工具执行和 Citation 验证前后再次查库。工具参数和模型不能指定 Principal。

```bash
# 由本地安全秘密管理器把 Token 送入 stdin；禁止把 Token 作为 argv。
printf '%s\n' "${HUIPI_ACCESS_TOKEN}" | uv run python -m huipi_cloud.modules.copilot \
  --token-stdin --assignment-id UUID --question '这份作业有哪些题目？'
```

上例假定 Token 已通过本地安全方式载入环境；不要启用 shell 命令回显或把 Token 写入历史。Service 调用者应使用 `AuthorizedBusinessTools.from_bearer(...)`；不能把教师请求接到原始 `BusinessTools` 操作员入口。没有新增聊天 HTTP API，没有改变生成模型、RAG 默认 Lexical 或 P3 评分合同。

## 测试与限制

- [完整权限矩阵](../security/http-permissions.md)；[机器可核对矩阵](../security/http-permissions.json)。新增业务路由无精确策略时拒绝；默认 CI 校验矩阵与 OpenAPI 路由一一对应。
- `tests/unit/test_identity_tokens.py` 使用运行时 RSA 测试键与 HTTPX MockTransport 验证真实签名、Issuer/Audience/时间、JWKS 轮换与故障。无静态私钥进入 Git，不依赖外部 IdP。
- `tests/integration/test_teacher_authorization.py` 使用真实独立 PostgreSQL、私有 S3 和真实签名 Token，不覆盖认证/授权依赖。P1–P4 原有业务测试显式使用可信操作员依赖覆盖，只验证业务回归，不冒充认证测试。
- `uv run python scripts/check_identity_migration.py` 新建随机独立测试库，升级、check、降回上一版本、再次升级/check；保留一条历史作业哨兵，最终只删除自己建立的测试库。
- 生产部署仍需可信 IdP 配置与验收、TLS、密钥治理、审计留存、入口限流、独立租户策略和部署凭据隔离。当前是最小教师/管理员及作业成员授权，不是完整组织 RBAC。没有学生认证、独立 Token 撤销表、成绩发布或公开 Agent 服务。
